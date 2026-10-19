from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import sys
import time
import threading
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest


def test_seatbelt_denies_native_scratch_save_but_preserves_host_output_fd(tmp_path):
    from CAD2BIM_backend.infrastructure.worker_runtime.darwin_seatbelt import DarwinSeatbeltSandbox

    output = tmp_path / "host-output.arrow"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    sandbox = DarwinSeatbeltSandbox(sys.executable, allow_scratch_writes=False)
    source = '''import os, pyarrow as pa
with os.fdopen(FD, "wb") as sink:
    with pa.ipc.new_stream(sink, pa.schema([("value", pa.int64())])) as writer:
        writer.write_table(pa.table({"value": [7]}))
try:
    pa.OSFile("native-save.arrow", "wb")
except PermissionError:
    print("native-save-denied")
else:
    raise AssertionError("Native path save unexpectedly succeeded")
'''.replace("FD", str(descriptor))
    try:
        result = subprocess.run(sandbox.command(scratch=tmp_path, child_args=("-I", "-B", "-c", source)),
                                cwd=tmp_path, pass_fds=(descriptor,), capture_output=True, timeout=10)
    finally:
        os.close(descriptor)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"native-save-denied\n"
    assert not (tmp_path / "native-save.arrow").exists()
    import pyarrow as pa
    assert pa.ipc.open_stream(output).read_all().to_pylist() == [{"value": 7}]

from CAD2BIM_backend.infrastructure.data_transform import worker_protocol as legacy_protocol
from CAD2BIM_backend.infrastructure import execution_observability
from CAD2BIM_backend.infrastructure.execution_observability import (
    current_execution_correlation_id,
    execution_observation_scope,
    observe_execution_phase,
)
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import (
    HostWorkerCapacity,
    WorkerCapacityBusy,
)
from CAD2BIM_backend.infrastructure.worker_runtime.protocol import (
    WorkerProtocolError,
    read_json_frame,
    write_json_frame,
)
from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import (
    WorkerLaunchSpec,
    WorkerProcessBudget,
    WorkerProcessSupervisor,
    WorkerSupervisorError,
)


def test_resource_sample_uses_one_consistent_cpu_snapshot_and_keeps_unknown_scratch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from CAD2BIM_backend.infrastructure.worker_runtime import supervisor
    from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import WorkerResourceViolation
    monkeypatch.setattr(supervisor, "_worker_children", lambda process: process.children(recursive=True))

    class Process:
        def __init__(self):
            self.cpu_reads = 0
        def children(self, recursive):
            return []
        def oneshot(self):
            return nullcontext()
        def memory_info(self):
            return SimpleNamespace(rss=100)
        def cpu_times(self):
            self.cpu_reads += 1
            return SimpleNamespace(user=0.25 * self.cpu_reads, system=0.5 * self.cpu_reads)
        def num_threads(self):
            return 1
        def num_fds(self):
            return 3

    process = Process()
    costs = {}
    violation, observation = WorkerProcessSupervisor.resource_violation(process, tmp_path,
        _budget(cpu_seconds=1), baseline_threads=1, _sample_costs=costs)
    assert violation is None and observation.peak_cpu_seconds == 0.75
    assert process.cpu_reads == 1
    assert costs['scratch_scan_wall_ns'] >= 0
    unknown = tmp_path / 'unknown-output'
    unknown.mkdir()
    (unknown / 'partial.bin').write_bytes(b'x' * 20)
    violation, observation = WorkerProcessSupervisor.resource_violation(Process(), tmp_path,
        _budget(scratch_bytes=10), baseline_threads=1)
    assert violation is WorkerResourceViolation.SCRATCH and observation.peak_scratch_bytes == 20
    (unknown / 'partial.bin').unlink()
    violation, _ = WorkerProcessSupervisor.resource_violation(Process(), tmp_path,
        _budget(scratch_bytes=10), baseline_threads=1)
    assert violation is None


def _budget(**overrides: object) -> WorkerProcessBudget:
    values: dict[str, object] = {
        "wall_seconds": 2.0,
        "cpu_seconds": 2.0,
        "rss_bytes": 256 * 1024**2,
        "scratch_bytes": 1024 * 1024,
        "threads": 4,
        "open_file_descriptors": 32,
        "stdout_bytes": 1024,
        "stderr_bytes": 1024,
        "cleanup_seconds": 1.0,
    }
    values.update(overrides)
    return WorkerProcessBudget(**values)  # type: ignore[arg-type]


def test_trusted_initialization_baseline_is_host_measured_once():
    from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import WorkerInvocationControl

    class Process:
        def num_threads(self):
            return 5

    ordinary = WorkerInvocationControl(Process(), _budget())
    with pytest.raises(RuntimeError, match="only be accepted once"):
        ordinary.accept_thread_baseline()
    initialized = WorkerInvocationControl(Process(), _budget(), initializing=True)
    assert initialized.accept_thread_baseline() == 5
    with pytest.raises(RuntimeError, match="only be accepted once"):
        initialized.accept_thread_baseline()


def test_exited_process_initialization_peak_is_not_live_author_thread_growth(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import psutil
    from CAD2BIM_backend.infrastructure.worker_runtime import supervisor

    class Process:
        alive = True

        def oneshot(self):
            if not self.alive:
                raise psutil.NoSuchProcess(123)
            return nullcontext()

        def memory_info(self):
            return SimpleNamespace(rss=100)

        def cpu_times(self):
            return SimpleNamespace(user=0.1, system=0.1)

        def num_threads(self):
            return 10

        def num_fds(self):
            return 3

    monkeypatch.setattr(supervisor, "_worker_children", lambda worker: [])
    process = Process()
    budget = _budget(threads=2)
    violation, peaks = supervisor.WorkerProcessSupervisor.resource_violation(
        process, tmp_path, budget, baseline_threads=0,
    )
    assert violation is None and peaks.peak_threads == 10
    violation, _ = supervisor.WorkerProcessSupervisor.resource_violation(
        process, tmp_path, budget, baseline_threads=5, previous=peaks,
    )
    assert violation is supervisor.WorkerResourceViolation.THREADS
    process.alive = False
    violation, final = supervisor.WorkerProcessSupervisor.resource_violation(
        process, tmp_path, budget, baseline_threads=5, previous=peaks,
    )
    assert violation is None and final.peak_threads == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,limits", (
    ("rss", {"rss_bytes": 1}),
    ("open_file_descriptors", {"open_file_descriptors": 1}),
    ("cpu", {"cpu_seconds": 0.05}),
    ("wall", {"wall_seconds": 0.05}),
))
async def test_trusted_initialization_keeps_process_limits_active(tmp_path, kind, limits):
    async def initialize(control):
        assert control.thread_baseline is None
        await asyncio.Event().wait()

    with pytest.raises(WorkerSupervisorError) as caught:
        await WorkerProcessSupervisor().run(
            WorkerLaunchSpec(command=(sys.executable, "-I", "-c", "while True: pass"),
                             cwd=tmp_path, env={}, scratch=tmp_path),
            budget=_budget(**limits), deadline=time.monotonic() + 2, initialize=initialize,
        )
    assert caught.value.resource_kind == kind


@pytest.mark.parametrize("native_result", ["empty", "child", "errno", "negative", "raises", "unavailable", "other_platform"])
def test_worker_child_probe_only_skips_recursive_query_for_verified_empty_tree(monkeypatch, native_result):
    import ctypes
    import errno
    from types import SimpleNamespace
    from CAD2BIM_backend.infrastructure.worker_runtime import supervisor

    descendants, recursive_calls = [object(), object()], []
    def recursive(*, recursive):
        recursive_calls.append(recursive)
        return descendants
    def query(pid, buffer, size):
        if native_result == "raises":
            raise OSError("injected libproc failure")
        if native_result == "errno":
            ctypes.set_errno(errno.EIO)
            return 0
        if native_result == "child":
            buffer[0] = 124
            return 1
        return -1 if native_result == "negative" else 0
    monkeypatch.setattr(supervisor.sys, "platform", "other" if native_result == "other_platform" else "darwin")
    monkeypatch.setattr(supervisor, "_darwin_child_query", lambda: None if native_result == "unavailable" else query)
    worker = SimpleNamespace(pid=123, is_running=lambda: True, children=recursive)
    actual = supervisor._worker_children(worker)
    assert actual == ([] if native_result == "empty" else descendants)
    assert recursive_calls == ([] if native_result == "empty" else [True])

@pytest.mark.parametrize("states", [(False,), (True, False)])
def test_worker_child_probe_rejects_reused_or_disappeared_identity(monkeypatch, states):
    import psutil
    from types import SimpleNamespace
    from CAD2BIM_backend.infrastructure.worker_runtime import supervisor

    identities = iter(states)
    worker = SimpleNamespace(pid=123, is_running=lambda: next(identities))
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(supervisor, "_darwin_child_query", lambda: lambda *args: 0)
    with pytest.raises(psutil.NoSuchProcess):
        supervisor._worker_children(worker)

@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="exercises actual Darwin child-query path")
@pytest.mark.parametrize("depth", [1, 2])
async def test_supervisor_rejects_and_cleans_actual_child_tree(tmp_path, depth):
    import json
    import psutil
    from CAD2BIM_backend.infrastructure.worker_runtime import supervisor

    assert supervisor._darwin_child_query() is not None
    pid_file = tmp_path / "tree-pids.json"
    source = tmp_path / "worker_tree.py"
    source.write_text('''import json, os, pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
child = None
try:
    depth, original_depth = int(sys.argv[1]), int(sys.argv[2])
    pids = [os.getpid()]
    if depth:
        child = subprocess.Popen([sys.executable, __file__, str(depth-1), str(original_depth), sys.argv[3]], stdout=subprocess.PIPE, text=True)
        pids += json.loads(child.stdout.readline())
    if depth == original_depth:
        pathlib.Path(sys.argv[3]).write_text(json.dumps(pids))
    print(json.dumps(pids), flush=True)
    time.sleep(30)
finally:
    if child is not None:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=2)
''')
    class ReadyTreeSupervisor(WorkerProcessSupervisor):
        @staticmethod
        async def settle_thread_baseline(worker):
            # Ensure the forbidden descendant depth exists before inspection.
            async with asyncio.timeout(2):
                while not pid_file.exists():
                    await asyncio.sleep(.005)
            return await WorkerProcessSupervisor.settle_thread_baseline(worker)
    owned = []
    try:
        with pytest.raises(WorkerSupervisorError) as failure:
            await ReadyTreeSupervisor().run(
                WorkerLaunchSpec(command=(sys.executable, "-I", str(source), str(depth), str(depth), str(pid_file)),
                    cwd=tmp_path, env={}, scratch=tmp_path),
                budget=_budget(wall_seconds=4), deadline=time.monotonic()+5)
        pids = json.loads(pid_file.read_text())
        owned = [psutil.Process(pid) for pid in pids if psutil.pid_exists(pid)]
        assert len(pids) == depth + 1
        assert failure.value.resource_kind == "subprocess"
        assert not owned
    finally:
        # Cleanup is limited to the exact PID objects created by this test.
        for process in owned:
            if process.is_running():
                process.kill()
        psutil.wait_procs(owned, timeout=2)


def _phase_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        record.cad2bim_execution_phase
        for record in caplog.records
        if hasattr(record, "cad2bim_execution_phase")
    ]


@pytest.mark.asyncio
async def test_guard_registration_commits_before_child_starts(tmp_path: Path) -> None:
    registered, release = asyncio.Event(), asyncio.Event()
    output = tmp_path / "child-started"

    async def register(pid: int, birth_us: int) -> None:
        assert pid > 0 and birth_us > 0
        registered.set()
        await release.wait()

    running = asyncio.create_task(WorkerProcessSupervisor().run(
        WorkerLaunchSpec(
            command=(sys.executable, "-I", "-c",
                     "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('started')",
                     str(output)),
            cwd=tmp_path, env={}, scratch=tmp_path,
        ),
        budget=_budget(), deadline=time.monotonic() + 3,
        on_guard_registered=register,
    ))
    await asyncio.wait_for(registered.wait(), timeout=2)
    assert not output.exists()
    release.set()
    terminal = await running
    assert terminal.returncode == 0
    assert output.read_text() == "started"


@pytest.mark.asyncio
async def test_failed_guard_registration_never_starts_child(tmp_path: Path) -> None:
    output = tmp_path / "must-not-start"

    async def reject(_pid: int, _birth_us: int) -> None:
        raise RuntimeError("association rejected")

    with pytest.raises(RuntimeError, match="association rejected"):
        await WorkerProcessSupervisor().run(
            WorkerLaunchSpec(
                command=(sys.executable, "-I", "-c",
                         "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('bad')",
                         str(output)),
                cwd=tmp_path, env={}, scratch=tmp_path,
            ),
            budget=_budget(), deadline=time.monotonic() + 3,
            on_guard_registered=reject,
        )
    assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_guard_registration_wait_obeys_deadline_and_cancellation(tmp_path, cancel):
    import psutil

    registered = asyncio.Event()
    output = tmp_path / "must-not-start"
    guard_pid = None

    async def register(pid, birth_us):
        nonlocal guard_pid
        guard_pid = pid
        registered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(WorkerProcessSupervisor().run(
        WorkerLaunchSpec(
            command=(sys.executable, "-I", "-c",
                     "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('bad')",
                     str(output)),
            cwd=tmp_path, env={}, scratch=tmp_path,
        ),
        budget=_budget(), deadline=time.monotonic() + (3 if cancel else 0.3),
        on_guard_registered=register,
    ))
    await asyncio.wait_for(registered.wait(), timeout=2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(WorkerSupervisorError) as failure:
            await task
        assert failure.value.code == "WORKER_TIMEOUT"
        assert failure.value.execution_started is False
        assert failure.value.resource_kind == "wall"
    assert not output.exists()
    assert guard_pid is not None and not psutil.pid_exists(guard_pid)


def test_cpu_budget_read_after_worker_exit_reports_supervisor_failure():
    import psutil
    from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import WorkerInvocationControl

    class ExitedProcess:
        def cpu_times(self):
            raise psutil.NoSuchProcess(12345)

    with pytest.raises(WorkerSupervisorError) as failure:
        _ = WorkerInvocationControl(ExitedProcess(), _budget()).remaining_cpu_seconds
    assert failure.value.code == "WORKER_SERVICE_UNAVAILABLE"
    assert failure.value.execution_started is True
    assert isinstance(failure.value.__cause__, psutil.NoSuchProcess)


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension,observed,scope,limit", [
    ("cpu", 31.0, "invocation", 30.0),
    ("cpu", 901.0, "process", 900.0),
    ("wall", 31.0, "invocation", 30.0),
])
async def test_invocation_failure_keeps_its_measured_limit_after_interval_closes(
    monkeypatch, dimension, observed, scope, limit,
):
    from types import SimpleNamespace
    from CAD2BIM_backend.infrastructure.worker_runtime import supervisor

    clock = [0.0]
    cpu = [0.0]
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock[0])
    process = SimpleNamespace(cpu_times=lambda: SimpleNamespace(user=cpu[0], system=0.0))
    control = supervisor.WorkerInvocationControl(process, _budget(wall_seconds=900, cpu_seconds=900))
    with pytest.raises(WorkerSupervisorError) as caught:
        async with control.invocation(wall_seconds=30, cpu_seconds=30):
            control.check()
            (cpu if dimension == "cpu" else clock)[0] = observed
            control.check()
    diagnostic = caught.value.diagnostic()
    assert diagnostic["resource_kind"] == dimension
    assert diagnostic["budget_scope"] == scope
    assert diagnostic["limit"] == limit
    assert diagnostic["observed"] == observed
    assert control._active is None


def test_json_frame_handles_short_writes():
    class ShortWriter(io.BytesIO):
        def write(self, value):
            return super().write(value[:3])
    stream = ShortWriter()
    write_json_frame(stream, {"message": "bounded frame"}, limit=1024)
    stream.seek(0)
    assert read_json_frame(stream, limit=1024) == {"message": "bounded frame"}
    assert stream.read() == b""


@pytest.mark.asyncio
async def test_async_worker_channel_backpressure_and_eof():
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import AsyncWorkerChannel
    read_fd, write_fd = os.pipe()
    channel = AsyncWorkerChannel(read_fd, write_fd)
    payload = b"x" * (512 * 1024)
    try:
        sender = asyncio.create_task(channel.write(payload))
        await asyncio.sleep(0)
        assert not sender.done()
        assert await channel.read_exact(len(payload)) == payload
        await sender
        os.close(write_fd)
        write_fd = -1
        with pytest.raises(WorkerProtocolError, match="ended early"):
            await channel.read_exact(1)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [WorkerProtocolError, ValueError])
async def test_supervisor_interaction_failure_retires_owned_process(tmp_path, failure_type):
    import psutil
    processes = []
    class TrackingSupervisor(WorkerProcessSupervisor):
        async def launch(self, *args, **kwargs):
            process = await super().launch(*args, **kwargs)
            processes.append(process)
            return process
    async def interaction(_supervision):
        raise failure_type("injected corrupt frame")
    with pytest.raises(failure_type, match="corrupt frame"):
        await TrackingSupervisor().run(WorkerLaunchSpec(
            command=(sys.executable, "-I", "-c", "import time; time.sleep(30)"), cwd=tmp_path, env={}, scratch=tmp_path),
            budget=_budget(), deadline=time.monotonic()+3, interaction=interaction)
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert not psutil.pid_exists(WorkerProcessSupervisor.execution_pid(processes[0]))


@pytest.mark.asyncio
async def test_supervisor_enforces_invocation_cpu_before_lifecycle_limit(tmp_path):
    async def interaction(supervision):
        async with supervision.invocation(wall_seconds=2, cpu_seconds=0.05):
            await asyncio.sleep(5)
    with pytest.raises(WorkerSupervisorError) as failure:
        await WorkerProcessSupervisor().run(WorkerLaunchSpec(
            command=(sys.executable,"-I","-c","while True: pass"),cwd=tmp_path,env={},scratch=tmp_path),
            budget=_budget(wall_seconds=3,cpu_seconds=3),deadline=time.monotonic()+4,interaction=interaction)
    assert failure.value.resource_kind == "cpu"
    assert failure.value.elapsed_seconds < 2


@pytest.mark.asyncio
async def test_supervisor_post_exit_interaction_deadline_is_wall_timeout(tmp_path):
    import psutil
    processes, tasks = [], []
    drained = asyncio.Event()
    class TrackingSupervisor(WorkerProcessSupervisor):
        async def launch(self, *args, **kwargs):
            process = await super().launch(*args, **kwargs)
            processes.append(process)
            return process
    async def interaction(_control):
        tasks.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()
    with pytest.raises(WorkerSupervisorError) as failure:
        await TrackingSupervisor().run(WorkerLaunchSpec(
            command=(sys.executable, '-I', '-c', 'import time; time.sleep(0.1)'),
            cwd=tmp_path, env={}, scratch=tmp_path), budget=_budget(),
            deadline=time.monotonic()+0.8, interaction=interaction)
    assert failure.value.code == 'WORKER_TIMEOUT'
    assert failure.value.execution_started and failure.value.resource_kind == 'wall'
    assert 'after process exit' in str(failure.value)
    assert drained.is_set() and len(tasks) == 1 and tasks[0].done()
    assert len(processes) == 1 and processes[0].returncode == 0
    assert not psutil.pid_exists(WorkerProcessSupervisor.execution_pid(processes[0]))


def test_execution_observation_scope_is_closed_nested_monotonic_and_isolated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    input_sha256 = "a" * 64
    with execution_observation_scope(
        operation="ifc_product_realization_preview",
        input_sha256=input_sha256,
    ) as first_correlation:
        assert current_execution_correlation_id() == first_correlation
        with observe_execution_phase("direct_provider", "dispatch"):
            with observe_execution_phase("geometry_codec", "render"):
                pass
    assert current_execution_correlation_id() is None
    with execution_observation_scope(operation="ifc_geometry_preview") as second_correlation:
        with observe_execution_phase("direct_provider", "dispatch"):
            pass

    assert first_correlation != second_correlation
    records = _phase_records(caplog)
    assert len(records) == 6
    assert all(
        set(record)
        == {
            "schema",
            "correlation_id",
            "operation",
            "input_sha256",
            "span_id",
            "parent_span_id",
            "component",
            "phase",
            "transition",
            "status",
            "monotonic_ns",
            "duration_ns",
        }
        for record in records
    )
    first_records = [record for record in records if record["correlation_id"] == first_correlation]
    assert {record["operation"] for record in first_records} == {"ifc_product_realization_preview"}
    assert {record["input_sha256"] for record in first_records} == {input_sha256}
    dispatch_start, render_start, render_end, dispatch_end = first_records
    assert dispatch_start["transition"] == render_start["transition"] == "start"
    assert render_end["transition"] == dispatch_end["transition"] == "end"
    assert render_start["parent_span_id"] == dispatch_start["span_id"]
    assert render_end["parent_span_id"] == dispatch_start["span_id"]
    assert dispatch_end["parent_span_id"] is None
    assert render_end["status"] == dispatch_end["status"] == "succeeded"
    assert render_end["monotonic_ns"] >= render_start["monotonic_ns"]
    assert dispatch_end["monotonic_ns"] >= dispatch_start["monotonic_ns"]
    assert render_end["duration_ns"] >= 0
    assert dispatch_end["duration_ns"] >= render_end["duration_ns"]


def test_execution_observation_rejects_unsafe_correlation_facts() -> None:
    with pytest.raises(ValueError, match="operation is invalid"):
        with execution_observation_scope(operation="/Users/private/source.py"):
            pass
    with pytest.raises(ValueError, match="SHA-256 is invalid"):
        with execution_observation_scope(input_sha256="not-a-digest"):
            pass


def test_execution_observation_failure_cannot_change_business_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_log(*_args, **_kwargs) -> None:
        raise RuntimeError("controlled handler failure")

    monkeypatch.setattr(execution_observability._LOGGER, "info", fail_log)
    with execution_observation_scope(operation="ifc_geometry_preview"):
        with observe_execution_phase("geometry_codec", "render"):
            pass


@pytest.mark.asyncio
async def test_execution_observation_scopes_do_not_cross_concurrent_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    entered = 0
    both_entered = asyncio.Event()

    async def run(operation: str, input_sha256: str) -> str:
        nonlocal entered
        with execution_observation_scope(
            operation=operation,
            input_sha256=input_sha256,
        ) as correlation_id:
            with observe_execution_phase("direct_provider", "dispatch"):
                entered += 1
                if entered == 2:
                    both_entered.set()
                await asyncio.wait_for(both_entered.wait(), timeout=1)
                assert current_execution_correlation_id() == correlation_id
            return correlation_id

    first, second = await asyncio.gather(
        run("ifc_geometry_preview", "a" * 64),
        run("ifc_product_realization_preview", "b" * 64),
    )

    assert first != second
    records = _phase_records(caplog)
    by_correlation = {
        correlation_id: [record for record in records if record["correlation_id"] == correlation_id]
        for correlation_id in (first, second)
    }
    assert {
        (records[0]["operation"], records[0]["input_sha256"]) for records in by_correlation.values()
    } == {
        ("ifc_geometry_preview", "a" * 64),
        ("ifc_product_realization_preview", "b" * 64),
    }
    assert all(len(records) == 2 for records in by_correlation.values())


def test_legacy_protocol_reexports_the_neutral_implementation() -> None:
    assert legacy_protocol.read_json_frame is read_json_frame
    assert legacy_protocol.write_json_frame is write_json_frame
    stream = io.BytesIO()
    write_json_frame(stream, {"event": "ready"}, limit=64)
    stream.seek(0)
    assert read_json_frame(stream, limit=64) == {"event": "ready"}

    oversized = io.BytesIO(struct.pack("!I", 65))
    with pytest.raises(WorkerProtocolError, match="exceeds its byte limit"):
        read_json_frame(oversized, limit=64)


@pytest.mark.asyncio
async def test_capacity_instances_share_one_host_lock(tmp_path: Path) -> None:
    first = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    second = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    descriptor = await first.acquire_host(time.monotonic() + 1)
    try:
        with pytest.raises(WorkerCapacityBusy):
            await second.acquire_host(time.monotonic() + 0.03)
    finally:
        os.close(descriptor)

    acquired = await second.acquire_host(time.monotonic() + 1)
    os.close(acquired)


@pytest.mark.asyncio
async def test_supervisor_captures_diagnostics_without_interpreting_stdout(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    supervisor = WorkerProcessSupervisor()
    terminal = await supervisor.run(
        WorkerLaunchSpec(
            command=(
                sys.executable,
                "-I",
                "-c",
                (
                    "import pathlib,sys; "
                    "pathlib.Path('terminal.json').write_text('{\"ok\":true}'); "
                    "print('diagnostic'); print('warning', file=sys.stderr)"
                ),
            ),
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            scratch=tmp_path,
        ),
        budget=_budget(),
        deadline=time.monotonic() + 3,
    )

    assert terminal.returncode == 0
    assert terminal.status_payload is None
    assert terminal.stdout == b"diagnostic\n"
    assert terminal.stderr == b"warning\n"
    assert terminal.execution_started is True
    assert (tmp_path / "terminal.json").read_text() == '{"ok":true}'
    worker_records = [
        record for record in _phase_records(caplog) if record["component"] == "worker_runtime"
    ]
    assert [(record["transition"], record["status"]) for record in worker_records] == [
        ("start", "running"),
        ("end", "succeeded"),
    ]


@pytest.mark.asyncio
async def test_supervisor_timeout_stops_the_process_group(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    supervisor = WorkerProcessSupervisor()
    with pytest.raises(WorkerSupervisorError) as caught:
        await supervisor.run(
            WorkerLaunchSpec(
                command=(sys.executable, "-I", "-c", "import time; time.sleep(30)"),
                cwd=tmp_path,
                env={"PATH": os.environ.get("PATH", "")},
                scratch=tmp_path,
            ),
            budget=_budget(wall_seconds=0.05),
            deadline=time.monotonic() + 1,
        )

    assert caught.value.code == "WORKER_TIMEOUT"
    assert caught.value.execution_started is True
    assert caught.value.resource_kind == "wall"
    assert caught.value.budget.wall_seconds == 0.05
    assert caught.value.elapsed_seconds >= 0.05
    assert 0 <= caught.value.effective_wall_seconds <= 0.05
    worker_records = [
        record for record in _phase_records(caplog) if record["component"] == "worker_runtime"
    ]
    assert [(record["transition"], record["status"]) for record in worker_records] == [
        ("start", "running"),
        ("end", "failed"),
    ]


@pytest.mark.asyncio
async def test_slow_resource_sample_keeps_ipc_and_hard_timeout_responsive(tmp_path, monkeypatch):
    import socket
    import psutil

    sample_entered = threading.Event()
    sample_finished = threading.Event()
    sample_calls = 0
    roundtrip_seconds = None
    sampler = WorkerProcessSupervisor.resource_violation

    def slow_sample(*args, **kwargs):
        nonlocal sample_calls
        sample_calls += 1
        sample_entered.set()
        try:
            # Model an expensive, cooperatively cancellable resource traversal.
            kwargs['_stop_event'].wait(1.0)
            return sampler(*args, **kwargs)
        finally:
            sample_finished.set()

    monkeypatch.setattr(WorkerProcessSupervisor, 'resource_violation', staticmethod(slow_sample))
    parent, child = socket.socketpair()
    parent.setblocking(False)
    pid_file = tmp_path / 'child.pid'
    source = (
        "import os,pathlib,socket,time; "
        "pathlib.Path('child.pid').write_text(str(os.getpid())); "
        f"channel=socket.socket(fileno={child.fileno()}); "
        "channel.sendall(channel.recv(4)); time.sleep(30)"
    )

    async def interact(control):
        nonlocal roundtrip_seconds
        while not sample_entered.is_set():
            await asyncio.sleep(0.005)
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(parent, b'ping')
        assert await loop.sock_recv(parent, 4) == b'ping'
        roundtrip_seconds = time.monotonic() - started
        assert not sample_finished.is_set()
        await asyncio.Future()

    started = time.monotonic()
    try:
        with pytest.raises(WorkerSupervisorError) as caught:
            await WorkerProcessSupervisor().run(
                WorkerLaunchSpec(command=(sys.executable, '-I', '-c', source), cwd=tmp_path,
                    env={'PATH': os.environ.get('PATH', '')}, scratch=tmp_path,
                    pass_fds=(child.fileno(),)),
                budget=_budget(wall_seconds=0.35), deadline=time.monotonic() + 2,
                interaction=interact,
            )
        elapsed = time.monotonic() - started
        assert caught.value.resource_kind == 'wall'
        assert sample_calls == 1 and sample_finished.is_set()
        assert roundtrip_seconds is not None and roundtrip_seconds < 0.25
        assert elapsed < 0.8  # The one-second sampler cannot hold the hard timeout.
        assert not psutil.pid_exists(int(pid_file.read_text()))
        print(f'slow-sample IPC={roundtrip_seconds:.6f}s timeout+cleanup={elapsed:.6f}s')
    finally:
        parent.close()
        child.close()


@pytest.mark.asyncio
async def test_supervisor_checks_final_scratch_after_fast_process_exit(tmp_path):
    with pytest.raises(WorkerSupervisorError) as caught:
        await WorkerProcessSupervisor().run(
            WorkerLaunchSpec(command=(sys.executable, '-I', '-c',
                "import pathlib; pathlib.Path('output').write_bytes(b'x'*1024)"),
                cwd=tmp_path, env={'PATH': os.environ.get('PATH', '')}, scratch=tmp_path),
            budget=_budget(scratch_bytes=64), deadline=time.monotonic() + 2,
        )
    assert caught.value.resource_kind == 'scratch'
    assert caught.value.observations.peak_scratch_bytes == 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "overrides", "source", "peak"),
    [
        ("cpu", {"cpu_seconds": 0.02}, "while True: pass", "peak_cpu_seconds"),
        ("rss", {"rss_bytes": 1}, "import time; time.sleep(30)", "peak_rss_bytes"),
        ("scratch", {"scratch_bytes": 64},
         "import pathlib,time; pathlib.Path('output').write_bytes(b'x'*1024); time.sleep(30)",
         "peak_scratch_bytes"),
        ("open_file_descriptors", {"open_file_descriptors": 4},
         "import time; files=[open('descriptor-'+str(i),'w') for i in range(8)]; time.sleep(30)",
         "peak_open_file_descriptors"),
    ],
)
async def test_supervisor_retains_actual_resource_trigger_and_sampled_peaks(
    tmp_path: Path, kind: str, overrides: dict, source: str, peak: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import psutil

    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    pid_file=tmp_path/'child.pid'
    source="import os,pathlib; pathlib.Path('child.pid').write_text(str(os.getpid()))\n"+source
    with pytest.raises(WorkerSupervisorError) as caught:
        await WorkerProcessSupervisor().run(
            WorkerLaunchSpec(command=(sys.executable,"-I","-c",source),cwd=tmp_path,
                             env={"PATH":os.environ.get("PATH","")},scratch=tmp_path),
            budget=_budget(**overrides),deadline=time.monotonic()+2,
        )
    error=caught.value
    assert error.execution_started is True
    assert error.code=="WORKER_RESOURCE_LIMIT"
    assert error.resource_kind==kind
    assert error.observations is not None
    assert getattr(error.observations,peak)>0
    assert error.budget==_budget(**overrides)
    assert error.elapsed_seconds is not None
    terminal = [record for record in _phase_records(caplog)
                if record["component"] == "worker_runtime" and record["transition"] == "end"]
    assert len(terminal) == 1
    assert terminal[0]["details"]["failure_diagnostic"] == error.diagnostic()
    if pid_file.exists():
        assert not psutil.pid_exists(int(pid_file.read_text()))


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated", (False, True))
async def test_cancelled_supervisor_preserves_observation_and_stops_child(tmp_path: Path, repeated: bool) -> None:
    import psutil

    pid_file = tmp_path / "child.pid"
    task = asyncio.create_task(WorkerProcessSupervisor().run(
        WorkerLaunchSpec(
            command=(sys.executable, "-I", "-c",
                     "import os,pathlib,time,signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"),
            cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")}, scratch=tmp_path,
        ),
        budget=_budget(), deadline=time.monotonic() + 3,
    ))
    try:
        async with asyncio.timeout(2):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        if repeated:
            await asyncio.sleep(0.03)
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        failure = caught.value.worker_failure
        assert failure.execution_started is True
        assert failure.resource_kind == "cancelled"
        assert failure.elapsed_seconds is not None
        assert not psutil.pid_exists(int(pid_file.read_text()))
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
