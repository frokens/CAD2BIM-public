from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from CAD2BIM_backend.application.runner_execution_service import RunnerExecutionService
from CAD2BIM_backend.domain.control_plane import ExecutionMode, RunRecord, RunStatus
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.system_interrupts import InterruptResumeEnvelope, SystemInterruptDecision
from CAD2BIM_backend.domain.execution_protocol import (
    ExecutionProtocolBinding,
    ExecutionProtocolMismatchError,
)
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.infrastructure.langgraph.user_gate import (
    build_run_pause_open_request,
    build_user_gate_open_request,
    reentry_route_update,
)
from CAD2BIM_backend.ports.graph_runtime import (
    GraphCheckpointDescriptor,
    GraphCheckpointState,
    GraphExecutionResult,
)


def _binding(bundle: str) -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id=f"bundle_{bundle}",
        module="workflow",
        stage_key="cad2bim_workflow",
    )


def _claimed_run(binding: ExecutionProtocolBinding) -> RunRecord:
    now = datetime.now(timezone.utc)
    return RunRecord.model_construct(
        run_id="run-1",
        task_id="task-1",
        owner_id="tenant-1",
        tenant_id="tenant-1",
        principal_id="principal-1",
        module="workflow",
        stage_key="cad2bim_workflow",
        input_payload={"goal": "test"},
        idempotency_key="key-1",
        thread_id="thread-1",
        execution_mode=ExecutionMode.GRAPH,
        execution_protocol=binding,
        status=RunStatus.RUNNING,
        claimed_by="runner-1",
        lease_token="lease-secret",
        writer_generation=1,
        created_at=now,
        updated_at=now,
    )


class _Runs:
    def __init__(self, run: RunRecord) -> None:
        self.run = run
        self.claims = 0
        self.completed = 0
        self.heartbeats = 0
        self.completion_events = ()
        self.heartbeat_signal = asyncio.Event()
        self.pauses = []

    async def claim_next_run(self, runner_id, *, lease_seconds=30):
        assert runner_id == "runner-1" and lease_seconds > 0
        self.claims += 1
        return self.run if self.claims == 1 else None

    async def heartbeat(self, run_id, runner_id, lease_token, *, lease_seconds=30):
        assert (run_id, runner_id, lease_token) == ("run-1", "runner-1", "lease-secret")
        self.heartbeats += 1
        self.heartbeat_signal.set()
        return self.run

    async def complete(self, run_id, runner_id, lease_token, *, events=()):
        assert (run_id, runner_id, lease_token) == ("run-1", "runner-1", "lease-secret")
        self.completed += 1
        self.completion_events = events
        return self.run.model_copy(update={"status": RunStatus.COMPLETED})

    async def pause_claimed(self, run_id, runner_id, lease_token, *, error=None):
        assert (run_id, runner_id, lease_token) == ("run-1", "runner-1", "lease-secret")
        self.pauses.append(error)
        self.run = self.run.model_copy(update={"status": RunStatus.PAUSED, "error": error,
            "claimed_by": None, "lease_token": None, "lease_expires_at": None})
        return self.run


class _Graph:
    def __init__(self, binding: ExecutionProtocolBinding, *, error: Exception | None = None):
        self.execution_protocol = binding
        self.error = error
        self.calls = []
        self.last_result = None

    async def start_durable(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        self.last_result = GraphExecutionResult(
            values={"done": True},
            checkpoint=GraphCheckpointDescriptor(
                thread_id="thread-1",
                checkpoint_id="checkpoint-1",
                root_checkpoint_id="checkpoint-1",
                checkpoint_map=(("", "checkpoint-1"),),
                checkpoint_map_digest=checkpoint_cursor_digest(
                    checkpoint_id="checkpoint-1",
                    checkpoint_ns="",
                    checkpoint_map=(("", "checkpoint-1"),),
                ),
            ),
        )
        return self.last_result

    async def get_checkpoint_state(self, *, thread_id, checkpoint_id, checkpoint_ns=""):
        assert (thread_id, checkpoint_id, checkpoint_ns) == (
            "thread-1", "checkpoint-1", ""
        )
        result = self.last_result
        assert result is not None
        return GraphCheckpointState(
            values=result.values,
            checkpoint=result.checkpoint,
            graph_operation_id=self.calls[-1]["operation_id"],
            target_run_id="run-1",
        )


class _Feedback:
    async def load_run_continue(self, _grant):
        return None

    async def load_terminal_reentry(self, _grant):
        return None

    async def load_gate_resume(self, _grant):
        return None


@contextmanager
def _bind_identity(_identity):
    yield


def _service(repo: _Runs, graph: _Graph, feedback=None, recoverable_execution_error=None) -> RunnerExecutionService:
    return RunnerExecutionService(
        repository=repo,
        graph_runtime=graph,
        feedback_service=_Feedback() if feedback is None else feedback,
        build_gate_open_request=build_user_gate_open_request,
        build_pause_open_request=build_run_pause_open_request,
        build_gate_reentry_update=lambda plan, clarifications: reentry_route_update(
            plan, clarifications=clarifications
        ),
        bind_execution_context=_bind_identity,
        runner_id="runner-1",
        recoverable_execution_error=recoverable_execution_error,
    )


@pytest.mark.asyncio
async def test_runner_claims_once_uses_private_grant_and_completes_terminal_graph():
    binding = _binding("1")
    repo = _Runs(_claimed_run(binding))
    graph = _Graph(binding)
    result = await _service(repo, graph).execute_next()

    assert result.status is RunStatus.COMPLETED
    assert repo.claims == repo.completed == 1
    assert len(repo.completion_events) == 1
    assert repo.completion_events[0].attributes == {
        "terminal_root_checkpoint_id": "checkpoint-1",
        "graph_operation_id": "run:run-1:start",
    }
    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call["thread_id"] == "thread-1"
    assert call["input_value"] == {"goal": "test"}
    assert call["grant"].graph_context.scope.run_id == "run-1"
    assert "lease-secret" not in repr(call["grant"])


@pytest.mark.asyncio
async def test_runner_rejects_wrong_execution_bundle_before_graph_execution():
    repo = _Runs(_claimed_run(_binding("1")))
    graph = _Graph(_binding("2"))

    with pytest.raises(ExecutionProtocolMismatchError, match="another graph route"):
        await _service(repo, graph).execute_next()

    assert graph.calls == []
    assert repo.completed == 0


@pytest.mark.asyncio
async def test_runner_process_error_leaves_run_reclaimable_instead_of_business_failed():
    binding = _binding("1")
    repo = _Runs(_claimed_run(binding))
    graph = _Graph(binding, error=RuntimeError("worker interrupted"))

    with pytest.raises(RuntimeError, match="worker interrupted"):
        await _service(repo, graph).execute_next()

    assert repo.completed == 0
    assert repo.run.status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_exhausted_model_transport_parks_original_claim_without_success_or_new_operation():
    import httpx
    from CAD2BIM_backend.infrastructure.chatgpt_model import ModelStreamInterrupted, model_execution_error

    binding = _binding("1")
    repo = _Runs(_claimed_run(binding))
    error = ModelStreamInterrupted("model stream incomplete chunked read")
    error.__cause__ = httpx.RemoteProtocolError("incomplete chunked read")
    graph = _Graph(binding, error=error)
    with pytest.raises(ModelStreamInterrupted, match="incomplete chunked read"):
        await _service(repo, graph, recoverable_execution_error=model_execution_error).execute_next()
    assert repo.pauses == ["model_network_interrupted"]
    assert repo.run.status is RunStatus.PAUSED and repo.run.claimed_by is None
    assert repo.completed == 0 and len(graph.calls) == 1
    assert graph.calls[0]["operation_id"] == "run:run-1:start"


@pytest.mark.asyncio
async def test_tool_transport_failure_does_not_enter_model_failure_pause():
    import httpx
    from CAD2BIM_backend.infrastructure.chatgpt_model import model_execution_error

    binding = _binding("1")
    repo = _Runs(_claimed_run(binding))
    graph = _Graph(binding, error=httpx.RemoteProtocolError("tool result connection lost"))
    with pytest.raises(httpx.RemoteProtocolError):
        await _service(repo, graph, recoverable_execution_error=model_execution_error).execute_next()
    assert not repo.pauses and repo.run.status is RunStatus.RUNNING


def _paused_checkpoint() -> GraphCheckpointDescriptor:
    mapping = (("", "root-pause"), ("component:child", "child-pause"))
    return GraphCheckpointDescriptor(
        thread_id="thread-1", checkpoint_id="child-pause", checkpoint_ns="component:child",
        root_checkpoint_id="root-pause", checkpoint_map=mapping,
        checkpoint_map_digest=checkpoint_cursor_digest(
            checkpoint_id="child-pause", checkpoint_ns="component:child", checkpoint_map=mapping,
        ),
        next_nodes=("pause_before_llm",), created_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.mark.asyncio
async def test_runner_dispatches_child_pause_to_atomic_pause_owner():
    class Graph(_Graph):
        async def start_durable(self, **kwargs):
            self.calls.append(kwargs)
            return GraphExecutionResult(
                values={"business_entry_id": "entry-1", "role": "component", "role_entry_index": 2},
                checkpoint=_paused_checkpoint(),
                interrupts=({"id": "graph-pause-1", "value": {"kind": "run_pause", "business_entry_id": "entry-1"}},),
            )

    class Feedback(_Feedback):
        def __init__(self):
            self.pauses = []

        async def open_run_pause(self, grant, request):
            self.pauses.append((grant, request))
            return repo.run.model_copy(update={"status": RunStatus.PAUSED})

    binding = _binding("1")
    repo = _Runs(_claimed_run(binding))
    graph = Graph(binding)
    feedback = Feedback()
    result = await _service(repo, graph, feedback).execute_next()
    assert result.status is RunStatus.PAUSED
    assert repo.completed == 0
    assert len(feedback.pauses) == 1
    _, request = feedback.pauses[0]
    assert request.runtime_envelope.graph_interrupt_id == "graph-pause-1"
    assert request.runtime_envelope.checkpoint_cursor.checkpoint_ns == "component:child"
    assert request.runtime_envelope.business_entry_id is None
    assert request.system_interrupt.allowed_actions == ("continue",)


@pytest.mark.asyncio
async def test_runner_continues_original_child_interrupt_with_saved_operation():
    checkpoint = _paused_checkpoint()
    envelope = InterruptResumeEnvelope(
        decision=SystemInterruptDecision(action="continue"),
        checkpoint_cursor=CheckpointCursor(
            thread_id=checkpoint.thread_id, checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_ns=checkpoint.checkpoint_ns, root_checkpoint_id=checkpoint.root_checkpoint_id,
            checkpoint_map=checkpoint.checkpoint_map, checkpoint_digest=checkpoint.checkpoint_map_digest,
        ),
        response_high_watermark=5, expected_writer_generation=1,
    )

    class Feedback(_Feedback):
        async def load_run_continue(self, grant):
            return "continue-original", "graph-pause-1", envelope

        async def load_gate_resume(self, grant):
            pytest.fail("run pause must not load a Gate resume")

    class Graph(_Graph):
        async def resume_durable(self, **kwargs):
            return await super().start_durable(**kwargs)

    binding = _binding("1")
    repo = _Runs(_claimed_run(binding))
    graph = Graph(binding)
    completed = await _service(repo, graph, Feedback()).execute_next()
    assert completed.status is RunStatus.COMPLETED
    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call["operation_id"] == "continue-original"
    assert call["source_checkpoint_id"] == "child-pause"
    assert call["checkpoint_ns"] == "component:child"
    assert call["resume_value"] == {"graph-pause-1": envelope.model_dump(mode="json")}
    assert "input_value" not in call


@pytest.mark.asyncio
async def test_runner_renews_lease_while_graph_is_waiting():
    binding = _binding("1")
    repo = _Runs(_claimed_run(binding))
    release_graph = asyncio.Event()

    class WaitingGraph(_Graph):
        async def start_durable(self, **kwargs):
            await release_graph.wait()
            return await super().start_durable(**kwargs)

    runner = RunnerExecutionService(
        repository=repo,
        graph_runtime=WaitingGraph(binding),
        feedback_service=_Feedback(),
        build_gate_open_request=build_user_gate_open_request,
        build_pause_open_request=build_run_pause_open_request,
        build_gate_reentry_update=lambda plan, clarifications: reentry_route_update(
            plan, clarifications=clarifications
        ),
        bind_execution_context=_bind_identity,
        runner_id="runner-1",
        lease_seconds=1,
        heartbeat_interval_seconds=0.01,
    )
    work = asyncio.create_task(runner.execute_next())
    try:
        await asyncio.wait_for(repo.heartbeat_signal.wait(), timeout=0.5)
        assert not work.done()
        release_graph.set()
        result = await work
    finally:
        release_graph.set()
        if not work.done():
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work

    assert result.status is RunStatus.COMPLETED
    assert repo.heartbeats >= 1


@pytest.mark.asyncio
async def test_heartbeat_failure_cancels_local_graph_without_failing_business_run():
    binding = _binding("1")
    cancelled = asyncio.Event()

    class BrokenHeartbeatRuns(_Runs):
        async def heartbeat(self, run_id, runner_id, lease_token, *, lease_seconds=30):
            await super().heartbeat(run_id, runner_id, lease_token, lease_seconds=lease_seconds)
            raise RuntimeError("heartbeat lost")

    class WaitingGraph(_Graph):
        async def start_durable(self, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    repo = BrokenHeartbeatRuns(_claimed_run(binding))
    runner = RunnerExecutionService(
        repository=repo,
        graph_runtime=WaitingGraph(binding),
        feedback_service=_Feedback(),
        build_gate_open_request=build_user_gate_open_request,
        build_pause_open_request=build_run_pause_open_request,
        build_gate_reentry_update=lambda plan, clarifications: reentry_route_update(
            plan, clarifications=clarifications
        ),
        bind_execution_context=_bind_identity,
        runner_id="runner-1",
        lease_seconds=1,
        heartbeat_interval_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="heartbeat lost"):
        await runner.execute_next()

    assert cancelled.is_set()
    assert repo.completed == 0
    assert repo.run.status is RunStatus.RUNNING
