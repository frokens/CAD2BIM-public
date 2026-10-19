from __future__ import annotations

import asyncio
from contextvars import ContextVar

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context, current_tenant_execution_context,
)


def _scope(run_id: str) -> IfcRunIdentity:
    return IfcRunIdentity(
        owner_id="owner",
        tenant_id="tenant",
        task_id="task",
        run_id=run_id,
    )


@pytest.mark.asyncio
async def test_first_waiter_deadline_does_not_cancel_surviving_waiter() -> None:
    coordinator = IfcWriteCoordinator()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "committed"

    first = asyncio.create_task(coordinator.invoke(
        _scope("run-deadline"), logical_operation_identity="shared",
        operation=operation, deadline=asyncio.get_running_loop().time() + 0.1,
    ))
    await entered.wait()
    second = asyncio.create_task(coordinator.invoke(
        _scope("run-deadline"), logical_operation_identity="shared",
        operation=operation, deadline=asyncio.get_running_loop().time() + 2,
    ))
    with pytest.raises(TimeoutError):
        await first
    assert not second.done()
    release.set()
    assert await second == "committed"
    assert calls == 1


@pytest.mark.asyncio
async def test_same_logical_write_is_single_flight() -> None:
    coordinator = IfcWriteCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "committed"

    first = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=operation,
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=operation,
        )
    )
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    assert await asyncio.gather(first, second) == ["committed", "committed"]


@pytest.mark.asyncio
async def test_different_writes_for_same_run_are_ordered() -> None:
    coordinator = IfcWriteCoordinator()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    events: list[str] = []

    async def first_operation() -> str:
        events.append("first-enter")
        first_entered.set()
        await release_first.wait()
        events.append("first-exit")
        return "first"

    async def second_operation() -> str:
        events.append("second-enter")
        return "second"

    first = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=first_operation,
        )
    )
    await first_entered.wait()
    second = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-2",
            operation=second_operation,
        )
    )
    await asyncio.sleep(0)
    assert events == ["first-enter"]
    release_first.set()
    assert await asyncio.gather(first, second) == ["first", "second"]
    assert events == ["first-enter", "first-exit", "second-enter"]


@pytest.mark.asyncio
async def test_different_runs_can_enter_independently() -> None:
    coordinator = IfcWriteCoordinator()
    both_entered = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def operation() -> str:
        nonlocal active
        active += 1
        if active == 2:
            both_entered.set()
        await release.wait()
        return "ok"

    tasks = [
        asyncio.create_task(
            coordinator.invoke(
                _scope(run_id),
                logical_operation_identity="operation",
                operation=operation,
            )
        )
        for run_id in ("run-1", "run-2")
    ]
    await asyncio.wait_for(both_entered.wait(), timeout=1)
    release.set()
    assert await asyncio.gather(*tasks) == ["ok", "ok"]


@pytest.mark.asyncio
async def test_cancelling_waiter_does_not_cancel_shared_owner() -> None:
    coordinator = IfcWriteCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> str:
        entered.set()
        await release.wait()
        return "ok"

    owner = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=operation,
        )
    )
    await entered.wait()
    waiter = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=operation,
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert await owner == "ok"


@pytest.mark.asyncio
async def test_cancelling_first_waiter_does_not_cancel_shared_write() -> None:
    coordinator = IfcWriteCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "committed"

    first = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=operation,
        )
    )
    await entered.wait()
    replay = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=operation,
        )
    )
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert calls == 1
    release.set()
    assert await replay == "committed"


@pytest.mark.asyncio
async def test_last_waiter_detaches_and_replay_joins_host() -> None:
    coordinator = IfcWriteCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "committed"

    first = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=operation,
        )
    )
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    replay = asyncio.create_task(
        coordinator.invoke(
            _scope("run-1"),
            logical_operation_identity="operation-1",
            operation=operation,
        )
    )
    await asyncio.sleep(0)
    assert calls == 1
    assert not replay.done()

    release.set()
    assert await replay == "committed"
    assert calls == 1
    assert coordinator._claims == {}
    assert coordinator._run_locks == {}


@pytest.mark.asyncio
async def test_host_task_keeps_only_explicit_tenant_context() -> None:
    coordinator = IfcWriteCoordinator(
        get_execution_context=current_tenant_execution_context,
        bind_execution_context=bind_tenant_execution_context,
    )
    request_marker = ContextVar("test_request_bearer", default=None)
    identity = TenantExecutionContext(
        tenant_id="tenant", principal_id="principal", task_id="task",
        actor_kind=TenantActorKind.USER, command_id="command",
    )

    async def operation() -> str:
        assert current_tenant_execution_context() == identity
        assert request_marker.get() is None
        return "ok"

    with bind_tenant_execution_context(identity):
        token = request_marker.set("request-secret")
        try:
            assert await coordinator.invoke(
                _scope("run-context"), logical_operation_identity="one",
                operation=operation,
            ) == "ok"
        finally:
            request_marker.reset(token)


@pytest.mark.asyncio
async def test_close_stops_admission_and_drains_owned_task() -> None:
    coordinator = IfcWriteCoordinator()
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def operation() -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "unreachable"

    waiter = asyncio.create_task(coordinator.invoke(
        _scope("run-close"), logical_operation_identity="one", operation=operation,
    ))
    await entered.wait()
    await coordinator.close(drain_seconds=0.01, stop_seconds=1)
    assert stopped.is_set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(RuntimeError, match="closing"):
        await coordinator.invoke(
            _scope("run-close"), logical_operation_identity="two", operation=operation,
        )


@pytest.mark.asyncio
async def test_direct_shutdown_drains_detached_write_before_releasing_dependencies():
    from CAD2BIM_mcp.direct.bootstrap import DirectRuntimeResources

    coordinator = IfcWriteCoordinator()
    entered, release, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation():
        entered.set()
        await release.wait()
        committed.set()

    waiter = asyncio.create_task(coordinator.invoke(
        _scope("run-direct-close"), logical_operation_identity="write", operation=operation))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    async def release_dependency():
        assert committed.is_set(), "an owned write was still using the dependency"

    dependency = SimpleNamespace(close=AsyncMock(side_effect=release_dependency))
    resources = SimpleNamespace(ifc_write_coordinator=coordinator,
        **{name: dependency for name in ("query_snapshots", "ifc_revision_runtime", "ifc_query_worker",
            "geometry_runtime", "product_realization_runtime", "geometry_codec", "python_transform_worker",
            "cad_measurement_service", "transform_worker")},
        database=SimpleNamespace(dispose=AsyncMock(side_effect=release_dependency)))
    closing = asyncio.create_task(DirectRuntimeResources.close(resources))
    try:
        await asyncio.sleep(0)
        dependency.close.assert_not_awaited()
        release.set()
        await asyncio.wait_for(closing, timeout=1)
        assert committed.is_set()
        resources.database.dispose.assert_awaited_once_with()
    finally:
        release.set()
        if not closing.done():
            await closing


@pytest.mark.asyncio
async def test_completed_run_locks_are_reclaimed() -> None:
    coordinator = IfcWriteCoordinator()

    for index in range(128):
        assert (
            await coordinator.invoke(
                _scope(f"run-{index}"),
                logical_operation_identity="operation",
                operation=lambda: asyncio.sleep(0, result="ok"),
            )
            == "ok"
        )

    assert coordinator._claims == {}
    assert coordinator._run_locks == {}
