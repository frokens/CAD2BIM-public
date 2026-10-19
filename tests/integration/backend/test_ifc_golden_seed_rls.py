from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeApplicationError,
    DataRuntimeService,
    DirectScopeResolver,
)
from CAD2BIM_backend.application.errors import ConflictError
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
)
from tests.process_support.ifc_golden_seed.materializer import materialize_seed
from tests.process_support.ifc_golden_seed.test_support import (
    IsolatedDatabaseUnavailable,
    create_seed_runtime,
    export_test_seed,
    isolated_database_pair,
    open_seed_scope,
    provision_seed_runtime_roles,
    runtime_database_url,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]


@pytest.mark.asyncio
async def test_restored_seed_rejects_cross_tenant_task_and_run_authority(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_seed_runtime_roles(database_url)
    source = create_seed_runtime(
        runtime_database_url(database_url),
        tmp_path / "source-storage",
    )
    target = None
    target_admin = None
    try:
        await source.database.verify_ready()
        seed = await export_test_seed(
            source,
            output_root=tmp_path / "rls-seed",
            configured_subject=f"seed-rls-source-{uuid4().hex}",
            session_name="Golden Seed RLS source",
        )
        try:
            with isolated_database_pair(database_url) as databases:
                target_storage = tmp_path / "target-storage"
                target = create_seed_runtime(databases.runtime_url_a, target_storage)
                target_admin = create_seed_runtime(
                    databases.admin_url_a,
                    tmp_path / "target-admin-unused-storage",
                )
                await target.database.verify_ready()
                await target_admin.database.verify_ready()
                target_subject = f"seed-rls-target-{uuid4().hex}"
                restored = await materialize_seed(
                    seed.exported.root_path,
                    session_factory=target.database.session_factory,
                    admin_session_factory=target_admin.database.session_factory,
                    repository=target.repository,
                    identity_directory=target.identity_directory,
                    storage_root=target_storage,
                    configured_subject=target_subject,
                    session_name="Golden Seed RLS restored",
                )
                data = DataRuntimeService(
                    target.repository,
                    ArtifactService(target.repository, restored.storage),
                )
                with bind_tenant_execution_context(restored.invocation_scope.identity):
                    baseline = await data.get_catalog(restored.invocation_scope)
                assert baseline.packages
                assert baseline.operations

                sibling = await open_seed_scope(
                    target,
                    configured_subject=target_subject,
                    session_name="Golden Seed sibling task",
                )
                wrong_task = restored.invocation_scope.model_copy(
                    update={
                        "identity": restored.invocation_scope.identity.model_copy(
                            update={"task_id": sibling.invocation_scope.identity.task_id}
                        )
                    }
                )
                with bind_tenant_execution_context(wrong_task.identity):
                    with pytest.raises(ConflictError, match="authoritative Run"):
                        await data.get_catalog(wrong_task)

                wrong_run = restored.invocation_scope.model_copy(
                    update={"run_id": sibling.invocation_scope.run_id}
                )
                with bind_tenant_execution_context(wrong_run.identity):
                    with pytest.raises(ConflictError, match="authoritative Run"):
                        await data.get_catalog(wrong_run)

                other_subject = f"seed-rls-other-tenant-{uuid4().hex}"
                other_resolver = DirectScopeResolver(
                    target.repository,
                    target.identity_directory,
                    other_subject,
                    bind_tenant_execution_context,
                )
                with pytest.raises(DataRuntimeApplicationError) as hidden_binding:
                    await other_resolver.resolve(
                        restored.binding.direct_session_alias,
                        command_id=f"seed-rls-hidden:{uuid4().hex}",
                    )
                assert hidden_binding.value.code == "DIRECT_SESSION_NOT_FOUND"

                other = await open_seed_scope(
                    target,
                    configured_subject=other_subject,
                    session_name="Golden Seed other tenant",
                )
                assert other.binding != restored.binding
                assert other.binding.task_id != restored.binding.task_id
                assert other.binding.run_id != restored.binding.run_id
                cross_tenant = restored.invocation_scope.model_copy(
                    update={"identity": other.invocation_scope.identity}
                )
                with bind_tenant_execution_context(cross_tenant.identity):
                    with pytest.raises(ConflictError, match="authoritative Run"):
                        await data.get_catalog(cross_tenant)
        except IsolatedDatabaseUnavailable as exc:
            pytest.skip(str(exc))
    finally:
        if target is not None:
            await target.close()
        if target_admin is not None:
            await target_admin.close()
        await source.close()
