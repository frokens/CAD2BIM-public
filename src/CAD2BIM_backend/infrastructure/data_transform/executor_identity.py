"""Local process identity used to prove a prior Data Transform publisher stopped."""

from __future__ import annotations

from CAD2BIM_backend.infrastructure.local_process_identity import (
    current_local_executor_owner,
    local_executor_stopped,
)


def current_data_transform_executor_owner() -> str:
    """Bind an execution claim to one process lifetime, including PID reuse."""
    return current_local_executor_owner("transform")


def data_transform_executor_stopped(owner: str) -> bool:
    """Return true only for a verifiably ended process on this host."""
    return local_executor_stopped(owner, kind="transform")


__all__ = ["current_data_transform_executor_owner", "data_transform_executor_stopped"]
