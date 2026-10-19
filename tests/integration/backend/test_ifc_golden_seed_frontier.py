from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService
from CAD2BIM_backend.domain.data_runtime import DirectSessionBinding
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
)
from tests.process_support.ifc_golden_seed.closure import (
    SeedClosureError,
    SeedExportScope,
    SeedSnapshot,
    build_export_closure,
)
from tests.process_support.ifc_golden_seed.materializer import materialize_seed
from tests.process_support.ifc_golden_seed.postgres_adapter import (
    PostgresSeedSnapshotReader,
)
from tests.process_support.ifc_golden_seed.test_support import (
    IsolatedDatabaseUnavailable,
    TEST_PROJECT_NAME,
    create_seed_runtime,
    export_test_seed,
    isolated_database_pair,
    open_seed_scope,
    provision_seed_runtime_roles,
    runtime_database_url,
)


pytestmark = pytest.mark.integration


def _scope() -> SeedExportScope:
    return SeedExportScope.normalized(
        tenant_id="tenant-frontier",
        principal_id="principal-frontier",
        owner_id="tenant-frontier",
        task_id="task-frontier",
        run_id="run-frontier",
    )


def _scoped(scope: SeedExportScope, **values: object) -> dict[str, object]:
    return {
        "tenant_id": scope.tenant_id,
        "owner_id": scope.owner_id,
        "task_id": scope.task_id,
        **values,
    }


def _snapshot(*, visible_operation_result: str = "package-visible") -> SeedSnapshot:
    scope = _scope()
    visible_bytes = b"visible exact payload"
    late_bytes = b"late exact payload"
    visible_sha256 = hashlib.sha256(visible_bytes).hexdigest()
    late_sha256 = hashlib.sha256(late_bytes).hexdigest()
    return SeedSnapshot.from_rows(
        catalog_entries=(
            _scoped(
                scope,
                catalog_entry_id="catalog-package-visible",
                run_id=scope.run_id,
                resource_kind="package",
                package_id="package-visible",
                operation_id=None,
                alias="data_001",
                ordinal=1,
                catalog_revision=1,
            ),
            _scoped(
                scope,
                catalog_entry_id="catalog-operation-visible",
                run_id=scope.run_id,
                resource_kind="operation",
                package_id=None,
                operation_id="operation-visible",
                alias="op_001",
                ordinal=1,
                catalog_revision=1,
            ),
            _scoped(
                scope,
                catalog_entry_id="catalog-package-late",
                run_id=scope.run_id,
                resource_kind="package",
                package_id="package-late",
                operation_id=None,
                alias="data_002",
                ordinal=2,
                catalog_revision=2,
            ),
            _scoped(
                scope,
                catalog_entry_id="catalog-operation-late",
                run_id=scope.run_id,
                resource_kind="operation",
                package_id=None,
                operation_id="operation-late",
                alias="op_002",
                ordinal=2,
                catalog_revision=2,
            ),
        ),
        packages=(
            _scoped(
                scope,
                package_id="package-visible",
                created_by_run_id=scope.run_id,
            ),
            _scoped(
                scope,
                package_id="package-late",
                created_by_run_id=scope.run_id,
            ),
        ),
        package_members=(
            _scoped(
                scope,
                member_id="member-visible",
                package_id="package-visible",
                member_name="payloads/visible.parquet",
                artifact_id="artifact-visible",
                artifact_version_id="version-visible",
                artifact_sha256=visible_sha256,
            ),
            _scoped(
                scope,
                member_id="member-late",
                package_id="package-late",
                member_name="payloads/late.parquet",
                artifact_id="artifact-late",
                artifact_version_id="version-late",
                artifact_sha256=late_sha256,
            ),
        ),
        operations=(
            _scoped(
                scope,
                operation_id="operation-visible",
                run_id=scope.run_id,
                result_package_id=visible_operation_result,
                repair_parent_operation_id=None,
                code_artifact_id=None,
                code_artifact_version_id=None,
                description_artifact_id=None,
                description_artifact_version_id=None,
            ),
            _scoped(
                scope,
                operation_id="operation-late",
                run_id=scope.run_id,
                result_package_id="package-late",
                repair_parent_operation_id=None,
                code_artifact_id=None,
                code_artifact_version_id=None,
                description_artifact_id=None,
                description_artifact_version_id=None,
            ),
        ),
        operation_inputs=(),
        artifacts=(
            _scoped(
                scope,
                artifact_id="artifact-visible",
                current_version_id="version-visible",
            ),
            _scoped(
                scope,
                artifact_id="artifact-late",
                current_version_id="version-late",
            ),
        ),
        artifact_versions=(
            _scoped(
                scope,
                artifact_id="artifact-visible",
                version_id="version-visible",
                version_no=1,
                size_bytes=len(visible_bytes),
                sha256=visible_sha256,
                content_type="application/octet-stream",
            ),
            _scoped(
                scope,
                artifact_id="artifact-late",
                version_id="version-late",
                version_no=1,
                size_bytes=len(late_bytes),
                sha256=late_sha256,
                content_type="application/octet-stream",
            ),
        ),
    )


def test_export_closure_excludes_every_resource_beyond_the_fixed_frontier() -> None:
    closure = build_export_closure(
        _snapshot(),
        _scope(),
        catalog_frontier=1,
        include_ifc=False,
    )

    assert closure.catalog_frontier == 1
    assert closure.operation_frontier == 1
    assert {row["package_id"] for row in closure.packages} == {"package-visible"}
    assert {row["member_id"] for row in closure.package_members} == {"member-visible"}
    assert {row["operation_id"] for row in closure.operations} == {"operation-visible"}
    assert {row["artifact_id"] for row in closure.artifacts} == {"artifact-visible"}
    assert {(row["artifact_id"], row["version_id"]) for row in closure.artifact_versions} == {
        ("artifact-visible", "version-visible")
    }
    assert all(row["catalog_revision"] <= 1 for row in closure.catalog_entries)
    database_payload = json.dumps(closure.database_document(_scope()), sort_keys=True)
    for late_identity in (
        "package-late",
        "member-late",
        "operation-late",
        "artifact-late",
        "version-late",
    ):
        assert late_identity not in database_payload


def test_export_closure_rejects_a_visible_operation_that_reaches_past_frontier() -> None:
    with pytest.raises(
        SeedClosureError,
        match="reachable resource lies beyond the catalog frontier",
    ):
        build_export_closure(
            _snapshot(visible_operation_result="package-late"),
            _scope(),
            catalog_frontier=1,
            include_ifc=False,
        )


@pytest.mark.asyncio
@pytest.mark.network
async def test_fixed_frontier_restores_through_formal_reads_after_resource_rebuild(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_seed_runtime_roles(database_url)
    runtime_url = runtime_database_url(database_url)
    source = create_seed_runtime(runtime_url, tmp_path / "source-storage")
    target_storage = tmp_path / "target-storage"
    target = None
    target_admin = None
    restarted = None
    try:
        await source.database.verify_ready()
        seed = await export_test_seed(
            source,
            output_root=tmp_path / "frontier-seed",
            configured_subject=f"seed-frontier-source-{uuid4().hex}",
            session_name="Golden Seed frontier source",
        )
        source_admin_engine = create_async_engine(database_url, pool_pre_ping=True)
        try:
            resolved, consistent_snapshot = await PostgresSeedSnapshotReader(
                source_admin_engine
            ).resolve_and_load(seed.source_binding.run_id)
        finally:
            await source_admin_engine.dispose()
        assert resolved.scope.run_id == seed.source_binding.run_id
        assert resolved.direct_session_alias == seed.source_binding.direct_session_alias
        assert len(consistent_snapshot.packages) == 2
        assert len(consistent_snapshot.operations) == 3
        target_subject = f"seed-frontier-target-{uuid4().hex}"
        try:
            with isolated_database_pair(database_url) as databases:
                target = create_seed_runtime(databases.runtime_url_a, target_storage)
                target_admin = create_seed_runtime(
                    databases.admin_url_a,
                    tmp_path / "target-admin-unused-storage",
                )
                await target.database.verify_ready()
                await target_admin.database.verify_ready()
                restored = await materialize_seed(
                    seed.exported.root_path,
                    session_factory=target.database.session_factory,
                    admin_session_factory=target_admin.database.session_factory,
                    repository=target.repository,
                    identity_directory=target.identity_directory,
                    storage_root=target_storage,
                    configured_subject=target_subject,
                    session_name="Golden Seed frontier restored",
                )

                assert isinstance(restored.binding, DirectSessionBinding)
                assert restored.binding != seed.source_binding
                assert restored.binding.session_id != seed.source_binding.session_id
                assert restored.binding.task_id != seed.source_binding.task_id
                assert restored.binding.run_id != seed.source_binding.run_id
                assert restored.binding.configured_subject == target_subject
                assert restored.binding.direct_session_alias.startswith("direct_")
                assert (
                    seed.exported.manifest["source_direct_session_alias"]
                    == seed.source_binding.direct_session_alias
                )

                data = DataRuntimeService(
                    target.repository,
                    ArtifactService(target.repository, restored.storage),
                )
                model = target.ifc.model_service
                with bind_tenant_execution_context(restored.invocation_scope.identity):
                    catalog = await data.get_catalog(restored.invocation_scope)
                    info = await model.info(restored.ifc_scope)
                package_aliases = {package.catalog_entry.alias for package in catalog.packages}
                operation_aliases = {
                    operation.catalog_entry.alias for operation in catalog.operations
                }
                assert package_aliases == {seed.visible_package_alias}
                assert seed.visible_operation_alias in operation_aliases
                assert seed.late_package_alias not in package_aliases
                assert seed.late_operation_alias not in operation_aliases
                assert info.project.name == TEST_PROJECT_NAME

                await target.close()
                target = None
                await target_admin.close()
                target_admin = None
                restarted = create_seed_runtime(databases.runtime_url_a, target_storage)
                await restarted.database.verify_ready()
                reopened = await open_seed_scope(
                    restarted,
                    configured_subject=target_subject,
                    session_name="Golden Seed frontier restored",
                )
                assert reopened.binding == restored.binding
                restarted_data = DataRuntimeService(
                    restarted.repository,
                    ArtifactService(restarted.repository, restarted.storage),
                )
                restarted_model = restarted.ifc.model_service
                with bind_tenant_execution_context(reopened.invocation_scope.identity):
                    restarted_catalog = await restarted_data.get_catalog(reopened.invocation_scope)
                    restarted_info = await restarted_model.info(restored.ifc_scope)
                assert {
                    package.catalog_entry.alias for package in restarted_catalog.packages
                } == package_aliases
                assert {
                    operation.catalog_entry.alias for operation in restarted_catalog.operations
                } == operation_aliases
                assert restarted_info == info == seed.source_ifc_info
        except IsolatedDatabaseUnavailable as exc:
            pytest.skip(str(exc))
    finally:
        if restarted is not None:
            await restarted.close()
        if target is not None:
            await target.close()
        if target_admin is not None:
            await target_admin.close()
        await source.close()
