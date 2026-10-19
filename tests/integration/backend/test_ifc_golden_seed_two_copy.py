from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService
from CAD2BIM_backend.application.ifc_geometry_basis_service import (
    IfcGeometryBasisService,
)
from CAD2BIM_backend.application.ifc_model_service import IfcModelService
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcWorldCoordinateSystem3D,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
)
from tests.process_support.ifc_golden_seed.core import load_manifest, verify_seed
from tests.process_support.ifc_golden_seed.materializer import materialize_seed
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


pytestmark = [pytest.mark.integration, pytest.mark.network]


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
                    origin=(0.0, 0.0, 0.0),
                    axis=(0.0, 0.0, 1.0),
                    ref_direction=(1.0, 0.0, 0.0),
                ),
                true_north=(0.0, 1.0),
            ),
        ),
    )


def _model_service(runtime) -> IfcModelService:
    return runtime.ifc.model_service


def _geometry_service(runtime) -> IfcGeometryBasisService:
    return IfcGeometryBasisService(
        runtime.ifc.writer,
        runtime.ifc.runtime,
        runtime.ifc.coordinator,
    )


@pytest.mark.asyncio
async def test_same_seed_in_two_databases_keeps_copy_b_and_parent_immutable_when_a_writes(
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
    runtime_a = runtime_b = admin_a = admin_b = None
    restarted_a = restarted_b = None
    try:
        await source.database.verify_ready()
        seed = await export_test_seed(
            source,
            output_root=tmp_path / "two-copy-seed",
            configured_subject=f"seed-two-copy-source-{uuid4().hex}",
            session_name="Golden Seed two-copy source",
        )
        seed_root = seed.exported.root_path
        seed_digest_before = _tree_digest(seed_root)
        source_storage_before = _tree_digest(tmp_path / "source-storage")
        with bind_tenant_execution_context(seed.source_invocation_scope.identity):
            source_exact_before = await _model_service(source).get_current_exact(
                seed.source_ifc_scope
            )
        assert source_exact_before is not None
        assert source_exact_before.revision_no == 1

        try:
            with isolated_database_pair(database_url) as databases:
                database_a = make_url(databases.admin_url_a)
                database_b = make_url(databases.admin_url_b)
                assert (database_a.host, database_a.port, database_a.database) != (
                    database_b.host,
                    database_b.port,
                    database_b.database,
                )
                storage_a = tmp_path / "copy-a-storage"
                storage_b = tmp_path / "copy-b-storage"
                runtime_a = create_seed_runtime(databases.runtime_url_a, storage_a)
                runtime_b = create_seed_runtime(databases.runtime_url_b, storage_b)
                admin_a = create_seed_runtime(
                    databases.admin_url_a,
                    tmp_path / "copy-a-admin-unused-storage",
                )
                admin_b = create_seed_runtime(
                    databases.admin_url_b,
                    tmp_path / "copy-b-admin-unused-storage",
                )
                await runtime_a.database.verify_ready()
                await runtime_b.database.verify_ready()
                await admin_a.database.verify_ready()
                await admin_b.database.verify_ready()

                subject_a = f"seed-copy-a-{uuid4().hex}"
                subject_b = f"seed-copy-b-{uuid4().hex}"
                copy_a = await materialize_seed(
                    seed_root,
                    session_factory=runtime_a.database.session_factory,
                    admin_session_factory=admin_a.database.session_factory,
                    repository=runtime_a.repository,
                    identity_directory=runtime_a.identity_directory,
                    storage_root=storage_a,
                    configured_subject=subject_a,
                    session_name="Golden Seed copy A",
                )
                copy_b = await materialize_seed(
                    seed_root,
                    session_factory=runtime_b.database.session_factory,
                    admin_session_factory=admin_b.database.session_factory,
                    repository=runtime_b.repository,
                    identity_directory=runtime_b.identity_directory,
                    storage_root=storage_b,
                    configured_subject=subject_b,
                    session_name="Golden Seed copy B",
                )

                assert copy_a.source_root_sha256 == copy_b.source_root_sha256
                assert copy_a.source_root_sha256 == seed.exported.manifest["root_sha256"]
                assert copy_a.binding != seed.source_binding
                assert copy_b.binding != seed.source_binding
                assert copy_a.binding.session_id != seed.source_binding.session_id
                assert copy_b.binding.session_id != seed.source_binding.session_id
                assert copy_a.binding.task_id != seed.source_binding.task_id
                assert copy_b.binding.task_id != seed.source_binding.task_id
                assert copy_a.binding.run_id != seed.source_binding.run_id
                assert copy_b.binding.run_id != seed.source_binding.run_id
                assert copy_a.binding.direct_session_alias.startswith("direct_")
                assert copy_b.binding.direct_session_alias.startswith("direct_")

                model_a = _model_service(runtime_a)
                model_b = _model_service(runtime_b)
                with bind_tenant_execution_context(copy_a.invocation_scope.identity):
                    exact_a_before = await model_a.get_current_exact(copy_a.ifc_scope)
                    info_a_before = await model_a.info(copy_a.ifc_scope)
                with bind_tenant_execution_context(copy_b.invocation_scope.identity):
                    exact_b_before = await model_b.get_current_exact(copy_b.ifc_scope)
                    info_b_before = await model_b.info(copy_b.ifc_scope)
                assert exact_a_before is not None
                assert exact_b_before is not None
                assert exact_a_before.revision_id == exact_b_before.revision_id
                assert exact_a_before.artifact == exact_b_before.artifact
                assert exact_a_before.revision_no == exact_b_before.revision_no == 1
                assert info_a_before == info_b_before == seed.source_ifc_info
                storage_b_before = _tree_digest(storage_b)

                with bind_tenant_execution_context(copy_a.invocation_scope.identity):
                    write_a = await _geometry_service(runtime_a).configure(
                        copy_a.ifc_scope,
                        _geometry_basis_command(),
                        idempotency_key=f"seed-copy-a-write:{uuid4().hex}",
                    )
                    exact_a_after = await model_a.get_current_exact(copy_a.ifc_scope)
                    info_a_after = await model_a.info(copy_a.ifc_scope)
                with bind_tenant_execution_context(copy_b.invocation_scope.identity):
                    exact_b_after = await model_b.get_current_exact(copy_b.ifc_scope)
                    info_b_after = await model_b.info(copy_b.ifc_scope)

                assert write_a.model == info_a_after
                assert info_a_after.geometry_basis_configured is True
                assert exact_a_after is not None
                assert exact_a_after.revision_no == 2
                assert exact_b_after == exact_b_before
                assert info_b_after == info_b_before
                assert _tree_digest(storage_b) == storage_b_before

                data_b = DataRuntimeService(
                    runtime_b.repository,
                    ArtifactService(runtime_b.repository, runtime_b.storage),
                )
                with bind_tenant_execution_context(copy_b.invocation_scope.identity):
                    catalog_b = await data_b.get_catalog(copy_b.invocation_scope)
                assert write_a.operation_alias not in {
                    operation.catalog_entry.alias for operation in catalog_b.operations
                }

                await runtime_a.close()
                runtime_a = None
                await runtime_b.close()
                runtime_b = None
                await admin_a.close()
                admin_a = None
                await admin_b.close()
                admin_b = None
                restarted_a = create_seed_runtime(databases.runtime_url_a, storage_a)
                restarted_b = create_seed_runtime(databases.runtime_url_b, storage_b)
                await restarted_a.database.verify_ready()
                await restarted_b.database.verify_ready()
                reopened_a = await open_seed_scope(
                    restarted_a,
                    configured_subject=subject_a,
                    session_name="Golden Seed copy A",
                )
                reopened_b = await open_seed_scope(
                    restarted_b,
                    configured_subject=subject_b,
                    session_name="Golden Seed copy B",
                )
                assert reopened_a.binding == copy_a.binding
                assert reopened_b.binding == copy_b.binding
                with bind_tenant_execution_context(reopened_a.invocation_scope.identity):
                    restarted_info_a = await _model_service(restarted_a).info(copy_a.ifc_scope)
                with bind_tenant_execution_context(reopened_b.invocation_scope.identity):
                    restarted_info_b = await _model_service(restarted_b).info(copy_b.ifc_scope)
                assert restarted_info_a == info_a_after
                assert restarted_info_b == info_b_before
        except IsolatedDatabaseUnavailable as exc:
            pytest.skip(str(exc))

        verify_seed(load_manifest(seed_root / "manifest.json"), seed_root)
        assert _tree_digest(seed_root) == seed_digest_before
        assert _tree_digest(tmp_path / "source-storage") == source_storage_before
        with bind_tenant_execution_context(seed.source_invocation_scope.identity):
            source_exact_after = await _model_service(source).get_current_exact(
                seed.source_ifc_scope
            )
            source_info_after = await _model_service(source).info(seed.source_ifc_scope)
        assert source_exact_after == source_exact_before
        assert source_info_after.project.name == TEST_PROJECT_NAME
        assert source_info_after.geometry_basis_configured is False
    finally:
        for runtime in (
            restarted_a,
            restarted_b,
            runtime_a,
            runtime_b,
            admin_a,
            admin_b,
        ):
            if runtime is not None:
                await runtime.close()
        await source.close()
