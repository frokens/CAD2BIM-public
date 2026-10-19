from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactDownload
from CAD2BIM_backend.application.product_realization_input_service import (
    ProductRealizationInputError,
)
from CAD2BIM_backend.application.product_realization_preview_service import (
    ProductRealizationPreviewService,
)

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogIntegrityError
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogIntegrityError,
    ProductRealizationPreviewAuthorization,
)
from CAD2BIM_backend.domain.geometry_interface import CircularInterfaceProfile, GeometryModelInterface
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometryPreviewInterfacesOverlayRequest,
    GeometryPreviewPanel,
    GeometryPreviewPresentation,
    GeometryPreviewPresetFitCamera,
    GeometryPreviewProfile,
    GeometryPreviewRequestError,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeUnavailableError,
    GeometryRuntimeValidationError,
    GeometryPreviewView,
    GeometryRenderCoordinateSpace,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyComponentContract,
    ProductFamilyContract,
    ProductFamilyModelInterfaceSource,
    ProductFamilyGeometryFragmentContract,
    ProductFamilyFragmentBindingOption,
    ProductFamilyIfcClass,
    ProductFamilyInterfaceContract,
    ProductFamilyInterfaceKind,
    ProductFamilyStructureKind,
)
from CAD2BIM_backend.domain.product_realization import (
    AmbiguousProductRealizationProposal,
    InfeasibleProductRealizationProposal,
    ProductGeometryInstanceReceipt,
    ProductRealizationAmbiguity,
    ProductRealizationComponentPlacement,
    ProductRealizationFragmentPlacement,
    ProductRealizationInvocation,
    ProductRealizationGeometryExecution,
    ProductRealizationRequest,
    ProductRealizationStatus,
    ProductRealizationViolation,
    ResolvedMappedGeometryBinding,
    RigidTransform3D,
    SolvedProductRealizationProposal,
    finalize_product_realization,
)
from CAD2BIM_backend.domain.product_realization_preview import (
    ProductRealizationPreviewError,
    ProductRealizationPreviewErrorCode,
    product_realization_render_scene,
)
from CAD2BIM_backend.ports.product_realization_runtime import (
    ProductRealizationRuntimePort,
    ProductRealizationRuntimeResourceLimitError,
    ProductRealizationRuntimeUnavailableError,
    ProductRealizationRuntimeValidationError,
)


NOW = datetime(2026, 9, 5, tzinfo=UTC)
BREP = b"canonical-product-realization-brep"
PNG = b"\x89PNG\r\n\x1a\nproduct-realization"
PINNED_IFC_DIGEST = "9" * 64


def _scope() -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-1",
            principal_id="principal-1",
            task_id="task-1",
            actor_kind=TenantActorKind.USER,
            command_id="command-1",
        ),
        owner_id="tenant-1",
        run_id="run-1",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=7,
    )


def _summary() -> GeometrySummary:
    return GeometrySummary(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        bounds=GeometryBounds(
            minimum_m=(0.0, 0.0, 0.0),
            maximum_m=(1.0, 1.0, 1.0),
        ),
        topology=GeometryTopologySummary(
            vertex_count=8,
            edge_count=12,
            wire_count=6,
            face_count=6,
            shell_count=1,
            solid_count=1,
            triangle_count=0,
            boundary_edge_count=0,
        ),
        surface_area_m2=6.0,
        volume_m3=1.0,
    )


def _geometry() -> CanonicalGeometry:
    return CanonicalGeometry(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type="application/vnd.opencascade.brep",
        payload=BREP,
        sha256=hashlib.sha256(BREP).hexdigest(),
        summary=_summary(),
    )


def _model_interfaces():
    return (GeometryModelInterface(interface_key="outlet", local_frame=RigidTransform3D(),
                                   profile=CircularInterfaceProfile(diameter_m=0.2)),)


def _request() -> ProductRealizationRequest:
    family = ProductFamilyContract(
        product_structure_kind=ProductFamilyStructureKind.SINGLE_PRODUCT,
        components=(
            ProductFamilyComponentContract(
                component_key="body",
                ifc_class=ProductFamilyIfcClass.VALVE,
            ),
        ),
        geometry_fragments=(
            ProductFamilyGeometryFragmentContract(
                fragment_key="body_shape",
                owner_component_key="body",
                binding_options=(ProductFamilyFragmentBindingOption(
                    binding_key="body_geometry", parameter_sources={},
                ),),
            ),
        ),
        interfaces=(ProductFamilyInterfaceContract(
            interface_key="outlet", owner_component_key="body", interface_kind=ProductFamilyInterfaceKind.EXTERNAL,
            source=ProductFamilyModelInterfaceSource(fragment_key="body_shape", model_interface_key="outlet"),
        ),),
    )
    source = b"exact canonical source bundle"
    geometry = _geometry()
    return ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        geometry_bindings=(
            ResolvedMappedGeometryBinding(
                binding_key="body_geometry",
                geometry_content_digest=geometry.sha256,
                geometry=geometry,
                interfaces=_model_interfaces(),
            ),
        ),
        invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}),
    )


def _execution(status: ProductRealizationStatus) -> ProductRealizationGeometryExecution:
    request = _request()
    if status is ProductRealizationStatus.SOLVED:
        proposal = SolvedProductRealizationProposal(
            components=(ProductRealizationComponentPlacement(component_key="body"),),
            fragments=(
                ProductRealizationFragmentPlacement(
                    fragment_key="body_shape",
                    binding_key="body_geometry",
                    geometry_parameters={},
                ),
            ),
        )
        receipts = (
            ProductGeometryInstanceReceipt(
                fragment_key="body_shape",
                binding_key="body_geometry",
                geometry_sha256=_geometry().sha256,
                geometry_summary=_summary(),
                interfaces=_model_interfaces(),
            ),
        )
        result = finalize_product_realization(request, proposal, receipts)
        return ProductRealizationGeometryExecution(realization=result, geometry=_geometry())
    if status is ProductRealizationStatus.AMBIGUOUS:
        proposal = AmbiguousProductRealizationProposal(
            ambiguities=(
                ProductRealizationAmbiguity(
                    ambiguity_key="clocking",
                    message="Two exact orientations remain.",
                ),
            )
        )
    else:
        proposal = InfeasibleProductRealizationProposal(
            violations=(
                ProductRealizationViolation(
                    violation_key="clearance",
                    message="The exact clearance constraint is violated.",
                ),
            )
        )
    result = finalize_product_realization(request, proposal, ())
    return ProductRealizationGeometryExecution(realization=result)


def _family_bundle(request: ProductRealizationRequest) -> SimpleNamespace:
    return SimpleNamespace(
        version=SimpleNamespace(
            family_version_id="family-version-1",
            content_digest="8" * 64,
            contract_digest=request.family_contract_digest,
            display_name="Exact regulator family",
        )
    )


class _Artifacts:
    def __init__(self) -> None:
        self.records: dict[str, ArtifactRecord] = {}
        self.contents: dict[tuple[str, str], bytes] = {}
        self.upload_count = 0

    async def find_by_idempotency_key(
        self,
        _owner_id: str,
        _task_id: str,
        idempotency_key: str,
    ) -> ArtifactRecord | None:
        return self.records.get(idempotency_key)

    async def upload_bytes(self, **values) -> ArtifactRecord:
        self.upload_count += 1
        content = values["content"]
        record = ArtifactRecord(
            artifact_id="artifact-realization-geometry",
            version_id="artifact-version-realization-geometry",
            owner_id=values["owner_id"],
            task_id=values["task_id"],
            kind=values["kind"],
            filename=values["filename"],
            content_type=values["content_type"],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            idempotency_key=values["idempotency_key"],
            created_at=NOW,
            run_id=values["run_id"],
            version_metadata=dict(values["version_metadata"]),
        )
        self.records[record.idempotency_key] = record
        self.contents[(record.artifact_id, record.version_id)] = content
        return record

    async def download_version(
        self,
        _owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactDownload:
        record = next(
            item
            for item in self.records.values()
            if (item.artifact_id, item.version_id) == (artifact_id, version_id)
        )
        assert run_id == record.run_id
        return ArtifactDownload(
            artifact=record,
            content=self.contents[(artifact_id, version_id)],
        )


def _preview_authorization() -> ProductRealizationPreviewAuthorization:
    return ProductRealizationPreviewAuthorization(
        family_version_ref="opaque-exact-family-version",
        title="Exact regulator family",
        size_bytes=len(PNG),
        content_sha256=hashlib.sha256(PNG).hexdigest(),
        content=PNG,
    )


def _service(
    status: ProductRealizationStatus = ProductRealizationStatus.SOLVED,
    *,
    artifacts: _Artifacts | None = None,
) -> tuple[
    ProductRealizationPreviewService,
    SimpleNamespace,
    AsyncMock,
    AsyncMock,
    AsyncMock,
    _Artifacts,
]:
    request = _request()
    family = _family_bundle(request)
    catalog = AsyncMock()
    catalog.version_ref = Mock(return_value="opaque-exact-family-version")
    catalog.resolve_exact_version.return_value = family
    inputs = AsyncMock()
    inputs.load_published_request.return_value = request
    runtime = AsyncMock(spec=ProductRealizationRuntimePort)
    runtime.execution_fixture = _execution(status)
    inputs.load_geometry_materials.return_value = (request,)
    def numeric(requests, *, output_need):
        async def values():
            from CAD2BIM_backend.domain.product_realization import ProductGeometryInterfaceEvaluation
            raw = runtime.execution_fixture.realization
            evaluations = tuple(ProductGeometryInterfaceEvaluation.model_validate(item.model_dump(
                exclude={"geometry_sha256", "geometry_summary"})) for item in raw.instance_receipts)
            result = raw.model_copy(update={"execution_stage": "interface_constraints_evaluated" if raw.proposal.status is ProductRealizationStatus.SOLVED else "solved",
                "instance_receipts": (), "interface_evaluations": evaluations})
            yield ProductRealizationGeometryExecution(realization=result, output_need=output_need)
        return values()
    def instantiate(requests, prechecks, *, output_need):
        async def values():
            yield ProductRealizationGeometryExecution.model_validate(runtime.execution_fixture)
        return values()
    runtime.realize_batch.side_effect = numeric
    runtime.instantiate_batch.side_effect = instantiate
    geometry_previews = AsyncMock()
    geometry_previews.preview_product_realization.return_value = _preview_authorization()
    artifact_store = artifacts or _Artifacts()
    service = ProductRealizationPreviewService(
        catalog,
        inputs,
        runtime,
        geometry_previews,
    )
    return service, family, catalog, inputs, runtime, artifact_store


@pytest.mark.asyncio
@pytest.mark.parametrize("render", [False, True])
async def test_engineering_build_returns_same_normalized_request_and_execution(render) -> None:
    service, family, catalog, inputs, runtime, artifacts = _service()
    request, execution, preview = await service.build(
        _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}),
        render_profile=GeometryPreviewProfile() if render else None,
    )
    assert request is inputs.load_published_request.return_value
    assert execution is runtime.execution_fixture
    runtime.instantiate_batch.assert_called_once()
    assert runtime.instantiate_batch.call_args.args[0] == (request,)
    catalog.resolve_exact_version.assert_awaited_once()
    assert artifacts.upload_count == 0
    assert (preview is not None) is render
    if render:
        evidence = service._geometry_previews.preview_product_realization.await_args.kwargs["product_realization"]
        assert evidence.realization == execution.realization
        assert evidence.invocation == request.invocation
    else:
        service._geometry_previews.preview_product_realization.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wrong_family", "wrong_request", "missing_geometry"])
async def test_engineering_build_rejects_invalid_exact_closure(failure) -> None:
    service, family, catalog, inputs, runtime, artifacts = _service()
    valid = runtime.execution_fixture
    if failure == "wrong_family":
        catalog.resolve_exact_version.return_value = SimpleNamespace(changed_exact_fact=True)
    elif failure == "wrong_request":
        runtime.execution_fixture = valid.model_copy(update={
            "realization": valid.realization.model_copy(update={"normalized_public_parameters": {"length_m": 99.0}}),
        })
    else:
        runtime.execution_fixture = valid.model_copy(update={"geometry": None})
    with pytest.raises(ProductRealizationPreviewError):
        await service.build(_scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}))
    if failure == "wrong_family":
        inputs.load_published_request.assert_not_awaited()
        runtime.realize_batch.assert_not_called()
    assert artifacts.records == {}
    service._geometry_previews.preview_product_realization.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_reference_loading_failure_remains_bounded() -> None:
    service, family, _, inputs, runtime, artifacts = _service()
    service._geometry_previews.resolve_references.side_effect = RuntimeError("private storage path")
    with pytest.raises(ProductRealizationPreviewError) as captured:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}),
            reference_geometries=(object(),),
        )
    assert captured.value.code is ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED
    assert "private storage path" not in str(captured.value)
    inputs.load_published_request.assert_not_awaited()
    runtime.realize_batch.assert_not_called()
    assert artifacts.records == {}


@pytest.mark.asyncio
async def test_solved_preview_reauthorizes_exact_family_executes_once_without_artifacts() -> (
    None
):
    service, family, catalog, inputs, runtime, artifacts = _service()

    outcome = await service.preview(
        _scope(),
        family_version=family,
        invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}),
    )

    catalog.version_ref.assert_called_once_with(_scope().identity, family)
    catalog.resolve_exact_version.assert_awaited_once_with(
        _scope().identity,
        "opaque-exact-family-version",
    )
    inputs.load_published_request.assert_awaited_once_with(
        _scope(),
        family_version=family,
        invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}),
        numeric_only=True,
    )
    runtime.instantiate_batch.assert_called_once()
    assert outcome.realization.proposal.status is ProductRealizationStatus.SOLVED
    assert outcome.preview is not None
    assert outcome.preview.content == PNG
    assert artifacts.upload_count == 0
    preview_call = service._geometry_previews.preview_product_realization.await_args
    assert preview_call.kwargs["scene"].coordinate_space is (
        GeometryRenderCoordinateSpace.PROJECT_WORLD
    )
    assert preview_call.kwargs["scene"].geometry_sha256 == _geometry().sha256
    assert preview_call.kwargs["render_profile"] == GeometryPreviewProfile()
    assert preview_call.kwargs["frame_check_status"] == outcome.realization.frame_check_status
    evidence = preview_call.kwargs["product_realization"]
    assert evidence.realization == outcome.realization
    assert evidence.invocation == _request().invocation
    assert evidence.contract_digest == _request().family_contract_digest
    assert evidence.geometry_sha256 == _geometry().sha256


@pytest.mark.asyncio
async def test_family_batch_renders_two_retained_cases_in_one_execution_scope(tmp_path) -> None:
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.domain.preview_cases import (
        FamilyCaseExpectation, FamilyPreviewCase, PreviewSelectedRender, PreviewSourceSnapshot,
    )
    from tests.unit.backend.test_geometry_inspection_service import _CapacityRenderer, _setup

    class FamilyRuntime(_CapacityRenderer):
        async def realize_batch(self, requests, *, output_need):
            from CAD2BIM_backend.domain.product_realization import ProductGeometryInterfaceEvaluation
            execution = _execution(ProductRealizationStatus.SOLVED)
            evaluation = ProductGeometryInterfaceEvaluation.model_validate(execution.realization.instance_receipts[0].model_dump(
                exclude={"geometry_sha256", "geometry_summary"}))
            yield ProductRealizationGeometryExecution(realization=execution.realization.model_copy(update={
                "execution_stage": "interface_constraints_evaluated", "instance_receipts": (), "interface_evaluations": (evaluation,)}),
                output_need=output_need)

        async def instantiate_batch(self, requests, prechecks, *, output_need):
            await self.capacity.admit_current_execution()
            execution = _execution(ProductRealizationStatus.SOLVED)
            yield execution.model_copy(update={"instance_geometries": {"body_shape": _geometry()}, "output_need": output_need})

    _, _, _, scope, inspections, _ = _setup()
    runtime = FamilyRuntime(tmp_path)
    geometry_previews = GeometryPreviewService(None, None, runtime, inspection_service=inspections)
    service = ProductRealizationPreviewService(None, None, runtime, geometry_previews, inspections)
    request = _request()
    source = PreviewSourceSnapshot(source_kind="family_realization", source_type="published",
        source_id="family", source_ref="cur.exact", snapshot_id="family-v1",
        content_digest="a" * 64, title="Family")
    inspections._family_catalog = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=_family_bundle(request)))
    cases = tuple(FamilyPreviewCase(case_key=key, invocation=request.invocation,
        expectation=FamilyCaseExpectation(expected_status="SOLVED")) for key in ("first", "second"))
    outcome = await service.preview_cases(scope, source=source, requests=(request, request), cases=cases,
        retain_for_inspection=True, request_identity="two-family-cases",
        render=PreviewSelectedRender(case_keys=("first", "second")))
    assert [item.render_status for item in outcome.report.cases] == ["rendered", "rendered"]
    assert [key for key, _ in outcome.images] == ["first", "second"]
    assert len(runtime.leases) == 2 and runtime.leases[0] is runtime.leases[1]
    assert not runtime.capacity.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["execution", "startup", "codec"])
async def test_family_batch_resource_failure_reports_case_measurement_and_keeps_remaining_unrun(tmp_path, failure_phase):
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.domain.preview_cases import (
        FamilyCaseExpectation, FamilyPreviewCase, PreviewSourceSnapshot,
    )
    from tests.unit.backend.test_geometry_inspection_service import _CapacityRenderer, _setup

    details = {"resource_kind": "cpu", "budget_scope": "invocation",
        "limit": 30.0, "observed": 33.35, "configured_limits": {"cpu_seconds": 900.0}}
    if failure_phase == "startup":
        details = {"phase": "startup", "resource_kind": "wall", "limit": 30.0, "observed": 30.2}
    elif failure_phase == "codec":
        details = {"phase": "codec", "codec_diagnostic": {
            "operation": "normalize_batch", "resource_kind": "wall",
            "elapsed_seconds": 45.1, "effective_wall_seconds": 45.0,
            "configured_limits": {"wall_seconds": 45.0}}}

    class FamilyRuntime(_CapacityRenderer):
        async def realize_batch(self, requests, *, output_need):
            raise ProductRealizationRuntimeResourceLimitError(
                "private child detail /private/session", diagnostic=details,
                retryable=False, item_index=0)
            yield  # An async iterator, matching the runtime port.

    _, _, _, scope, inspections, _ = _setup()
    runtime = FamilyRuntime(tmp_path)
    service = ProductRealizationPreviewService(None, None, runtime,
        GeometryPreviewService(None, None, runtime, inspection_service=inspections), inspections)
    request = _request()
    source = PreviewSourceSnapshot(source_kind="family_realization", source_type="published",
        source_id="family", source_ref="cur.exact", snapshot_id="family-v1",
        content_digest="a" * 64, title="Family")
    inspections._family_catalog = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=_family_bundle(request)))
    cases = tuple(FamilyPreviewCase(case_key=key, invocation=request.invocation,
        expectation=FamilyCaseExpectation(expected_status="SOLVED")) for key in ("first", "second"))
    outcome = await service.preview_cases(scope, source=source, requests=(request, request), cases=cases,
        retain_for_inspection=True, request_identity="limited-family-cases")
    assert [case.execution_status for case in outcome.report.cases] == ["technical_error", "not_run"]
    diagnostic = outcome.report.cases[0].diagnostics[0]
    assert diagnostic.code == "WORKER_RESOURCE_LIMIT"
    if failure_phase == "execution":
        assert "this case" in diagnostic.message
        assert "33.350 observed, 30.000 limit" in diagnostic.message
        assert "all native threads" in diagnostic.message
    elif failure_phase == "startup":
        assert "no case was dispatched" in diagnostic.message
        assert "30.200 observed, 30.000 limit" in diagnostic.message
    else:
        assert "normalize_batch" in diagnostic.message
        assert "45.100 observed, 45.000 limit" in diagnostic.message
        assert diagnostic.phase == "output_validation"
    assert "900" not in diagnostic.message and "/private" not in diagnostic.message


@pytest.mark.asyncio
async def test_display_references_are_authorized_before_solve_and_do_not_enter_invocation():
    from CAD2BIM_backend.domain.geometry_catalog import GeometryPreviewReference, GeometryCatalogReferenceError
    service, family, _, inputs, runtime, artifacts = _service()
    references = (GeometryPreviewReference(reference_key="mesh", resource_version_ref="exact-reference",
        datum_transform=RigidTransform3D()),)
    service._geometry_previews.resolve_references.side_effect = GeometryCatalogReferenceError("hidden")
    with pytest.raises(ProductRealizationPreviewError) as caught:
        await service.preview(_scope(), family_version=family,
            invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}), reference_geometries=references)
    assert caught.value.code is ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED
    inputs.load_published_request.assert_not_awaited()
    runtime.realize_batch.assert_not_called()
    assert artifacts.upload_count == 0
    service._geometry_previews.resolve_references.side_effect = None
    service._geometry_previews.resolve_references.return_value = references
    result = await service.preview(_scope(), family_version=family,
        invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}), reference_geometries=references)
    assert result.preview is not None
    assert runtime.realize_batch.call_args.args[0][0] == _request()
    assert service._geometry_previews.preview_product_realization.await_args.kwargs["reference_geometries"] == references


@pytest.mark.asyncio
async def test_solved_frame_fail_returns_numeric_report_without_geometry() -> None:
    service, family, _catalog, _inputs, runtime, artifacts = _service()
    execution = _execution(ProductRealizationStatus.SOLVED)
    realization = execution.realization.model_copy(update={"frame_check_status": "FAIL"})
    runtime.execution_fixture = execution.model_copy(update={"realization": realization})

    outcome = await service.preview(
        _scope(),
        family_version=family,
        invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}),
    )

    assert outcome.realization.frame_check_status == "FAIL"
    assert outcome.preview is None
    assert outcome.realization.execution_stage == "interface_constraints_evaluated"
    runtime.instantiate_batch.assert_not_called()
    assert artifacts.upload_count == 0




@pytest.mark.parametrize(
    "status",
    (ProductRealizationStatus.AMBIGUOUS, ProductRealizationStatus.INFEASIBLE),
)
@pytest.mark.asyncio
async def test_non_solved_outcomes_never_publish_or_authorize_geometry(
    status: ProductRealizationStatus,
) -> None:
    service, family, _catalog, _inputs, runtime, artifacts = _service(status)

    outcome = await service.preview(
        _scope(),
        family_version=family,
        invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}),
    )

    runtime.realize_batch.assert_called_once()
    runtime.instantiate_batch.assert_not_called()
    assert outcome.realization.proposal.status is status
    assert outcome.preview is None
    assert artifacts.records == {}
    service._geometry_previews.preview_product_realization.assert_not_awaited()


@pytest.mark.asyncio
async def test_result_validation_rejects_wrong_request_without_partial_artifact() -> None:
    service, family, _catalog, _inputs, runtime, artifacts = _service()
    valid = _execution(ProductRealizationStatus.SOLVED)
    def invalid(requests, *, output_need):
        async def values():
            yield ProductRealizationGeometryExecution(
                realization=valid.realization.model_copy(update={"normalized_public_parameters": {"length_m": 99.0}}),
                geometry=valid.geometry)
        return values()
    runtime.realize_batch.side_effect = invalid

    with pytest.raises(ProductRealizationPreviewError) as captured:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )

    assert captured.value.code is ProductRealizationPreviewErrorCode.RESULT_INTEGRITY_FAILED
    assert artifacts.records == {}
    service._geometry_previews.preview_product_realization.assert_not_awaited()


@pytest.mark.asyncio
async def test_family_exact_mismatch_fails_before_inputs_or_runtime() -> None:
    service, family, catalog, inputs, runtime, artifacts = _service()
    catalog.resolve_exact_version.return_value = SimpleNamespace(
        version=family.version,
        changed_exact_fact=True,
    )

    with pytest.raises(ProductRealizationPreviewError) as captured:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )

    assert captured.value.code is (ProductRealizationPreviewErrorCode.FAMILY_REAUTHORIZATION_FAILED)
    inputs.load_published_request.assert_not_awaited()
    runtime.realize_batch.assert_not_called()
    assert artifacts.records == {}


@pytest.mark.asyncio
async def test_dependency_failures_map_to_bounded_safe_codes() -> None:
    service, family, _catalog, inputs, runtime, _artifacts = _service()
    inputs.load_published_request.side_effect = ProductRealizationInputError(
        "INPUT_INVALID",
        "internal-id-that-must-not-leak",
    )
    with pytest.raises(ProductRealizationPreviewError) as input_failure:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )
    assert input_failure.value.code is ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED
    assert input_failure.value.message == "Exact Product Realization inputs could not be loaded."
    assert input_failure.value.retryable is False
    assert "internal-id" not in input_failure.value.message

    inputs.load_published_request.side_effect = ProductRealizationInputError(
        "INPUT_INVALID",
        "private-invalid-input",
        phase="output_validation",
        public_message=(
            "Exact Product Realization inputs are invalid; "
            "invalid_path=geometry_bindings.0.interfaces.0.profile.circular.diameter_m; "
            "error_type=greater_than"
        ),
    )
    with pytest.raises(ProductRealizationPreviewError) as structured_input_failure:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )
    assert structured_input_failure.value.code is (
        ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED
    )
    assert structured_input_failure.value.retryable is False
    assert "invalid_path=geometry_bindings.0.interfaces.0.profile" in (
        structured_input_failure.value.message
    )
    assert "error_type=greater_than" in structured_input_failure.value.message
    assert "private-invalid-input" not in structured_input_failure.value.message

    inputs.load_published_request.side_effect = ProductRealizationInputError(
        "GEOMETRY_NOT_FOUND",
        "internal-id-that-must-not-leak",
    )
    with pytest.raises(ProductRealizationPreviewError) as missing_geometry:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )
    assert missing_geometry.value.code is ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED
    assert "internal-id" not in missing_geometry.value.message

    inputs.load_published_request.side_effect = None
    inputs.load_published_request.return_value = _request()
    runtime.realize_batch.side_effect = ProductRealizationRuntimeUnavailableError(
        "runtime-worker-id-must-not-leak"
    )
    with pytest.raises(ProductRealizationPreviewError) as runtime_failure:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )
    assert runtime_failure.value.code is (
        ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED
    )
    assert runtime_failure.value.retryable is True
    assert "worker-id" not in runtime_failure.value.message

    runtime.realize_batch.side_effect = ProductRealizationRuntimeResourceLimitError(
        "private-native-limit-must-not-leak"
    )
    with pytest.raises(ProductRealizationPreviewError) as resource_failure:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )
    assert resource_failure.value.code is (
        ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED
    )
    assert resource_failure.value.retryable is False
    assert resource_failure.value.message == (
        "The Product Realization exceeded its fixed resource limit."
    )
    assert "private-native-limit" not in resource_failure.value.message

    runtime.realize_batch.side_effect = ProductRealizationRuntimeValidationError(
        "REALIZATION_OUTPUT_INVALID",
        "instantiated geometry did not reach a canonical reopen orbit",
        phase="output_validation",
    )
    with pytest.raises(ProductRealizationPreviewError) as validation_failure:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )
    assert validation_failure.value.retryable is False
    assert validation_failure.value.message == (
        "runtime_code=REALIZATION_OUTPUT_INVALID; runtime_phase=output_validation; "
        "diagnostic=instantiated geometry did not reach a canonical reopen orbit"
    )

    runtime.realize_batch.side_effect = ProductRealizationRuntimeValidationError(
        "UNKNOWN_INTERNAL_FAILURE",
        "server-path-and-row-id-must-not-leak",
        phase="internal",
    )
    with pytest.raises(ProductRealizationPreviewError) as unknown_validation_failure:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )
    assert unknown_validation_failure.value.message == (
        "The Product Realization runtime rejected the exact request."
    )
    assert "server-path" not in unknown_validation_failure.value.message


@pytest.mark.asyncio
async def test_preview_integrity_failure_is_bounded_without_artifact() -> None:
    service, family, _catalog, _inputs, _runtime, artifacts = _service()
    service._geometry_previews.preview_product_realization.side_effect = (
        GeometryCatalogIntegrityError("artifact-id-must-not-leak")
    )

    with pytest.raises(ProductRealizationPreviewError) as captured:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )

    assert captured.value.code is (ProductRealizationPreviewErrorCode.PREVIEW_AUTHORIZATION_FAILED)
    assert "artifact-id" not in captured.value.message
    assert artifacts.upload_count == 0
    service._geometry_previews.preview_product_realization.assert_awaited_once()


@pytest.mark.asyncio
async def test_family_asset_integrity_failure_is_not_reported_as_permission_failure():
    service, family, catalog, inputs, runtime, artifacts = _service()
    catalog.resolve_exact_version.side_effect = ProductFamilyCatalogIntegrityError("private asset location")
    with pytest.raises(ProductRealizationPreviewError) as caught:
        await service.preview(_scope(), family_version=family, invocation=_request().invocation)
    assert caught.value.code is ProductRealizationPreviewErrorCode.ARTIFACT_INTEGRITY_FAILED
    assert not caught.value.retryable and "private asset location" not in caught.value.message
    inputs.load_published_request.assert_not_awaited()
    runtime.realize_batch.assert_not_called()


@pytest.mark.asyncio
async def test_geometry_asset_integrity_failure_retains_public_category():
    service, family, catalog, inputs, runtime, artifacts = _service()
    inputs.load_published_request.side_effect = ProductRealizationInputError(
        "GEOMETRY_ASSET_INTEGRITY_FAILED", "private asset location", phase="storage_verification",
    )
    with pytest.raises(ProductRealizationPreviewError) as caught:
        await service.preview(_scope(), family_version=family, invocation=_request().invocation)
    assert caught.value.code is ProductRealizationPreviewErrorCode.ARTIFACT_INTEGRITY_FAILED
    assert not caught.value.retryable and "private asset location" not in caught.value.message
    runtime.realize_batch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("resource", "unavailable", "validation"))
async def test_geometry_codec_resource_limit_is_safe_and_not_retryable(
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, family, _catalog, _inputs, _runtime, artifacts = _service()
    diagnostic = {
        "resource_pool": "python",
        "phase": "admission",
        "reason": "queue_full",
        "execution_started": False,
    }
    failure = {
        "resource": GeometryRuntimeResourceLimitError("private native limit detail"),
        "unavailable": GeometryRuntimeUnavailableError(
            "private native limit detail", diagnostic=diagnostic
        ),
        "validation": GeometryRuntimeValidationError("private native limit detail"),
    }[failure_kind]
    service._geometry_previews.preview_product_realization.side_effect = failure

    with pytest.raises(ProductRealizationPreviewError) as captured:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )

    assert captured.value.code is ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED
    assert captured.value.retryable is (failure_kind == "unavailable")
    expected_message = {
        "resource": "fixed resource limit",
        "unavailable": "runtime is unavailable",
        "validation": "failed validation",
    }
    assert expected_message[failure_kind] in captured.value.message
    if failure_kind == "unavailable":
        assert captured.value.diagnostic == diagnostic
    assert "private native limit detail" not in captured.value.message


@pytest.mark.asyncio
async def test_invalid_focus_request_is_not_reported_as_authorization_failure() -> None:
    service, family, _catalog, _inputs, _runtime, _artifacts = _service()
    message = "interface focus extent does not contain both frames; omit it for automatic framing"
    service._geometry_previews.preview_product_realization.side_effect = GeometryPreviewRequestError(message)
    with pytest.raises(ProductRealizationPreviewError) as captured:
        await service.preview(
            _scope(), family_version=family, invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
        )
    assert captured.value.code is ProductRealizationPreviewErrorCode.PREVIEW_REQUEST_INVALID
    assert captured.value.message == message
    assert captured.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("temporary_interfaces", [None, ()])
async def test_family_preview_rejects_any_explicit_temporary_override_before_dependencies(
    temporary_interfaces,
) -> None:
    service, family, catalog, inputs, runtime, artifacts = _service()
    profile = GeometryPreviewProfile(
        presentation=GeometryPreviewPresentation(
            panels=(
                GeometryPreviewPanel(
                    panel_key="family",
                    camera=GeometryPreviewPresetFitCamera(view=GeometryPreviewView.ISOMETRIC),
                ),
            ),
            overlay=GeometryPreviewInterfacesOverlayRequest(
                temporary_interfaces=temporary_interfaces
            ),
        )
    )

    with pytest.raises(ProductRealizationPreviewError) as captured:
        await service.preview(
            _scope(),
            family_version=family,
            invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}),
            render_profile=profile,
        )
    assert captured.value.code is ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED
    catalog.resolve_exact_version.assert_not_awaited()
    inputs.load_published_request.assert_not_awaited()
    runtime.realize_batch.assert_not_called()
    assert artifacts.records == {}
    service._geometry_previews.preview_product_realization.assert_not_awaited()


def test_orchestrator_reuses_inspection_owner_without_ifc_writer_or_repository() -> None:
    assert tuple(inspect.signature(ProductRealizationPreviewService.__init__).parameters) == (
        "self",
        "family_catalog",
        "inputs",
        "runtime",
        "geometry_previews",
        "inspection_service",
    )




def test_product_render_scene_projects_same_build_family_frames():
    request = _request()
    result = _execution(ProductRealizationStatus.SOLVED).realization
    scene = product_realization_render_scene(request, result, geometry_sha256=_geometry().sha256)
    assert scene.coordinate_space is GeometryRenderCoordinateSpace.PROJECT_WORLD
    assert tuple(item.overlay_id for item in scene.interface_overlays) == ("family:outlet",)
    assert scene.interface_overlays[0].frame == result.interface_project_world_frames[0].project_world_frame
    assert not scene.host_overlays and not scene.straight_profile_overlays
    assert scene.display_context_sha256 is None


@pytest.mark.parametrize("retired_field", ["attachments", "interface_bindings"])
def test_retired_invocation_fields_are_rejected(retired_field):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ProductRealizationInvocation.model_validate({retired_field: []})
