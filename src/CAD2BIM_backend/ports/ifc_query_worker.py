"""Application-facing boundary for isolated, read-only IFC Python queries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from contextlib import AbstractAsyncContextManager
from CAD2BIM_backend.domain.runtime_execution import RuntimeExecutionPolicy

from CAD2BIM_backend.ports.arrow_inputs import ArrowInput


MAX_IFC_INPUT_BYTES = 512 * 1024**2
_MAX_QUERY_CODE_BYTES = 65_536
_MAX_RESULT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class IfcQueryWorkerRequest:
    """Exact private inputs for one query execution; no durable identity crosses this Port."""

    code: str
    ifc_bytes: bytes
    expected_ifc_sha256: str
    max_result_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        if not self.code.strip() or "\x00" in self.code:
            raise ValueError("IFC query code must not be blank or contain NUL")
        if len(self.code.encode("utf-8")) > _MAX_QUERY_CODE_BYTES:
            raise ValueError("IFC query code exceeds 65536 UTF-8 bytes")
        if not isinstance(self.ifc_bytes, bytes) or not self.ifc_bytes:
            raise ValueError("IFC query input must be non-empty exact bytes")
        if len(self.ifc_bytes) > MAX_IFC_INPUT_BYTES:
            raise ValueError("IFC query input exceeds 512 MiB")
        actual_sha256 = hashlib.sha256(self.ifc_bytes).hexdigest()
        if self.expected_ifc_sha256 != actual_sha256:
            raise ValueError("IFC query input digest does not match exact bytes")
        if (
            not isinstance(self.max_result_bytes, int)
            or isinstance(self.max_result_bytes, bool)
            or not 1 <= self.max_result_bytes <= _MAX_RESULT_BYTES
        ):
            raise ValueError("IFC query result budget must be between 1 byte and 1 MiB")


@dataclass(frozen=True, slots=True)
class IfcQueryWorkerResult:
    result: Any
    result_json: str
    result_bytes: int
    ifc_sha256: str
    logs: tuple[str, ...] = ()


class IfcQueryWorkerError(RuntimeError):
    """Stable typed failure returned by the isolated query adapter."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        execution_started: bool,
        diagnostic: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.phase = phase
        self.execution_started = execution_started
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None
        self.retryable = retryable
        super().__init__(message)


class IfcQueryWorkerPort(Protocol):
    def execution_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None) -> AbstractAsyncContextManager[object]: ...
    async def verify_ready(self) -> None: ...

    async def execute_query(
        self,
        request: IfcQueryWorkerRequest,
        inputs: tuple[ArrowInput, ...] = (),
        *, deadline: float | None = None,
    ) -> IfcQueryWorkerResult: ...

    async def close(self) -> None: ...


__all__ = [
    "MAX_IFC_INPUT_BYTES",
    "IfcQueryWorkerError",
    "IfcQueryWorkerPort",
    "IfcQueryWorkerRequest",
    "IfcQueryWorkerResult",
]
