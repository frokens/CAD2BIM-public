"""Compatibility exports for the shared controlled-worker protocol."""

from CAD2BIM_backend.infrastructure.worker_runtime.protocol import (
    MAX_CONTROL_BYTES,
    MAX_STATUS_BYTES,
    WorkerProtocolError,
    open_binary_fd,
    read_json_frame,
    write_json_frame,
)


__all__ = [
    "MAX_CONTROL_BYTES",
    "MAX_STATUS_BYTES",
    "WorkerProtocolError",
    "open_binary_fd",
    "read_json_frame",
    "write_json_frame",
]
