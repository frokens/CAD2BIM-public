from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import ifcopenshell
import ifcopenshell.util.placement
import numpy as np
import pytest
from sqlalchemy import func, select

from CAD2BIM_backend.application.ifc_spatial_structure_service import (
    IfcSpatialStructureService,
)
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    DeleteIfcSpatialOperation,
    IfcSpatialNotEmptyError,
    IfcSpatialStructureListQuery,
    SetIfcSpatialPlacementOperation,
)
from CAD2BIM_backend.domain.ifc_model import IfcModelNotInitializedError
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.ifc.codec import (
    MAX_IFC_REVISION_BYTES,
    IfcOpenShellModelCodec,
)
from CAD2BIM_backend.infrastructure.ifc.spatial_structure import (
    IfcOpenShellSpatialStructureCompiler,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    IfcModelModel,
    IfcModelRevisionModel,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
)
from tests.process_support.ifc_golden_seed.golden_candidate import (
    materialize_golden_candidate,
)
from tests.process_support.ifc_golden_seed.test_support import (
    IsolatedDatabaseUnavailable,
    SeedTestRuntime,
    create_seed_runtime,
    ifc_scope,
    isolated_database_pair,
    open_seed_scope,
    provision_seed_runtime_roles,
)
from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack


pytestmark = pytest.mark.integration

_GOLDEN_CANDIDATE = Path(
    "/home/example/代码/CAD2BIM-worktrees/ifc-golden-baseline-validation/"
    "validation/output/golden-baseline.ifc"
)
_GOLDEN_CANDIDATE_SHA256 = "02aade531690f7a4624f64ed0f10ba47b416f052f223160c33c9c7dbb2a210a7"
_RUN_GOLDEN_MAINTENANCE = "CAD2BIM_RUN_G1_GOLDEN_CANDIDATE_MAINTENANCE"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _open_exact(content: bytes) -> Any:
    return ifcopenshell.file.from_string(content.decode("utf-8"))


def _candidate_or_skip() -> bytes:
    _candidate_path_or_skip()
    return _GOLDEN_CANDIDATE.read_bytes()


def _candidate_path_or_skip() -> Path:
    if os.getenv(_RUN_GOLDEN_MAINTENANCE, "").strip() != "1":
        pytest.skip(f"set {_RUN_GOLDEN_MAINTENANCE}=1 for the 231 MiB maintenance scenario")
    if not _GOLDEN_CANDIDATE.is_file():
        pytest.skip("the isolated Golden Candidate worktree is not present")
    with _GOLDEN_CANDIDATE.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == _GOLDEN_CANDIDATE_SHA256
    return _GOLDEN_CANDIDATE


def _spatial_service(runtime: SeedTestRuntime) -> IfcSpatialStructureService:
    codec = IfcOpenShellModelCodec()
    stack = build_in_process_ifc_stack(
        runtime.repository,
        runtime.storage,
        codec=codec,
    )
    return IfcSpatialStructureService(
        stack.writer,
        CursorCodec("A" * 43),
        stack.runtime,
        stack.coordinator,
    )


async def _visible_ifc_counts(
    runtime: SeedTestRuntime,
    *,
    invocation: Any,
    run_id: str,
) -> tuple[int, int]:
    with bind_tenant_execution_context(invocation.identity):
        async with runtime.database.session_factory() as session:
            model_count = await session.scalar(
                select(func.count())
                .select_from(IfcModelModel)
                .where(IfcModelModel.run_id == run_id)
            )
            revision_count = await session.scalar(
                select(func.count())
                .select_from(IfcModelRevisionModel)
                .where(IfcModelRevisionModel.run_id == run_id)
            )
    return int(model_count or 0), int(revision_count or 0)


def _direct_contained_products(model: Any, site: Any) -> tuple[Any, ...]:
    return tuple(
        product
        for relationship in tuple(site.ContainsElements or ())
        for product in tuple(relationship.RelatedElements or ())
    )


def test_g1_b_golden_candidate_list_blocker_and_product_site_move_are_copy_only() -> None:
    content = _candidate_or_skip()
    original_digest = _sha256(content)
    compiler = IfcOpenShellSpatialStructureCompiler()

    page = compiler.list_spatial_structure(
        content,
        IfcSpatialStructureListQuery(spatial_classes=("site",), page_size=64),
    )
    assert page.returned_count == 3
    assert page.next_after is None
    target = min(
        (item for item in page.items if item.blockers.contained_element_count),
        key=lambda item: item.blockers.contained_element_count,
    )

    with pytest.raises(IfcSpatialNotEmptyError) as blocked:
        compiler.apply(
            content,
            ApplyIfcSpatialStructureCommand(
                operations=(DeleteIfcSpatialOperation(target_global_id=target.global_id),)
            ),
        )
    assert blocked.value.blockers.contained_element_count > 0
    assert _sha256(content) == original_digest
    assert _sha256(_GOLDEN_CANDIDATE.read_bytes()) == _GOLDEN_CANDIDATE_SHA256

    before_model = _open_exact(content)
    before_site = before_model.by_guid(target.global_id)
    before_products = _direct_contained_products(before_model, before_site)
    before_product_ids = {str(product.GlobalId) for product in before_products}
    before_world = {
        str(product.GlobalId): np.asarray(
            ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement),
            dtype=float,
        )
        for product in before_products
    }
    old_site_matrix = np.asarray(
        ifcopenshell.util.placement.get_local_placement(before_site.ObjectPlacement),
        dtype=float,
    )
    moved_placement = target.project_world_placement.model_copy(
        update={
            "origin": (
                target.project_world_placement.origin[0] + 10.0,
                target.project_world_placement.origin[1] - 5.0,
                target.project_world_placement.origin[2] + 2.0,
            )
        }
    )
    # Do not retain a second 231 MiB IfcOpenShell graph while the compiler
    # opens its private candidate copy.
    del before_products, before_site, before_model
    moved = compiler.apply(
        content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=target.global_id,
                    project_world_placement=moved_placement,
                ),
            )
        ),
    )
    after_model = _open_exact(moved.content)
    after_site = after_model.by_guid(target.global_id)
    after_products = _direct_contained_products(after_model, after_site)
    assert {str(product.GlobalId) for product in after_products} == before_product_ids
    new_site_matrix = np.asarray(
        ifcopenshell.util.placement.get_local_placement(after_site.ObjectPlacement),
        dtype=float,
    )
    delta = new_site_matrix @ np.linalg.inv(old_site_matrix)
    for product in after_products:
        global_id = str(product.GlobalId)
        actual = np.asarray(
            ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement),
            dtype=float,
        )
        assert np.allclose(actual, delta @ before_world[global_id], atol=1e-7, rtol=0.0)

    # The source Candidate is a read-only fixture. Passing its bytes to a
    # private compiler never changes the retained worktree or upgrades its
    # status from Candidate to user-approved Golden.
    assert _sha256(content) == original_digest
    assert _sha256(_GOLDEN_CANDIDATE.read_bytes()) == _GOLDEN_CANDIDATE_SHA256


@pytest.mark.network
@pytest.mark.asyncio
async def test_g1_b_golden_candidate_is_durable_run_scoped_and_restart_readable(
    tmp_path: Path,
) -> None:
    source_path = _candidate_path_or_skip()
    database_url = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_seed_runtime_roles(database_url)

    runtime: SeedTestRuntime | None = None
    restarted: SeedTestRuntime | None = None
    try:
        with isolated_database_pair(database_url) as databases:
            storage_root = tmp_path / "golden-candidate-storage"
            runtime = create_seed_runtime(
                databases.runtime_url_a,
                storage_root,
                storage_max_size_bytes=MAX_IFC_REVISION_BYTES,
            )
            await runtime.database.verify_ready()
            subject = f"g1-golden-candidate-{uuid4().hex}"
            session_name = "I0-B2 G1-B durable Golden Candidate"
            materialized = await materialize_golden_candidate(
                runtime,
                source_path=source_path,
                expected_sha256=_GOLDEN_CANDIDATE_SHA256,
                configured_subject=subject,
                session_name=session_name,
            )
            invocation = materialized.opened_scope.invocation_scope
            scope = ifc_scope(invocation)
            assert materialized.exact_revision.scope == scope
            assert materialized.exact_revision.revision_no == 1
            assert materialized.exact_revision.write_epoch == 1
            assert materialized.exact_revision.artifact.sha256 == _GOLDEN_CANDIDATE_SHA256
            assert await _visible_ifc_counts(
                runtime,
                invocation=invocation,
                run_id=scope.run_id,
            ) == (1, 1)

            model_service = build_in_process_ifc_stack(
                runtime.repository,
                runtime.storage,
            ).model_service
            with bind_tenant_execution_context(invocation.identity):
                exact_info = await model_service.get_exact_info(
                    scope,
                    revision_id=materialized.exact_revision.revision_id,
                )
            assert exact_info.site_count == 3

            spatial = _spatial_service(runtime)
            with bind_tenant_execution_context(invocation.identity):
                page = await spatial.list(
                    scope,
                    IfcSpatialStructureListQuery(
                        spatial_classes=("site",),
                        page_size=64,
                    ),
                    None,
                )
            assert page.returned_count == 3
            target = min(
                (item for item in page.items if item.blockers.contained_element_count),
                key=lambda item: item.blockers.contained_element_count,
            )

            with bind_tenant_execution_context(invocation.identity):
                with pytest.raises(IfcSpatialNotEmptyError) as blocked:
                    await spatial.apply(
                        scope,
                        ApplyIfcSpatialStructureCommand(
                            operations=(
                                DeleteIfcSpatialOperation(
                                    target_global_id=target.global_id,
                                ),
                            )
                        ),
                        idempotency_key="g1-golden-delete-nonempty",
                    )
                after_failed_delete = await runtime.repository.get_current(scope)
            assert blocked.value.blockers.contained_element_count > 0
            assert after_failed_delete == materialized.exact_revision
            assert await _visible_ifc_counts(
                runtime,
                invocation=invocation,
                run_id=scope.run_id,
            ) == (1, 1)

            moved_placement = target.project_world_placement.model_copy(
                update={
                    "origin": (
                        target.project_world_placement.origin[0] + 10.0,
                        target.project_world_placement.origin[1] - 5.0,
                        target.project_world_placement.origin[2] + 2.0,
                    )
                }
            )
            with bind_tenant_execution_context(invocation.identity):
                moved = await spatial.apply(
                    scope,
                    ApplyIfcSpatialStructureCommand(
                        operations=(
                            SetIfcSpatialPlacementOperation(
                                target_global_id=target.global_id,
                                project_world_placement=moved_placement,
                            ),
                        )
                    ),
                    idempotency_key="g1-golden-safe-site-move",
                )
                moved_exact = await runtime.repository.get_current(scope)
            assert moved.applied_count == 1
            assert moved_exact is not None
            assert moved_exact.revision_no == 2
            assert moved_exact.write_epoch == 2
            assert moved_exact.artifact.sha256 != materialized.exact_revision.artifact.sha256
            assert await _visible_ifc_counts(
                runtime,
                invocation=invocation,
                run_id=scope.run_id,
            ) == (1, 2)

            sibling = await open_seed_scope(
                runtime,
                configured_subject=subject,
                session_name="I0-B2 G1-B sibling Run",
            )
            sibling_scope = ifc_scope(sibling.invocation_scope)
            with bind_tenant_execution_context(sibling.invocation_scope.identity):
                with pytest.raises(IfcModelNotInitializedError):
                    await spatial.list(
                        sibling_scope,
                        IfcSpatialStructureListQuery(page_size=64),
                        None,
                    )
            assert await _visible_ifc_counts(
                runtime,
                invocation=sibling.invocation_scope,
                run_id=scope.run_id,
            ) == (0, 0)

            other_tenant = await open_seed_scope(
                runtime,
                configured_subject=f"g1-golden-other-tenant-{uuid4().hex}",
                session_name="I0-B2 G1-B isolated Tenant",
            )
            other_tenant_scope = ifc_scope(other_tenant.invocation_scope)
            with bind_tenant_execution_context(other_tenant.invocation_scope.identity):
                with pytest.raises(IfcModelNotInitializedError):
                    await spatial.list(
                        other_tenant_scope,
                        IfcSpatialStructureListQuery(page_size=64),
                        None,
                    )
            assert await _visible_ifc_counts(
                runtime,
                invocation=other_tenant.invocation_scope,
                run_id=scope.run_id,
            ) == (0, 0)

            await runtime.close()
            runtime = None
            restarted = create_seed_runtime(
                databases.runtime_url_a,
                storage_root,
                storage_max_size_bytes=MAX_IFC_REVISION_BYTES,
            )
            await restarted.database.verify_ready()
            reopened = await open_seed_scope(
                restarted,
                configured_subject=subject,
                session_name=session_name,
            )
            assert (
                reopened.binding.direct_session_alias
                == materialized.opened_scope.binding.direct_session_alias
            )
            reopened_scope = ifc_scope(reopened.invocation_scope)
            restarted_spatial = _spatial_service(restarted)
            with bind_tenant_execution_context(reopened.invocation_scope.identity):
                reopened_exact = await restarted.repository.get_current(reopened_scope)
                reopened_page = await restarted_spatial.list(
                    reopened_scope,
                    IfcSpatialStructureListQuery(
                        global_ids=(target.global_id,),
                        page_size=8,
                    ),
                    None,
                )
            assert reopened_exact == moved_exact
            assert reopened_page.returned_count == 1
            assert reopened_page.items[0].project_world_placement == moved_placement
            with source_path.open("rb") as source_handle:
                assert (
                    hashlib.file_digest(source_handle, "sha256").hexdigest()
                    == _GOLDEN_CANDIDATE_SHA256
                )
    except IsolatedDatabaseUnavailable as exc:
        pytest.skip(str(exc))
    finally:
        if restarted is not None:
            await restarted.close()
        if runtime is not None:
            await runtime.close()
