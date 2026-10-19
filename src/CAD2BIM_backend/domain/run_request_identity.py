"""Client-owned identity for idempotent Run commands.

Execution protocol, thread identity, and the materialized IFC policy are chosen
by the server.  They must not turn a replay of the same client command into a
conflict after a deployment changes its default protocol.
"""

from __future__ import annotations

from typing import Any

from CAD2BIM_backend.domain.control_plane import ConflictError, RunRecord, StartRunRequest
from CAD2BIM_backend.domain.ifc_execution import IFC_EXECUTION_POLICY_KEY


def run_client_request_identity(request: StartRunRequest) -> dict[str, Any]:
    """Return the exact client-controlled portion of a Run start command."""

    payload = dict(request.input_payload)
    payload.pop(IFC_EXECUTION_POLICY_KEY, None)
    return {
        "module": request.module,
        "stage_key": request.stage_key,
        "input_payload": payload,
        "content": [item.model_dump(mode="json") for item in request.content],
    }


def new_run_request_identity(
    request: StartRunRequest,
    *,
    resume_from_savepoint_id: str | None = None,
) -> dict[str, Any]:
    """Build the persisted request fingerprint without server-owned fields."""

    return {
        "request": run_client_request_identity(request),
        "resume_from_savepoint_id": resume_from_savepoint_id,
    }


def require_exact_resume_request(
    source_run: RunRecord,
    *,
    savepoint_thread_id: str,
    request: StartRunRequest,
) -> None:
    """Validate source scope while keeping target thread identity server-owned."""

    if savepoint_thread_id != source_run.thread_id:
        raise ConflictError("savepoint thread does not match the source Run")
    if request.module != source_run.module or request.stage_key != source_run.stage_key:
        raise ConflictError("resume Run scope does not match the source Run")
    if request.thread_id is not None:
        raise ConflictError("resume target thread is server-owned")


def require_exact_resume_target(
    source_run: RunRecord,
    *,
    savepoint_thread_id: str,
    target_run: RunRecord,
) -> None:
    """Require a distinct deterministic target thread for one resumed Run."""

    if savepoint_thread_id != source_run.thread_id:
        raise ConflictError("savepoint thread does not match the source Run")
    if target_run.module != source_run.module or target_run.stage_key != source_run.stage_key:
        raise ConflictError("resume Run scope does not match the source Run")
    if (
        target_run.thread_id == source_run.thread_id
        or target_run.thread_id != f"thread_{target_run.run_id}"
    ):
        raise ConflictError("resume Run does not own a distinct target thread")


__all__ = [
    "new_run_request_identity",
    "require_exact_resume_request",
    "require_exact_resume_target",
    "run_client_request_identity",
]
