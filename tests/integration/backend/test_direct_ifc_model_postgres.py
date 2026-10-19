from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.ifc_model_service import (
    IfcModelService,
    IfcOperationReconciler,
)
from CAD2BIM_backend.application.ifc_geometry_basis_service import (
    IfcGeometryBasisService,
)
from CAD2BIM_backend.domain.data_runtime import CriticalOperationErrorPhase
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    CreateSubcontextOperation,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcWorldCoordinateSystem3D,
)
from CAD2BIM_backend.domain.ifc_model import (
    IfcModelAlreadyInitializedError,
    IfcModelConflictError,
    IfcModelFrozenError,
    IfcModelIntegrityError,
    IfcRunIdentity,
    InitializeIfcModelCommand,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
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
from CAD2BIM_backend.ports.storage import StorageKey
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcModelInitializeOperationDetail,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    IfcGeometryBasisConfigureOperationDetail,
    OperationSummaryCard,
    SucceededIfcGeometryBasisConfigureOperationCard,
    SucceededIfcModelWriteOperationCard,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack


pytestmark = [pytest.mark.integration, pytest.mark.network]

TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
API_PASSWORD = "i0b0-api-password"
RUNNER_PASSWORD = "i0b0-runner-password"
MCP_PASSWORD = "i0b0-mcp-password"


@dataclass(slots=True)
class _Runtime:
    database: Database
    repository: PostgresControlPlaneRepository
    identity_directory: PostgresIdentityDirectory
    storage: LocalStorage

    async def close(self) -> None:
        await self.database.dispose()


@dataclass(slots=True)
class _DirectIfc:
    facade: DirectApplicationFacade
    service: IfcModelService
    geometry_service: IfcGeometryBasisService
    alias: str
    context: object
    scope: IfcRunIdentity


class _FailingWriteStorage:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage
        self.write_calls = 0

    def __getattr__(self, name: str):
        return getattr(self._storage, name)

    async def write(self, *args, **kwargs):
        del args, kwargs
        self.write_calls += 1
        raise RuntimeError("injected private storage failure")


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


def _create_runtime(database_url: str, storage_root: Path) -> _Runtime:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    database = Database(engine=engine, session_factory=sessions)
    return _Runtime(
        database=database,
        repository=PostgresControlPlaneRepository(sessions),
        identity_directory=PostgresIdentityDirectory(sessions),
        storage=LocalStorage(storage_root),
    )


async def _open_direct_ifc(
    runtime: _Runtime,
    *,
    configured_subject: str,
    session_name: str,
    service: IfcModelService | None = None,
) -> _DirectIfc:
    sessions = DirectSessionService(
        runtime.repository,
        runtime.identity_directory,
        configured_subject,
        bind_tenant_execution_context,
    )
    resolver = DirectScopeResolver(
        runtime.repository,
        runtime.identity_directory,
        configured_subject,
        bind_tenant_execution_context,
    )
    codec = IfcOpenShellModelCodec()
    stack = build_in_process_ifc_stack(
        runtime.repository,
        runtime.storage,
        codec=codec,
    )
    ifc_service = service or stack.model_service
    geometry_service = IfcGeometryBasisService(
        stack.writer,
        stack.runtime,
        stack.coordinator,
    )
    facade = DirectApplicationFacade(
        DataRuntimeService(
            runtime.repository,
            ArtifactService(runtime.repository, runtime.storage),
        ),
        sessions,
        resolver,
        bind_tenant_execution_context,
        ifc_model_service=ifc_service,
        ifc_geometry_basis_service=geometry_service,
    )
    opened = await facade.open_direct_session(session_name)
    invocation = await resolver.resolve(
        opened.direct_session_alias,
        command_id=f"i0b0-test:{uuid4().hex}",
    )
    assert invocation.identity.task_id is not None
    return _DirectIfc(
        facade=facade,
        service=ifc_service,
        geometry_service=geometry_service,
        alias=opened.direct_session_alias,
        context=invocation.identity,
        scope=IfcRunIdentity(
            owner_id=invocation.owner_id,
            tenant_id=invocation.identity.tenant_id,
            task_id=invocation.identity.task_id,
            run_id=invocation.run_id,
        ),
    )


async def _ifc_counts(runtime: _Runtime, direct: _DirectIfc) -> dict[str, int]:
    with bind_tenant_execution_context(direct.context):
        async with runtime.database.session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM ifc_models WHERE run_id = :run_id) model_count, "
                            "(SELECT count(*) FROM ifc_model_revisions WHERE run_id = :run_id) revision_count, "
                            "(SELECT count(*) FROM critical_operations "
                            " WHERE run_id = :run_id AND operation_kind = 'ifc_model_write') operation_count, "
                            "(SELECT count(*) FROM critical_operations "
                            " WHERE run_id = :run_id AND status = 'indeterminate') indeterminate_count, "
                            "(SELECT count(*) FROM data_catalog_entries "
                            " WHERE run_id = :run_id AND resource_kind = 'operation') catalog_count"
                        ),
                        {"run_id": direct.scope.run_id},
                    )
                )
                .mappings()
                .one()
            )
    return {key: int(value) for key, value in row.items()}


def _geometry_basis_command() -> ConfigureIfcGeometryBasisCommand:
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
                    origin=(1000.0, 2000.0, 0.0),
                    axis=(0.0, 0.0, 1.0),
                    ref_direction=(1.0, 0.0, 0.0),
                ),
                true_north=(0.0, 1.0),
            ),
            CreateSubcontextOperation(
                context_identifier="Axis",
                context_type="Model",
                parent_context_identifier="Model",
                target_view="GRAPH_VIEW",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_i0b1_direct_geometry_basis_operation_and_restart(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    runtime_url = _runtime_database_url(TEST_DATABASE_URL)
    storage_root = tmp_path / "storage"
    runtime = _create_runtime(runtime_url, storage_root)
    restarted: _Runtime | None = None
    try:
        await runtime.database.verify_ready()
        subject = f"i0b1-primary-{uuid4().hex}"
        session_name = "I0-B1 Direct geometry basis"
        direct = await _open_direct_ifc(
            runtime,
            configured_subject=subject,
            session_name=session_name,
        )
        initialized = await direct.facade.initialize_ifc_model(
            direct.alias,
            "I0-B1 Gas Network",
        )
        command = _geometry_basis_command()
        configured = await direct.facade.configure_ifc_geometry_basis(
            direct.alias,
            command,
            idempotency_key=f"direct-request:{uuid4().hex}",
        )
        info = await direct.facade.get_ifc_model_info(direct.alias)
        contexts = await direct.facade.list_ifc_representation_contexts(direct.alias)

        assert initialized.operation_alias == "op_001"
        assert configured.operation_alias == "op_002"
        assert configured.model == info
        assert info.geometry_basis_configured is True
        assert info.geometry_basis is not None
        assert info.geometry_basis.length_unit.prefix == "MILLI"
        assert info.geometry_basis.coordinate_reference.mode == "local"
        assert info.geometry_basis.main_context_identifier == "Model"
        assert [item.context_identifier for item in contexts.contexts] == ["Axis", "Model"]
        assert contexts.contexts[0].parent_context_identifier == "Model"
        assert contexts.contexts[0].target_view == "GRAPH_VIEW"
        assert contexts.contexts[1].precision == 1e-5
        assert await _ifc_counts(runtime, direct) == {
            "model_count": 1,
            "revision_count": 2,
            "operation_count": 2,
            "indeterminate_count": 0,
            "catalog_count": 2,
        }

        operations = await direct.facade.list_operations(direct.alias, None, 20)
        assert [item.operation_alias for item in operations.operations] == ["op_002", "op_001"]
        configure_card = operations.operations[0]
        assert isinstance(configure_card, OperationSummaryCard)
        assert configure_card.operation_kind == "ifc_model_write"
        assert configure_card.tool_name == "ifc_model_configure_geometry_basis"
        assert configure_card.outcome == "succeeded"
        inspected = await direct.facade.get_operation(
            direct.alias,
            configured.operation_alias,
        )
        assert isinstance(
            inspected.operation,
            SucceededIfcGeometryBasisConfigureOperationCard,
        )
        assert inspected.operation.operation_alias == configure_card.operation_alias
        assert inspected.operation.primary_result.result == "geometry_basis_configured"
        assert isinstance(inspected.detail, IfcGeometryBasisConfigureOperationDetail)
        assert inspected.detail.result == "geometry_basis_configured"
        assert inspected.detail.request.project_unit_change is not None
        assert inspected.detail.request.project_unit_change.length_unit is not None
        assert inspected.detail.request.project_unit_change.length_unit.prefix == "MILLI"
        assert inspected.detail.request.coordinate_reference_change is not None
        assert inspected.detail.request.coordinate_reference_change.mode == "local"
        assert [
            operation.context_identifier
            for operation in inspected.detail.request.context_operations
        ] == ["Model", "Axis"]
        serialized_history = inspected.model_dump_json()
        for hidden in (
            "direct_session_alias",
            "request_digest",
            "idempotency_key",
            "run_id",
            "model_id",
            "revision_id",
            "artifact_id",
            "artifact_version_id",
            "storage_key",
            "writer_generation",
            "write_epoch",
        ):
            assert hidden not in serialized_history

        other = await _open_direct_ifc(
            runtime,
            configured_subject=subject,
            session_name="I0-B1 isolated Run",
        )
        assert other.alias != direct.alias
        with pytest.raises(DirectApplicationError) as uninitialized:
            await other.facade.get_ifc_model_info(other.alias)
        assert uninitialized.value.code == "IFC_MODEL_NOT_INITIALIZED"
        with pytest.raises(DirectApplicationError) as hidden_operation:
            await other.facade.get_operation(other.alias, configured.operation_alias)
        assert hidden_operation.value.code == "OPERATION_NOT_FOUND"

        restarted = _create_runtime(runtime_url, storage_root)
        await restarted.database.verify_ready()
        reopened = await _open_direct_ifc(
            restarted,
            configured_subject=subject,
            session_name=session_name,
        )
        assert reopened.alias == direct.alias
        assert await reopened.facade.get_ifc_model_info(reopened.alias) == info
        assert await reopened.facade.list_ifc_representation_contexts(reopened.alias) == contexts
        reopened_operation = await reopened.facade.get_operation(
            reopened.alias,
            configured.operation_alias,
        )
        assert reopened_operation == inspected
    finally:
        if restarted is not None:
            await restarted.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_i0b0_direct_initialize_info_operation_and_restart(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    runtime_url = _runtime_database_url(TEST_DATABASE_URL)
    storage_root = tmp_path / "storage"
    runtime = _create_runtime(runtime_url, storage_root)
    restarted: _Runtime | None = None
    try:
        await runtime.database.verify_ready()
        subject = f"i0b0-primary-{uuid4().hex}"
        direct = await _open_direct_ifc(
            runtime,
            configured_subject=subject,
            session_name="I0-B0 public entry",
        )

        with pytest.raises(DirectApplicationError) as uninitialized:
            await direct.facade.get_ifc_model_info(direct.alias)
        assert uninitialized.value.code == "IFC_MODEL_NOT_INITIALIZED"
        assert await _ifc_counts(runtime, direct) == {
            "model_count": 0,
            "revision_count": 0,
            "operation_count": 0,
            "indeterminate_count": 0,
            "catalog_count": 0,
        }

        initialized = await direct.facade.initialize_ifc_model(
            direct.alias,
            "  I0-B0 Gas Network  ",
        )
        info = await direct.facade.get_ifc_model_info(direct.alias)
        assert initialized.operation_alias == "op_001"
        assert initialized.model == info
        assert info.project.name == "I0-B0 Gas Network"
        assert info.geometry_basis_configured is False
        assert info.geometry_basis is None
        assert info.site_count == info.element_count == 0

        replay = await direct.facade.initialize_ifc_model(
            direct.alias,
            "I0-B0 Gas Network",
        )
        assert replay == initialized
        request = InitializeIfcModelCommand(project_name="I0-B0 Gas Network")
        original_arguments = {
            "request_digest": hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
            "project_global_id": info.project.global_id,
            "project_name": request.project_name,
        }
        changed_request = InitializeIfcModelCommand(project_name="Another Project")
        changed_arguments = {
            **original_arguments,
            "request_digest": hashlib.sha256(changed_request.model_dump_json().encode()).hexdigest(),
            "project_name": changed_request.project_name,
        }
        operation_query = text(
            "SELECT to_jsonb(operation) FROM critical_operations operation "
            "WHERE run_id = :run_id AND tool_name = 'ifc_model_initialize'"
        )
        with bind_tenant_execution_context(direct.context):
            exact_before_duplicate = await direct.service.get_current_exact(direct.scope)
            async with runtime.database.session_factory() as session:
                operation_before_duplicate = (
                    await session.execute(operation_query, {"run_id": direct.scope.run_id})
                ).scalar_one()
            original_state = await runtime.repository.get_initialize_operation_state(
                direct.scope, **original_arguments,
            )
            assert original_state is not None
            assert original_state.status.value == "succeeded"
            assert await runtime.repository.prepare_initialize(
                direct.scope, **original_arguments,
            ) == original_state.prepared
            for read in (
                runtime.repository.get_initialize_operation_state,
                runtime.repository.prepare_initialize,
            ):
                with pytest.raises(IfcModelAlreadyInitializedError):
                    await read(direct.scope, **changed_arguments)
                for invalid_exact_identity in (
                    {**original_arguments, "project_global_id": "0" * 22},
                    {**original_arguments, "project_name": "Mismatched name with original digest"},
                ):
                    with pytest.raises(IfcModelConflictError):
                        await read(direct.scope, **invalid_exact_identity)
        assert exact_before_duplicate is not None
        original_key = StorageKey(
            owner_id=direct.scope.owner_id,
            task_id=direct.scope.task_id,
            object_id=exact_before_duplicate.artifact.artifact_id,
            version=exact_before_duplicate.artifact.version_no,
        )
        bytes_before_duplicate = await runtime.storage.read(original_key)
        with pytest.raises(DirectApplicationError) as duplicate:
            await direct.facade.initialize_ifc_model(
                direct.alias,
                "Another Project",
            )
        assert duplicate.value.code == "IFC_MODEL_ALREADY_INITIALIZED"
        with bind_tenant_execution_context(direct.context):
            assert await direct.service.get_current_exact(direct.scope) == exact_before_duplicate
            async with runtime.database.session_factory() as session:
                assert (
                    await session.execute(operation_query, {"run_id": direct.scope.run_id})
                ).scalar_one() == operation_before_duplicate
        assert await runtime.storage.read(original_key) == bytes_before_duplicate
        assert await _ifc_counts(runtime, direct) == {
            "model_count": 1,
            "revision_count": 1,
            "operation_count": 1,
            "indeterminate_count": 0,
            "catalog_count": 1,
        }

        operations = await direct.facade.list_operations(direct.alias, None, 20)
        assert len(operations.operations) == 1
        card = operations.operations[0]
        assert isinstance(card, OperationSummaryCard)
        assert card.operation_kind == "ifc_model_write"
        assert card.tool_name == "ifc_model_initialize"
        assert card.outcome == "succeeded"
        assert card.operation_alias == initialized.operation_alias
        inspected = await direct.facade.get_operation(direct.alias, card.operation_alias)
        assert isinstance(inspected.operation, SucceededIfcModelWriteOperationCard)
        assert inspected.operation.operation_alias == card.operation_alias
        assert inspected.operation.primary_result.project_name == "I0-B0 Gas Network"
        assert isinstance(inspected.detail, IfcModelInitializeOperationDetail)
        assert inspected.detail.project_name == "I0-B0 Gas Network"
        assert inspected.detail.project_global_id == info.project.global_id
        assert inspected.detail.result == "model_initialized"
        serialized_history = inspected.model_dump_json()
        for hidden in (
            "direct_session_alias",
            "request_digest",
            "idempotency_key",
            "run_id",
            "model_id",
            "revision_id",
            "artifact_id",
            "storage_key",
            "writer_generation",
        ):
            assert hidden not in serialized_history

        with bind_tenant_execution_context(direct.context):
            exact = await direct.service.get_current_exact(direct.scope)
        assert exact is not None
        assert exact.revision_no == exact.write_epoch == 1
        exact_bytes = await runtime.storage.read(
            StorageKey(
                owner_id=direct.scope.owner_id,
                task_id=direct.scope.task_id,
                object_id=exact.artifact.artifact_id,
                version=1,
            )
        )
        inspection = IfcOpenShellModelCodec().inspect_revision1(
            exact_bytes,
            expected_project_global_id=info.project.global_id,
            expected_project_name="I0-B0 Gas Network",
        )
        assert inspection.to_safe_info().project.name == info.project.name

        other = await _open_direct_ifc(
            runtime,
            configured_subject=f"i0b0-other-{uuid4().hex}",
            session_name="I0-B0 isolated",
        )
        with bind_tenant_execution_context(other.context):
            assert await runtime.repository.get_current(other.scope) is None

        restarted = _create_runtime(runtime_url, storage_root)
        await restarted.database.verify_ready()
        reopened = await _open_direct_ifc(
            restarted,
            configured_subject=subject,
            session_name="I0-B0 public entry",
        )
        assert reopened.alias == direct.alias
        assert await reopened.facade.get_ifc_model_info(reopened.alias) == info
    finally:
        if restarted is not None:
            await restarted.close()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["prepared", "indeterminate", "failed"])
async def test_initialize_uncommitted_request_mismatch_keeps_original_classification(
    tmp_path: Path,
    status: str,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    try:
        direct = await _open_direct_ifc(
            runtime,
            configured_subject=f"init-classification-{uuid4().hex}",
            session_name=f"Initialization {status} classification",
        )
        command = InitializeIfcModelCommand(project_name="Original initialization")
        original_arguments = {
            "request_digest": hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
            "project_global_id": IfcOpenShellModelCodec().stable_project_global_id(direct.scope.model_dump_json()),
            "project_name": command.project_name,
        }
        changed = InitializeIfcModelCommand(project_name="Different initialization")
        changed_arguments = {
            **original_arguments,
            "request_digest": hashlib.sha256(changed.model_dump_json().encode()).hexdigest(),
            "project_name": changed.project_name,
        }
        with bind_tenant_execution_context(direct.context):
            prepared = await runtime.repository.prepare_initialize(direct.scope, **original_arguments)
            if status != "prepared":
                claim = await runtime.repository.claim_execution(
                    direct.scope,
                    operation_id=prepared.operation_id,
                    request_digest=prepared.request_digest,
                )
                try:
                    if status == "indeterminate":
                        await runtime.repository.freeze_indeterminate(
                            direct.scope,
                            model_id=None,
                            operation_id=prepared.operation_id,
                            reason_code="IFC_INITIALIZATION_UNCERTAIN",
                            claim=claim,
                        )
                    else:
                        await runtime.repository.record_failed_ifc_operation(
                            direct.scope,
                            operation_id=prepared.operation_id,
                            error_code="IFC_INITIALIZATION_REJECTED",
                            error_message="Controlled initialization rejection",
                            error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
                            claim=claim,
                        )
                finally:
                    await runtime.repository.retire_execution(direct.scope, claim)
            state_before = await runtime.repository.get_initialize_operation_state(direct.scope, **original_arguments)
            assert state_before is not None
            assert state_before.status.value == status
            for read in (
                runtime.repository.get_initialize_operation_state,
                runtime.repository.prepare_initialize,
            ):
                with pytest.raises(IfcModelConflictError):
                    await read(direct.scope, **changed_arguments)
            assert await runtime.repository.get_initialize_operation_state(
                direct.scope, **original_arguments,
            ) == state_before
            assert await runtime.repository.get_current(direct.scope) is None
        assert await _ifc_counts(runtime, direct) == {
            "model_count": 0, "revision_count": 0, "operation_count": 1,
            "indeterminate_count": int(status == "indeterminate"),
            "catalog_count": int(status == "failed"),
        }
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_i0b0_model_less_pause_survives_restart_and_reconciles(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    runtime_url = _runtime_database_url(TEST_DATABASE_URL)
    storage_root = tmp_path / "storage"
    runtime = _create_runtime(runtime_url, storage_root)
    restarted: _Runtime | None = None
    try:
        await runtime.database.verify_ready()
        subject = f"i0b0-paused-{uuid4().hex}"
        failing_storage = _FailingWriteStorage(runtime.storage)
        failing_stack = build_in_process_ifc_stack(
            runtime.repository,
            failing_storage,
        )
        failing_service = failing_stack.model_service
        direct = await _open_direct_ifc(
            runtime,
            configured_subject=subject,
            session_name="I0-B0 model-less pause",
            service=failing_service,
        )
        command = InitializeIfcModelCommand(project_name="I0-B0 Recovery")

        with pytest.raises(IfcModelIntegrityError, match="technical retries"):
            with bind_tenant_execution_context(direct.context):
                await failing_service.initialize(direct.scope, command)
        assert failing_storage.write_calls == 3
        assert await _ifc_counts(runtime, direct) == {
            "model_count": 0,
            "revision_count": 0,
            "operation_count": 1,
            "indeterminate_count": 1,
            "catalog_count": 0,
        }

        normal_service = build_in_process_ifc_stack(
            runtime.repository,
            runtime.storage,
        ).model_service
        with pytest.raises(IfcModelFrozenError):
            with bind_tenant_execution_context(direct.context):
                await normal_service.initialize(direct.scope, command)

        restarted = _create_runtime(runtime_url, storage_root)
        await restarted.database.verify_ready()
        restarted_stack = build_in_process_ifc_stack(
            restarted.repository,
            restarted.storage,
        )
        restarted_service = restarted_stack.model_service
        with bind_tenant_execution_context(direct.context):
            recovered = await IfcOperationReconciler(
                restarted_service,
                restarted.repository,
                restarted_stack.codec,
            ).reconcile_initialize(direct.scope, command)
            info = await restarted_service.info(direct.scope)
        assert recovered.operation_alias == "op_001"
        assert recovered.model == info
        assert info.project.name == "I0-B0 Recovery"
        assert await _ifc_counts(restarted, direct) == {
            "model_count": 1,
            "revision_count": 1,
            "operation_count": 1,
            "indeterminate_count": 0,
            "catalog_count": 1,
        }
    finally:
        if restarted is not None:
            await restarted.close()
        await runtime.close()
