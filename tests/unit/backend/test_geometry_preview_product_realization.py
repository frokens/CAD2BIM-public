from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactDownload
from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
from CAD2BIM_backend.ports.geometry_runtime import GeometryRuntimePort
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogIntegrityError
from CAD2BIM_backend.domain.geometry_query import GeometryQueryArtifactRef
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometryPreview,
    GeometryPreviewView,
    GeometryRenderCoordinateSpace,
    GeometryRenderScene,
    GeometryResolvedOrthographicCamera,
    GeometryResolvedPreviewPanel,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyVersionAssetRole,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation,
    ProductRealizationResult, ProductRealizationPreviewEvidence,
)
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec


NOW = datetime(2026, 9, 5, tzinfo=UTC)



PNG = b"\x89PNG\r\n\x1a\nproduct-realization"
GEOMETRY = b"canonical-product-brep"
CONTENT_DIGEST = "1" * 64
CONTRACT_DIGEST = "2" * 64
SOURCE_BUNDLE_SHA256 = "3" * 64
PINNED_IFC_REVISION_DIGEST = "6" * 64


def _codec() -> CursorCodec:
    key = base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode()
    return CursorCodec(key)


def _scope(
    *,
    task_id: str = "task-1",
    run_id: str = "run-1",
    execution_mode: ExecutionMode = ExecutionMode.DIRECT_MCP,
    catalog_frontier: int | None = 0,
) -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-1",
            principal_id="principal-1",
            task_id=task_id,
            actor_kind=TenantActorKind.USER,
            command_id="command-current",
        ),
        owner_id="tenant-1",
        run_id=run_id,
        execution_mode=execution_mode,
        catalog_frontier=catalog_frontier,
    )


def _family_bundle() -> SimpleNamespace:
    binding = SimpleNamespace(
        binding_key="body",
        expected_resource_kind=GeometryResourceKind.MAPPED_MODEL,
        geometry_content_digest="7" * 64,
    )
    return SimpleNamespace(
        version=SimpleNamespace(
            family_version_id="family-version-1",
            content_digest=CONTENT_DIGEST,
            contract_digest=CONTRACT_DIGEST,
            display_name="Solved regulator assembly",
        ),
        geometry_bindings=(binding,),
        assets=(
            SimpleNamespace(
                asset_role=ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
                sha256=SOURCE_BUNDLE_SHA256,
            ),
        ),
    )


def _binding_digest(bundle: SimpleNamespace) -> str:
    return sha256_digest(
        [
            {
                "binding_key": item.binding_key,
                "resource_kind": item.expected_resource_kind.value,
                "geometry_content_digest": item.geometry_content_digest,
            }
            for item in bundle.geometry_bindings
        ]
    )


def _canonical() -> CanonicalGeometry:
    return CanonicalGeometry(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type="application/vnd.opencascade.brep",
        payload=GEOMETRY,
        sha256=hashlib.sha256(GEOMETRY).hexdigest(),
        summary=GeometrySummary(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            bounds=GeometryBounds(
                minimum_m=(-1.0, -0.5, 0.0),
                maximum_m=(1.0, 0.5, 1.5),
            ),
            topology=GeometryTopologySummary(
                vertex_count=16,
                edge_count=24,
                wire_count=12,
                face_count=12,
                shell_count=2,
                solid_count=2,
                triangle_count=0,
                boundary_edge_count=0,
            ),
            surface_area_m2=4.0,
            volume_m3=0.8,
        ),
    )


def _scene() -> GeometryRenderScene:
    return GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256=_canonical().sha256,
    )


def _resolved_panel() -> GeometryResolvedPreviewPanel:
    return GeometryResolvedPreviewPanel(
        panel_key="legacy",
        x_px=0,
        y_px=0,
        width_px=640,
        height_px=480,
        camera=GeometryResolvedOrthographicCamera(
            target_m=(0.0, 0.0, 0.75),
            direction=(0.0, 0.0, -1.0),
            up=(0.0, 1.0, 0.0),
            right=(1.0, 0.0, 0.0),
            horizontal_extent_m=8.0 / 3.0,
            vertical_extent_m=2.0,
        ),
    )


def _geometry_metadata(bundle: SimpleNamespace, canonical: CanonicalGeometry) -> dict[str, object]:
    return {
        "target_kind": "product_realization_preview",
        "geometry_format": canonical.format.value,
        "geometry_sha256": canonical.sha256,
        "family_version_id": bundle.version.family_version_id,
        "family_version_content_digest": CONTENT_DIGEST,
        "contract_digest": CONTRACT_DIGEST,
        "source_bundle_sha256": SOURCE_BUNDLE_SHA256,
        "binding_digest": _binding_digest(bundle),
    }


def _artifacts(
    bundle: SimpleNamespace,
    canonical: CanonicalGeometry,
) -> tuple[AsyncMock, GeometryQueryArtifactRef]:
    artifacts = AsyncMock()
    reference = GeometryQueryArtifactRef(
        artifact_id="artifact-product-geometry",
        version_id="artifact-version-product-geometry",
        sha256=canonical.sha256,
    )
    canonical_record = ArtifactRecord(
        artifact_id=reference.artifact_id,
        version_id=reference.version_id,
        owner_id="tenant-1",
        task_id="task-1",
        kind="product_realization_preview_geometry",
        filename="product-realization.brep",
        content_type=canonical.content_type,
        size_bytes=len(canonical.payload),
        sha256=canonical.sha256,
        idempotency_key="product-realization-geometry:one",
        created_at=NOW,
        run_id="run-1",
        version_metadata=_geometry_metadata(bundle, canonical),
    )
    preview_by_key: dict[str, ArtifactRecord] = {}
    preview_contents: dict[tuple[str, str], bytes] = {}

    async def find_by_key(_owner_id: str, _task_id: str, key: str):
        return preview_by_key.get(key)

    async def upload_bytes(**kwargs) -> ArtifactRecord:
        content = kwargs["content"]
        ordinal = len(preview_by_key) + 1
        record = ArtifactRecord(
            artifact_id=f"artifact-product-derived-{ordinal}",
            version_id=f"artifact-version-product-derived-{ordinal}",
            owner_id=kwargs["owner_id"],
            task_id=kwargs["task_id"],
            kind=kwargs["kind"],
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            idempotency_key=kwargs["idempotency_key"],
            created_at=NOW,
            run_id=kwargs["run_id"],
            version_metadata=kwargs["version_metadata"],
        )
        preview_by_key[record.idempotency_key] = record
        preview_contents[(record.artifact_id, record.version_id)] = content
        return record

    async def download_version(
        _owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactDownload:
        if (artifact_id, version_id) == (reference.artifact_id, reference.version_id):
            active_canonical = artifacts.canonical_record
            assert run_id == active_canonical.run_id
            return ArtifactDownload(artifact=active_canonical, content=canonical.payload)
        record = next(
            item
            for item in preview_by_key.values()
            if (item.artifact_id, item.version_id) == (artifact_id, version_id)
        )
        assert run_id == record.run_id
        return ArtifactDownload(
            artifact=record,
            content=preview_contents[(record.artifact_id, record.version_id)],
        )

    artifacts.find_by_idempotency_key.side_effect = find_by_key
    artifacts.upload_bytes.side_effect = upload_bytes
    artifacts.download_version.side_effect = download_version
    artifacts.canonical_record = canonical_record
    artifacts.preview_by_key = preview_by_key
    artifacts.preview_contents = preview_contents
    return artifacts, reference


def _service() -> tuple[
    GeometryPreviewService,
    AsyncMock,
    AsyncMock,
    AsyncMock,
    GeometryQueryArtifactRef,
    SimpleNamespace,
    CursorCodec,
]:
    bundle = _family_bundle()
    canonical = _canonical()
    artifacts, reference = _artifacts(bundle, canonical)
    runtime = AsyncMock(spec=GeometryRuntimePort)
    runtime.render.return_value = GeometryPreview(
        view=GeometryPreviewView.ISOMETRIC,
        width_px=640,
        height_px=480,
        payload=PNG,
        sha256=hashlib.sha256(PNG).hexdigest(),
        resolved_panels=(_resolved_panel(),),
    )
    runtime.normalize.return_value = _canonical()
    family_catalog = AsyncMock()
    family_catalog.resolve_exact_version.return_value = bundle
    codec = _codec()
    service = GeometryPreviewService(
        AsyncMock(),
        AsyncMock(),
        runtime,
        family_catalog,
    )
    return service, runtime, artifacts, family_catalog, reference, bundle, codec




def _evidence(bundle):
    """Typed current same-solve evidence for the Artifact authorization fixture."""
    invocation = ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
    result = ProductRealizationResult.model_validate({
        "binding_digest": _binding_digest(bundle),
        "invocation": invocation, "policy": {}, "family_contract_digest": CONTRACT_DIGEST,
        "source_bundle_sha256": SOURCE_BUNDLE_SHA256,
        "normalized_public_parameters": {}, "frame_check_status": "FAIL",
        "proposal": {"status": "SOLVED",
            "components": [{"component_key": "body"}],
            "fragments": [{"fragment_key": "shape", "binding_key": "body", "geometry_parameters": {}}]},
        "instance_receipts": [{"fragment_key": "shape", "binding_key": "body",
            "geometry_sha256": _canonical().sha256, "geometry_summary": _canonical().summary}],
    })
    return ProductRealizationPreviewEvidence(
        contract_digest=CONTRACT_DIGEST, invocation=invocation,
        realization=result,
        geometry_sha256=_canonical().sha256,
    )


async def _preview(
    service: GeometryPreviewService,
    bundle,
    *,
    scope: InvocationScope | None = None,
    evidence="current",
    references=(),
):
    return await service.preview_product_realization(
        scope or _scope(),
        family_version_ref="cur.family-version",
        canonical_geometry=_canonical(),
        family_version_content_digest=CONTENT_DIGEST,
        contract_digest=CONTRACT_DIGEST,
        source_bundle_sha256=SOURCE_BUNDLE_SHA256,
        binding_digest=_binding_digest(bundle),
        frame_check_status="FAIL",
        scene=_scene(),
        product_realization=_evidence(bundle) if evidence == "current" else evidence,
        reference_geometries=references,
    )


@pytest.mark.asyncio
async def test_product_preview_returns_fresh_png_without_publishing_artifacts() -> None:
    service, runtime, artifacts, family_catalog, _reference, bundle, _codec = _service()

    first = await _preview(service, bundle)
    second = await _preview(service, bundle)

    assert first.content == second.content == PNG
    assert first.content_sha256 == hashlib.sha256(PNG).hexdigest()
    assert "capability" not in first.model_dump()
    assert first.render_metadata.scene_sha256 == sha256_digest(_scene())
    assert runtime.render.await_count == 2
    assert family_catalog.resolve_exact_version.await_count == 2
    artifacts.upload_bytes.assert_not_awaited()
    artifacts.find_by_idempotency_key.assert_not_awaited()
    artifacts.download_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_family_standard_model_reads_formal_asset_without_build_or_inspection():
    from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionModelObservation
    from CAD2BIM_backend.internal_authoring_api import _family_standard_preview_payload
    from CAD2BIM_mcp.runner.authoring_tools import _geometry_preview
    service, runtime, artifacts, catalog, _, bundle, _ = _service()
    canonical = _canonical()
    bundle.version.family_contract = SimpleNamespace(standard_model=SimpleNamespace(public_parameters={"size_m": 0.5}))
    assets = SimpleNamespace(standard_model_assets=AsyncMock(return_value=(
        SimpleNamespace(geometry_sha256=canonical.sha256, geometry_summary=canonical.summary), canonical.payload)))
    service._family_assets = assets
    result = await service.preview_family_standard_model(_scope(), "cur.family-exact", GeometryInspectionModelObservation())
    catalog.resolve_exact_version.assert_awaited_once_with(_scope().identity, "cur.family-exact", verify_assets=False)
    assets.standard_model_assets.assert_awaited_once_with(bundle)
    assert runtime.render.call_args.args[0].geometry.payload == canonical.payload
    runtime.normalize.assert_not_awaited()
    runtime.build_fixed.assert_not_awaited()
    artifacts.upload_bytes.assert_not_awaited()
    public, images = _geometry_preview(_family_standard_preview_payload(result))
    assert public.mode == "family_standard_model" and public.family_version_alias == "cur.family-exact"
    assert public.public_parameters_json == '{"size_m":0.5}' and images == (PNG,)
    assert "inspection_ref" not in public.model_dump() and "operation_alias" not in public.model_dump()


@pytest.mark.asyncio
async def test_family_standard_model_authorization_failure_never_reads_asset_or_renders():
    from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionModelObservation
    service, runtime, _, catalog, _, _, _ = _service()
    catalog.resolve_exact_version.side_effect = GeometryCatalogIntegrityError("exact version unavailable")
    assets = SimpleNamespace(standard_model_assets=AsyncMock())
    service._family_assets = assets
    with pytest.raises(GeometryCatalogIntegrityError):
        await service.preview_family_standard_model(_scope(), "cur.private", GeometryInspectionModelObservation())
    assets.standard_model_assets.assert_not_awaited()
    runtime.render.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_preview_rejects_missing_evidence_before_render() -> None:
    service, runtime, artifacts, _family_catalog, _reference, bundle, _codec = _service()

    with pytest.raises(GeometryCatalogIntegrityError):
        await _preview(service, bundle, evidence=None)

    runtime.render.assert_not_awaited()
    artifacts.upload_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_preview_render_failure_and_cancellation_publish_nothing() -> None:
    import asyncio

    service, runtime, artifacts, _family_catalog, _reference, bundle, _codec = _service()
    runtime.render.side_effect = RuntimeError("render failed")
    with pytest.raises(RuntimeError, match="render failed"):
        await _preview(service, bundle)
    runtime.render.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _preview(service, bundle)

    artifacts.upload_bytes.assert_not_awaited()
    artifacts.find_by_idempotency_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("placed", [False, True])
async def test_exact_mapped_geometry_preview_returns_fresh_png_without_artifacts(placed) -> None:
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile
    from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D

    service, runtime, artifacts, _family_catalog, _reference, _family, _codec = _service()
    asset = SimpleNamespace(
        asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
        asset_format=CanonicalGeometryFormat.OCCT_BREP_V1.value,
        sha256=_canonical().sha256,
    )
    bundle = SimpleNamespace(
        resource=SimpleNamespace(resource_id="resource-1", resource_kind=GeometryResourceKind.MAPPED_MODEL,
                                 display_name="Mapped body"),
        version=SimpleNamespace(resource_version_id="version-1", content_digest="a" * 64,
                                manifest={"interfaces": []}),
        assets=(asset,),
    )
    service._catalog.resolve_exact_version.return_value = bundle
    service._resources.read_exact_asset.return_value = (asset, GEOMETRY)
    service._reopen_canonical_asset = AsyncMock(return_value=_canonical())

    placement = RigidTransform3D() if placed else None
    first = await service._preview_scoped(_scope(), "exact-body", None, GeometryPreviewProfile(), placement)
    second = await service._preview_scoped(_scope(), "exact-body", None, GeometryPreviewProfile(), placement)

    assert first.content == second.content == PNG
    assert first.resource_version_ref == "exact-body"
    assert "capability" not in first.model_dump()
    assert runtime.render.await_count == 2
    assert runtime.normalize.await_count == (2 if placed else 0)
    artifacts.upload_bytes.assert_not_awaited()
    artifacts.find_by_idempotency_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_geometry_sample_is_rendered_without_saving_derived_brep(monkeypatch) -> None:
    from CAD2BIM_backend.application import geometry_preview_service as module
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile

    service, runtime, artifacts, _family_catalog, _reference, _family, _codec = _service()
    service._catalog.resolve_exact_version.return_value = SimpleNamespace(
        resource=SimpleNamespace(resource_id="function-1", resource_kind=GeometryResourceKind.MODELING_FUNCTION,
                                 display_name="Parametric body"),
        version=SimpleNamespace(resource_version_id="version-1", content_digest="a" * 64),
    )
    service._build_function_sample = AsyncMock(return_value=(_canonical(), ()))
    monkeypatch.setattr(module, "normalize_geometry_function_sample_parameters",
                        AsyncMock(return_value={"radius_m": 0.1}))

    result = await service._preview_scoped(
        _scope(), "exact-function", {"radius_m": 0.1}, GeometryPreviewProfile()
    )

    assert result.content == PNG
    assert result.sample_parameters_json == '{"radius_m":0.1}'
    runtime.render.assert_awaited_once()
    artifacts.upload_bytes.assert_not_awaited()
    artifacts.find_by_idempotency_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_precomputed_geometry_png_is_read_once_without_new_artifact() -> None:
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile

    service, runtime, artifacts, _family_catalog, _reference, _family, _codec = _service()
    service._catalog.resolve_exact_version.return_value = SimpleNamespace(
        resource=SimpleNamespace(resource_id="function-1", resource_kind=GeometryResourceKind.MODELING_FUNCTION,
                                 display_name="Parametric body"),
        version=SimpleNamespace(resource_version_id="version-1", content_digest="a" * 64),
    )
    asset = SimpleNamespace(asset_role=GeometryResourceAssetRole.PREVIEW, content_type="image/png",
                            sha256=hashlib.sha256(PNG).hexdigest())
    service._resources.read_exact_asset.return_value = (asset, PNG)

    result = await service._preview_scoped(_scope(), "exact-function", None, GeometryPreviewProfile())

    assert result.content == PNG
    assert result.render_metadata.metadata_kind == "stored_preview"
    runtime.render.assert_not_awaited()
    artifacts.upload_bytes.assert_not_awaited()
    artifacts.find_by_idempotency_key.assert_not_awaited()
