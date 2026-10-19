from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image

from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryNormalizeRequest,
    GeometryPreviewView,
    GeometryPreviewRequestError,
    GeometryRenderRequest,
    GeometryRuntimeValidationError,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec import MeshData, normalize_geometry
from CAD2BIM_backend.infrastructure.geometry_runtime.render import (
    _clip_screen_segment,
    _preset_basis,
    _project_vertices,
    _rasterize_surfaces,
    render_mesh,
)


def _request(**kwargs):
    mesh = MeshData(
        vertices=[[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]],
        triangles=[[0, 1, 2], [0, 2, 3]],
    )
    geometry = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=json.dumps(
                {
                    "schema": "indexed_triangle_mesh_v1",
                    "unit": "METRE",
                    "vertices": mesh.vertices,
                    "triangles": mesh.triangles,
                }
            ).encode(),
        )
    )
    return GeometryRenderRequest(geometry=geometry, width_px=256, height_px=256, **kwargs), mesh


def test_intersecting_triangles_use_per_pixel_depth_independent_of_submission_order():
    # Identical projected footprints, crossing depth planes. A centroid painter
    # cannot show red on the left and blue on the right of this same triangle.
    xy = [[10, 10], [90, 10], [50, 90]]
    points = np.asarray(
        [p + [z] for p, z in zip(xy, [1, -1, 0])] + [p + [z] for p, z in zip(xy, [-1, 1, 0])],
        dtype=float,
    )
    triangles = np.asarray([[0, 1, 2], [3, 4, 5]])
    shades = np.asarray([[240, 0, 0], [0, 0, 240]], dtype=np.uint8)
    first, _ = _rasterize_surfaces(points, triangles, shades, 100, 100)
    reverse, _ = _rasterize_surfaces(points, triangles[::-1], shades[::-1], 100, 100)
    assert tuple(first[25, 30]) == (240, 0, 0)
    assert tuple(first[25, 70]) == (0, 0, 240)
    np.testing.assert_array_equal(first, reverse)


def test_planar_quad_has_no_internal_triangle_line_and_png_is_deterministic():
    request, mesh = _request(view="top")
    result = render_mesh(request, mesh)
    pixels = np.asarray(Image.open(io.BytesIO(result.payload)))
    assert np.unique(pixels[45:210, 45:210].reshape(-1, 3), axis=0).shape[0] == 1
    assert result.payload == render_mesh(request, mesh).payload
    assert len(result.resolved_panels) == 1
    assert result.resolved_panels[0].geometry_clipped is False


def test_rear_feature_edges_do_not_bleed_through_an_occluding_surface():
    request, mesh = _request(view="top")
    # A small disconnected rear plate has genuine boundary edges, all hidden
    # behind the original front quad. It must not change any rendered pixel.
    expected = render_mesh(request, mesh).payload
    mesh.vertices.extend([[-0.5, -0.5, -1], [0.5, -0.5, -1], [0.5, 0.5, -1], [-0.5, 0.5, -1]])
    mesh.triangles.extend([[4, 5, 6], [4, 6, 7]])
    assert render_mesh(request, mesh).payload == expected


def test_curved_surface_keeps_shading_without_internal_mesh_grid():
    request, _ = _request(view="front")
    count = 48
    angles = np.linspace(0, 2 * np.pi, count, endpoint=False)
    mesh = MeshData(
        vertices=[[float(np.cos(a)), float(np.sin(a)), z] for z in [-1, 1] for a in angles],
        triangles=[
            triangle
            for i in range(count)
            for triangle in (
                [i, (i + 1) % count, i + count],
                [(i + 1) % count, (i + 1) % count + count, i + count],
            )
        ],
    )
    pixels = np.asarray(Image.open(io.BytesIO(render_mesh(request, mesh).payload)))
    interior = pixels[55:200, 60:195]
    assert not np.any(np.all(interior == (65, 75, 82), axis=2))
    assert np.unique(interior.reshape(-1, 3), axis=0).shape[0] > 5
    assert np.any(np.all(pixels == (65, 75, 82), axis=2))


@pytest.mark.parametrize(
    "view,direction,right",
    [
        ("front", (0, 1, 0), (1, 0, 0)),
        ("back", (0, -1, 0), (-1, 0, 0)),
        ("right", (-1, 0, 0), (0, 1, 0)),
        ("left", (1, 0, 0), (0, -1, 0)),
        ("top", (0, 0, -1), (1, 0, 0)),
        ("bottom", (0, 0, 1), (-1, 0, 0)),
    ],
)
def test_standard_views_have_consistent_screen_handedness(view, direction, right):
    actual_direction, up = _preset_basis(GeometryPreviewView(view))
    np.testing.assert_allclose(actual_direction, direction)
    np.testing.assert_allclose(np.cross(actual_direction, up), right)


def test_isometric_positive_z_projects_up_and_custom_extent_is_physical():
    request, mesh = _request()
    camera = render_mesh(request, mesh).resolved_panels[0].camera
    projected = _project_vertices(np.asarray([[0, 0, 0], [0, 0, 1]]), camera, 256, 256)
    assert projected[1, 1] < projected[0, 1]
    request, mesh = _request(
        presentation={
            "panels": [
                {
                    "panel_key": "detail",
                    "camera": {
                        "camera_kind": "custom",
                        "target_m": [0, 0, 0],
                        "direction": [0, 0, -1],
                        "up": [0, 1, 0],
                        "vertical_extent_m": 4,
                    },
                }
            ]
        }
    )
    panel = render_mesh(request, mesh).resolved_panels[0]
    projected = _project_vertices(np.asarray([[0, 0, 0], [1, 0, 0]]), panel.camera, 256, 256)
    assert projected[1, 0] - projected[0, 0] == 64


def test_four_panels_share_one_png_and_report_actual_crop():
    request, mesh = _request(
        presentation={
            "panels": [
                {
                    "panel_key": name,
                    "camera": {
                        "camera_kind": "preset_region",
                        "view": "top",
                        "target_m": [0, 0, 0],
                        "vertical_extent_m": extent,
                    },
                }
                for name, extent in [("a", 4), ("b", 1), ("c", 4), ("d", 4)]
            ]
        }
    )
    result = render_mesh(request, mesh)
    assert Image.open(io.BytesIO(result.payload)).size == (256, 256)
    assert [(p.x_px, p.y_px, p.width_px, p.height_px) for p in result.resolved_panels] == [
        (0, 0, 128, 128),
        (128, 0, 128, 128),
        (0, 128, 128, 128),
        (128, 128, 128, 128),
    ]
    assert [p.geometry_clipped for p in result.resolved_panels] == [False, True, False, False]


def test_pair_focus_contains_both_frames_and_projects_selected_marker_roles():
    request, mesh = _request()
    scene = {
        "coordinate_space": "geometry_coordinates",
        "geometry_sha256": request.geometry.sha256,
        "interface_overlays": [
            {
                "overlay_id": "model:a",
                "label": "left",
                "marker_kind": "model",
                "frame": {"translation_m": [-0.5, 0, 0]},
                "profile": {"kind": "circular", "diameter_m": 0.3},
            },
            {
                "overlay_id": "temporary:b",
                "label": "right",
                "marker_kind": "temporary",
                "frame": {"translation_m": [0.5, 0, 0]},
                "profile": {"kind": "rectangle", "width_m": 0.3, "height_m": 0.2},
            },
        ],
    }
    presentation = {
        "panels": [
            {
                "panel_key": "pair",
                "camera": {
                    "camera_kind": "interface_focus",
                    "overlay_ids": ["model:a", "temporary:b"],
                    "orientation": "axial",
                },
            }
        ],
        "overlay": {
            "overlay_kind": "interfaces",
            "focus": {"focus_kind": "interface_pair", "overlay_ids": ["model:a", "temporary:b"]},
        },
    }
    request = GeometryRenderRequest.model_validate(
        {**request.model_dump(), "scene": scene, "presentation": presentation}
    )
    result = render_mesh(request, mesh)
    assert result.resolved_panels[0].visible_overlay_ids == ("model:a", "temporary:b")
    pixels = np.asarray(Image.open(io.BytesIO(result.payload)))
    for color in [(101, 76, 172), (31, 113, 204)]:
        assert np.any(np.all(pixels == color, axis=2))
    presentation["panels"][0]["camera"]["vertical_extent_m"] = 0.5
    request = GeometryRenderRequest.model_validate(
        {**request.model_dump(), "presentation": presentation}
    )
    with pytest.raises(GeometryPreviewRequestError, match="omit it for automatic framing"):
        render_mesh(request, mesh)


def test_profile_screen_clipping_keeps_crossing_segments_and_bounds_work():
    clipped = _clip_screen_segment(np.asarray([-1e12, 50.0]), np.asarray([1e12, 50.0]), 100, 100)
    np.testing.assert_allclose(clipped, [[0, 50], [99, 50]], atol=0.001)
    assert _clip_screen_segment(np.asarray([-20, -20]), np.asarray([-10, -10]), 100, 100) is None


def test_finite_input_that_overflows_render_arithmetic_fails_cleanly():
    request, mesh = _request()
    mesh.vertices = [[-1e308, 0, 0], [1e308, 0, 0], [0, 1, 0]]
    mesh.triangles = [[0, 1, 2]]
    with pytest.raises(GeometryRuntimeValidationError, match="finite rendering arithmetic"):
        render_mesh(request, mesh)
