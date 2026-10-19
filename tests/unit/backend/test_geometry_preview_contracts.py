from __future__ import annotations

import hashlib
import math

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryPreviewAuthorization,
    GeometryPreviewProfile as CatalogGeometryPreviewProfile,
    ProductRealizationPreviewAuthorization,
)
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_interface import CircularInterfaceProfile
from CAD2BIM_backend.domain.geometry_runtime import (
    MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometryBuildDisplayContextRequest,
    GeometryBuildDisplayContextResult,
    GeometryPreview,
    GeometryPreviewCustomCamera,
    GeometryPreviewInterfaceFocusCamera,
    GeometryPreviewInterfacePairFocus,
    GeometryPreviewInterfacesOverlayRequest,
    GeometryPreviewPanel,
    GeometryPreviewPresentation,
    GeometryPreviewPresetFitCamera,
    GeometryPreviewPresetRegionCamera,
    GeometryPreviewProfile,
    GeometryPreviewView,
    GeometryRenderedPreviewMetadata,
    GeometryRenderCoordinateSpace,
    GeometryRenderHostOverlay,
    GeometryRenderInterfaceMarkerKind,
    GeometryRenderInterfaceOverlay,
    GeometryRenderRequest,
    GeometryRenderScene,
    GeometryRenderStraightProfileOverlay,
    GeometryResolvedOrthographicCamera,
    GeometryResolvedPreviewPanel,
    GeometryStoredPreviewMetadata,
    GeometryStraightProfileRecipe,
    GeometrySummary,
    GeometryTopologySummary,
)


def _geometry() -> CanonicalGeometry:
    return CanonicalGeometry(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type="application/vnd.opencascade.brep",
        payload=b"brep",
        sha256="a" * 64,
        summary=GeometrySummary(
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
                triangle_count=12,
                boundary_edge_count=0,
            ),
            surface_area_m2=6.0,
            volume_m3=1.0,
        ),
    )


def _overlay(
    key: str = "left",
    *,
    marker_kind: GeometryRenderInterfaceMarkerKind = GeometryRenderInterfaceMarkerKind.MODEL,
) -> GeometryRenderInterfaceOverlay:
    return GeometryRenderInterfaceOverlay(
        overlay_id=f"model:{key}",
        label=key,
        marker_kind=marker_kind,
        frame={},
        profile={"kind": "circular", "diameter_m": 0.1},
    )


def _scene(*overlays: GeometryRenderInterfaceOverlay) -> GeometryRenderScene:
    return GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.GEOMETRY_COORDINATES,
        geometry_sha256="a" * 64,
        interface_overlays=overlays,
    )


def _resolved_camera() -> GeometryResolvedOrthographicCamera:
    return GeometryResolvedOrthographicCamera(
        target_m=(0.0, 0.0, 0.0),
        direction=(0.0, 1.0, 0.0),
        up=(0.0, 0.0, 1.0),
        right=(1.0, 0.0, 0.0),
        horizontal_extent_m=2.0,
        vertical_extent_m=1.5,
    )


def _resolved_panel(key: str = "main") -> GeometryResolvedPreviewPanel:
    return GeometryResolvedPreviewPanel(
        panel_key=key,
        x_px=0,
        y_px=0,
        width_px=640,
        height_px=480,
        camera=_resolved_camera(),
    )


def test_profile_reexport_and_default_render_shapes_stay_exact() -> None:
    assert CatalogGeometryPreviewProfile is GeometryPreviewProfile
    assert GeometryPreviewProfile().model_dump(mode="json") == {
        "view": "isometric",
        "width_px": 640,
        "height_px": 480,
    }
    request = GeometryRenderRequest(
        geometry=_geometry(),
        view=GeometryPreviewView.FRONT,
        width_px=800,
        height_px=600,
    )
    dumped = request.model_dump(mode="json")
    assert dumped["view"] == "front"
    assert dumped["width_px"] == 800
    assert dumped["height_px"] == 600
    assert "presentation" not in dumped
    assert "scene" not in dumped

    payload = b"png"
    preview = GeometryPreview(
        view=GeometryPreviewView.FRONT,
        width_px=800,
        height_px=600,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert "resolved_panels" not in preview.model_dump(mode="json")


@pytest.mark.parametrize("option", [
    {"mode": "none", "items": []}, {"mode": "auto", "axis": [1, 0, 0]},
    {"mode": "pipes", "items": []},
    {"mode": "pipes", "items": [{"start_m": [0, 0, 0], "end_m": [0, 0, 0], "outer_diameter_m": 0.05}]},
    {"mode": "pipes", "items": [{"start_m": [0, 0, 0], "end_m": [1, 0, 0], "outer_diameter_m": 0}]},
    {"mode": "pipes", "items": [{"start_m": [0, float("inf"), 0], "end_m": [1, 0, 0], "outer_diameter_m": 0.05}]},
])
def test_connection_context_rejects_open_or_invalid_display_input(option):
    with pytest.raises(ValueError):
        GeometryPreviewProfile(connection_context=option)


def test_connection_context_shared_profile_preserves_default_and_closed_pipe_fields():
    option = {"mode": "pipes", "items": [{"start_m": [0, 0, 0], "end_m": [1, 0, 0], "outer_diameter_m": 0.05}]}
    assert GeometryPreviewProfile(connection_context=option).model_dump(mode="json")["connection_context"] == option
    assert GeometryPreviewProfile(connection_context=None).model_dump(mode="json") == GeometryPreviewProfile().model_dump(mode="json")


def test_preview_views_cover_both_sides_of_each_axis_and_isometric() -> None:
    assert {item.value for item in GeometryPreviewView} == {
        "isometric",
        "front",
        "back",
        "top",
        "bottom",
        "right",
        "left",
    }


def test_camera_branches_are_closed_and_numerically_bounded() -> None:
    GeometryPreviewPresetRegionCamera(
        view=GeometryPreviewView.TOP,
        target_m=(1e200, -1e200, 0.0),
        vertical_extent_m=1e200,
    )
    GeometryPreviewCustomCamera(
        target_m=(0.0, 0.0, 0.0),
        direction=(0.0, 1.0, 0.0),
        up=(0.0, 0.0, 1.0),
        vertical_extent_m=2.0,
    )
    with pytest.raises(ValidationError, match="finite"):
        GeometryPreviewPresetRegionCamera(
            view=GeometryPreviewView.TOP,
            target_m=(math.inf, 0.0, 0.0),
            vertical_extent_m=1.0,
        )
    with pytest.raises(ValidationError, match="unit vector"):
        GeometryPreviewCustomCamera(
            target_m=(0.0, 0.0, 0.0),
            direction=(0.0, 2.0, 0.0),
            up=(0.0, 0.0, 1.0),
            vertical_extent_m=2.0,
        )
    with pytest.raises(ValidationError, match="perpendicular"):
        GeometryPreviewCustomCamera(
            target_m=(0.0, 0.0, 0.0),
            direction=(0.0, 1.0, 0.0),
            up=(0.0, 1.0, 0.0),
            vertical_extent_m=2.0,
        )
    with pytest.raises(ValidationError, match="extra"):
        GeometryPreviewPresetFitCamera.model_validate(
            {"camera_kind": "preset_fit", "view": "front", "target_m": [0, 0, 0]}
        )


def test_interface_focus_camera_requires_one_or_two_unique_qualified_ids() -> None:
    camera = GeometryPreviewInterfaceFocusCamera(
        overlay_ids=("family:left", "host:pipe"),
        orientation="side_x",
    )
    assert camera.vertical_extent_m is None
    with pytest.raises(ValidationError, match="at most 2"):
        GeometryPreviewInterfaceFocusCamera(
            overlay_ids=("family:a", "family:b", "family:c"),
            orientation="oblique",
        )
    with pytest.raises(ValidationError, match="unique"):
        GeometryPreviewInterfaceFocusCamera(
            overlay_ids=("family:a", "family:a"),
            orientation="axial",
        )


def test_interfaces_overlay_preserves_temporary_replace_set_tristate() -> None:
    inherited = GeometryPreviewInterfacesOverlayRequest()
    cleared = GeometryPreviewInterfacesOverlayRequest(temporary_interfaces=())
    replaced = GeometryPreviewInterfacesOverlayRequest(
        temporary_interfaces=(
            {
                "interface_key": "candidate",
                "local_frame": {},
                "profile": {"kind": "rectangle", "width_m": 0.2, "height_m": 0.1},
            },
        )
    )
    assert "temporary_interfaces" not in inherited.model_dump(mode="json")
    assert cleared.model_dump(mode="json")["temporary_interfaces"] == []
    assert replaced.temporary_interfaces is not None
    assert replaced.temporary_interfaces[0].interface_key == "candidate"


def test_pair_focus_requires_exactly_two_unique_qualified_ids() -> None:
    pair = GeometryPreviewInterfacePairFocus(overlay_ids=("family:left", "family:right"))
    assert pair.overlay_ids == ("family:left", "family:right")
    with pytest.raises(ValidationError):
        GeometryPreviewInterfacePairFocus.model_validate(
            {"focus_kind": "interface_pair", "overlay_ids": ["family:left"]}
        )
    with pytest.raises(ValidationError, match="unique"):
        GeometryPreviewInterfacePairFocus(overlay_ids=("family:left", "family:left"))


def test_presentation_limits_panels_keys_and_minimum_viewports() -> None:
    panels = tuple(
        GeometryPreviewPanel(
            panel_key=f"panel_{index}",
            camera=GeometryPreviewPresetFitCamera(view=GeometryPreviewView.ISOMETRIC),
        )
        for index in range(4)
    )
    modern = GeometryPreviewProfile(
        width_px=128,
        height_px=128,
        presentation=GeometryPreviewPresentation(panels=panels),
    )
    assert "view" not in modern.model_dump(mode="json")
    with pytest.raises(ValidationError, match="at most 4"):
        GeometryPreviewPresentation(
            panels=panels
            + (
                GeometryPreviewPanel(
                    panel_key="panel_4",
                    camera=GeometryPreviewPresetFitCamera(view=GeometryPreviewView.FRONT),
                ),
            )
        )
    with pytest.raises(ValidationError, match="unique"):
        GeometryPreviewPresentation(panels=(panels[0], panels[0]))
    with pytest.raises(ValidationError, match="too small"):
        GeometryPreviewProfile(
            width_px=127,
            height_px=128,
            presentation=GeometryPreviewPresentation(panels=panels),
        )
    with pytest.raises(ValidationError, match="legacy view"):
        GeometryPreviewProfile(
            view=GeometryPreviewView.FRONT,
            presentation=GeometryPreviewPresentation(panels=(panels[0],)),
        )


def test_scene_rejects_duplicate_and_excess_overlay_facts() -> None:
    with pytest.raises(ValidationError, match="unique"):
        _scene(_overlay(), _overlay())
    overlays = tuple(
        _overlay(f"item_{index}") for index in range(MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS + 1)
    )
    with pytest.raises(ValidationError, match="at most 128"):
        _scene(*overlays)
    with pytest.raises(ValidationError, match="String should match pattern"):
        GeometryRenderInterfaceOverlay(
            overlay_id="other:item",
            label="item",
            marker_kind="model",
            frame={},
        )
    with pytest.raises(ValidationError, match="source and marker kind"):
        GeometryRenderInterfaceOverlay(
            overlay_id="host:item",
            label="item",
            marker_kind="model",
            frame={},
        )


def test_render_request_closes_geometry_scene_and_interface_references() -> None:
    overlay = _overlay()
    presentation = GeometryPreviewPresentation(
        panels=(
            GeometryPreviewPanel(
                panel_key="focus",
                camera=GeometryPreviewInterfaceFocusCamera(
                    overlay_ids=(overlay.overlay_id,),
                    orientation="side_y",
                ),
            ),
        ),
        overlay=GeometryPreviewInterfacesOverlayRequest(),
    )
    request = GeometryRenderRequest(
        geometry=_geometry(),
        presentation=presentation,
        scene=_scene(overlay),
    )
    assert request.scene is not None
    with pytest.raises(ValidationError, match="requires a render scene"):
        GeometryRenderRequest(geometry=_geometry(), presentation=presentation)
    with pytest.raises(ValidationError, match="absent"):
        GeometryRenderRequest(
            geometry=_geometry(),
            presentation=presentation,
            scene=_scene(_overlay("other")),
        )
    with pytest.raises(ValidationError, match="digest"):
        GeometryRenderRequest(
            geometry=_geometry(),
            scene=GeometryRenderScene(
                coordinate_space="geometry_coordinates",
                geometry_sha256="b" * 64,
            ),
        )


def test_pair_highlight_references_must_exist_in_the_same_scene() -> None:
    presentation = GeometryPreviewPresentation(
        panels=(
            GeometryPreviewPanel(
                panel_key="main",
                camera=GeometryPreviewPresetFitCamera(view=GeometryPreviewView.FRONT),
            ),
        ),
        overlay=GeometryPreviewInterfacesOverlayRequest(
            focus=GeometryPreviewInterfacePairFocus(overlay_ids=("family:left", "family:right"))
        ),
    )
    with pytest.raises(ValidationError, match="absent"):
        GeometryRenderRequest(
            geometry=_geometry(),
            presentation=presentation,
            scene=_scene(_overlay()),
        )


def test_resolved_camera_enforces_direction_cross_up_basis() -> None:
    assert _resolved_camera().right == (1.0, 0.0, 0.0)
    with pytest.raises(ValidationError, match="direction cross up"):
        GeometryResolvedOrthographicCamera(
            target_m=(0.0, 0.0, 0.0),
            direction=(0.0, 1.0, 0.0),
            up=(0.0, 0.0, 1.0),
            right=(-1.0, 0.0, 0.0),
            horizontal_extent_m=2.0,
            vertical_extent_m=1.5,
        )


def test_resolved_panel_disclosure_is_unique_disjoint_and_within_canvas() -> None:
    panel = GeometryResolvedPreviewPanel.model_validate(
        {
            **_resolved_panel().model_dump(),
            "visible_overlay_ids": ("model:left",),
            "hidden_overlay_ids": ("model:right",),
        }
    )
    payload = b"png"
    preview = GeometryPreview(
        width_px=640,
        height_px=480,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        resolved_panels=(panel,),
    )
    dumped = preview.model_dump(mode="json")
    assert "view" not in dumped
    assert dumped["resolved_panels"][0]["visible_overlay_ids"] == ["model:left"]
    with pytest.raises(ValidationError, match="both visible and hidden"):
        GeometryResolvedPreviewPanel.model_validate(
            {
                **_resolved_panel().model_dump(),
                "visible_overlay_ids": ("model:left",),
                "hidden_overlay_ids": ("model:left",),
            }
        )
    with pytest.raises(ValidationError, match="exceeds its canvas"):
        GeometryPreview(
            width_px=640,
            height_px=480,
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            resolved_panels=(
                GeometryResolvedPreviewPanel(
                    panel_key="outside",
                    x_px=600,
                    y_px=0,
                    width_px=64,
                    height_px=64,
                    camera=_resolved_camera(),
                ),
            ),
        )


@pytest.mark.parametrize(
    "authorization_type,reference_field",
    [
        (GeometryPreviewAuthorization, "resource_version_ref"),
        (ProductRealizationPreviewAuthorization, "family_version_ref"),
    ],
)
def test_authorizations_omit_absent_fields_and_close_interface_metadata(
    authorization_type,
    reference_field: str,
) -> None:
    payload = b"png"
    common = {
        reference_field: "ref",
        "title": "Preview",
        "size_bytes": len(payload),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "content": payload,
    }
    legacy = authorization_type(**common)
    dumped = legacy.model_dump(mode="json")
    assert "render_metadata" not in dumped
    assert "interface_overlays" not in dumped

    scene = _scene(_overlay())
    metadata = GeometryRenderedPreviewMetadata(
        scene_sha256=sha256_digest(scene),
        coordinate_space=scene.coordinate_space,
        resolved_panels=(_resolved_panel(),),
    )
    modern = authorization_type(
        **common,
        render_metadata=metadata,
        interface_overlays=scene.interface_overlays,
    )
    assert modern.render_metadata == metadata
    with pytest.raises(ValidationError, match="require rendered metadata"):
        authorization_type(
            **common,
            render_metadata=GeometryStoredPreviewMetadata(),
            interface_overlays=scene.interface_overlays,
        )
    disclosed = metadata.model_copy(
        update={
            "resolved_panels": (
                GeometryResolvedPreviewPanel.model_validate(
                    {
                        **_resolved_panel().model_dump(),
                        "visible_overlay_ids": ("model:other",),
                    }
                ),
            )
        }
    )
    with pytest.raises(ValidationError, match="absent interface overlay"):
        authorization_type(
            **common,
            render_metadata=disclosed,
            interface_overlays=scene.interface_overlays,
        )


def test_product_authorization_projects_optional_frame_status() -> None:
    payload = b"png"
    legacy = ProductRealizationPreviewAuthorization(
        family_version_ref="family-ref",
        title="Preview",
        size_bytes=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        content=payload,
    )
    legacy_dump = legacy.model_dump(mode="json")
    assert "frame_check_status" not in legacy_dump

    projected = legacy.model_copy(update={"frame_check_status": "FAIL"})
    assert projected.model_dump(mode="json")["frame_check_status"] == "FAIL"


def test_straight_profile_build_contract_is_neutral_ordered_and_bounded() -> None:
    recipe = GeometryStraightProfileRecipe(
        recipe_key="fixture:one",
        frame=RigidTransform3D(translation_m=(1.0, 2.0, 3.0)),
        profile=CircularInterfaceProfile(diameter_m=0.2),
        length_m=4.0,
    )
    request = GeometryBuildDisplayContextRequest(recipes=(recipe,))
    result = GeometryBuildDisplayContextResult(
        geometry=_geometry(),
        recipe_digest=sha256_digest(request),
        recipe_keys=(recipe.recipe_key,),
    )
    assert result.recipe_keys == ("fixture:one",)
    with pytest.raises(ValidationError, match="unique"):
        GeometryBuildDisplayContextRequest(recipes=(recipe, recipe))
    with pytest.raises(ValidationError, match="greater than 0"):
        GeometryStraightProfileRecipe.model_validate({**recipe.model_dump(), "length_m": 0.0})


def test_geometry_renderer_context_checks_host_scene_consistency() -> None:
    profile = CircularInterfaceProfile(diameter_m=0.2)
    host_marker = GeometryRenderInterfaceOverlay(
        overlay_id="host:pipe",
        label="pipe",
        marker_kind=GeometryRenderInterfaceMarkerKind.HOST_ATTACHMENT,
        frame=RigidTransform3D(translation_m=(0.0, 0.0, 6.0)),
        profile=profile,
    )
    candidate_marker = GeometryRenderInterfaceOverlay(
        overlay_id="candidate:pipe",
        label="pipe candidate",
        marker_kind=GeometryRenderInterfaceMarkerKind.HOST_CANDIDATE,
        frame=RigidTransform3D(translation_m=(0.0, 0.0, 4.0)),
        profile=profile,
    )
    original = GeometryRenderStraightProfileOverlay(
        recipe_key="host:pipe",
        frame=RigidTransform3D(),
        profile=profile,
        length_m=6.0,
        display_kind="original",
    )
    candidate = GeometryRenderStraightProfileOverlay(
        recipe_key="candidate:pipe",
        frame=RigidTransform3D(),
        profile=profile,
        length_m=4.0,
        display_kind="candidate",
    )
    context = _geometry().model_copy(update={"sha256": "b" * 64})
    scene = GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256=_geometry().sha256,
        display_context_sha256=context.sha256,
        interface_overlays=(host_marker, candidate_marker),
        host_overlays=(
            GeometryRenderHostOverlay(
                overlay_id="host:pipe",
                construction_kind="retained_straight",
            ),
        ),
        straight_profile_overlays=(original, candidate),
    )
    request = GeometryRenderRequest(
        geometry=_geometry(),
        scene=scene,
        display_context=context,
    )
    assert request.display_context == context

    with pytest.raises(ValidationError, match="matching interface marker"):
        GeometryRenderScene.model_validate(
            {
                **scene.model_dump(mode="python"),
                "interface_overlays": (host_marker,),
            }
        )
    with pytest.raises(ValidationError, match="digest does not match"):
        GeometryRenderRequest(
            geometry=_geometry(),
            scene=scene,
            display_context=context.model_copy(update={"sha256": "d" * 64}),
        )


def test_preview_defaults_omit_unused_context_and_overlay_fields() -> None:
    scene = _scene()
    assert "display_context_sha256" not in scene.model_dump(mode="json")
    assert "host_overlays" not in scene.model_dump(mode="json")
    assert "straight_profile_overlays" not in scene.model_dump(mode="json")
    request = GeometryRenderRequest(geometry=_geometry(), scene=scene)
    assert "display_context" not in request.model_dump(mode="json")
    metadata = GeometryRenderedPreviewMetadata(
        scene_sha256=sha256_digest(scene),
        coordinate_space=scene.coordinate_space,
        resolved_panels=(_resolved_panel(),),
    )
    payload = b"png"
    authorization = ProductRealizationPreviewAuthorization(
        family_version_ref="family-ref",
        title="Preview",
        size_bytes=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        content=payload,
        render_metadata=metadata,
    )
    dumped = authorization.model_dump(mode="json")
    assert "host_overlays" not in dumped
    assert "straight_profile_overlays" not in dumped
