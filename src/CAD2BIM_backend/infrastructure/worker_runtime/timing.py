"""Bounded optional child phase observations, separate from result authority."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from .protocol import encode_json_frame, write_all, write_json_frame, WorkerProtocolError


MAX_TIMING_BYTES = 256 * 1024
_ACTIVE: ContextVar["WorkerTiming | None"] = ContextVar("worker_timing", default=None)
ISOLATED_TIMED_BOOTSTRAP = (
    "import time; _entered=time.monotonic_ns(); import os,runpy,sys; "
    "os.environ['CAD2BIM_WORKER_BOOTSTRAP_NS']=str(_entered); "
    "sys.path.insert(0,sys.argv[1]); "
    "runpy.run_module(sys.argv[2],run_name='__main__')"
)


class WorkerTiming:
    def __init__(self, role: str, module_entered_ns: int) -> None:
        descriptor = os.environ.pop("CAD2BIM_WORKER_TIMING_FD", None)
        self._stream = os.fdopen(int(descriptor), "wb", buffering=16 * 1024) if descriptor else None
        self._count = 0
        self._aggregates: dict[str, list[int]] = {}
        self._omitted_details = 0
        self._unclassified_events = 0
        self._token = _ACTIVE.set(self)
        self.mark("module_import", module_entered_ns, time.monotonic_ns(), role=role)
        bootstrap = os.environ.pop("CAD2BIM_WORKER_BOOTSTRAP_NS", None)
        if bootstrap:
            self.mark("bootstrap", int(bootstrap), module_entered_ns, role=role)

    def mark(self, phase: str, start_ns: int, end_ns: int, *,
             flush: bool = False, transition: str | None = None, **details: object) -> None:
        if self._stream is None:
            return
        aggregate = self._aggregates.get(phase)
        if aggregate is None and transition != "start":
            if len(self._aggregates) < 64 and len(phase) <= 80:
                aggregate = [0, 0, start_ns, end_ns]
                self._aggregates[phase] = aggregate
            else:
                self._unclassified_events += 1
        if aggregate is not None and transition != "start":
            aggregate[0] += 1
            aggregate[1] += end_ns - start_ns
            aggregate[2] = min(aggregate[2], start_ns)
            aggregate[3] = max(aggregate[3], end_ns)
        # Reserve space for all-event aggregates; detailed events are a sample.
        try:
            if transition is not None:
                details["transition"] = transition
            if self._count >= 1024:
                self._omitted_details += 1
                return
            if self._stream.tell() >= MAX_TIMING_BYTES - 65 * 1024:
                self._omitted_details += 1
                return
            write_all(self._stream, encode_json_frame({
                "phase": phase, "start_ns": start_ns, "end_ns": end_ns,
                "pid": os.getpid(), "details": details,
            }, limit=1020))
            self._count += 1
        except (OSError, ValueError, WorkerProtocolError):
            # Optional observations cannot turn valid geometry into a failure.
            self._count = 1024
            self._omitted_details += 1
        finally:
            # A release must flush prior details even when this event itself
            # was omitted by the count/byte budget. Do not add an extra frame.
            if flush or phase in {"module_import", "bootstrap", "batch_item_released", "batch_frame_released"}:
                try:
                    self._stream.flush()
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        _ACTIVE.reset(self._token)
        if self._stream is not None:
            try:
                for phase, (count, duration, first, last) in self._aggregates.items():
                    write_json_frame(self._stream, {
                        "phase": "timing_aggregate", "start_ns": first, "end_ns": last,
                        "pid": os.getpid(), "details": {"phase": phase, "count": count,
                            "total_duration_ns": duration, "nested_durations_are_not_wall_time": True},
                    }, limit=1020)
                now = time.monotonic_ns()
                write_json_frame(self._stream, {
                    "phase": "timing_coverage", "start_ns": now, "end_ns": now,
                    "pid": os.getpid(), "details": {"detailed_events": self._count,
                        "omitted_details": self._omitted_details,
                        "unclassified_events": self._unclassified_events},
                }, limit=1020)
            except (OSError, ValueError, WorkerProtocolError):
                pass
            finally:
                try:
                    self._stream.close()
                except (OSError, ValueError):
                    # Buffered close may fail while flushing. Retire its raw
                    # owned handle without letting optional telemetry replace
                    # the business result or original exception.
                    raw = getattr(self._stream, "raw", None)
                    if raw is not None:
                        try:
                            raw.close()
                        except (OSError, ValueError):
                            pass


@contextmanager
def timed_phase(phase: str, *, flush: bool = False, **details: object) -> Iterator[None]:
    start = time.monotonic_ns()
    active = _ACTIVE.get()
    if flush and active is not None:
        active.mark(phase, start, start, flush=True, transition="start", **details)
    status = "succeeded"
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        active = _ACTIVE.get()
        if active is not None:
            if flush:
                active.mark(phase, start, time.monotonic_ns(), flush=True,
                            transition="end", status=status, **details)
            else:
                active.mark(phase, start, time.monotonic_ns(), **details)


def sample_worker_memory(phase: str, **details: object) -> None:
    """Optional self RSS sample at a known release boundary; not a resource gate."""
    active = _ACTIVE.get()
    if active is None or active._stream is None:
        return
    started = time.monotonic_ns()
    try:
        import psutil
        rss_bytes = psutil.Process().memory_info().rss
    except Exception:
        rss_bytes = None
    active.mark(phase, started, time.monotonic_ns(), rss_bytes=rss_bytes, **details)
