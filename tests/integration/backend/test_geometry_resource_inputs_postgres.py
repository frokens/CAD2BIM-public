"""Named-input projection, restart visibility and deletion protection under real RLS."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.domain.control_plane import ConflictError
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceInput, GeometryResourceScope, GeometryResourceScopeKind
from CAD2BIM_backend.infrastructure.postgres.models import GeometryResourceVersionInputModel, GeometryResourceVersionAssetModel, GeometryResourceVersionModel
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context, set_tenant_execution_context
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from tests.integration.backend.test_geometry_resource_postgres import (
    TEST_DATABASE_URL, _create_test_runtime_logins, _drop_test_runtime_logins,
    _create_runtime, _runtime_database_url, _open_direct_context, _mapped_bundle,
    _publish_after_storage_validation, _cleanup_geometry_rows,
)

pytestmark = [pytest.mark.integration, pytest.mark.network, pytest.mark.asyncio]


async def test_named_inputs_are_atomic_immutable_rls_scoped_and_restartable(tmp_path):
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    logins = _create_test_runtime_logins(TEST_DATABASE_URL)
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL, logins["mcp"]))
    admin = create_async_engine(TEST_DATABASE_URL)
    restarted = None
    resource_ids = set()
    storage = LocalStorage(tmp_path / "storage")
    prefix = "inputs-" + uuid4().hex
    try:
        await runtime.database.verify_ready()
        context = await _open_direct_context(runtime, subject=prefix, session_name="input owner")
        other = await _open_direct_context(runtime, subject=prefix, session_name="other task")
        scope = GeometryResourceScope(scope_kind=GeometryResourceScopeKind.TASK,
            tenant_id=context.tenant_id, task_id=context.task_id)
        target = await _mapped_bundle(storage=storage, resource_id=prefix + "-target",
            resource_version_id=prefix + "-target-v1", version_no=1, scope=scope,
            created_at=datetime.now(UTC), salt="original", include_canonical_sha=True)
        resource_ids.add(target.resource.resource_id)
        await _publish_after_storage_validation(runtime, storage, context, target)
        frozen = GeometryResourceInput(input_key="body", resource_id=target.resource.resource_id,
            resource_version_id=target.version.resource_version_id, content_digest=target.version.content_digest,
            canonical_geometry_sha256=target.version.manifest["canonical_geometry_sha256"])
        consumer = await _mapped_bundle(storage=storage, resource_id=prefix + "-consumer",
            resource_version_id=prefix + "-consumer-v1", version_no=1, scope=scope,
            created_at=datetime.now(UTC), salt="consumer", geometry_inputs=(frozen,))
        resource_ids.add(consumer.resource.resource_id)
        published = await _publish_after_storage_validation(runtime, storage, context, consumer)
        assert published.version.manifest["geometry_inputs"] == [frozen.model_dump(mode="json")]
        restarted = _create_runtime(_runtime_database_url(TEST_DATABASE_URL, logins["mcp"]))
        with bind_tenant_execution_context(context):
            reread = await restarted.repository.get_exact_geometry_resource_version(context,
                resource_id=consumer.resource.resource_id, resource_version_id=consumer.version.resource_version_id)
        assert reread == published
        with bind_tenant_execution_context(other):
            assert await runtime.repository.get_exact_geometry_resource_version(other,
                resource_id=consumer.resource.resource_id, resource_version_id=consumer.version.resource_version_id) is None
            async with runtime.sessions() as session:
                await set_tenant_execution_context(session, other)
                assert not (await session.scalars(select(GeometryResourceVersionInputModel).where(
                    GeometryResourceVersionInputModel.resource_version_id == consumer.version.resource_version_id))).all()
        foreign = await _mapped_bundle(storage=storage, resource_id=prefix + "-foreign",
            resource_version_id=prefix + "-foreign-v1", version_no=1,
            scope=GeometryResourceScope(scope_kind=GeometryResourceScopeKind.TASK,
                tenant_id=other.tenant_id, task_id=other.task_id),
            created_at=datetime.now(UTC), salt="foreign", geometry_inputs=(frozen,))
        resource_ids.add(foreign.resource.resource_id)
        with pytest.raises(ConflictError, match="not visible"):
            await _publish_after_storage_validation(runtime, storage, other, foreign)
        # Remove unrelated child asset FKs inside a rolled-back transaction so the
        # assertion isolates the named-input protection itself.
        with pytest.raises(DBAPIError) as blocked:
            async with admin.begin() as connection:
                await connection.execute(delete(GeometryResourceVersionAssetModel).where(
                    GeometryResourceVersionAssetModel.resource_version_id == target.version.resource_version_id))
                await connection.execute(delete(GeometryResourceVersionModel).where(
                    GeometryResourceVersionModel.resource_version_id == target.version.resource_version_id))
        assert "geometry_resource_version_inputs" in str(blocked.value)
        async with admin.begin() as connection:
            assert not (await connection.execute(text("SELECT resource_id FROM geometry_resources WHERE resource_id = :id"),
                {"id": foreign.resource.resource_id})).all()
            await connection.execute(delete(GeometryResourceVersionInputModel).where(
                GeometryResourceVersionInputModel.resource_version_id == consumer.version.resource_version_id))
        with bind_tenant_execution_context(context), pytest.raises(RuntimeError, match="projection disagrees"):
            await runtime.repository.get_exact_geometry_resource_version(context,
                resource_id=consumer.resource.resource_id, resource_version_id=consumer.version.resource_version_id)
    finally:
        if restarted is not None:
            await restarted.engine.dispose()
        await runtime.engine.dispose()
        await _cleanup_geometry_rows(admin, resource_ids)
        await admin.dispose()
        _drop_test_runtime_logins(TEST_DATABASE_URL, logins)
