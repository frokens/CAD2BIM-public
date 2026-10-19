from __future__ import annotations

from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.sql.elements import TextClause

from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.control_plane import RunStatus
from CAD2BIM_backend.domain.run_events import make_run_status_event
from CAD2BIM_backend.infrastructure.postgres.models import RunModel
from CAD2BIM_backend.infrastructure.postgres.repository_task_run import PostgresTaskRunRepositoryMixin
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantContextMismatch,
    _bind_new_rows_to_context,
    bind_tenant_execution_context,
    current_tenant_execution_context,
    set_tenant_execution_context,
)


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        statement: TextClause,
        parameters: dict[str, Any],
    ) -> None:
        self.calls.append((statement.text, parameters))


@pytest.mark.asyncio
async def test_tenant_context_is_set_with_transaction_local_postgres_settings() -> None:
    session = _RecordingSession()
    context = TenantExecutionContext(
        tenant_id="tenant-a",
        principal_id="principal-a",
        task_id="task-a",
        actor_kind=TenantActorKind.USER,
        command_id="command-a",
    )

    await set_tenant_execution_context(session, context)  # type: ignore[arg-type]

    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    assert "set_config('cad2bim.tenant_id', :tenant_id, true)" in statement
    assert "set_config('cad2bim.principal_id', :principal_id, true)" in statement
    assert "set_config('cad2bim.task_id', :task_id, true)" in statement
    assert parameters == {
        "tenant_id": "tenant-a",
        "principal_id": "principal-a",
        "task_id": "task-a",
    }


@pytest.mark.asyncio
async def test_context_without_task_sets_an_empty_transaction_local_task() -> None:
    session = _RecordingSession()
    context = TenantExecutionContext(
        tenant_id="tenant-a",
        principal_id="principal-a",
        actor_kind=TenantActorKind.SYSTEM_CURATOR,
        command_id="command-a",
    )

    await set_tenant_execution_context(session, context)  # type: ignore[arg-type]

    assert session.calls[0][1]["task_id"] == ""


def test_tenant_context_is_scoped_and_rejects_nested_identity_switch() -> None:
    first = TenantExecutionContext(
        tenant_id="tenant-a",
        principal_id="principal-a",
        actor_kind=TenantActorKind.USER,
        command_id="command-a",
    )
    other = first.model_copy(
        update={"tenant_id": "tenant-b", "principal_id": "principal-b"}
    )

    assert current_tenant_execution_context() is None
    with bind_tenant_execution_context(first):
        assert current_tenant_execution_context() == first
        with bind_tenant_execution_context(first):
            assert current_tenant_execution_context() == first
        with pytest.raises(TenantContextMismatch, match="already active"):
            with bind_tenant_execution_context(other):
                pass
    assert current_tenant_execution_context() is None


def test_new_tenant_rows_are_bound_and_cross_tenant_rows_are_rejected() -> None:
    context = TenantExecutionContext(
        tenant_id="tenant-a",
        principal_id="principal-a",
        actor_kind=TenantActorKind.USER,
        command_id="command-a",
    )

    class Row:
        def __init__(self, tenant_id=None):
            self.tenant_id = tenant_id

    local = Row()
    foreign = Row("tenant-b")

    with bind_tenant_execution_context(context):
        _bind_new_rows_to_context(type("Session", (), {"new": [local]})(), None, None)
        assert local.tenant_id == "tenant-a"
        with pytest.raises(TenantContextMismatch, match="another tenant"):
            _bind_new_rows_to_context(
                type("Session", (), {"new": [foreign]})(), None, None
            )


@pytest.mark.asyncio
async def test_run_event_append_uses_saved_run_tenant_without_python_context() -> None:
    """SQL claims bind transaction GUCs before there is a Python ContextVar."""

    from datetime import datetime, timezone

    assert current_tenant_execution_context() is None
    run = RunModel(run_id="run-a", task_id="task-a", owner_id="tenant-a",
        tenant_id="tenant-a", module="component", stage_key="component", status="running")
    event = make_run_status_event(source_event_key="claim:2", task_id=run.task_id, run_id=run.run_id,
        module=run.module, stage_key=run.stage_key, run_status=RunStatus.RUNNING,
        pause_requested=False, reason="claimed", occurred_at=datetime.now(timezone.utc))
    added = []
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
        scalar=AsyncMock(return_value=None), add=added.append, flush=AsyncMock(),
    )
    repository = PostgresTaskRunRepositoryMixin(None)
    appended = await repository._append_events(session, run, (event,))

    assert len(added) == 1 and appended == (event,)
    row = added[0]
    assert (row.tenant_id, row.owner_id, row.task_id, row.run_id) == (
        run.tenant_id, run.owner_id, run.task_id, run.run_id,
    )
    _bind_new_rows_to_context(SimpleNamespace(new=added), None, None)
    assert row.tenant_id == run.tenant_id
    session.flush.assert_awaited_once()
    foreign = TenantExecutionContext(tenant_id="tenant-b", principal_id="principal-b",
        task_id="task-b", actor_kind=TenantActorKind.RUNNER, command_id="claim-b")
    with bind_tenant_execution_context(foreign), pytest.raises(TenantContextMismatch, match="another tenant"):
        _bind_new_rows_to_context(SimpleNamespace(new=added), None, None)
