from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, FastAPI

from CAD2BIM_backend import bootstrap
from CAD2BIM_backend.domain.control_plane import CreateTaskRequest, RunStatus, StartRunRequest
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.infrastructure.chatgpt_model import ModelStreamInterrupted, model_execution_error
from CAD2BIM_backend.infrastructure.component_demo import _drive_runners, _serve_runtime
from CAD2BIM_backend.infrastructure.langgraph.fenced_saver import FencedAsyncPostgresSaver
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import InMemoryControlPlaneRepository
from CAD2BIM_backend.infrastructure.memory.identity_directory import InMemoryIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.tenant_context import current_tenant_execution_context
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointDescriptor, GraphCheckpointState, GraphExecutionResult


class _Runs(InMemoryControlPlaneRepository):
    def __init__(self, *, expected_completions=2):
        super().__init__()
        self.claims = []
        self.claiming_runners = set()
        self.both_polled = asyncio.Event()
        self.completed = asyncio.Event()
        self.paused = asyncio.Event()
        self.completion_count = 0
        self.expected_completions = expected_completions

    async def load_terminal_reentry(self, grant):
        return None

    async def load_run_continue(self, grant):
        return None

    async def load_gate_resume(self, grant):
        return None

    async def claim_next_run(self, runner_id, *, lease_seconds=30):
        run = await super().claim_next_run(runner_id, lease_seconds=lease_seconds)
        self.claiming_runners.add(runner_id)
        if len(self.claiming_runners) == 2:
            self.both_polled.set()
        if run is not None:
            self.claims.append((runner_id, run.run_id))
        return run

    async def complete(self, *args, **kwargs):
        result = await super().complete(*args, **kwargs)
        self.completion_count += 1
        if self.completion_count == self.expected_completions:
            self.completed.set()
        return result

    async def pause_claimed(self, *args, **kwargs):
        result = await super().pause_claimed(*args, **kwargs)
        self.paused.set()
        return result


class _Graph:
    # The node is a scheduling witness, without a real role consuming feedback.
    # Use the shared Graph entry's valid empty payload instead of leaving a fake
    # Component initial input unacknowledged at the completion transaction.
    execution_protocol = ExecutionProtocolBinding(bundle_id="component", module="workflow", stage_key="cad2bim_workflow")

    def __init__(self, *, expected_active=2):
        self.expected_active = expected_active
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = set()
        self.observed_grants = {}
        self.results = {}
        self.fail_run = None
        self.fence = FencedAsyncPostgresSaver(object())
        self.close = AsyncMock()

    async def start_durable(self, *, grant, thread_id, operation_id, **kwargs):
        run_id = grant.scope.run_id
        if run_id == self.fail_run:
            raise ModelStreamInterrupted("inference exhausted after connection interruption")
        self.active.add(run_id)
        if len(self.active) == self.expected_active:
            self.started.set()
        with self.fence.bind_execution(grant):
            await self.release.wait()
            observed = self.fence.require_current_execution_grant()
            assert current_tenant_execution_context() == grant.scope.identity
            self.observed_grants[run_id] = observed
        checkpoint_id = "checkpoint-" + run_id
        mapping = (("", checkpoint_id),)
        result = GraphExecutionResult(values={"done": True}, checkpoint=GraphCheckpointDescriptor(
            thread_id=thread_id, checkpoint_id=checkpoint_id, root_checkpoint_id=checkpoint_id,
            checkpoint_map=mapping, checkpoint_map_digest=checkpoint_cursor_digest(
                checkpoint_id=checkpoint_id, checkpoint_ns="", checkpoint_map=mapping),
        ))
        self.results[thread_id] = (result, operation_id, run_id)
        self.active.remove(run_id)
        return result

    async def get_checkpoint_state(self, *, thread_id, checkpoint_id, **kwargs):
        result, operation_id, run_id = self.results[thread_id]
        return GraphCheckpointState(values=result.values, checkpoint=result.checkpoint,
            graph_operation_id=operation_id, target_run_id=run_id)

    def require_current_execution_grant(self):
        return self.fence.require_current_execution_grant()


async def _backend(monkeypatch, runs, graph, *, runner_slots=2):
    database = SimpleNamespace(session_factory=object(), verify_ready=AsyncMock(), dispose=AsyncMock())
    identities = InMemoryIdentityDirectory()
    monkeypatch.setattr(bootstrap, "create_database", lambda _settings: database)
    monkeypatch.setattr(bootstrap, "PostgresControlPlaneRepository", lambda _sessions: runs)
    monkeypatch.setattr(bootstrap, "PostgresIdentityDirectory", lambda _sessions: identities)
    monkeypatch.setattr(bootstrap.AsyncPostgresGraphRuntime, "open", AsyncMock(return_value=graph))
    monkeypatch.setattr(bootstrap, "JwtRs256Authenticator", lambda **kwargs: object())
    monkeypatch.setattr(bootstrap, "create_task_api", lambda **kwargs: FastAPI())
    monkeypatch.setattr(bootstrap, "create_internal_state_router", lambda **kwargs: APIRouter())
    return await bootstrap.open_backend_runtime(
        api_settings=object(), runner_settings=SimpleNamespace(database_url="postgresql+psycopg://unused/test"),
        public_key_pem=b"", issuer="unit", audience="unit", execution_protocol=graph.execution_protocol,
        graph_builder=lambda saver, feedback: graph, runner_id="component-unit",
        state_access_secret=b"component-unit-private-state-secret", runner_slots=runner_slots,
        recoverable_execution_error=model_execution_error,
    )


async def _queued_run(backend, user):
    now = datetime.now(timezone.utc)
    subject = AuthenticatedSubject(issuer="https://unit.example", subject=user, audience="component",
        authenticated_at=now, token_expires_at=now + timedelta(minutes=5))
    task = await backend.task_runs.create_task(subject, CreateTaskRequest(title=user, active_module="component"))
    return await backend.task_runs.start_run(subject, task.task_id, StartRunRequest(
        idempotency_key="first", input_payload={"goal": "runner concurrency witness"}))


def _ready_server():
    return SimpleNamespace(started=True, should_exit=False)


@pytest.mark.parametrize("run_count", [1, 2])
async def test_independent_slots_overlap_different_runs_and_never_double_claim_one_run(monkeypatch, run_count):
    runs = _Runs(expected_completions=run_count)
    graph = _Graph(expected_active=run_count)
    backend = await _backend(monkeypatch, runs, graph)
    original = [await _queued_run(backend, f"owner-{index}") for index in range(run_count)]
    assert len(backend.runners) == 2 and backend.runners[0] is not backend.runners[1]
    assert backend.runner is backend.runners[0]
    api, mcp = _ready_server(), _ready_server()
    driver = asyncio.create_task(_drive_runners(backend.runners, api, mcp))
    try:
        await asyncio.wait_for(graph.started.wait(), 2)
        await asyncio.wait_for(runs.both_polled.wait(), 2)
        assert graph.active == {run.run_id for run in original}
        assert len(runs.claims) == run_count
        graph.release.set()
        await asyncio.wait_for(runs.completed.wait(), 2)
        assert all(runs._runs[run.run_id].claim_count == 1 for run in original)
        assert all(runs._runs[run.run_id].status is RunStatus.COMPLETED for run in original)
        assert set(graph.observed_grants) == {run.run_id for run in original}
        assert len({grant.runner_id for grant in graph.observed_grants.values()}) == run_count
        assert len({grant.scope.identity.tenant_id for grant in graph.observed_grants.values()}) == run_count
    finally:
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        await backend.close()


async def test_model_failure_pauses_only_original_run_while_other_slot_completes(monkeypatch):
    runs, graph = _Runs(expected_completions=1), _Graph(expected_active=1)
    backend = await _backend(monkeypatch, runs, graph)
    failed = await _queued_run(backend, "failed-owner")
    successful = await _queued_run(backend, "successful-owner")
    graph.fail_run = failed.run_id
    api, mcp = _ready_server(), _ready_server()
    driver = asyncio.create_task(_drive_runners(backend.runners, api, mcp))
    try:
        await asyncio.wait_for(runs.paused.wait(), 2)
        await asyncio.wait_for(graph.started.wait(), 2)
        graph.release.set()
        await asyncio.wait_for(runs.completed.wait(), 2)
        assert runs._runs[failed.run_id].status is RunStatus.PAUSED
        assert runs._runs[failed.run_id].error == "model_network_interrupted"
        assert runs._runs[successful.run_id].status is RunStatus.COMPLETED
        assert not driver.done()
        assert len(runs.claims) == 2
    finally:
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        await backend.close()


class _Server:
    def __init__(self):
        self.started = False
        self.should_exit = False
        self.settled = asyncio.Event()

    async def serve(self):
        self.started = True
        try:
            while not self.should_exit:
                await asyncio.sleep(0.01)
        finally:
            self.settled.set()


class _BlockedRunner:
    def __init__(self):
        self.started = asyncio.Event()
        self.settled = asyncio.Event()

    async def execute_next(self):
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.settled.set()


async def test_unknown_execution_error_propagates_and_cancels_other_slot_before_shared_close():
    blocked = _BlockedRunner()
    class UnknownWrite:
        async def execute_next(self):
            await blocked.started.wait()
            raise RuntimeError("unknown WRITE outcome")
    api, mcp = _Server(), _Server()
    with pytest.raises(RuntimeError, match="unknown WRITE outcome"):
        await _serve_runtime(SimpleNamespace(runners=(blocked, UnknownWrite())), api, mcp)
    assert blocked.settled.is_set()
    assert api.settled.is_set() and mcp.settled.is_set()
    assert api.should_exit and mcp.should_exit


async def test_service_cancellation_settles_all_slots_and_servers():
    runners = (_BlockedRunner(), _BlockedRunner())
    api, mcp = _Server(), _Server()
    service = asyncio.create_task(_serve_runtime(SimpleNamespace(runners=runners), api, mcp))
    await asyncio.wait_for(asyncio.gather(*(runner.started.wait() for runner in runners)), 2)
    service.cancel()
    with pytest.raises(asyncio.CancelledError):
        await service
    assert all(runner.settled.is_set() for runner in runners)
    assert api.settled.is_set() and mcp.settled.is_set()


async def test_bootstrap_retains_single_slot_default_and_original_runner_identity(monkeypatch):
    backend = await _backend(monkeypatch, _Runs(), _Graph(), runner_slots=1)
    try:
        assert len(backend.runners) == 1
        assert backend.runner._runner_id == "component-unit"
    finally:
        await backend.close()
