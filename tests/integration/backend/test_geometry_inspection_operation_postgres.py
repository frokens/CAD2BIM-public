from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.domain.control_plane import ConflictError, ExecutionMode
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, CriticalOperationErrorPhase, CriticalOperationKind,
    CriticalOperationRecord, CriticalOperationStatus, GeometryInspectionOperationDetail,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactVersionModel, CriticalOperationModel, GeometryResourceModel, GeometryResourceVersionModel,
    GeometryResourceVersionAssetModel,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context, set_tenant_execution_context
from tests.integration.backend.test_geometry_query_postgres import (
    TEST_DATABASE_URL, _artifact, _create_logins, _open_context, _runtime, _runtime_url,
    _task_owner_and_run,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]


@pytest.mark.asyncio
async def test_inspection_operation_admission_claim_publication_restart_and_scope():
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    logins = _create_logins(TEST_DATABASE_URL)
    runtime = _runtime(_runtime_url(TEST_DATABASE_URL, logins["mcp"]))
    admin = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex
    try:
        context = await _open_context(runtime, subject=f"inspection-{suffix}", session_name=f"Inspection one {suffix}")
        other_context = await _open_context(runtime, subject=f"inspection-{suffix}", session_name=f"Inspection other {suffix}")
        owner_id, run_id = await _task_owner_and_run(admin, context)
        scope = InvocationScope(identity=context, owner_id=owner_id, run_id=run_id,
                                execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
        other_owner, other_run = await _task_owner_and_run(admin, other_context)
        other_scope = InvocationScope(identity=other_context, owner_id=other_owner, run_id=other_run,
                                      execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
        resource_id, version_id = f"inspection-source-{suffix}", f"inspection-source-version-{suffix}"
        async with admin.begin() as connection:
            await connection.execute(GeometryResourceModel.__table__.insert().values(
                resource_id=resource_id, resource_kind="modeling_function", owner_scope="SYSTEM",
                owner_tenant_id=None, owner_task_id=None, display_name="Neutral source for Operation identity test",
            ))
            await connection.execute(GeometryResourceVersionModel.__table__.insert().values(
                resource_id=resource_id, resource_version_id=version_id, version_no=1,
                origin_kind="neutral_fixture", content_digest="a" * 64, manifest_digest="b" * 64,
                manifest_json={}, lineage_json={},
            ))
            for role, asset_format, media_type in (
                ("source_bundle", "source_bundle_v1", "application/zip"),
                ("parameter_schema", "json_schema_v1", "application/json"),
            ):
                await connection.execute(GeometryResourceVersionAssetModel.__table__.insert().values(
                    resource_version_id=version_id, asset_role=role, asset_format=asset_format,
                    filename=f"{role}.fixture", content_type=media_type, size_bytes=2, sha256="b" * 64,
                ))
        request = await _artifact(runtime, context, owner_id=owner_id, run_id=run_id,
                                  suffix=f"inspection-request-{suffix}", digest="c" * 64,
                                  kind="geometry_inspection_request")
        result = await _artifact(runtime, context, owner_id=owner_id, run_id=run_id,
                                 suffix=f"inspection-result-{suffix}", digest="d" * 64,
                                 kind="geometry_inspection", content_type="application/vnd.cad2bim.geometry-inspection+zip",
                                 version_metadata={"source_kind": "geometry_sample",
                                                   "request_digest": "e" * 64})
        now = datetime.now(UTC)
        prepared = CriticalOperationRecord(
            operation_id=f"inspection-operation-{suffix}", owner_id=owner_id, tenant_id=context.tenant_id,
            task_id=context.task_id, run_id=run_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION,
            tool_name="ifc_geometry_preview", intent_summary="Retain a neutral exact result for persistence validation",
            idempotency_key=f"inspection-action-{suffix}", request_fingerprint="e" * 64,
            status=CriticalOperationStatus.PREPARED, started_at=now,
            detail=GeometryInspectionOperationDetail(source_kind="geometry_sample", source_id=resource_id,
                source_version_id=version_id, source_version_ref="neutral-exact-source-reference",
                source_content_digest="a" * 64, request_artifact=ArtifactVersionRef(**request.model_dump())),
        )
        with bind_tenant_execution_context(context):
            admitted = await runtime.repository.prepare_geometry_inspection_operation(scope, prepared)
            assert admitted == prepared
            assert await runtime.repository.get_operation(scope, prepared.operation_id) is None
            claim = await runtime.repository.claim_authoring_execution(
                scope, prepared.operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION)
            assert claim is not None
            assert await runtime.repository.claim_authoring_execution(
                scope, prepared.operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION) is None
            state = await runtime.repository.get_authoring_execution_state(
                scope, prepared.operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION)
            assert state.operation == prepared and state.execution_generation == claim.generation
            terminal = CriticalOperationRecord.model_validate({**prepared.model_dump(),
                "status": CriticalOperationStatus.SUCCEEDED, "finished_at": datetime.now(UTC),
                "result_inspection_artifact": ArtifactVersionRef(**result.model_dump())})
            with pytest.raises(ConflictError, match="stale"):
                await runtime.repository.complete_geometry_inspection_operation(
                    scope, terminal, execution_claim=claim.model_copy(update={"generation": claim.generation + 1}))
            published = await runtime.repository.complete_geometry_inspection_operation(scope, terminal, execution_claim=claim)
            replay = await runtime.repository.complete_geometry_inspection_operation(scope, terminal, execution_claim=claim)
            assert replay == published and published.catalog_entry.alias == "op_001"
        restarted = _runtime(_runtime_url(TEST_DATABASE_URL, logins["mcp"]))
        try:
            with bind_tenant_execution_context(context):
                loaded = await restarted.repository.get_operation(scope, prepared.operation_id)
                assert loaded == published
                catalog = await restarted.repository.list_operation_catalog(
                    scope, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION)
                assert catalog.entries == (published.catalog_entry,)
            with bind_tenant_execution_context(other_context):
                assert await restarted.repository.get_operation(other_scope, prepared.operation_id) is None
                assert await restarted.repository.get_authoring_execution_state(
                    other_scope, prepared.operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION) is None
        finally:
            await restarted.close()
        with bind_tenant_execution_context(context):
            with pytest.raises(DBAPIError):
                async with runtime.sessions() as session, session.begin():
                    await set_tenant_execution_context(session, context)
                    await session.execute(text("UPDATE critical_operations SET status='prepared', finished_at=NULL "
                        "WHERE operation_id=:id"), {"id": prepared.operation_id})
        for table, column, identity in (
            (ArtifactVersionModel, ArtifactVersionModel.version_id, result.version_id),
            (GeometryResourceVersionModel, GeometryResourceVersionModel.resource_version_id, version_id),
        ):
            with pytest.raises(DBAPIError):
                async with admin.begin() as connection:
                    await connection.execute(table.__table__.delete().where(column == identity))

        # An independent call with the same business digest remains a distinct action.
        second = CriticalOperationRecord.model_validate({**prepared.model_dump(),
            "operation_id": f"inspection-operation-second-{suffix}", "idempotency_key": f"inspection-action-second-{suffix}"})
        with bind_tenant_execution_context(context):
            assert await runtime.repository.prepare_geometry_inspection_operation(scope, second) == second
            second_claim = await runtime.repository.claim_authoring_execution(
                scope, second.operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION)
            failed = CriticalOperationRecord.model_validate({**second.model_dump(),
                "status": CriticalOperationStatus.FAILED, "finished_at": datetime.now(UTC),
                "error_code": "BUILD_FAILED", "error_message": "The accepted same-build request failed",
                "error_phase": CriticalOperationErrorPhase.EXECUTION})
            failed_published = await runtime.repository.complete_geometry_inspection_operation(
                scope, failed, execution_claim=second_claim)
            assert failed_published.catalog_entry.alias == "op_002"
            assert failed_published.operation.detail.request_artifact == prepared.detail.request_artifact
            assert failed_published.operation.result_inspection_artifact is None
        async with admin.connect() as connection:
            count = (await connection.execute(select(CriticalOperationModel.operation_id).where(
                CriticalOperationModel.operation_id.in_((prepared.operation_id, second.operation_id))))).all()
            assert len(count) == 2
    finally:
        await runtime.close()
        await admin.dispose()
        # The enclosing owner-validation database is disposable and retained as evidence.
