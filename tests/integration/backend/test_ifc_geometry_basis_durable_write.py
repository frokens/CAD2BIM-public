from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.application.data_runtime_service import (
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.ifc_geometry_basis_service import (
    IfcGeometryBasisService,
)
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    CreateSubcontextOperation,
    IfcGeometryBasisNoChangeError,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcWorldCoordinateSystem3D,
    UpdateParentContextOperation,
)
from CAD2BIM_backend.domain.ifc_model import (
    IfcGeometryBasisConfigureResult,
    IfcModelConflictError,
    IfcRunIdentity,
    InitializeIfcModelCommand,
)
from CAD2BIM_backend.infrastructure.postgres.database import Database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import (
    provision_runtime_login_roles,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack


pytestmark = [pytest.mark.integration, pytest.mark.network]

TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
MCP_PASSWORD = "ifc-geometry-basis-mcp-password"


@dataclass(slots=True)
class _Runtime:
    database: Database
    repository: PostgresControlPlaneRepository
    identity_directory: PostgresIdentityDirectory
    storage: LocalStorage

    async def close(self) -> None:
        await self.database.dispose()


class _CommitResponseLoss:
    def __init__(self, repository: PostgresControlPlaneRepository) -> None:
        self._repository = repository
        self.commit_calls = 0

    def __getattr__(self, name: str):
        return getattr(self._repository, name)

    async def commit_revision(self, *args, **kwargs):
        self.commit_calls += 1
        committed = await self._repository.commit_revision(*args, **kwargs)
        if self.commit_calls == 1:
            raise RuntimeError("injected geometry-basis commit response loss")
        return committed


class _CommitBarrier:
    """Hold real commits until two writes have prepared the same CURRENT."""

    def __init__(self, repository: PostgresControlPlaneRepository) -> None:
        self._repository = repository
        self._arrivals = 0
        self._lock = asyncio.Lock()
        self._release = asyncio.Event()

    def __getattr__(self, name: str):
        return getattr(self._repository, name)

    async def commit_revision(self, *args, **kwargs):
        async with self._lock:
            self._arrivals += 1
            if self._arrivals == 2:
                self._release.set()
        await asyncio.wait_for(self._release.wait(), timeout=10.0)
        return await self._repository.commit_revision(*args, **kwargs)


class _SingleAttemptSupervisor:
    """Expose the repository's real CAS result without retry/freeze projection."""

    async def invoke(self, attempt, *, on_exhausted, non_retryable_errors=()):
        del on_exhausted, non_retryable_errors
        return await attempt()


def _runtime_database_url(value: str) -> str:
    return (
        make_url(value)
        .set(
            drivername="postgresql+psycopg",
            username="cad2bim_mcp_login",
            password=MCP_PASSWORD,
        )
        .render_as_string(hide_password=False)
    )


def _runtime(database_url: str, storage_root: Path) -> _Runtime:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    return _Runtime(
        database=Database(engine=engine, session_factory=sessions),
        repository=PostgresControlPlaneRepository(sessions),
        identity_directory=PostgresIdentityDirectory(sessions),
        storage=LocalStorage(storage_root),
    )


async def _scope(runtime: _Runtime) -> tuple[object, IfcRunIdentity]:
    subject = f"ifc-geometry-basis-{uuid4().hex}"
    sessions = DirectSessionService(
        runtime.repository,
        runtime.identity_directory,
        subject,
        bind_tenant_execution_context,
    )
    resolver = DirectScopeResolver(
        runtime.repository,
        runtime.identity_directory,
        subject,
        bind_tenant_execution_context,
    )
    opened = await sessions.open_direct_session("IFC geometry basis durable write")
    invocation = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id=f"geometry-basis:{uuid4().hex}",
    )
    assert invocation.identity.task_id is not None
    return invocation.identity, IfcRunIdentity(
        owner_id=invocation.owner_id,
        tenant_id=invocation.identity.tenant_id,
        task_id=invocation.identity.task_id,
        run_id=invocation.run_id,
    )


def _initial_command() -> ConfigureIfcGeometryBasisCommand:
    return ConfigureIfcGeometryBasisCommand(
        project_unit_change=IfcProjectUnitChange(
            length_unit=IfcLengthUnit(prefix="MILLI"),
            plane_angle_unit=IfcPlaneAngleUnit(),
        ),
        coordinate_reference_change=IfcLocalCoordinateReferenceChange(),
        context_operations=(
            CreateParentContextOperation(
                context_identifier="Model",
                context_type="Model",
                coordinate_space_dimension=3,
                precision=1e-5,
                world_coordinate_system=IfcWorldCoordinateSystem3D(
                    origin=(0.0, 0.0, 0.0),
                    axis=(0.0, 0.0, 1.0),
                    ref_direction=(1.0, 0.0, 0.0),
                ),
                true_north=(0.0, 1.0),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_geometry_basis_write_is_durable_reconcilable_and_no_change_safe(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": "ifc-geometry-basis-api-password",
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": "ifc-geometry-basis-runner-password",
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    runtime = _runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    try:
        await runtime.database.verify_ready()
        context, scope = await _scope(runtime)
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        model_service = stack.model_service
        service = IfcGeometryBasisService(
            stack.writer,
            stack.runtime,
            stack.coordinator,
        )
        with bind_tenant_execution_context(context):
            await model_service.initialize(
                scope,
                InitializeIfcModelCommand(project_name="Durable Geometry Basis"),
            )
            configured = await service.configure(
                scope,
                _initial_command(),
                idempotency_key=f"geometry-basis:{uuid4().hex}",
            )
            current_v2 = await runtime.repository.get_current(scope)

        assert configured.operation_alias == "op_002"
        assert configured.model.geometry_basis_configured is True
        assert configured.model.geometry_basis is not None
        assert configured.model.geometry_basis.length_unit.prefix == "MILLI"
        assert current_v2 is not None and current_v2.revision_no == 2

        response_loss = _CommitResponseLoss(runtime.repository)
        response_loss_stack = build_in_process_ifc_stack(
            response_loss,
            runtime.storage,
            codec=stack.codec,
        )
        response_loss_service = IfcGeometryBasisService(
            response_loss_stack.writer,
            response_loss_stack.runtime,
            response_loss_stack.coordinator,
        )
        with bind_tenant_execution_context(context):
            reconciled = await response_loss_service.configure(
                scope,
                ConfigureIfcGeometryBasisCommand(
                    context_operations=(
                        CreateSubcontextOperation(
                            context_identifier="Body",
                            context_type="Model",
                            parent_context_identifier="Model",
                            target_view="MODEL_VIEW",
                        ),
                    )
                ),
                idempotency_key=f"geometry-basis:{uuid4().hex}",
            )
            current_v3 = await runtime.repository.get_current(scope)
            catalog = await service.list_representation_contexts(scope)

        assert response_loss.commit_calls == 2
        assert reconciled.operation_alias == "op_003"
        assert current_v3 is not None and current_v3.revision_no == 3
        assert [item.context_identifier for item in catalog.contexts] == ["Body", "Model"]

        barrier = _CommitBarrier(runtime.repository)
        concurrent_stacks = tuple(
            build_in_process_ifc_stack(
                barrier,
                runtime.storage,
                codec=stack.codec,
            )
            for _ in range(2)
        )
        concurrent_services = tuple(
            IfcGeometryBasisService(
                concurrent_stack.writer,
                concurrent_stack.runtime,
                concurrent_stack.coordinator,
                supervisor=_SingleAttemptSupervisor(),
            )
            for concurrent_stack in concurrent_stacks
        )
        concurrent_commands = (
            ConfigureIfcGeometryBasisCommand(
                context_operations=(
                    UpdateParentContextOperation(
                        context_identifier="Model",
                        precision=1e-6,
                    ),
                )
            ),
            ConfigureIfcGeometryBasisCommand(
                context_operations=(
                    UpdateParentContextOperation(
                        context_identifier="Model",
                        precision=1e-7,
                    ),
                )
            ),
        )
        with bind_tenant_execution_context(context):
            concurrent_results = await asyncio.gather(
                *(
                    concurrent_service.configure(
                        scope,
                        command,
                        idempotency_key=f"geometry-basis-cas:{uuid4().hex}",
                    )
                    for concurrent_service, command in zip(
                        concurrent_services,
                        concurrent_commands,
                        strict=True,
                    )
                ),
                return_exceptions=True,
            )
            current_v4 = await runtime.repository.get_current(scope)

        winners = [
            result
            for result in concurrent_results
            if isinstance(result, IfcGeometryBasisConfigureResult)
        ]
        losers = [
            result for result in concurrent_results if isinstance(result, IfcModelConflictError)
        ]
        assert len(winners) == len(losers) == 1, concurrent_results
        assert current_v4 is not None and current_v4.revision_no == 4
        assert winners[0].model.geometry_basis is not None
        assert winners[0].model.geometry_basis.precision in {1e-6, 1e-7}

        with bind_tenant_execution_context(context):
            with pytest.raises(IfcGeometryBasisNoChangeError):
                await service.configure(
                    scope,
                    ConfigureIfcGeometryBasisCommand(
                        coordinate_reference_change=IfcLocalCoordinateReferenceChange()
                    ),
                    idempotency_key=f"geometry-basis:{uuid4().hex}",
                )
            unchanged = await runtime.repository.get_current(scope)
            async with runtime.database.session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT status, error_code, operation_detail_json "
                                "FROM critical_operations WHERE run_id = :run_id "
                                "AND tool_name = 'ifc_model_configure_geometry_basis' "
                                "ORDER BY started_at"
                            ),
                            {"run_id": scope.run_id},
                        )
                    )
                    .mappings()
                    .all()
                )

        assert unchanged == current_v4
        assert [row["status"] for row in rows].count("succeeded") == 3
        assert [row["status"] for row in rows].count("failed") == 2
        assert {row["error_code"] for row in rows if row["status"] == "failed"} == {
            "IFC_CURRENT_CONFLICT",
            "IFC_NO_CHANGES",
        }
        assert rows[0]["operation_detail_json"]["result"] == "geometry_basis_configured"
        assert "result" not in rows[-1]["operation_detail_json"]
    finally:
        await runtime.close()
