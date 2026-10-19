"""Server-selected graph routing for a Run."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExecutionProtocolError(RuntimeError):
    """The saved Run cannot be dispatched to this graph."""


class ExecutionProtocolMismatchError(ExecutionProtocolError):
    """A saved Run belongs to a different graph route."""


class ExecutionProtocolBinding(BaseModel):
    """The actual composition-root route, without implementation qualification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    module: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    stage_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")

    def require_scope(self, *, module: str, stage_key: str) -> None:
        if (self.module, self.stage_key) != (module, stage_key):
            raise ExecutionProtocolMismatchError(
                f"Run route {module}:{stage_key} does not match {self.module}:{self.stage_key}"
            )


def require_current_execution_protocol(
    binding: ExecutionProtocolBinding,
    *,
    expected: ExecutionProtocolBinding,
    module: str,
    stage_key: str,
) -> None:
    """Dispatch only to the selected graph, retaining the Run's scope."""

    binding.require_scope(module=module, stage_key=stage_key)
    if binding != expected:
        raise ExecutionProtocolMismatchError(
            "Run belongs to another graph route"
        )


__all__ = [
    "ExecutionProtocolBinding",
    "ExecutionProtocolError",
    "ExecutionProtocolMismatchError",
    "require_current_execution_protocol",
]
