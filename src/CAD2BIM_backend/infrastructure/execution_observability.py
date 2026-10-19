"""Host-side monotonic phase observations for one server execution call."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import uuid4


_LOGGER = logging.getLogger("CAD2BIM.execution")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CORRELATION_ID: ContextVar[str | None] = ContextVar(
    "cad2bim_execution_correlation_id",
    default=None,
)
_OPERATION: ContextVar[str | None] = ContextVar(
    "cad2bim_execution_operation",
    default=None,
)
_INPUT_SHA256: ContextVar[str | None] = ContextVar(
    "cad2bim_execution_input_sha256",
    default=None,
)
_SPAN_ID: ContextVar[str | None] = ContextVar(
    "cad2bim_execution_span_id",
    default=None,
)


@contextmanager
def execution_observation_scope(
    correlation_id: str | None = None,
    *,
    operation: str | None = None,
    input_sha256: str | None = None,
) -> Iterator[str]:
    """Isolate one server call and propagate only safe correlation facts."""

    resolved_correlation_id = correlation_id or f"exec_{uuid4().hex}"
    _require_safe_name(resolved_correlation_id, label="execution correlation ID")
    if operation is not None:
        _require_safe_name(operation, label="execution operation")
    if input_sha256 is not None and _SHA256.fullmatch(input_sha256) is None:
        raise ValueError("execution input SHA-256 is invalid")
    correlation_token = _CORRELATION_ID.set(resolved_correlation_id)
    operation_token = _OPERATION.set(operation)
    input_token = _INPUT_SHA256.set(input_sha256)
    span_token = _SPAN_ID.set(None)
    try:
        yield resolved_correlation_id
    finally:
        _SPAN_ID.reset(span_token)
        _INPUT_SHA256.reset(input_token)
        _OPERATION.reset(operation_token)
        _CORRELATION_ID.reset(correlation_token)


@contextmanager
def observe_execution_phase(
    component: str, phase: str, *, details: dict[str, object] | None = None,
) -> Iterator[None]:
    """Emit one start and one terminal record around a host execution phase."""

    _require_safe_name(component, label="execution component")
    _require_safe_name(phase, label="execution phase")
    correlation_token: Token[str | None] | None = None
    correlation_id = _CORRELATION_ID.get()
    if correlation_id is None:
        correlation_id = f"exec_{uuid4().hex}"
        correlation_token = _CORRELATION_ID.set(correlation_id)
    parent_span_id = _SPAN_ID.get()
    span_id = f"span_{uuid4().hex}"
    span_token = _SPAN_ID.set(span_id)
    started_at = time.monotonic_ns()
    _emit_phase(
        correlation_id=correlation_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        component=component,
        phase=phase,
        transition="start",
        status="running",
        monotonic_ns=started_at,
        duration_ns=None,
        details=details,
    )
    status = "succeeded"
    try:
        yield
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except BaseException:
        status = "failed"
        raise
    finally:
        ended_at = time.monotonic_ns()
        _emit_phase(
            correlation_id=correlation_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            component=component,
            phase=phase,
            transition="end",
            status=status,
            monotonic_ns=ended_at,
            duration_ns=max(0, ended_at - started_at),
            details=details,
        )
        _SPAN_ID.reset(span_token)
        if correlation_token is not None:
            _CORRELATION_ID.reset(correlation_token)


def current_execution_correlation_id() -> str | None:
    """Return the correlation ID visible to the current async context."""

    return _CORRELATION_ID.get()


def _emit_phase(
    *,
    correlation_id: str,
    span_id: str,
    parent_span_id: str | None,
    component: str,
    phase: str,
    transition: str,
    status: str,
    monotonic_ns: int,
    duration_ns: int | None,
    details: dict[str, object] | None = None,
) -> None:
    record = {
        "schema": "cad2bim_execution_phase_v1",
        "correlation_id": correlation_id,
        "operation": _OPERATION.get(),
        "input_sha256": _INPUT_SHA256.get(),
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "component": component,
        "phase": phase,
        "transition": transition,
        "status": status,
        "monotonic_ns": monotonic_ns,
        "duration_ns": duration_ns,
    }
    if details:
        record["details"] = dict(details)
    try:
        _LOGGER.info(
            "CAD2BIM execution phase",
            extra={"cad2bim_execution_phase": record},
        )
    except Exception:
        # Observability must never alter the execution or cleanup result.
        pass


def _require_safe_name(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


__all__ = [
    "current_execution_correlation_id",
    "execution_observation_scope",
    "observe_execution_phase",
]
