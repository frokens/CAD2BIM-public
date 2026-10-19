"""Focused lifecycle, RLS and deletion/reference races in an isolated database."""

import asyncio
from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.domain.geometry_resource import GeometryResourceScope, GeometryResourceScopeKind
from CAD2BIM_backend.domain.product_family import ProductFamilyRecord
from CAD2BIM_backend.domain.identity import TenantActorKind
from CAD2BIM_backend.domain.resource_scope import ResourceDeletionError
from CAD2BIM_backend.infrastructure.postgres.models import (
    GeometryResourceModel, GeometryResourceVersionModel, ProductFamilyModel, ProductFamilyVersionModel,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context, set_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from tests.integration.backend.test_geometry_query_postgres import (
    _create_logins, _drop_logins, _open_context, _runtime, _runtime_url,
)
from tests.integration.backend.test_geometry_resource_postgres import _mapped_bundle, _insert_admin_bundle
from tests.integration.backend.test_product_family_postgres import (
    _current_clean_geometry_bundle, _current_repository_family_bundle, _family_bundle, _insert_admin_family_bundle,
)

pytestmark = [pytest.mark.integration, pytest.mark.network]


@pytest.mark.asyncio
async def test_library_root_deletion_preserves_history_and_enforces_rls_and_reference_races(tmp_path):
    database_url = os.environ.get("CAD2BIM_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("isolated migrated CAD2BIM_TEST_DATABASE_URL is required")
    logins = _create_logins(database_url)
    admin = create_async_engine(database_url)
    mcp = _runtime(_runtime_url(database_url, logins["mcp"]))
    api = _runtime(_runtime_url(database_url, logins["api"]))
    storage = LocalStorage(tmp_path / "storage")
    try:
        identity = await _open_context(mcp, subject="library-delete-owner", session_name="library-delete-task")
        other_task = await _open_context(mcp, subject="library-delete-owner", session_name="library-delete-other-task")
        foreign = await _open_context(mcp, subject="library-delete-foreign", session_name="library-delete-foreign-task")

        async def geometry(owner_scope, name):
            bundle = _current_clean_geometry_bundle(await _mapped_bundle(storage=storage,
                resource_id=f"delete-{name}-{uuid4().hex}", resource_version_id=f"delete-gv-{uuid4().hex}",
                version_no=1, scope=owner_scope, created_at=datetime.now(UTC), salt=name))
            await _insert_admin_bundle(admin, bundle)
            return bundle

        async def family(geometry_bundle, name, version_no=1, family_root=None):
            root = family_root or ProductFamilyRecord(family_id=f"delete-{name}-{uuid4().hex}",
                owner_scope=geometry_bundle.resource.owner_scope, created_at=datetime.now(UTC))
            return _current_repository_family_bundle(_family_bundle(family=root,
                family_version_id=f"delete-fv-{uuid4().hex}", version_no=version_no,
                geometry_resource_id=geometry_bundle.resource.resource_id,
                geometry_resource_version_id=geometry_bundle.version.resource_version_id,
                geometry_content_digest=geometry_bundle.version.content_digest, created_at=datetime.now(UTC), formal=True))

        tenant_scope = GeometryResourceScope(scope_kind=GeometryResourceScopeKind.TENANT, tenant_id=identity.tenant_id)
        used = await geometry(tenant_scope, "used")
        free = await geometry(tenant_scope, "free")
        system = await geometry(GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM), "system")
        task_geometry = await geometry(GeometryResourceScope(scope_kind=GeometryResourceScopeKind.TASK,
            tenant_id=identity.tenant_id, task_id=identity.task_id), "task")
        dependent = await family(used, "family")
        await _insert_admin_family_bundle(admin, dependent, insert_root=True)
        # The latest Family version uses another Geometry. An old version still
        # protects the original resource while this Family remains in the library.
        newer = await family(free, "family", version_no=2, family_root=dependent.family)
        await _insert_admin_family_bundle(admin, newer, insert_root=False)
        system_family = await family(system, "system-family")
        await _insert_admin_family_bundle(admin, system_family, insert_root=True)
        with bind_tenant_execution_context(foreign):
            assert not await api.repository.delete_geometry_resource(foreign, resource_id=free.resource.resource_id)
            assert not await api.repository.delete_product_family(foreign, family_id=dependent.family.family_id)
        with bind_tenant_execution_context(other_task):
            assert not await api.repository.delete_geometry_resource(other_task, resource_id=task_geometry.resource.resource_id)
        with bind_tenant_execution_context(identity):
            for kind, target in (("geometry", system.resource.resource_id), ("family", system_family.family.family_id)):
                with pytest.raises(ResourceDeletionError) as error:
                    if kind == "geometry":
                        await api.repository.delete_geometry_resource(identity, resource_id=target)
                    else:
                        await api.repository.delete_product_family(identity, family_id=target)
                assert error.value.code == "LIBRARY_SYSTEM_RESOURCE"
            with pytest.raises(ResourceDeletionError) as error:
                await api.repository.delete_geometry_resource(identity, resource_id=used.resource.resource_id)
            assert error.value.code == "LIBRARY_GEOMETRY_IN_USE"
        # Browser requests with an existing Run restore its saved RUNNER identity
        # for scope-bound references, after the endpoint authenticates the user.
        saved_identity = identity.model_copy(update={"actor_kind": TenantActorKind.RUNNER})
        with bind_tenant_execution_context(saved_identity):
            assert await api.repository.delete_product_family(saved_identity, family_id=dependent.family.family_id)
        with bind_tenant_execution_context(identity):
            assert await api.repository.get_product_family(identity, family_id=dependent.family.family_id) is None
            assert await api.repository.get_product_family_version_by_id(identity,
                family_version_id=dependent.version.family_version_id) is None
            assert await api.repository.delete_geometry_resource(identity, resource_id=used.resource.resource_id)
            assert await api.repository.get_geometry_resource_version_by_id(identity,
                resource_version_id=used.version.resource_version_id) is None
            assert not await api.repository.delete_geometry_resource(identity, resource_id=used.resource.resource_id)
            # Deletion wins the lock. A concurrent new Family cannot commit a
            # binding to the removed resource after the lock becomes available.
            rejected_family = await family(free, "racing-family")
            async with api.sessions() as session, session.begin():
                await set_tenant_execution_context(session, identity)
                assert await session.scalar(select(func.cad2bim_delete_library_resource("geometry", free.resource.resource_id))) == "deleted"
                publication = asyncio.create_task(_insert_admin_family_bundle(admin, rejected_family, insert_root=True))
                # Observe PostgreSQL actually waiting on the shared advisory
                # lock, rather than inferring a race from elapsed time alone.
                async def wait_for_binding_lock():
                    async with admin.connect() as connection:
                        while not await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_locks "
                            "WHERE locktype='advisory' AND NOT granted AND database="
                            "(SELECT oid FROM pg_database WHERE datname=current_database()))")):
                            await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_binding_lock(), timeout=5)
                assert not publication.done()
            with pytest.raises(DBAPIError):
                await asyncio.wait_for(publication, timeout=5)

        async with admin.connect() as connection:
            assert await connection.scalar(select(func.count()).select_from(GeometryResourceVersionModel)
                .where(GeometryResourceVersionModel.resource_id == used.resource.resource_id)) == 1
            assert await connection.scalar(select(func.count()).select_from(ProductFamilyVersionModel)
                .where(ProductFamilyVersionModel.family_id == dependent.family.family_id)) == 2
            assert await connection.scalar(select(GeometryResourceModel.deleted_at)
                .where(GeometryResourceModel.resource_id == used.resource.resource_id)) is not None
            assert await connection.scalar(select(ProductFamilyModel.deleted_at)
                .where(ProductFamilyModel.family_id == system_family.family.family_id)) is None
    finally:
        await api.close()
        await mcp.close()
        await admin.dispose()
        _drop_logins(database_url, logins)
