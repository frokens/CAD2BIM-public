from __future__ import annotations

import hashlib
import io
import math

import numpy as np
import pytest
from PIL import Image

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryBuildDisplayContextRequest,
    GeometryRenderRequest,
    GeometryRuntimeValidationError,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import OcpCadQueryGeometryRuntime
from CAD2BIM_backend.infrastructure.geometry_runtime.codec import reopen_geometry
from CAD2BIM_backend.infrastructure.geometry_runtime.straight_profile import build_display_context


@pytest.mark.parametrize(
    "profile,volume",
    [
        ({"kind": "circular", "diameter_m": 2}, 3 * math.pi),
        ({"kind": "rectangle", "width_m": 2, "height_m": 1}, 6),
        ({"kind": "polygon", "points_m": [[0, 0], [2, 0], [0, 1]]}, 3),
    ],
)
def test_straight_profiles_have_analytic_volume_and_exact_canonical_reopen(profile, volume):
    request = GeometryBuildDisplayContextRequest.model_validate(
        {
            "recipes": [
                {
                    "recipe_key": "host:test",
                    "frame": {},
                    "profile": profile,
                    "length_m": 3,
                }
            ]
        }
    )
    result = build_display_context(request)
    assert result.geometry.summary.volume_m3 == pytest.approx(volume)
    assert result.geometry.summary.bounds.minimum_m[2] == pytest.approx(0)
    assert result.geometry.summary.bounds.maximum_m[2] == pytest.approx(3)
    assert result.geometry.sha256 == hashlib.sha256(result.geometry.payload).hexdigest()
    assert result.recipe_digest == sha256_digest(request)
    assert result.recipe_keys == ("host:test",)
    shape, summary = reopen_geometry(result.geometry)
    assert shape.isValid()
    assert summary == result.geometry.summary
    assert build_display_context(request) == result


def test_asymmetric_polygon_keeps_xy_coordinates_through_nonidentity_frame():
    request = GeometryBuildDisplayContextRequest.model_validate(
        {
            "recipes": [
                {
                    "recipe_key": "candidate:asymmetric",
                    "length_m": 2,
                    "frame": {
                        "translation_m": [10, 20, 30],
                        "rotation": [[0, 0, -1], [1, 0, 0], [0, -1, 0]],
                    },
                    "profile": {"kind": "polygon", "points_m": [[0, 0], [2, 0], [0, 1]]},
                }
            ]
        }
    )
    result = build_display_context(request)
    assert result.geometry.summary.volume_m3 == pytest.approx(2)
    assert result.geometry.summary.bounds.minimum_m == pytest.approx((8, 20, 29), abs=1e-6)
    assert result.geometry.summary.bounds.maximum_m == pytest.approx((10, 22, 30), abs=1e-6)


def test_display_context_preserves_separate_solids_and_request_order():
    request = GeometryBuildDisplayContextRequest.model_validate(
        {
            "recipes": [
                {
                    "recipe_key": f"host:{key}",
                    "length_m": length,
                    "frame": {"translation_m": [x, 0, 0]},
                    "profile": {"kind": "rectangle", "width_m": 1, "height_m": 1},
                }
                for key, length, x in [("b", 2, 3), ("a", 1, 0)]
            ]
        }
    )
    result = build_display_context(request)
    assert result.geometry.summary.topology.solid_count == 2
    assert result.geometry.summary.volume_m3 == pytest.approx(3)
    assert result.recipe_keys == ("host:b", "host:a")


def test_overflowing_straight_endpoint_fails_before_native_construction(monkeypatch):
    request = GeometryBuildDisplayContextRequest.model_validate(
        {
            "recipes": [
                {
                    "recipe_key": "host:overflow",
                    "length_m": 1e308,
                    "frame": {"translation_m": [0, 0, 1e308]},
                    "profile": {"kind": "circular", "diameter_m": 1},
                }
            ]
        }
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("overflowing input reached OCP")

    monkeypatch.setattr(
        "CAD2BIM_backend.infrastructure.geometry_runtime.straight_profile.cq.Plane", forbidden
    )
    with pytest.raises(GeometryRuntimeValidationError, match="finite arithmetic"):
        build_display_context(request)


@pytest.mark.asyncio
async def test_renderer_adds_context_surfaces_and_original_wire_without_changing_product():
    profile = {"kind": "circular", "diameter_m": 0.2}
    product = build_display_context(
        GeometryBuildDisplayContextRequest.model_validate(
            {
                "recipes": [
                    {
                        "recipe_key": "host:product",
                        "frame": {},
                        "profile": profile,
                        "length_m": 1,
                    }
                ]
            }
        )
    ).geometry
    candidate = {
        "recipe_key": "candidate:a",
        "frame": {"translation_m": [0, 0, 1]},
        "profile": profile,
        "length_m": 2,
    }
    original = {**candidate, "recipe_key": "host:a", "length_m": 3}
    runtime = OcpCadQueryGeometryRuntime()
    try:
        context = await runtime.build_display_context(
            GeometryBuildDisplayContextRequest.model_validate({"recipes": [candidate]})
        )
        request = GeometryRenderRequest.model_validate(
            {
                "geometry": product,
                "display_context": context.geometry,
                "width_px": 320,
                "height_px": 480,
                "view": "front",
                "scene": {
                    "coordinate_space": "project_world",
                    "geometry_sha256": product.sha256,
                    "display_context_sha256": context.geometry.sha256,
                    "interface_overlays": [
                        {
                            "overlay_id": "host:a",
                            "label": "original",
                            "marker_kind": "host_attachment",
                            "frame": {"translation_m": [0, 0, 4]},
                            "profile": profile,
                        },
                        {
                            "overlay_id": "candidate:a",
                            "label": "candidate",
                            "marker_kind": "host_candidate",
                            "frame": {"translation_m": [0, 0, 3]},
                            "profile": profile,
                        },
                    ],
                    "host_overlays": [
                        {"overlay_id": "host:a", "construction_kind": "retained_straight"}
                    ],
                    "straight_profile_overlays": [
                        {**original, "display_kind": "original"},
                        {**candidate, "display_kind": "candidate"},
                    ],
                },
            }
        )
        before = product.model_dump()
        rendered = await runtime.render(request)
        assert product.model_dump() == before
        assert (await runtime.reopen(product)).volume_m3 == pytest.approx(math.pi * 0.1**2)
        assert rendered.resolved_panels[0].camera.target_m[2] == pytest.approx(2, abs=1e-6)
        pixels = np.asarray(Image.open(io.BytesIO(rendered.payload))).astype(int)
        assert (
            np.count_nonzero(
                (pixels[:, :, 1] > pixels[:, :, 0] + 20) & (pixels[:, :, 1] > pixels[:, :, 2] + 20)
            )
            > 100
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_connection_context_real_render_reloads_saved_separate_background_without_build(tmp_path, monkeypatch):
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.domain.geometry_inspection import InspectionWholeSelection, InspectionResultCoordinates
    from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionModelObservation
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile, GeometryRenderScene
    from tests.unit.backend.test_geometry_inspection_service import _setup
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    product = build_display_context(GeometryBuildDisplayContextRequest.model_validate({"recipes": [{
        "recipe_key": "body", "frame": {"translation_m": [0, 0, -0.1]},
        "profile": {"kind": "rectangle", "width_m": 0.2, "height_m": 0.2}, "length_m": 0.2,
    }]})).geometry
    _, _, _, scope, inspections, arguments = _setup()
    runtime = OcpCadQueryGeometryRuntime()
    service = GeometryPreviewService(None, None, runtime, inspection_service=inspections)
    try:
        scene, background = await service._prepare_connection_context(GeometryPreviewProfile(connection_context={
            "mode": "pipes", "items": [
                {"start_m": [-0.5, 0, 0], "end_m": [0.5, 0, 0], "outer_diameter_m": 0.05},
                {"start_m": [0, -0.5, 0.2], "end_m": [0, 0.5, 0.2], "outer_diameter_m": 0.05},
            ]}), GeometryRenderScene(coordinate_space="geometry_coordinates", geometry_sha256=product.sha256))
        assert background.summary.topology.solid_count == 2
        assert background.summary.volume_m3 == pytest.approx(2 * math.pi * 0.025**2)
        bundle = SimpleNamespace(resource=SimpleNamespace(resource_id="geometry", display_name="Product"),
            version=SimpleNamespace(resource_version_id="geometry-v1", content_digest="a"*64))
        loaded, _ = await inspections.retain_sample(scope, await inspections.begin(scope, **arguments),
            bundle=bundle, resource_version_ref=arguments["source_version_ref"], parameters={},
            local_geometry=product, geometry=product, interfaces=(), scene=scene, display_context=background)
        reference = inspections.reference(scope, loaded.artifact)
        forbidden = AsyncMock(side_effect=AssertionError("saved observation tried to rebuild geometry"))
        monkeypatch.setattr(runtime, "build_display_context", forbidden)
        monkeypatch.setattr(runtime, "build_fixed", forbidden)
        monkeypatch.setattr(runtime, "validate_function", forbidden)
        result = await service.preview_inspection(scope, reference, InspectionWholeSelection(), InspectionResultCoordinates(),
            GeometryInspectionModelObservation(render_profile={"width_px": 320, "height_px": 320}))
        assert not result.diagnostics and result.preview.connection_context == scene.connection_context
        assert result.preview.width_px == result.preview.height_px == 320
        assert product == loaded.geometry and product.summary.volume_m3 == pytest.approx(0.008)
        assert (await inspections.read(scope, reference)).display_context == background
        pixels = np.asarray(Image.open(io.BytesIO(result.preview.payload))).astype(int)
        assert np.count_nonzero((pixels[:, :, 1] > pixels[:, :, 0] + 20)
                               & (pixels[:, :, 1] > pixels[:, :, 2] + 20)) > 100
        (tmp_path / "connection-context.png").write_bytes(result.preview.payload)
        forbidden.assert_not_called()
    finally:
        await runtime.close()
