from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import ifcopenshell.api.geometry
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import numpy as np
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
from CAD2BIM_backend.application.ifc_model_service import IfcModelService
from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
from CAD2BIM_backend.application.ifc_spatial_structure_service import (
    IfcSpatialStructureService,
)
from CAD2BIM_backend.domain.data_runtime import (
    IfcProjectUpdateOperationDetail as DomainIfcProjectUpdateOperationDetail,
    IfcSpatialStructureApplyOperationDetail as DomainIfcSpatialStructureApplyOperationDetail,
)
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.domain.ifc_project import IfcProjectPatch, UpdateIfcProjectCommand
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    DeleteIfcSpatialOperation,
    IfcClientRefSpatialParentReference,
    IfcProjectParentReference,
    IfcProjectWorldPlacement,
    IfcSpatialNotEmptyError,
    IfcSpatialPatch,
    IfcSpatialStructureListQuery,
    UpdateIfcSpatialOperation,
)
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.spatial_structure import (
    IfcOpenShellSpatialStructureCompiler,
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
from CAD2BIM_backend.ports.storage import StorageKey
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.operation.contracts import (
    FailedIfcSpatialStructureApplyOperationCard,
    FailedOperationGetResult,
    IfcProjectUpdateOperationDetail,
    IfcSpatialStructureApplyOperationDetail,
    OperationSummaryCard,
    SucceededIfcProjectUpdateOperationCard,
    SucceededIfcSpatialStructureApplyOperationCard,
    SucceededOperationGetResult,
)
from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack


pytestmark = [pytest.mark.integration, pytest.mark.network]

TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
API_PASSWORD = "i0b2-api-password"
RUNNER_PASSWORD = "i0b2-runner-password"
MCP_PASSWORD = "i0b2-mcp-password"
CURSOR_KEY = "A" * 43


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
    model_service: IfcModelService
    alias: str
    context: object
    scope: IfcRunIdentity


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
    model_service = stack.model_service
    facade = DirectApplicationFacade(
        DataRuntimeService(
            runtime.repository,
            ArtifactService(runtime.repository, runtime.storage),
        ),
        sessions,
        resolver,
        bind_tenant_execution_context,
        ifc_model_service=model_service,
        ifc_project_service=IfcProjectService(
            stack.writer,
            stack.runtime,
            stack.coordinator,
        ),
        ifc_spatial_structure_service=IfcSpatialStructureService(
            stack.writer,
            CursorCodec(CURSOR_KEY),
            stack.runtime,
            stack.coordinator,
        ),
    )
    opened = await facade.open_direct_session(session_name)
    invocation = await resolver.resolve(
        opened.direct_session_alias,
        command_id=f"i0b2-test:{uuid4().hex}",
    )
    assert invocation.identity.task_id is not None
    return _DirectIfc(
        facade=facade,
        model_service=model_service,
        alias=opened.direct_session_alias,
        context=invocation.identity,
        scope=IfcRunIdentity(
            owner_id=invocation.owner_id,
            tenant_id=invocation.identity.tenant_id,
            task_id=invocation.identity.task_id,
            run_id=invocation.run_id,
        ),
    )


async def _ifc_state(runtime: _Runtime, direct: _DirectIfc) -> dict[str, int]:
    with bind_tenant_execution_context(direct.context):
        async with runtime.database.session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM ifc_models WHERE run_id = :run_id) model_count, "
                            "(SELECT coalesce(max(write_epoch), 0) FROM ifc_models "
                            " WHERE run_id = :run_id) write_epoch, "
                            "(SELECT count(*) FROM ifc_model_revisions "
                            " WHERE run_id = :run_id) revision_count, "
                            "(SELECT count(*) FROM critical_operations "
                            " WHERE run_id = :run_id AND operation_kind = 'ifc_model_write') "
                            " operation_count, "
                            "(SELECT count(*) FROM critical_operations "
                            " WHERE run_id = :run_id AND operation_kind = 'ifc_model_write' "
                            " AND status = 'succeeded') succeeded_count, "
                            "(SELECT count(*) FROM critical_operations "
                            " WHERE run_id = :run_id AND operation_kind = 'ifc_model_write' "
                            " AND status = 'failed') failed_count, "
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


async def _current_exact_content(runtime: _Runtime, direct: _DirectIfc) -> tuple[object, bytes]:
    with bind_tenant_execution_context(direct.context):
        exact = await direct.model_service.get_current_exact(direct.scope)
    assert exact is not None
    content = await runtime.storage.read(
        StorageKey(
            owner_id=direct.scope.owner_id,
            task_id=direct.scope.task_id,
            object_id=exact.artifact.artifact_id,
            version=exact.artifact.version_no,
        )
    )
    return exact, content


def _placement(
    origin: tuple[float, float, float],
    ref_direction: tuple[float, float, float],
) -> IfcProjectWorldPlacement:
    return IfcProjectWorldPlacement(
        origin=origin,
        axis=(0.0, 0.0, 1.0),
        ref_direction=ref_direction,
    )


def _complex_tree_command() -> ApplyIfcSpatialStructureCommand:
    # Deliberately reverse the dependency order so every parent is a forward client_ref.
    return ApplyIfcSpatialStructureCommand(
        operations=(
            CreateIfcSpatialOperation(
                client_ref="room_b",
                spatial_class="space",
                parent=IfcClientRefSpatialParentReference(client_ref="storey_b"),
                project_world_placement=_placement(
                    (260.0, 130.0, 18.0),
                    (0.0, -1.0, 0.0),
                ),
                name="Room B",
                composition_type="ELEMENT",
                predefined_type="INTERNAL",
            ),
            CreateIfcSpatialOperation(
                client_ref="room_a",
                spatial_class="space",
                parent=IfcClientRefSpatialParentReference(client_ref="storey_a"),
                project_world_placement=_placement(
                    (120.0, 70.0, 9.0),
                    (-1.0, 0.0, 0.0),
                ),
                name="Room A",
                composition_type="ELEMENT",
                predefined_type="INTERNAL",
            ),
            CreateIfcSpatialOperation(
                client_ref="storey_b",
                spatial_class="building_storey",
                parent=IfcClientRefSpatialParentReference(client_ref="building_b"),
                project_world_placement=_placement(
                    (250.0, 120.0, 18.0),
                    (0.0, 1.0, 0.0),
                ),
                name="Storey B",
                composition_type="ELEMENT",
            ),
            CreateIfcSpatialOperation(
                client_ref="storey_a",
                spatial_class="building_storey",
                parent=IfcClientRefSpatialParentReference(client_ref="building_a"),
                project_world_placement=_placement(
                    (110.0, 60.0, 9.0),
                    (0.0, -1.0, 0.0),
                ),
                name="Storey A",
                composition_type="ELEMENT",
            ),
            CreateIfcSpatialOperation(
                client_ref="building_b",
                spatial_class="building",
                parent=IfcClientRefSpatialParentReference(client_ref="site"),
                project_world_placement=_placement(
                    (240.0, 110.0, 3.0),
                    (-1.0, 0.0, 0.0),
                ),
                name="Building B",
                composition_type="ELEMENT",
            ),
            CreateIfcSpatialOperation(
                client_ref="building_a",
                spatial_class="building",
                parent=IfcClientRefSpatialParentReference(client_ref="site"),
                project_world_placement=_placement(
                    (100.0, 50.0, 3.0),
                    (0.0, 1.0, 0.0),
                ),
                name="Building A",
                composition_type="ELEMENT",
            ),
            CreateIfcSpatialOperation(
                client_ref="site",
                spatial_class="site",
                parent=IfcProjectParentReference(),
                project_world_placement=_placement(
                    (80.0, 30.0, 3.0),
                    (0.0, 1.0, 0.0),
                ),
                name="Root Site",
                description="Integration test project-world root",
                composition_type="ELEMENT",
                ref_latitude=31.2304,
                ref_longitude=121.4737,
            ),
        )
    )


async def _assert_run_hidden_by_rls(
    runtime: _Runtime,
    *,
    observer: _DirectIfc,
    hidden_run_id: str,
) -> None:
    with bind_tenant_execution_context(observer.context):
        async with runtime.database.session_factory() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM ifc_models WHERE run_id = :run_id"),
                {"run_id": hidden_run_id},
            )
    assert count == 0


@pytest.mark.asyncio
async def test_i0b2_direct_project_spatial_operations_rls_and_restart(
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
        subject = f"i0b2-primary-{uuid4().hex}"
        session_name = "I0-B2 Project and spatial integration"
        direct = await _open_direct_ifc(
            runtime,
            configured_subject=subject,
            session_name=session_name,
        )

        initialized = await direct.facade.initialize_ifc_model(
            direct.alias,
            "I0-B2 Initial Project",
        )
        project_command = UpdateIfcProjectCommand(
            patch=IfcProjectPatch(
                name="I0-B2 Updated Project",
                description="Durable Project and spatial integration",
                long_name="Project Spatial Contract",
                phase="I0-B2",
            )
        )
        project_key = f"direct-request:{uuid4().hex}"
        project = await direct.facade.update_ifc_project(
            direct.alias,
            project_command,
            idempotency_key=project_key,
        )
        project_replay = await direct.facade.update_ifc_project(
            direct.alias,
            project_command,
            idempotency_key=project_key,
        )
        assert project_replay == project
        assert project.project.global_id == initialized.model.project.global_id
        assert project.project.name == "I0-B2 Updated Project"
        assert project.project.phase == "I0-B2"
        assert await _ifc_state(runtime, direct) == {
            "model_count": 1,
            "write_epoch": 2,
            "revision_count": 2,
            "operation_count": 2,
            "succeeded_count": 2,
            "failed_count": 0,
            "catalog_count": 2,
        }

        tree_key = f"direct-request:{uuid4().hex}"
        tree = await direct.facade.apply_ifc_spatial_structure(
            direct.alias,
            _complex_tree_command(),
            idempotency_key=tree_key,
        )
        assert tree.applied_count == 7
        created = {item.client_ref: item.global_id for item in tree.applied}
        assert set(created) == {
            "site",
            "building_a",
            "building_b",
            "storey_a",
            "storey_b",
            "room_a",
            "room_b",
        }

        complete = await direct.facade.list_ifc_spatial_structure(
            direct.alias,
            IfcSpatialStructureListQuery(page_size=64),
            None,
        )
        assert complete.returned_count == 7
        by_name = {item.name: item for item in complete.items}
        assert by_name["Root Site"].parent_global_id == project.project.global_id
        assert by_name["Building A"].parent_global_id == by_name["Root Site"].global_id
        assert by_name["Storey A"].parent_global_id == by_name["Building A"].global_id
        assert by_name["Room A"].parent_global_id == by_name["Storey A"].global_id
        assert by_name["Room B"].project_world_placement.origin == pytest.approx(
            (260.0, 130.0, 18.0)
        )

        first_page = await direct.facade.list_ifc_spatial_structure(
            direct.alias,
            IfcSpatialStructureListQuery(page_size=1),
            None,
        )
        assert first_page.returned_count == 1
        assert first_page.next_cursor is not None
        second_before = complete.items[1]
        cursor_update = ApplyIfcSpatialStructureCommand(
            operations=(
                UpdateIfcSpatialOperation(
                    target_global_id=second_before.global_id,
                    patch=IfcSpatialPatch(name="Cursor Current Name"),
                ),
            )
        )
        cursor_update_result = await direct.facade.apply_ifc_spatial_structure(
            direct.alias,
            cursor_update,
            idempotency_key=f"direct-request:{uuid4().hex}",
        )
        assert cursor_update_result.applied_count == 1

        exact_second_page = await direct.facade.list_ifc_spatial_structure(
            direct.alias,
            None,
            first_page.next_cursor,
        )
        assert exact_second_page.items == (second_before,)
        fresh_target = await direct.facade.list_ifc_spatial_structure(
            direct.alias,
            IfcSpatialStructureListQuery(
                global_ids=(second_before.global_id,),
                page_size=1,
            ),
            None,
        )
        assert fresh_target.items[0].name == "Cursor Current Name"

        before_failure = await _ifc_state(runtime, direct)
        blocked_command = ApplyIfcSpatialStructureCommand(
            operations=(
                DeleteIfcSpatialOperation(target_global_id=by_name["Building A"].global_id),
            )
        )
        blocked_key = f"direct-request:{uuid4().hex}"
        with pytest.raises(DirectApplicationError) as blocked:
            await direct.facade.apply_ifc_spatial_structure(
                direct.alias,
                blocked_command,
                idempotency_key=blocked_key,
            )
        assert blocked.value.code == "IFC_SPATIAL_NOT_EMPTY"
        assert blocked.value.operation_alias is not None
        assert blocked.value.diagnostic is not None
        assert blocked.value.diagnostic["child_spatial_count"] == 1
        after_failure = await _ifc_state(runtime, direct)
        assert after_failure["revision_count"] == before_failure["revision_count"]
        assert after_failure["write_epoch"] == before_failure["write_epoch"]
        assert after_failure["operation_count"] == before_failure["operation_count"] + 1
        assert after_failure["failed_count"] == before_failure["failed_count"] + 1
        assert after_failure["catalog_count"] == before_failure["catalog_count"] + 1

        deleted = await direct.facade.apply_ifc_spatial_structure(
            direct.alias,
            ApplyIfcSpatialStructureCommand(
                operations=(
                    DeleteIfcSpatialOperation(target_global_id=by_name["Room B"].global_id),
                )
            ),
            idempotency_key=f"direct-request:{uuid4().hex}",
        )
        assert deleted.applied[0].action == "delete_if_empty"
        assert by_name["Room B"].global_id not in {
            item.global_id for item in deleted.spatial_summary
        }
        assert by_name["Storey B"].global_id in {item.global_id for item in deleted.spatial_summary}
        after_delete = await _ifc_state(runtime, direct)
        assert after_delete["revision_count"] == after_failure["revision_count"] + 1
        assert after_delete["write_epoch"] == after_failure["write_epoch"] + 1
        assert after_delete["operation_count"] == after_failure["operation_count"] + 1
        assert after_delete["succeeded_count"] == after_failure["succeeded_count"] + 1

        replayed_tree = await direct.facade.apply_ifc_spatial_structure(
            direct.alias,
            _complex_tree_command(),
            idempotency_key=tree_key,
        )
        assert replayed_tree == tree
        replayed_project_after_current_moved = await direct.facade.update_ifc_project(
            direct.alias, project_command, idempotency_key=project_key,
        )
        assert replayed_project_after_current_moved == project
        with pytest.raises(DirectApplicationError) as blocked_replay:
            await direct.facade.apply_ifc_spatial_structure(
                direct.alias, blocked_command, idempotency_key=blocked_key,
            )
        assert blocked_replay.value.code == "IFC_SPATIAL_NOT_EMPTY"
        assert blocked_replay.value.phase == "ifc_validation"
        assert blocked_replay.value.retryable is False
        assert blocked_replay.value.operation_alias == blocked.value.operation_alias
        assert blocked_replay.value.diagnostic == blocked.value.diagnostic
        assert await _ifc_state(runtime, direct) == after_delete

        operation_list = await direct.facade.list_operations(direct.alias, None, 20)
        cards = {item.operation_alias: item for item in operation_list.operations}
        for alias, tool_name, outcome, error_code in (
            (project.operation_alias, "ifc_project_update", "succeeded", None),
            (tree.operation_alias, "ifc_spatial_structure_apply", "succeeded", None),
            (blocked.value.operation_alias, "ifc_spatial_structure_apply", "failed", "IFC_SPATIAL_NOT_EMPTY"),
        ):
            card = cards[alias]
            assert isinstance(card, OperationSummaryCard)
            assert (card.operation_kind, card.tool_name, card.outcome, card.error_code) == (
                "ifc_model_write", tool_name, outcome, error_code,
            )

        async def original_operation_details(scope):
            return {
                alias: (await direct.facade._application.get_operation(scope, alias)).operation.operation.detail
                for alias in (project.operation_alias, tree.operation_alias, blocked.value.operation_alias)
            }

        saved_details = await direct.facade._run_runtime_read(
            direct.alias, "operation_get", original_operation_details,
        )

        project_operation = await direct.facade.get_operation(
            direct.alias,
            project.operation_alias,
        )
        assert isinstance(project_operation, SucceededOperationGetResult)
        assert isinstance(project_operation.operation, SucceededIfcProjectUpdateOperationCard)
        assert project_operation.operation.operation_alias == project.operation_alias
        assert isinstance(project_operation.detail, IfcProjectUpdateOperationDetail)
        assert project_operation.detail.request == project_command
        assert project_operation.detail.result == "project_updated"
        saved_project = saved_details[project.operation_alias]
        assert isinstance(saved_project, DomainIfcProjectUpdateOperationDetail)
        assert saved_project.request == project_operation.detail.request
        assert saved_project.result is not None
        assert saved_project.result.name == project.project.name
        assert saved_project.result.phase == project.project.phase

        spatial_operation = await direct.facade.get_operation(
            direct.alias,
            tree.operation_alias,
        )
        assert isinstance(spatial_operation, SucceededOperationGetResult)
        assert isinstance(spatial_operation.operation, SucceededIfcSpatialStructureApplyOperationCard)
        assert spatial_operation.operation.operation_alias == tree.operation_alias
        assert isinstance(spatial_operation.detail, IfcSpatialStructureApplyOperationDetail)
        assert spatial_operation.detail.request == _complex_tree_command()
        assert spatial_operation.detail.result == "spatial_structure_applied"
        assert spatial_operation.detail.applied_count == 7
        assert spatial_operation.operation.primary_result.applied_count == spatial_operation.detail.applied_count
        saved_spatial = saved_details[tree.operation_alias]
        assert isinstance(saved_spatial, DomainIfcSpatialStructureApplyOperationDetail)
        assert saved_spatial.request == spatial_operation.detail.request
        assert saved_spatial.result is not None
        assert saved_spatial.result.applied_count == 7
        assert tuple(item.global_id for item in saved_spatial.result.applied) == tuple(
            item.global_id for item in tree.applied
        )
        assert tuple(item.global_id for item in saved_spatial.result.spatial_summary) == tuple(
            item.global_id for item in tree.spatial_summary
        )

        failed_operation = await direct.facade.get_operation(
            direct.alias,
            blocked.value.operation_alias,
        )
        assert isinstance(failed_operation, FailedOperationGetResult)
        assert isinstance(failed_operation.operation, FailedIfcSpatialStructureApplyOperationCard)
        assert failed_operation.operation.operation_alias == blocked.value.operation_alias
        assert isinstance(failed_operation.detail, IfcSpatialStructureApplyOperationDetail)
        assert isinstance(
            failed_operation.detail.request.operations[0],
            DeleteIfcSpatialOperation,
        )
        assert failed_operation.error.code == "IFC_SPATIAL_NOT_EMPTY"
        assert failed_operation.detail.result is None
        assert failed_operation.detail.applied_count is None
        saved_failed = saved_details[blocked.value.operation_alias]
        assert isinstance(saved_failed, DomainIfcSpatialStructureApplyOperationDetail)
        assert saved_failed.request == failed_operation.detail.request
        assert saved_failed.failure_blockers is not None
        assert saved_failed.failure_blockers.total_count > 0
        operation_json = "\n".join(
            item.model_dump_json()
            for item in (project_operation, spatial_operation, failed_operation)
        )
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
            "write_epoch",
            "writer_generation",
        ):
            assert hidden not in operation_json

        other_run = await _open_direct_ifc(
            runtime,
            configured_subject=subject,
            session_name="I0-B2 isolated sibling Run",
        )
        with pytest.raises(DirectApplicationError) as other_uninitialized:
            await other_run.facade.list_ifc_spatial_structure(
                other_run.alias,
                IfcSpatialStructureListQuery(page_size=64),
                None,
            )
        assert other_uninitialized.value.code == "IFC_MODEL_NOT_INITIALIZED"
        with pytest.raises(DirectApplicationError) as hidden_operation:
            await other_run.facade.get_operation(other_run.alias, project.operation_alias)
        assert hidden_operation.value.code == "OPERATION_NOT_FOUND"
        await _assert_run_hidden_by_rls(
            runtime,
            observer=other_run,
            hidden_run_id=direct.scope.run_id,
        )

        other_tenant = await _open_direct_ifc(
            runtime,
            configured_subject=f"i0b2-other-tenant-{uuid4().hex}",
            session_name="I0-B2 isolated Tenant",
        )
        await _assert_run_hidden_by_rls(
            runtime,
            observer=other_tenant,
            hidden_run_id=direct.scope.run_id,
        )

        restarted = _create_runtime(runtime_url, storage_root)
        await restarted.database.verify_ready()
        reopened = await _open_direct_ifc(
            restarted,
            configured_subject=subject,
            session_name=session_name,
        )
        assert reopened.alias == direct.alias
        reopened_info = await reopened.facade.get_ifc_model_info(reopened.alias)
        assert reopened_info.project.name == "I0-B2 Updated Project"
        reopened_spatial = await reopened.facade.list_ifc_spatial_structure(
            reopened.alias,
            IfcSpatialStructureListQuery(page_size=64),
            None,
        )
        assert by_name["Room B"].global_id not in {
            item.global_id for item in reopened_spatial.items
        }
        assert (
            await reopened.facade.get_operation(
                reopened.alias,
                blocked.value.operation_alias,
            )
            == failed_operation
        )

        # No public product writer exists in B2.  Use the exact durable bytes reopened above
        # as the test fixture, add one primary-contained product only to a private copy, and
        # exercise the production preflight compiler.  The durable CURRENT remains untouched.
        exact_before_product_check, current_content = await _current_exact_content(
            restarted,
            reopened,
        )
        private = IfcOpenShellModelCodec().private_copy(current_content)
        model = private.model
        room_a = model.by_guid(by_name["Room A"].global_id)
        assert room_a is not None
        wall = ifcopenshell.api.root.create_entity(
            model,
            ifc_class="IfcWall",
            name="Primary-contained blocker",
        )
        wall_matrix = np.eye(4)
        wall_matrix[:3, 3] = (121.0, 71.0, 9.0)
        ifcopenshell.api.geometry.edit_object_placement(
            model,
            product=wall,
            matrix=wall_matrix,
            is_si=False,
        )
        ifcopenshell.api.spatial.assign_container(
            model,
            products=[wall],
            relating_structure=room_a,
        )
        with pytest.raises(IfcSpatialNotEmptyError) as product_blocked:
            IfcOpenShellSpatialStructureCompiler().apply(
                private.to_bytes(),
                ApplyIfcSpatialStructureCommand(
                    operations=(DeleteIfcSpatialOperation(target_global_id=room_a.GlobalId),)
                ),
            )
        assert product_blocked.value.blockers.contained_element_count == 1
        exact_after_product_check, unchanged_content = await _current_exact_content(
            restarted,
            reopened,
        )
        assert exact_after_product_check == exact_before_product_check
        assert unchanged_content == current_content
    finally:
        if restarted is not None:
            await restarted.close()
        await runtime.close()
