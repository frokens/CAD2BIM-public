"""Authenticated user Task/Run HTTP entry, composed by the backend bootstrap."""

from __future__ import annotations

from datetime import datetime
import mimetypes
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Literal, TYPE_CHECKING
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.application.feedback_service import FeedbackService
from CAD2BIM_backend.domain.control_plane import (
    ConflictError,
    CreateTaskRequest,
    FeedbackCommandReceipt,
    FeedbackDeliveryMode,
    FeedbackScope,
    ResourceNotFoundError,
    RespondGateCommand,
    RunRecord,
    RunStatus,
    StartRunRequest,
    SubmitFeedbackCommand,
    TaskRecord,
    TaskStatus,
    UserContentBlock,
)
from CAD2BIM_backend.domain.feedback_target import FeedbackTargetConflictError
from CAD2BIM_backend.domain.system_interrupts import (
    SystemInterruptKind,
    SystemInterruptResponseReceipt,
    SystemInterruptSource,
)
from CAD2BIM_backend.domain.identity import AuthenticatedSubject, TenantExecutionContext
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolError
from CAD2BIM_backend.infrastructure.authentication import (
    AuthenticationError,
    JwtRs256Authenticator,
)
from CAD2BIM_backend.infrastructure.state_access_tokens import StateAccessTokenCodec
from CAD2BIM_backend.ports.identity import IdentityAccessDenied

if TYPE_CHECKING:
    from CAD2BIM_backend.application.chat_projection import ChatProjectionService


class _PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTaskBody(_PublicModel):
    title: str = Field(min_length=1, max_length=500)
    instruction: str | None = Field(default=None, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1)


class StartRunBody(_PublicModel):
    module: str = Field(default="workflow", min_length=1)
    stage_key: str = Field(default="cad2bim_workflow", min_length=1)
    input_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)
    content: tuple[UserContentBlock, ...] = ()


class TaskView(_PublicModel):
    task_id: str
    title: str
    instruction: str | None
    status: TaskStatus
    active_module: str
    task_type: Literal["cad2bim", "component"]
    created_at: datetime
    updated_at: datetime


class RunView(_PublicModel):
    run_id: str
    task_id: str
    module: str
    stage_key: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    pause_requested: bool = False
    error: str | None = None
    initial_feedback: FeedbackReceiptView | None = None


class StateReadAccessView(_PublicModel):
    access_token: str
    read_binding: str


class SubmitFeedbackBody(_PublicModel):
    client_request_id: str = Field(min_length=1, max_length=200)
    delivery_mode: FeedbackDeliveryMode
    content: tuple[UserContentBlock, ...] = Field(min_length=1)
    stage_key: str | None = Field(default=None, min_length=1, max_length=255)
    role_execution_id: str | None = Field(default=None, min_length=1, max_length=255)
    target_task_id: str | None = Field(default=None, min_length=1, max_length=255)
    check_id: str | None = Field(default=None, min_length=1, max_length=255)
    expected_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)


class FeedbackReceiptView(_PublicModel):
    feedback_id: str
    accepted_sequence: int
    delivery_mode: FeedbackDeliveryMode
    resolved_scope: FeedbackScope
    status: str


class TaskListEntryView(TaskView):
    latest_run: RunView | None = None


class TaskListView(_PublicModel):
    tasks: tuple[TaskListEntryView, ...]
    next_after_task_id: str | None = None


class RunControlBody(_PublicModel):
    client_request_id: str = Field(min_length=1, max_length=200)


class RespondGateBody(_PublicModel):
    action: str = Field(pattern="^(approve|revise|cancel)$")
    content: tuple[UserContentBlock, ...] = ()
    idempotency_key: str = Field(min_length=1, max_length=200)


class GateResponseView(_PublicModel):
    interrupt_id: str
    status: str
    action: str
    responded_at: datetime


class CurrentGateView(_PublicModel):
    interrupt_id: str
    kind: SystemInterruptKind
    reason_code: str
    source: SystemInterruptSource
    question: str
    context_summary: str
    allowed_actions: tuple[str, ...]


class AttachmentView(_PublicModel):
    artifact_id: str
    version_id: str
    filename: str
    content_type: str
    size_bytes: int
    content_url: str


def _task_view(task: TaskRecord) -> TaskView:
    return TaskView(
        task_id=task.task_id,
        title=task.title,
        instruction=task.instruction,
        status=task.status,
        active_module=task.active_module,
        task_type="component" if task.metadata.get("task_type") == "component" else "cad2bim",
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _run_view(run: RunRecord) -> RunView:
    return RunView(
        run_id=run.run_id,
        task_id=run.task_id,
        module=run.module,
        stage_key=run.stage_key,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        pause_requested=run.pause_requested,
        error=run.error if run.status is RunStatus.PAUSED else None,
        initial_feedback=(
            FeedbackReceiptView.model_validate(run.initial_feedback.model_dump())
            if run.initial_feedback is not None else None
        ),
    )


def create_task_api(
    *,
    authenticator: JwtRs256Authenticator,
    service: TaskRunService,
    state_access_codec: StateAccessTokenCodec | None = None,
    feedback_service: FeedbackService | None = None,
    artifact_service: ArtifactService | None = None,
    bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]] | None = None,
    chat_projection: ChatProjectionService | None = None,
) -> FastAPI:
    """Build the API with production verification and service dependencies supplied by bootstrap."""

    app = FastAPI()
    bearer = HTTPBearer(auto_error=False)

    def authenticated_subject(
        credential: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> AuthenticatedSubject:
        if credential is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        try:
            return authenticator.authenticate(credential.credentials)
        except AuthenticationError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
            ) from None

    @app.post("/tasks", response_model=TaskView, status_code=status.HTTP_201_CREATED)
    async def create_task(
        body: CreateTaskBody,
        subject: AuthenticatedSubject = Depends(authenticated_subject),
    ) -> TaskView:
        try:
            task = await service.create_task(
                subject,
                CreateTaskRequest(
                    title=body.title,
                    instruction=body.instruction,
                    metadata=body.metadata,
                    idempotency_key=body.idempotency_key,
                ),
            )
        except IdentityAccessDenied:
            raise HTTPException(status_code=403, detail="access denied") from None
        except ConflictError:
            raise HTTPException(status_code=409, detail="task conflict") from None
        return _task_view(task)

    @app.get("/tasks", response_model=TaskListView)
    async def list_tasks(
        after_task_id: str | None = Query(default=None, min_length=1, max_length=255),
        limit: int = Query(default=50, ge=1, le=100),
        subject: AuthenticatedSubject = Depends(authenticated_subject),
    ) -> TaskListView:
        try:
            rows = await service.list_tasks(subject, after_task_id=after_task_id, limit=limit + 1)
        except IdentityAccessDenied:
            raise HTTPException(status_code=403, detail="access denied") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid task page") from None
        selected = rows[:limit]
        return TaskListView(
            tasks=tuple(
                TaskListEntryView(
                    **_task_view(task).model_dump(),
                    latest_run=_run_view(run) if run is not None else None,
                ) for task, run in selected
            ),
            next_after_task_id=selected[-1][0].task_id if len(rows) > limit else None,
        )

    @app.get("/tasks/{task_id}", response_model=TaskView)
    async def get_task(
        task_id: str,
        subject: AuthenticatedSubject = Depends(authenticated_subject),
    ) -> TaskView:
        try:
            return _task_view(await service.get_task(subject, task_id))
        except IdentityAccessDenied:
            raise HTTPException(status_code=403, detail="access denied") from None
        except ResourceNotFoundError:
            raise HTTPException(status_code=404, detail="task not found") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid task ID") from None

    @app.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_task(
        task_id: str,
        subject: AuthenticatedSubject = Depends(authenticated_subject),
    ) -> Response:
        try:
            await service.delete_task(subject, task_id)
        except IdentityAccessDenied:
            raise HTTPException(status_code=403, detail="access denied") from None
        except ResourceNotFoundError:
            raise HTTPException(status_code=404, detail="task not found") from None
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid task ID") from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/tasks/{task_id}/runs", response_model=RunView, status_code=status.HTTP_201_CREATED)
    async def start_run(
        task_id: str,
        body: StartRunBody,
        subject: AuthenticatedSubject = Depends(authenticated_subject),
    ) -> RunView:
        try:
            run = await service.start_run(
                subject,
                task_id,
                StartRunRequest(
                    module=body.module,
                    stage_key=body.stage_key,
                    input_payload=body.input_payload,
                    idempotency_key=body.idempotency_key,
                    content=body.content,
                ),
            )
        except IdentityAccessDenied:
            raise HTTPException(status_code=403, detail="access denied") from None
        except ResourceNotFoundError:
            raise HTTPException(status_code=404, detail="task not found") from None
        except ConflictError:
            raise HTTPException(status_code=409, detail="run conflict") from None
        except (ExecutionProtocolError, ValueError):
            raise HTTPException(status_code=422, detail="invalid run request") from None
        return _run_view(run)

    @app.get("/tasks/{task_id}/runs/{run_id}", response_model=RunView)
    async def get_run(
        task_id: str,
        run_id: str,
        subject: AuthenticatedSubject = Depends(authenticated_subject),
    ) -> RunView:
        try:
            return _run_view(await service.get_run(subject, task_id, run_id))
        except IdentityAccessDenied:
            raise HTTPException(status_code=403, detail="access denied") from None
        except ResourceNotFoundError:
            raise HTTPException(status_code=404, detail="run not found") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid task ID") from None

    async def control_run(
        task_id: str, run_id: str, body: RunControlBody,
        subject: AuthenticatedSubject, *, resume: bool,
    ) -> RunView:
        try:
            command = service.continue_run if resume else service.pause_run
            run = await command(subject, task_id, run_id, body.client_request_id)
        except IdentityAccessDenied:
            raise HTTPException(status_code=403, detail="access denied") from None
        except ResourceNotFoundError:
            raise HTTPException(status_code=404, detail="run not found") from None
        except ConflictError:
            raise HTTPException(status_code=409, detail="run control conflict") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid run control") from None
        return _run_view(run)

    @app.post("/tasks/{task_id}/runs/{run_id}/pause", response_model=RunView)
    async def pause_run(
        task_id: str, run_id: str, body: RunControlBody,
        subject: AuthenticatedSubject = Depends(authenticated_subject),
    ) -> RunView:
        return await control_run(task_id, run_id, body, subject, resume=False)

    @app.post("/tasks/{task_id}/runs/{run_id}/resume", response_model=RunView)
    async def resume_run(
        task_id: str, run_id: str, body: RunControlBody,
        subject: AuthenticatedSubject = Depends(authenticated_subject),
    ) -> RunView:
        return await control_run(task_id, run_id, body, subject, resume=True)

    if state_access_codec is not None:

        @app.post(
            "/tasks/{task_id}/runs/{run_id}/state-read-access",
            response_model=StateReadAccessView,
        )
        async def completed_state_read_access(
            task_id: str,
            run_id: str,
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> StateReadAccessView:
            try:
                binding = await service.completed_state_read_binding(subject, task_id, run_id)
                pair = state_access_codec.issue_completed(subject, binding)
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="run not found") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid State read request") from None
            except RuntimeError:
                raise HTTPException(status_code=409, detail="completed State unavailable") from None
            return StateReadAccessView(
                access_token=pair.access_token, read_binding=pair.read_binding
            )

    if feedback_service is not None:

        @app.get(
            "/tasks/{task_id}/runs/{run_id}/gates/current",
            response_model=CurrentGateView,
        )
        async def current_gate(
            task_id: str,
            run_id: str,
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> CurrentGateView:
            try:
                gate = await feedback_service.current_gate(subject, task_id, run_id)
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="gate not found") from None
            except ConflictError:
                raise HTTPException(status_code=409, detail="gate conflict") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid Gate read") from None
            if gate is None:
                raise HTTPException(status_code=404, detail="gate not found")
            return CurrentGateView(
                interrupt_id=gate.interrupt_id,
                kind=gate.kind,
                reason_code=gate.reason_code,
                source=gate.source,
                question=gate.question,
                context_summary=gate.context_summary,
                allowed_actions=gate.allowed_actions,
            )

        @app.post(
            "/tasks/{task_id}/runs/{run_id}/feedback",
            response_model=FeedbackReceiptView,
            status_code=status.HTTP_201_CREATED,
        )
        async def submit_feedback(
            task_id: str,
            run_id: str,
            body: SubmitFeedbackBody,
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> FeedbackReceiptView:
            try:
                receipt: FeedbackCommandReceipt = await feedback_service.submit_feedback(
                    subject,
                    task_id,
                    run_id,
                    SubmitFeedbackCommand(
                        client_request_id=body.client_request_id,
                        delivery_mode=body.delivery_mode,
                        content=body.content,
                        scope=FeedbackScope(
                            run_id=run_id,
                            stage_key=body.stage_key,
                            role_execution_id=body.role_execution_id,
                            task_id=body.target_task_id,
                            check_id=body.check_id,
                            expected_checkpoint_id=body.expected_checkpoint_id,
                        ),
                    ),
                )
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="run not found") from None
            except (ConflictError, FeedbackTargetConflictError):
                raise HTTPException(status_code=409, detail="feedback conflict") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid feedback") from None
            return FeedbackReceiptView.model_validate(receipt.model_dump())

        @app.post(
            "/tasks/{task_id}/runs/{run_id}/gates/{interrupt_id}/respond",
            response_model=GateResponseView,
        )
        async def respond_gate(
            task_id: str,
            run_id: str,
            interrupt_id: str,
            body: RespondGateBody,
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> GateResponseView:
            try:
                receipt: SystemInterruptResponseReceipt = await feedback_service.respond_gate(
                    subject,
                    task_id,
                    run_id,
                    RespondGateCommand(
                        interrupt_id=interrupt_id,
                        action=body.action,
                        content=body.content,
                        idempotency_key=body.idempotency_key,
                    ),
                )
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="gate not found") from None
            except ConflictError:
                raise HTTPException(status_code=409, detail="gate conflict") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid Gate response") from None
            return GateResponseView(
                interrupt_id=receipt.interrupt_id,
                status=receipt.status,
                action=receipt.action,
                responded_at=receipt.responded_at,
            )

    if artifact_service is not None and bind_identity is not None:

        @app.post(
            "/tasks/{task_id}/attachments",
            response_model=AttachmentView,
            status_code=status.HTTP_201_CREATED,
        )
        async def upload_attachment(
            task_id: str,
            request: Request,
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> AttachmentView:
            try:
                context, _ = await service.authorized_task_context(subject, task_id)
                async with request.form() as form:
                    uploaded = form.get("file")
                    request_id = form.get("client_request_id")
                    if not isinstance(uploaded, UploadFile) or not isinstance(request_id, str):
                        raise ValueError("file and client_request_id are required")
                    if not request_id.strip() or len(request_id) > 200:
                        raise ValueError("invalid upload request identity")

                    async def chunks():
                        while content := await uploaded.read(64 * 1024):
                            yield content

                    with bind_identity(context):
                        content_type = uploaded.content_type or "application/octet-stream"
                        if content_type == "application/octet-stream":
                            guessed_type, _ = mimetypes.guess_type(uploaded.filename or "")
                            if guessed_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                                content_type = guessed_type
                        artifact = await artifact_service.upload(
                            owner_id=context.tenant_id,
                            task_id=task_id,
                            filename=uploaded.filename or "attachment",
                            content_type=content_type,
                            chunks=chunks(),
                            idempotency_key=request_id,
                        )
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="task not found") from None
            except ConflictError:
                raise HTTPException(status_code=409, detail="attachment conflict") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid attachment") from None
            return AttachmentView(
                artifact_id=artifact.artifact_id,
                version_id=artifact.version_id,
                filename=artifact.filename,
                content_type=artifact.content_type,
                size_bytes=artifact.size_bytes,
                content_url=(f"/tasks/{task_id}/artifacts/{artifact.artifact_id}"
                             f"/versions/{artifact.version_id}/content"),
            )

        @app.get("/tasks/{task_id}/artifacts/{artifact_id}/versions/{version_id}/content")
        async def attachment_content(
            task_id: str,
            artifact_id: str,
            version_id: str,
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> Response:
            try:
                context, _ = await service.authorized_task_context(subject, task_id)
                with bind_identity(context):
                    download = await artifact_service.download_version(
                        context.tenant_id, artifact_id, version_id,
                    )
                if download.artifact.task_id != task_id:
                    raise ResourceNotFoundError("artifact not found in task")
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="artifact version not found") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid artifact reference") from None
            artifact = download.artifact
            disposition = "inline" if artifact.content_type in {"image/png", "image/jpeg"} else "attachment"
            return Response(
                download.content,
                media_type=artifact.content_type,
                headers={
                    "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(artifact.filename, safe='')}",
                    "Cache-Control": "private, no-cache",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    if chat_projection is not None:

        @app.get("/tasks/{task_id}/runs/{run_id}/snapshot")
        async def chat_snapshot(
            task_id: str,
            run_id: str,
            before_sequence: int | None = Query(default=None, ge=1),
            limit: int = Query(default=50, ge=1, le=100),
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> dict[str, Any]:
            try:
                return await chat_projection.snapshot(
                    subject, task_id, run_id, before_sequence=before_sequence, limit=limit,
                )
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="run not found") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid snapshot request") from None

        @app.get("/tasks/{task_id}/runs/{run_id}/items/{item_id}")
        async def chat_item(
            task_id: str,
            run_id: str,
            item_id: str,
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> dict[str, Any]:
            try:
                return await chat_projection.item(subject, task_id, run_id, item_id)
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="chat item not found") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid item request") from None

        @app.get("/tasks/{task_id}/runs/{run_id}/items/{item_id}/content/{index}")
        async def chat_item_content(
            task_id: str,
            run_id: str,
            item_id: str,
            index: int,
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> Response:
            try:
                content = await chat_projection.content(subject, task_id, run_id, item_id, index)
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="chat content not found") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid content request") from None
            return Response(
                content.content, media_type=content.media_type,
                headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"},
            )

        @app.get("/tasks/{task_id}/runs/{run_id}/events")
        async def chat_events(
            task_id: str,
            run_id: str,
            after_sequence: int = Query(default=0, ge=0),
            last_event_id: str | None = Header(default=None),
            subject: AuthenticatedSubject = Depends(authenticated_subject),
        ) -> StreamingResponse:
            try:
                if last_event_id is not None:
                    resumed_sequence = int(last_event_id)
                    if resumed_sequence < 0:
                        raise ValueError("invalid event cursor")
                    after_sequence = max(after_sequence, resumed_sequence)
                await service.authorized_run_context(subject, task_id, run_id)
            except IdentityAccessDenied:
                raise HTTPException(status_code=403, detail="access denied") from None
            except ResourceNotFoundError:
                raise HTTPException(status_code=404, detail="run not found") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid event cursor") from None
            return StreamingResponse(
                chat_projection.stream(subject, task_id, run_id, after_sequence=after_sequence),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache, private", "X-Accel-Buffering": "no"},
            )

    return app


__all__ = ["create_task_api"]
