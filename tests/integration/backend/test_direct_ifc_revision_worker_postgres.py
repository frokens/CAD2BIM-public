from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.ifc_geometry_basis_service import IfcGeometryBasisService
from CAD2BIM_backend.application.ifc_model_service import IfcModelService
from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.application.ifc_spatial_structure_service import (
    IfcSpatialStructureService,
)
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcWorldCoordinateSystem3D,
)
from CAD2BIM_backend.domain.ifc_model import IfcModelIntegrityError, IfcRunIdentity
from CAD2BIM_backend.domain.ifc_project import IfcProjectPatch, UpdateIfcProjectCommand
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    IfcProjectParentReference,
    IfcProjectWorldPlacement,
    IfcSpatialStructureListQuery,
)
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc_revision_runtime import (
    IfcOpenShellRevisionRuntime,
)
from CAD2BIM_backend.infrastructure.postgres.database import Database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
    current_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.infrastructure.worker_runtime import (
    DarwinSeatbeltSandbox,
    WorkerProcessSupervisor,
)
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from tests.integration.backend.test_geometry_resource_postgres import (
    _create_test_runtime_logins,
    _drop_test_runtime_logins,
    _runtime_database_url,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]

TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
CURSOR_KEY = "A" * 43


@dataclass(slots=True)
class _Runtime:
    database: Database
    repository: PostgresControlPlaneRepository
    identity_directory: PostgresIdentityDirectory
    storage: LocalStorage

    async def close(self) -> None:
        await self.database.dispose()


class _KillingSandbox:
    """Launch a real child that deterministically dies before writing a terminal."""

    def __init__(self) -> None:
        self._sandbox = DarwinSeatbeltSandbox(sys.executable)

    def command(
        self, *, scratch: Path, child_args: tuple[str, ...],
        metadata_read_fds: tuple[int, ...] = (),
    ) -> tuple[str, ...]:
        del child_args
        return self._sandbox.command(
            scratch=scratch,
            metadata_read_fds=metadata_read_fds,
            child_args=(
                "-I",
                "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
            ),
        )


class _KillObservingSupervisor(WorkerProcessSupervisor):
    def __init__(self) -> None:
        super().__init__()
        self.returncodes: list[int] = []

    async def run(self, *args, **kwargs):
        terminal = await super().run(*args, **kwargs)
        self.returncodes.append(terminal.returncode)
        return terminal


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


def _geometry_basis() -> ConfigureIfcGeometryBasisCommand:
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
async def test_real_revision_worker_crosses_direct_postgres_rls_and_survives_kill(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    roles = {}
    async with AsyncExitStack() as cleanup:
        cleanup.callback(_drop_test_runtime_logins, TEST_DATABASE_URL, roles)
        roles.update(_create_test_runtime_logins(TEST_DATABASE_URL))
        runtime = _runtime(
            _runtime_database_url(TEST_DATABASE_URL, roles["mcp"]),
            tmp_path / "storage",
        )
        cleanup.push_async_callback(runtime.close)
        revision_runtime = IfcOpenShellRevisionRuntime(
            capacity_root=tmp_path / "worker-capacity"
        )
        cleanup.push_async_callback(revision_runtime.close)
        await runtime.database.verify_ready()
        async with runtime.database.session_factory() as role_session:
            privileges = (await role_session.execute(text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ))).mappings().one()
        assert privileges["rolsuper"] is False
        assert privileges["rolbypassrls"] is False
        (tmp_path / "runtime-role-observation.json").write_text(
            json.dumps(dict(privileges), indent=2) + "\n", encoding="utf-8"
        )
        await revision_runtime.verify_ready()
        subject = f"ifc-revision-worker-{uuid4().hex}"
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
        codec = IfcOpenShellModelCodec()
        reader = IfcRevisionReader(runtime.repository, runtime.storage, revision_runtime)
        coordinator = IfcWriteCoordinator(
            get_execution_context=current_tenant_execution_context,
            bind_execution_context=bind_tenant_execution_context,
        )
        cleanup.push_async_callback(coordinator.close)
        writer = IfcRevisionWriter(
            runtime.repository,
            runtime.storage,
            reader,
            revision_runtime,
        )
        model_service = IfcModelService(
            runtime.repository,
            runtime.storage,
            codec,
            writer,
            revision_runtime,
            coordinator,
        )
        facade = DirectApplicationFacade(
            DataRuntimeService(
                runtime.repository,
                ArtifactService(runtime.repository, runtime.storage),
            ),
            sessions,
            resolver,
            bind_tenant_execution_context,
            ifc_model_service=model_service,
            ifc_geometry_basis_service=IfcGeometryBasisService(
                writer,
                revision_runtime,
                coordinator,
            ),
            ifc_project_service=IfcProjectService(
                writer,
                revision_runtime,
                coordinator,
            ),
            ifc_spatial_structure_service=IfcSpatialStructureService(
                writer,
                CursorCodec(CURSOR_KEY),
                revision_runtime,
                coordinator,
            ),
        )
        opened = await facade.open_direct_session("Real IFC revision worker")
        alias = opened.direct_session_alias
        invocation = await resolver.resolve(alias, command_id=f"worker:{uuid4().hex}")
        assert invocation.identity.task_id is not None
        scope = IfcRunIdentity(
            owner_id=invocation.owner_id,
            tenant_id=invocation.identity.tenant_id,
            task_id=invocation.identity.task_id,
            run_id=invocation.run_id,
        )

        initialized = await facade.initialize_ifc_model(alias, "Worker Integration Project")
        configured = await facade.configure_ifc_geometry_basis(
            alias,
            _geometry_basis(),
            idempotency_key=f"worker:{uuid4().hex}",
        )
        project = await facade.update_ifc_project(
            alias,
            UpdateIfcProjectCommand(
                patch=IfcProjectPatch(name="Worker Integration Updated", phase="I0-B3.04W")
            ),
            idempotency_key=f"worker:{uuid4().hex}",
        )
        spatial = await facade.apply_ifc_spatial_structure(
            alias,
            ApplyIfcSpatialStructureCommand(
                operations=(
                    CreateIfcSpatialOperation(
                        client_ref="worker_site",
                        spatial_class="site",
                        parent=IfcProjectParentReference(),
                        project_world_placement=IfcProjectWorldPlacement(
                            origin=(100.0, 200.0, 3.0),
                            axis=(0.0, 0.0, 1.0),
                            ref_direction=(1.0, 0.0, 0.0),
                        ),
                        name="Worker Site",
                        composition_type="ELEMENT",
                    ),
                )
            ),
            idempotency_key=f"worker:{uuid4().hex}",
        )
        listed = await facade.list_ifc_spatial_structure(
            alias,
            IfcSpatialStructureListQuery(page_size=8),
            None,
        )
        assert initialized.operation_alias == "op_001"
        assert configured.operation_alias == "op_002"
        assert project.operation_alias == "op_003"
        assert spatial.operation_alias == "op_004"
        assert listed.items[0].name == "Worker Site"

        with bind_tenant_execution_context(invocation.identity):
            current = await runtime.repository.get_current(scope)
            assert current is not None
            assert current.revision_no == current.write_epoch == 4
            inspection = await writer.inspect_exact(current)
            async with runtime.database.session_factory() as database_session:
                counts = (
                    (
                        await database_session.execute(
                            text(
                                "SELECT "
                                "(SELECT count(*) FROM ifc_model_revisions WHERE run_id=:run_id) revisions, "
                                "(SELECT count(*) FROM artifact_versions WHERE owner_id=:owner_id) artifact_versions, "
                                "(SELECT count(*) FROM critical_operations WHERE run_id=:run_id AND operation_kind='ifc_model_write') operations"
                            ),
                            {"run_id": scope.run_id, "owner_id": scope.owner_id},
                        )
                    )
                    .mappings()
                    .one()
                )
        assert inspection.project_name == "Worker Integration Updated"
        assert {key: int(value) for key, value in counts.items()} == {
            "revisions": 4,
            "artifact_versions": 4,
            "operations": 4,
        }

        observer_subject = f"ifc-revision-observer-{uuid4().hex}"
        observer_sessions = DirectSessionService(
            runtime.repository,
            runtime.identity_directory,
            observer_subject,
            bind_tenant_execution_context,
        )
        observer_resolver = DirectScopeResolver(
            runtime.repository,
            runtime.identity_directory,
            observer_subject,
            bind_tenant_execution_context,
        )
        observer_opened = await observer_sessions.open_direct_session("RLS observer")
        observer = await observer_resolver.resolve(
            observer_opened.binding.direct_session_alias,
            command_id=f"observer:{uuid4().hex}",
        )
        with bind_tenant_execution_context(observer.identity):
            async with runtime.database.session_factory() as database_session:
                hidden_revisions = await database_session.scalar(
                    text("SELECT count(*) FROM ifc_model_revisions WHERE run_id=:run_id"),
                    {"run_id": scope.run_id},
                )
        assert hidden_revisions == 0

        kill_supervisor = _KillObservingSupervisor()
        kill_runtime = IfcOpenShellRevisionRuntime(
            sandbox=_KillingSandbox(),  # type: ignore[arg-type]
            supervisor=kill_supervisor,
            capacity_root=tmp_path / "kill-capacity",
        )
        cleanup.push_async_callback(kill_runtime.close)
        kill_writer = IfcRevisionWriter(
            runtime.repository,
            runtime.storage,
            reader,
            kill_runtime,
        )
        kill_coordinator = IfcWriteCoordinator(
            get_execution_context=current_tenant_execution_context,
            bind_execution_context=bind_tenant_execution_context,
        )
        cleanup.push_async_callback(kill_coordinator.close)
        kill_service = IfcProjectService(
            kill_writer,
            kill_runtime,
            kill_coordinator,
        )
        with bind_tenant_execution_context(invocation.identity):
            with pytest.raises(IfcModelIntegrityError, match="technical retries"):
                await kill_service.update(
                    scope,
                    UpdateIfcProjectCommand(
                        patch=IfcProjectPatch(description="must not commit after SIGKILL")
                    ),
                    idempotency_key=f"worker-kill:{uuid4().hex}",
                )
            unchanged = await runtime.repository.get_current(scope)
            async with runtime.database.session_factory() as database_session:
                revision_count = await database_session.scalar(
                    text("SELECT count(*) FROM ifc_model_revisions WHERE run_id=:run_id"),
                    {"run_id": scope.run_id},
                )
        assert kill_supervisor.returncodes
        assert set(kill_supervisor.returncodes) == {-signal.SIGKILL}
        (tmp_path / "kill-observation.json").write_text(
            json.dumps({"returncodes": kill_supervisor.returncodes}, indent=2) + "\n",
            encoding="utf-8",
        )
        assert unchanged is not None
        assert unchanged.revision_id == current.revision_id
        assert unchanged.revision_no == unchanged.write_epoch == 4
        assert unchanged.model_state.value == "frozen"
        assert revision_count == 4
