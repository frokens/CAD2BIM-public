from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi import FastAPI

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import ExecutionMode, ExecutionProtocolStatus, RunStatus
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    TenantActorKind,
    TenantExecutionContext,
)
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.domain.state_read_binding import ExactStateReadBinding, root_field_sources
from CAD2BIM_backend.application.state_resource_service import StateResourceContent, StateResourceEntry
from CAD2BIM_backend.infrastructure.state_access_tokens import (
    StateAccessDenied,
    StateAccessTokenCodec,
)
from CAD2BIM_backend.internal_state_api import create_internal_state_router


_SECRET = b"state-token-test-secret-material-32-bytes-minimum"


def _binding(*, run_id: str = "run-one") -> ExactStateReadBinding:
    checkpoint_map = (("", "root-one"),)
    return ExactStateReadBinding(
        owner_id="tenant-one",
        task_id="task-one",
        run_id=run_id,
        thread_id="thread-one",
        checkpoint_cursor=CheckpointCursor(
            thread_id="thread-one",
            checkpoint_id="root-one",
            checkpoint_ns="",
            root_checkpoint_id="root-one",
            checkpoint_map=checkpoint_map,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id="root-one",
                checkpoint_ns="",
                checkpoint_map=checkpoint_map,
            ),
        ),
        field_sources=root_field_sources("root-one"),
    )


def _subject() -> AuthenticatedSubject:
    now = datetime.now(timezone.utc)
    return AuthenticatedSubject(
        issuer="https://identity.example.test",
        subject="alice",
        audience="cad2bim-api",
        authenticated_at=now,
        token_expires_at=now + timedelta(minutes=2),
    )


def test_completed_state_pair_is_exact_and_rejects_cross_pair_or_tampering() -> None:
    codec = StateAccessTokenCodec(_SECRET)
    first = codec.issue_completed(_subject(), _binding())
    second = codec.issue_completed(_subject(), _binding(run_id="run-two"))

    verified = codec.verify_pair(first.access_token, first.read_binding)
    assert verified.mode == "completed"
    assert verified.binding.run_id == "run-one"
    assert verified.subject is not None and verified.subject.subject == "alice"
    with pytest.raises(StateAccessDenied, match="binding"):
        codec.verify_pair(first.access_token, second.read_binding)
    with pytest.raises(StateAccessDenied, match="invalid"):
        codec.verify_pair(first.access_token, first.read_binding + "x")
    with pytest.raises(StateAccessDenied, match="invalid"):
        codec.verify_pair(first.read_binding, first.access_token)


def test_state_access_rejects_expired_credential_and_wrong_mode() -> None:
    codec = StateAccessTokenCodec(_SECRET)
    pair = codec.issue_completed(_subject(), _binding())
    claims = jwt.decode(
        pair.access_token,
        _SECRET,
        algorithms=["HS256"],
        audience="cad2bim-runner-state",
        issuer="urn:cad2bim:runner-state",
    )
    expired = jwt.encode(
        {**claims, "exp": int(datetime.now(timezone.utc).timestamp()) - 1},
        _SECRET,
        algorithm="HS256",
    )
    with pytest.raises(StateAccessDenied, match="invalid"):
        codec.verify_pair(expired, pair.read_binding)

    # Even a signed credential cannot reinterpret a completed user read as a Run claim.
    wrong_mode = jwt.encode({**claims, "mode": "running"}, _SECRET, algorithm="HS256")
    with pytest.raises(StateAccessDenied, match="scope"):
        codec.verify_pair(wrong_mode, pair.read_binding)


def test_state_access_uses_bounded_lifetime_and_subject_expiry() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        StateAccessTokenCodec(b"short")
    with pytest.raises(ValueError, match="15 minutes"):
        StateAccessTokenCodec(_SECRET, lifetime=timedelta(hours=1))
    codec = StateAccessTokenCodec(_SECRET)
    now = datetime.now(timezone.utc)
    expired_subject = AuthenticatedSubject(
        issuer="https://identity.example.test",
        subject="alice",
        audience="cad2bim-api",
        authenticated_at=now - timedelta(hours=1),
        token_expires_at=now - timedelta(seconds=1),
    )
    with pytest.raises(StateAccessDenied, match="expired"):
        codec.issue_completed(expired_subject, _binding())


@pytest.mark.asyncio
async def test_backend_resource_request_rechecks_current_run_claim() -> None:
    codec = StateAccessTokenCodec(_SECRET)
    binding = _binding()
    identity = TenantExecutionContext(
        tenant_id="tenant-one",
        principal_id="runner-principal",
        task_id="task-one",
        actor_kind=TenantActorKind.RUNNER,
        command_id="read-command",
    )
    claim = SimpleNamespace(
        scope=SimpleNamespace(identity=identity, owner_id="tenant-one", run_id="run-one"),
        thread_id="thread-one",
        runner_id="runner-one",
        writer_generation=1,
    )
    pair = codec.issue_running(claim, binding)

    # The public read projection supplies neither write-grant helpers nor a principal.
    run = SimpleNamespace(
        run_id="run-one",
        owner_id="tenant-one",
        tenant_id="tenant-one",
        principal_id=None,
        task_id="task-one",
        thread_id="thread-one",
        status=RunStatus.RUNNING,
        execution_mode=ExecutionMode.GRAPH,
        execution_protocol_status=ExecutionProtocolStatus.PINNED,
        claimed_by="runner-one",
        lease_token="private-lease",
        writer_generation=1,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        execution_protocol=ExecutionProtocolBinding(
            bundle_id="workflow", module="workflow", stage_key="cad2bim_workflow",
        ),
    )

    class _Repository:
        async def get_run(self, _owner_id, _run_id):
            return run

    entry = StateResourceEntry(
        uri="state://requirement-md", name="requirement-md",
        description="saved", mime_type="text/markdown",
    )

    class _Resources:
        async def list_resources(self, _binding):
            return (entry,)

        async def read_resource(self, _binding, _uri):
            return StateResourceContent(entry=entry, text="saved markdown")

    @contextmanager
    def bind_identity(_identity):
        yield

    app = FastAPI()
    app.include_router(create_internal_state_router(
        codec=codec,
        resources=_Resources(),
        task_runs=object(),
        runner_repository=_Repository(),
        bind_identity=bind_identity,
    ))
    headers = {
        "Authorization": f"Bearer {pair.access_token}",
        "X-CAD2BIM-State-Binding": pair.read_binding,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post(
            "/internal/state/resources/read", headers=headers,
            json={"uri": "state://requirement-md"},
        )
        assert result.status_code == 200
        run.writer_generation = 2
        result = await client.post(
            "/internal/state/resources/read", headers=headers,
            json={"uri": "state://requirement-md"},
        )
        assert result.status_code == 403
        run.writer_generation = 1
        run.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        for field, invalid in (
            ("execution_mode", ExecutionMode.DIRECT_MCP),
            ("execution_protocol_status", ExecutionProtocolStatus.QUARANTINED),
            ("lease_token", None),
            ("owner_id", "another-tenant"),
            ("tenant_id", "another-tenant"),
            ("task_id", "another-task"),
            ("thread_id", "another-thread"),
            ("status", RunStatus.PAUSED),
            ("claimed_by", "another-runner"),
        ):
            original = getattr(run, field)
            setattr(run, field, invalid)
            result = await client.post("/internal/state/resources/list", headers=headers)
            assert result.status_code == 403, field
            setattr(run, field, original)
        assert (await client.post("/internal/state/resources/list", headers=headers)).status_code == 200
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        result = await client.post("/internal/state/resources/list", headers=headers)
        assert result.status_code == 403
