"""Real PostgreSQL evidence for Runner-owned checkpoint transactions."""

from __future__ import annotations

import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TypedDict
from uuid import uuid4

import pytest
import psycopg
import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.engine import make_url

from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.api import create_task_api
from CAD2BIM_backend.application.runner_execution_service import RunnerExecutionService
from CAD2BIM_backend.application.feedback_service import FeedbackService
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.bootstrap import open_backend_runtime
from CAD2BIM_backend.domain.control_plane import (
    CreateTaskRequest,
    LeaseConflictError,
    StartRunRequest,
)
from CAD2BIM_backend.domain.execution_protocol import (
    ExecutionProtocolBinding,
)
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant, RunnerGraphContext
from CAD2BIM_backend.infrastructure.langgraph.fenced_saver import FencedAsyncPostgresSaver
from CAD2BIM_backend.infrastructure.langgraph.runtime import (
    AsyncPostgresGraphRuntime,
    _psycopg_connection_string,
)
from CAD2BIM_backend.infrastructure.langgraph.user_gate import (
    build_run_pause_open_request,
    build_user_gate_open_request,
    reentry_route_update,
)
from CAD2BIM_backend.infrastructure.authentication import JwtRs256Authenticator
from CAD2BIM_backend.infrastructure.postgres.database import create_database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context


pytestmark = [pytest.mark.integration, pytest.mark.network]


def _binding() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="runner_checkpoint_fencing",
        module="workflow",
        stage_key="cad2bim_workflow",
    )


async def _claimed_run(database_url: str, *, lease_seconds: int = 30):
    database = create_database(Settings(database_url=database_url, environment="test"))
    repository = PostgresControlPlaneRepository(database.session_factory)
    directory = PostgresIdentityDirectory(database.session_factory)
    now = datetime.now(timezone.utc)
    identity = await directory.resolve_authenticated_subject(
        AuthenticatedSubject(
            issuer="https://stage1-test.example",
            subject=f"person-{uuid4().hex}",
            audience="cad2bim-stage1",
            authenticated_at=now,
            token_expires_at=now + timedelta(hours=1),
        ),
        command_id=f"cmd_{uuid4().hex}",
    )
    with bind_tenant_execution_context(identity):
        task = await repository.create_task(
            identity.tenant_id,
            CreateTaskRequest(title="Runner checkpoint fencing"),
        )
        run = await repository.create_run(
            identity.tenant_id,
            task.task_id,
            StartRunRequest(idempotency_key=f"start-{uuid4().hex}"),
            execution_protocol_factory=_binding,
        )
    claimed = await repository.claim_next_run("runner-stage1", lease_seconds=lease_seconds)
    assert claimed is not None and claimed.run_id == run.run_id
    return database, repository, claimed


async def _close_probe_run(database, run_id: str) -> None:
    """Keep a failed probe from becoming the next test's claim candidate."""

    async with database.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE runs SET status = 'cancelled', claimed_by = NULL, "
                "lease_token = NULL, lease_expires_at = NULL, ended_at = clock_timestamp() "
                "WHERE run_id = :run_id AND status IN ('running', 'queued')"
            ),
            {"run_id": run_id},
        )


@pytest.mark.asyncio
async def test_fenced_saver_commits_and_rejects_stale_checkpoint_and_writes() -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    database, repository, claimed = await _claimed_run(database_url)
    runner_url = make_url(database_url).set(username="cad2bim_runner_login")
    grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="runner-stage1")
    try:
        async with FencedAsyncPostgresSaver.from_conn_string(
            _psycopg_connection_string(runner_url.render_as_string(hide_password=False))
        ) as saver:
            config = {"configurable": {"thread_id": claimed.thread_id, "checkpoint_ns": ""}}
            checkpoint = empty_checkpoint()
            with saver.bind_execution(grant):
                saved = await saver.aput(
                    config,
                    checkpoint,
                    {"source": "input", "step": -1, "writes": {}},
                    {},
                )
                await saver.aput_writes(saved, [("probe", {"value": 1})], "probe-task")
            async with database.session_factory() as session:
                stored = await session.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                    {"thread_id": claimed.thread_id},
                )
                pending = await session.scalar(
                    text("SELECT count(*) FROM checkpoint_writes WHERE thread_id = :thread_id"),
                    {"thread_id": claimed.thread_id},
                )
            assert stored == 1 and pending == 1

            async with database.session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE runs SET lease_expires_at = clock_timestamp() - interval '1 second' "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": claimed.run_id},
                )
            reclaimed = await repository.claim_next_run("runner-stage1-new", lease_seconds=30)
            assert reclaimed is not None and reclaimed.run_id == claimed.run_id
            with saver.bind_execution(grant):
                with pytest.raises(LeaseConflictError):
                    await saver.aput(
                        config,
                        empty_checkpoint(),
                        {"source": "input", "step": -1, "writes": {}},
                        {},
                    )
                with pytest.raises(LeaseConflictError):
                    await saver.aput_writes(saved, [("probe", {"value": 2})], "late-task")
            with saver.bind_execution(
                RunnerCheckpointGrant.from_claim(reclaimed, runner_id="runner-stage1-new")
            ):
                await saver.aput_writes(saved, [("probe", {"value": 3})], "new-task")
            async with database.session_factory() as session:
                pending_after = await session.scalar(
                    text("SELECT count(*) FROM checkpoint_writes WHERE thread_id = :thread_id"),
                    {"thread_id": claimed.thread_id},
                )
            assert pending_after == 2
            await repository.complete(
                reclaimed.run_id,
                "runner-stage1-new",
                reclaimed.lease_token,
            )
    finally:
        await _close_probe_run(database, claimed.run_id)
        await database.dispose()


@pytest.mark.asyncio
async def test_fenced_saver_rolls_back_blobs_when_checkpoint_insert_fails(monkeypatch) -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    database, repository, claimed = await _claimed_run(database_url)
    runner_url = make_url(database_url).set(username="cad2bim_runner_login")
    grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="runner-stage1")
    try:
        async with FencedAsyncPostgresSaver.from_conn_string(
            _psycopg_connection_string(runner_url.render_as_string(hide_password=False))
        ) as saver:
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"payload": {"value": 1}}
            checkpoint["channel_versions"] = {"payload": "1"}
            config = {"configurable": {"thread_id": claimed.thread_id, "checkpoint_ns": ""}}
            with monkeypatch.context() as patch:
                patch.setattr(
                    saver,
                    "UPSERT_CHECKPOINTS_SQL",
                    "INSERT INTO absent_checkpoint_table VALUES (%s, %s, %s, %s, %s, %s)",
                )
                with saver.bind_execution(grant), pytest.raises(psycopg.errors.UndefinedTable):
                    await saver.aput(
                        config,
                        checkpoint,
                        {"source": "input", "step": -1, "writes": {}},
                        {"payload": "1"},
                    )
            async with database.session_factory() as session:
                blobs = await session.scalar(
                    text("SELECT count(*) FROM checkpoint_blobs WHERE thread_id = :thread_id"),
                    {"thread_id": claimed.thread_id},
                )
                saved = await session.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                    {"thread_id": claimed.thread_id},
                )
            assert blobs == 0 and saved == 0
        await repository.complete(claimed.run_id, "runner-stage1", claimed.lease_token)
    finally:
        await _close_probe_run(database, claimed.run_id)
        await database.dispose()


@pytest.mark.asyncio
async def test_fenced_saver_checks_clock_after_waiting_for_run_row_lock() -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    database, _, claimed = await _claimed_run(database_url, lease_seconds=2)
    runner_url = make_url(database_url).set(username="cad2bim_runner_login")
    grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="runner-stage1")
    config = {"configurable": {"thread_id": claimed.thread_id, "checkpoint_ns": ""}}
    admin_url = _psycopg_connection_string(database_url)
    try:
        async with FencedAsyncPostgresSaver.from_conn_string(
            _psycopg_connection_string(runner_url.render_as_string(hide_password=False))
        ) as saver:
            async with await psycopg.AsyncConnection.connect(admin_url) as admin:
                async with admin.transaction():
                    await admin.execute(
                        "SELECT run_id FROM runs WHERE run_id = %s FOR UPDATE",
                        (claimed.run_id,),
                    )
                    with saver.bind_execution(grant):
                        write = asyncio.create_task(
                            saver.aput(
                                config,
                                empty_checkpoint(),
                                {"source": "input", "step": -1, "writes": {}},
                                {},
                            )
                        )
                        await asyncio.sleep(0.2)
                        assert not write.done()
                        await asyncio.sleep(2.1)
                with pytest.raises(LeaseConflictError):
                    await write
            async with database.session_factory() as session:
                saved = await session.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                    {"thread_id": claimed.thread_id},
                )
            assert saved == 0
    finally:
        await _close_probe_run(database, claimed.run_id)
        await database.dispose()


class _NestedState(TypedDict):
    value: int


@pytest.mark.asyncio
async def test_nested_graph_saves_with_context_without_persisting_claim_secret() -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    database, repository, claimed = await _claimed_run(database_url)
    runner_url = make_url(database_url).set(username="cad2bim_runner_login")
    grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="runner-stage1")

    async def increment(state: _NestedState, runtime: Runtime[RunnerGraphContext]) -> dict:
        assert runtime.context.scope.run_id == claimed.run_id
        return {"value": state["value"] + 1}

    try:
        async with FencedAsyncPostgresSaver.from_conn_string(
            _psycopg_connection_string(runner_url.render_as_string(hide_password=False))
        ) as saver:
            child = StateGraph(_NestedState, context_schema=RunnerGraphContext)
            child.add_node("increment", increment)
            child.add_edge(START, "increment")
            parent = StateGraph(_NestedState, context_schema=RunnerGraphContext)
            parent.add_node("nested", child.compile(checkpointer=True))
            parent.add_edge(START, "nested")
            graph = parent.compile(checkpointer=saver)
            with saver.bind_execution(grant):
                result = await graph.ainvoke(
                    {"value": 1},
                    {"configurable": {"thread_id": claimed.thread_id}},
                    context=grant.graph_context,
                )
            assert result == {"value": 2}
            assert grant.lease_token not in repr(grant)
            async with database.session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT checkpoint_ns, checkpoint::text, metadata::text, NULL::bytea AS blob "
                            "FROM checkpoints WHERE thread_id = :thread_id "
                            "UNION ALL SELECT checkpoint_ns, NULL, NULL, blob "
                            "FROM checkpoint_blobs WHERE thread_id = :thread_id "
                            "UNION ALL SELECT checkpoint_ns, NULL, NULL, blob "
                            "FROM checkpoint_writes WHERE thread_id = :thread_id"
                        ),
                        {"thread_id": claimed.thread_id},
                    )
                ).all()
            assert any(row.checkpoint_ns for row in rows)
            serialized = b"".join(
                (row.checkpoint or "").encode() + (row.metadata or "").encode() + (row.blob or b"")
                for row in rows
            )
            assert grant.lease_token.encode() not in serialized
        await repository.complete(claimed.run_id, "runner-stage1", claimed.lease_token)
    finally:
        await _close_probe_run(database, claimed.run_id)
        await database.dispose()


@pytest.mark.asyncio
async def test_old_save_holding_run_lock_finishes_before_new_claim() -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    database, repository, claimed = await _claimed_run(database_url, lease_seconds=2)
    runner_url = make_url(database_url).set(username="cad2bim_runner_login")
    grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="runner-stage1")
    entered = asyncio.Event()
    release = asyncio.Event()

    class PausingSaver(FencedAsyncPostgresSaver):
        @asynccontextmanager
        async def _cursor(self, *, pipeline=False):
            async with super()._cursor(pipeline=pipeline) as cursor:
                if self._write_grant.get() is not None:
                    entered.set()
                    await release.wait()
                yield cursor

    config = {"configurable": {"thread_id": claimed.thread_id, "checkpoint_ns": ""}}
    try:
        async with PausingSaver.from_conn_string(
            _psycopg_connection_string(runner_url.render_as_string(hide_password=False))
        ) as saver:
            with saver.bind_execution(grant):
                write = asyncio.create_task(
                    saver.aput(
                        config,
                        empty_checkpoint(),
                        {"source": "input", "step": -1, "writes": {}},
                        {},
                    )
                )
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    await asyncio.sleep(2.1)
                    # The queued claim cannot take a Run whose row is locked
                    # by the old generation's in-flight checkpoint transaction.
                    competing = await repository.claim_next_run(
                        "runner-stage1-new", lease_seconds=30
                    )
                    assert competing is None
                finally:
                    release.set()
                saved_config = await write

            reclaimed = await repository.claim_next_run("runner-stage1-new", lease_seconds=30)
            assert reclaimed is not None and reclaimed.run_id == claimed.run_id
            with saver.bind_execution(grant), pytest.raises(LeaseConflictError):
                await saver.aput_writes(saved_config, [("late", 1)], "late-task")
            async with database.session_factory() as session:
                saved = await session.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                    {"thread_id": claimed.thread_id},
                )
            assert saved == 1
            await repository.complete(reclaimed.run_id, "runner-stage1-new", reclaimed.lease_token)
    finally:
        release.set()
        await _close_probe_run(database, claimed.run_id)
        await database.dispose()


@pytest.mark.asyncio
async def test_verified_http_subject_creates_task_and_run_with_api_rls() -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    api_url = make_url(database_url).set(username="cad2bim_api_login")
    database = create_database(
        Settings(database_url=api_url.render_as_string(hide_password=False), environment="test")
    )
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    issuer = "https://stage1-test.example"
    audience = "cad2bim-stage1"
    authenticator = JwtRs256Authenticator(
        public_key_pem=public_pem, issuer=issuer, audience=audience
    )
    service = TaskRunService(
        repository=PostgresControlPlaneRepository(database.session_factory),
        identity_directory=PostgresIdentityDirectory(database.session_factory),
        execution_protocol_factory=lambda _module, _stage_key: _binding(),
        bind_identity=bind_tenant_execution_context,
    )

    def token(subject: str) -> str:
        return jwt.encode(
            {
                "iss": issuer,
                "aud": audience,
                "sub": subject,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            signing_key,
            algorithm="RS256",
        )

    user_a = {"Authorization": f"Bearer {token(f'person-{uuid4().hex}')}"}
    user_b = {"Authorization": f"Bearer {token(f'person-{uuid4().hex}')}"}
    run_id: str | None = None
    try:
        async with database.session_factory() as session:
            role = (
                await session.execute(
                    text(
                        "SELECT current_user, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
        assert role.current_user == "cad2bim_api_login"
        assert role.rolsuper is False and role.rolbypassrls is False

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_task_api(authenticator=authenticator, service=service)
            ),
            base_url="http://stage1.test",
        ) as client:
            unauthenticated = await client.post("/tasks", json={"title": "denied"})
            assert unauthenticated.status_code == 401
            rejected_scope = await client.post(
                "/tasks", headers=user_a, json={"title": "denied", "tenant_id": "forged"}
            )
            assert rejected_scope.status_code == 422
            created = await client.post("/tasks", headers=user_a, json={"title": "Stage1 HTTP"})
            assert created.status_code == 201, created.text
            task_id = created.json()["task_id"]
            assert (await client.get(f"/tasks/{task_id}", headers=user_b)).status_code == 404

            rejected_thread = await client.post(
                f"/tasks/{task_id}/runs",
                headers=user_a,
                json={"idempotency_key": "rejected", "thread_id": "forged"},
            )
            assert rejected_thread.status_code == 422
            started = await client.post(
                f"/tasks/{task_id}/runs",
                headers=user_a,
                json={"idempotency_key": f"start-{uuid4().hex}"},
            )
            assert started.status_code == 201, started.text
            run_id = started.json()["run_id"]
            assert set(started.json()) == {
                "run_id",
                "task_id",
                "module",
                "stage_key",
                "status",
                "created_at",
                "updated_at",
            }
            assert (
                await client.get(f"/tasks/{task_id}/runs/{run_id}", headers=user_b)
            ).status_code == 404
            other_task = await client.post(
                "/tasks", headers=user_a, json={"title": "Unrelated task"}
            )
            assert other_task.status_code == 201
            assert (
                await client.get(
                    f"/tasks/{other_task.json()['task_id']}/runs/{run_id}",
                    headers=user_a,
                )
            ).status_code == 404
    finally:
        if run_id is not None:
            admin_database = create_database(
                Settings(database_url=database_url, environment="test")
            )
            try:
                await _close_probe_run(admin_database, run_id)
            finally:
                await admin_database.dispose()
        await database.dispose()


@pytest.mark.asyncio
async def test_bootstrap_executes_verified_request_to_fenced_checkpoint() -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    api_url = make_url(database_url).set(username="cad2bim_api_login")
    runner_url = make_url(database_url).set(username="cad2bim_runner_login")
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    issuer = "https://stage1-test.example"
    audience = "cad2bim-stage1"

    def graph_builder(saver, _feedback_nodes):
        async def calculate(state: _NestedState, runtime: Runtime[RunnerGraphContext]) -> dict:
            assert runtime.context.scope.identity.actor_kind.value == "runner"
            return {"value": state["value"] + 1}

        graph = StateGraph(_NestedState, context_schema=RunnerGraphContext)
        graph.add_node("calculate", calculate)
        graph.add_edge(START, "calculate")
        return graph.compile(checkpointer=saver)

    backend = await open_backend_runtime(
        api_settings=Settings(
            database_url=api_url.render_as_string(hide_password=False), environment="test"
        ),
        runner_settings=Settings(
            database_url=runner_url.render_as_string(hide_password=False), environment="test"
        ),
        public_key_pem=public_pem,
        issuer=issuer,
        audience=audience,
        execution_protocol=_binding(),
        graph_builder=graph_builder,
        runner_id="runner-stage1-bootstrap",
        state_access_secret=b"runner-state-test-secret-32-bytes-long",
    )
    run_id: str | None = None
    try:
        credential = jwt.encode(
            {
                "iss": issuer,
                "aud": audience,
                "sub": f"person-{uuid4().hex}",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            signing_key,
            algorithm="RS256",
        )
        headers = {"Authorization": f"Bearer {credential}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend.api), base_url="http://stage1.test"
        ) as client:
            task = await client.post("/tasks", headers=headers, json={"title": "Runner E2E"})
            assert task.status_code == 201, task.text
            task_id = task.json()["task_id"]
            started = await client.post(
                f"/tasks/{task_id}/runs",
                headers=headers,
                json={"idempotency_key": f"start-{uuid4().hex}", "input_payload": {"value": 3}},
            )
            assert started.status_code == 201, started.text
            run_id = started.json()["run_id"]
            finished = await backend.runner.execute_next()
            assert finished is not None and finished.run_id == run_id
            assert finished.status.value == "completed"
            viewed = await client.get(f"/tasks/{task_id}/runs/{run_id}", headers=headers)
            assert viewed.status_code == 200
            assert viewed.json()["status"] == "completed"
            assert "lease_token" not in viewed.json()
            async with backend.runner_database.session_factory() as session:
                count = await session.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                    {"thread_id": finished.thread_id},
                )
            assert count is not None and count > 0
    finally:
        await backend.close()
        if run_id is not None:
            admin_database = create_database(
                Settings(database_url=database_url, environment="test")
            )
            try:
                await _close_probe_run(admin_database, run_id)
            finally:
                await admin_database.dispose()


@pytest.mark.asyncio
async def test_new_runner_continues_while_expired_old_graph_is_still_alive() -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    database, _, claimed = await _claimed_run(database_url, lease_seconds=2)
    runner_url = make_url(database_url).set(username="cad2bim_runner_login")
    runner_database = create_database(
        Settings(database_url=runner_url.render_as_string(hide_password=False), environment="test")
    )
    runner_repository = PostgresControlPlaneRepository(runner_database.session_factory)
    old_entered = asyncio.Event()
    release_old = asyncio.Event()

    def graph_builder(saver):
        async def calculate(state: _NestedState, runtime: Runtime[RunnerGraphContext]) -> dict:
            generation = runtime.context.scope.identity.command_id.rsplit(":", 1)[-1]
            if generation == "1":
                old_entered.set()
                await release_old.wait()
                return {"value": 100}
            return {"value": state["value"] + 1}

        graph = StateGraph(_NestedState, context_schema=RunnerGraphContext)
        graph.add_node("calculate", calculate)
        graph.add_edge(START, "calculate")
        return graph.compile(checkpointer=saver)

    old_runtime: AsyncPostgresGraphRuntime | None = None
    new_runtime: AsyncPostgresGraphRuntime | None = None
    old_execution: asyncio.Task | None = None
    try:
        runtime_url = runner_url.render_as_string(hide_password=False)
        old_runtime = await AsyncPostgresGraphRuntime.open(
            database_url=runtime_url, graph_builder=graph_builder, execution_protocol=_binding()
        )
        new_runtime = await AsyncPostgresGraphRuntime.open(
            database_url=runtime_url, graph_builder=graph_builder, execution_protocol=_binding()
        )
        old_grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="runner-stage1")
        operation_id = f"run:{claimed.run_id}:start"
        with bind_tenant_execution_context(old_grant.scope.identity):
            old_execution = asyncio.create_task(
                old_runtime.start_durable(
                    grant=old_grant,
                    thread_id=claimed.thread_id,
                    operation_id=operation_id,
                    target_run_id=claimed.run_id,
                    input_value={"value": 0},
                )
            )
        await asyncio.wait_for(old_entered.wait(), 3)
        assert old_execution is not None and not old_execution.done()
        await asyncio.sleep(2.1)
        reclaimed = await runner_repository.claim_next_run(
            "runner-stage1-replacement", lease_seconds=30
        )
        assert reclaimed is not None and reclaimed.run_id == claimed.run_id
        new_grant = RunnerCheckpointGrant.from_claim(
            reclaimed, runner_id="runner-stage1-replacement"
        )
        with bind_tenant_execution_context(new_grant.scope.identity):
            result = await asyncio.wait_for(
                new_runtime.start_durable(
                    grant=new_grant,
                    thread_id=claimed.thread_id,
                    operation_id=operation_id,
                    target_run_id=claimed.run_id,
                    input_value={"value": 0},
                ),
                5,
            )
        assert result.values == {"value": 1}
        with bind_tenant_execution_context(new_grant.scope.identity):
            await runner_repository.complete(
                reclaimed.run_id, "runner-stage1-replacement", reclaimed.lease_token
            )
        release_old.set()
        with pytest.raises(LeaseConflictError):
            await old_execution
        exact = await new_runtime.get_checkpoint_state(
            thread_id=claimed.thread_id,
            checkpoint_id=result.checkpoint.checkpoint_id,
            checkpoint_ns=result.checkpoint.checkpoint_ns,
        )
        assert exact is not None and exact.values == {"value": 1}
    finally:
        release_old.set()
        if old_execution is not None and not old_execution.done():
            old_execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await old_execution
        elif old_execution is not None:
            try:
                old_execution.result()
            except Exception:
                pass
        if old_runtime is not None:
            await old_runtime.close()
        if new_runtime is not None:
            await new_runtime.close()
        await _close_probe_run(database, claimed.run_id)
        await runner_database.dispose()
        await database.dispose()


@pytest.mark.asyncio
async def test_runner_heartbeat_keeps_one_claim_during_long_graph() -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    api_url = make_url(database_url).set(username="cad2bim_api_login")
    runner_url = make_url(database_url).set(username="cad2bim_runner_login")
    api_database = create_database(
        Settings(database_url=api_url.render_as_string(hide_password=False), environment="test")
    )
    runner_database = create_database(
        Settings(database_url=runner_url.render_as_string(hide_password=False), environment="test")
    )
    admin_database = create_database(Settings(database_url=database_url, environment="test"))
    api_repository = PostgresControlPlaneRepository(api_database.session_factory)
    runner_repository = PostgresControlPlaneRepository(runner_database.session_factory)
    directory = PostgresIdentityDirectory(api_database.session_factory)
    entered = asyncio.Event()
    release = asyncio.Event()
    graph_instance: AsyncPostgresGraphRuntime | None = None
    execution: asyncio.Task | None = None
    run_id: str | None = None

    def graph_builder(saver):
        async def calculate(state: _NestedState, runtime: Runtime[RunnerGraphContext]) -> dict:
            assert runtime.context.scope.run_id == run_id
            entered.set()
            await release.wait()
            return {"value": state["value"] + 1}

        graph = StateGraph(_NestedState, context_schema=RunnerGraphContext)
        graph.add_node("calculate", calculate)
        graph.add_edge(START, "calculate")
        return graph.compile(checkpointer=saver)

    try:
        async with runner_database.session_factory() as session:
            role = (
                await session.execute(
                    text(
                        "SELECT current_user, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
        assert role.current_user == "cad2bim_runner_login"
        assert role.rolsuper is False and role.rolbypassrls is False
        now = datetime.now(timezone.utc)
        identity = await directory.resolve_authenticated_subject(
            AuthenticatedSubject(
                issuer="https://stage1-test.example",
                subject=f"person-{uuid4().hex}",
                audience="cad2bim-stage1",
                authenticated_at=now,
                token_expires_at=now + timedelta(hours=1),
            ),
            command_id=f"cmd_{uuid4().hex}",
        )
        with bind_tenant_execution_context(identity):
            task = await api_repository.create_task(
                identity.tenant_id, CreateTaskRequest(title="Heartbeat probe")
            )
            run = await api_repository.create_run(
                identity.tenant_id,
                task.task_id,
                StartRunRequest(idempotency_key=f"start-{uuid4().hex}", input_payload={"value": 2}),
                execution_protocol_factory=_binding,
            )
        run_id = run.run_id
        graph_instance = await AsyncPostgresGraphRuntime.open(
            database_url=runner_url.render_as_string(hide_password=False),
            graph_builder=graph_builder,
            execution_protocol=_binding(),
        )
        runner = RunnerExecutionService(
            repository=runner_repository,
            graph_runtime=graph_instance,
            feedback_service=FeedbackService(
                repository=runner_repository, identity_directory=directory,
                bind_identity=bind_tenant_execution_context,
            ),
            build_gate_open_request=build_user_gate_open_request,
            build_pause_open_request=build_run_pause_open_request,
            build_gate_reentry_update=lambda plan, clarifications: reentry_route_update(
                plan, clarifications=clarifications
            ),
            bind_execution_context=bind_tenant_execution_context,
            runner_id="runner-stage1-heartbeat",
            lease_seconds=2,
            heartbeat_interval_seconds=0.4,
        )
        execution = asyncio.create_task(runner.execute_next())
        await asyncio.wait_for(entered.wait(), 3)
        async with admin_database.session_factory() as session:
            before = (
                await session.execute(
                    text(
                        "SELECT claimed_by, lease_token, claim_count, writer_generation, "
                        "heartbeat_at FROM runs WHERE run_id = :run_id"
                    ),
                    {"run_id": run_id},
                )
            ).one()
        await asyncio.sleep(2.2)
        competing = await runner_repository.claim_next_run(
            "runner-stage1-heartbeat-competitor", lease_seconds=5
        )
        assert competing is None
        async with admin_database.session_factory() as session:
            after = (
                await session.execute(
                    text(
                        "SELECT status, claimed_by, lease_token, claim_count, writer_generation, "
                        "heartbeat_at, lease_expires_at, clock_timestamp() AS checked_at "
                        "FROM runs WHERE run_id = :run_id"
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert after.status == "running"
        assert after.claimed_by == before.claimed_by == "runner-stage1-heartbeat"
        assert after.lease_token == before.lease_token
        assert after.claim_count == before.claim_count == 1
        assert after.writer_generation == before.writer_generation == 1
        assert after.heartbeat_at > before.heartbeat_at
        assert after.lease_expires_at > after.checked_at
        release.set()
        completed = await asyncio.wait_for(execution, 5)
        assert completed is not None and completed.run_id == run_id
        assert completed.status.value == "completed"
    finally:
        release.set()
        if execution is not None and not execution.done():
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
        elif execution is not None:
            try:
                execution.result()
            except Exception:
                pass
        if graph_instance is not None:
            await graph_instance.close()
        if run_id is not None:
            await _close_probe_run(admin_database, run_id)
        await admin_database.dispose()
        await runner_database.dispose()
        await api_database.dispose()
