"""A feedback write must keep the exact nonprivileged Runner claim."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from CAD2BIM_backend.application.feedback_service import FeedbackService
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.domain.control_plane import (
    CreateTaskRequest,
    FeedbackDeliveryMode,
    FeedbackScope,
    LeaseConflictError,
    ResourceNotFoundError,
    RespondGateCommand,
    StartRunRequest,
    SubmitFeedbackCommand,
)
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.infrastructure.postgres.database import create_database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import PostgresIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context


pytestmark = [pytest.mark.integration, pytest.mark.network]


def _binding() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="feedback_claim", module="workflow", stage_key="cad2bim_workflow",
    )


def _isolated_urls() -> tuple[str, str]:
    url = make_url(os.environ["CAD2BIM_FOUNDATION_DATABASE_URL"])
    if not (url.database or "").startswith("cad2bim_foundation_"):
        raise RuntimeError("feedback claim proof requires an isolated foundation database")
    if url.host not in {None, "127.0.0.1", "localhost"}:
        raise RuntimeError("feedback claim proof requires local PostgreSQL")
    return (
        url.set(username="cad2bim_api_login").render_as_string(hide_password=False),
        url.set(username="cad2bim_runner_login").render_as_string(hide_password=False),
    )


@pytest.mark.asyncio
async def test_stale_claim_cannot_activate_or_seal_feedback_after_takeover() -> None:
    api_url, runner_url = _isolated_urls()
    api_database = create_database(Settings(database_url=api_url, environment="test"))
    runner_database = create_database(Settings(database_url=runner_url, environment="test"))
    api_repository = PostgresControlPlaneRepository(api_database.session_factory)
    runner_repository = PostgresControlPlaneRepository(runner_database.session_factory)
    identities = PostgresIdentityDirectory(api_database.session_factory)
    service = FeedbackService(
        repository=runner_repository,
        identity_directory=identities,
        bind_identity=bind_tenant_execution_context,
    )
    try:
        async with runner_database.engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == "cad2bim_runner_login"
            privileges = (await connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )).one()
            assert tuple(privileges) == (False, False)
        now = datetime.now(timezone.utc)
        subject = AuthenticatedSubject(
            issuer="https://foundation-feedback-test.example",
            subject=f"person-{uuid4().hex}",
            audience="cad2bim-foundation",
            authenticated_at=now,
            token_expires_at=now + timedelta(hours=1),
        )
        identity = await identities.resolve_authenticated_subject(
            subject,
            command_id=f"foundation_{uuid4().hex}",
        )
        with bind_tenant_execution_context(identity):
            task = await api_repository.create_task(
                identity.tenant_id, CreateTaskRequest(title="Feedback claim takeover")
            )
            other_task = await api_repository.create_task(
                identity.tenant_id, CreateTaskRequest(title="Wrong Task")
            )
            run = await api_repository.create_run(
                identity.tenant_id,
                task.task_id,
                StartRunRequest(idempotency_key=f"run-{uuid4().hex}"),
                execution_protocol_factory=_binding,
            )
        with pytest.raises(ResourceNotFoundError, match="run not found"):
            await service.submit_feedback(
                subject,
                other_task.task_id,
                run.run_id,
                SubmitFeedbackCommand(
                    client_request_id=f"feedback-{uuid4().hex}",
                    delivery_mode=FeedbackDeliveryMode.STEER,
                    body="Wrong task must not address this Run.",
                    scope=FeedbackScope(run_id=run.run_id),
                ),
            )
        with pytest.raises(ResourceNotFoundError, match="run not found"):
            await service.respond_gate(
                subject,
                other_task.task_id,
                run.run_id,
                RespondGateCommand(
                    interrupt_id="gate-one",
                    action="approve",
                    idempotency_key=f"response-{uuid4().hex}",
                ),
            )

        first = await runner_repository.claim_next_run("foundation-runner-one", lease_seconds=1)
        assert first is not None and first.run_id == run.run_id
        first_grant = RunnerCheckpointGrant.from_claim(first, runner_id="foundation-runner-one")
        mapping = (("", "saved-input"),)
        source = CheckpointCursor(
            thread_id=run.thread_id, checkpoint_id="saved-input", checkpoint_ns="",
            root_checkpoint_id="saved-input", checkpoint_map=mapping,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id="saved-input", checkpoint_ns="", checkpoint_map=mapping,
            ),
        )
        first_subject = await service.activate_feedback_subject(
            first_grant,
            business_entry_id="entry-one",
            stage_key=first.stage_key,
            agent="requirement",
            attempt=1,
            checkpoint_id=None,
        )

        await asyncio.sleep(1.2)
        with pytest.raises(LeaseConflictError):
            await service.activate_feedback_subject(
                first_grant,
                business_entry_id="expired-entry",
                stage_key=first.stage_key,
                agent="requirement",
                attempt=2,
                checkpoint_id=None,
            )
        with pytest.raises(LeaseConflictError):
            await service.decide_role_exit(
                first_grant, first_subject.subject_id, 0,
                input_checkpoint_cursor=source, delivered_batch_id=None,
            )
        with bind_tenant_execution_context(identity):
            expired_run = await api_repository.get_run(identity.tenant_id, run.run_id)
        assert expired_run is not None
        assert expired_run.writer_generation == first.writer_generation
        assert expired_run.active_feedback_subject == first_subject
        assert expired_run.feedback_sealed is False
        assert expired_run.feedback_transition_subject_id is None

        second = await runner_repository.claim_next_run("foundation-runner-two")
        assert second is not None and second.run_id == run.run_id
        assert second.writer_generation == first.writer_generation + 1
        second_grant = RunnerCheckpointGrant.from_claim(second, runner_id="foundation-runner-two")
        with pytest.raises(LeaseConflictError):
            await service.activate_feedback_subject(
                first_grant,
                business_entry_id="stale-entry",
                stage_key=first.stage_key,
                agent="requirement",
                attempt=2,
                checkpoint_id=None,
            )
        with pytest.raises(LeaseConflictError):
            await service.decide_role_exit(
                first_grant, first_subject.subject_id, 0,
                input_checkpoint_cursor=source, delivered_batch_id=None,
            )
        with bind_tenant_execution_context(identity):
            taken_over_run = await api_repository.get_run(identity.tenant_id, run.run_id)
        assert taken_over_run is not None
        assert taken_over_run.active_feedback_subject == first_subject
        assert taken_over_run.feedback_sealed is False
        assert taken_over_run.feedback_transition_subject_id is None

        current_subject = await service.activate_feedback_subject(
            second_grant,
            business_entry_id="entry-two",
            stage_key=second.stage_key,
            agent="requirement",
            attempt=2,
            checkpoint_id=None,
        )
        assert current_subject.revision == first_subject.revision + 1
        assert current_subject.writer_generation == second.writer_generation
        assert await service.decide_role_exit(
            second_grant, current_subject.subject_id, 0,
            input_checkpoint_cursor=source, delivered_batch_id=None,
        )
        assert second.lease_token is not None
        with bind_tenant_execution_context(second_grant.scope.identity):
            await runner_repository.complete(
                run.run_id, "foundation-runner-two", second.lease_token
            )
    finally:
        await runner_database.dispose()
        await api_database.dispose()
