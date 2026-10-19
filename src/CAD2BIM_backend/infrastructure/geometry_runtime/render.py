"""Shared deterministic orthographic surface rendering and interface overlays."""

from __future__ import annotations

import hashlib
import io
import math
from collections import defaultdict

import numpy as np
from PIL import Image, ImageDraw

from CAD2BIM_backend.domain.geometry_interface import (
    CircularInterfaceProfile,
    PolygonInterfaceProfile,
    RectangleInterfaceProfile,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryDisplayQualityWarning,
    GeometryPreview,
    GeometryPreviewCustomCamera,
    GeometryPreviewInterfaceFocusCamera,
    GeometryPreviewInterfacesOverlayRequest,
    GeometryPreviewPanel,
    GeometryPreviewPresetFitCamera,
    GeometryPreviewPresetRegionCamera,
    GeometryPreviewRequestError,
    GeometryPreviewView,
    GeometryRenderRequest,
    GeometryResolvedOrthographicCamera,
    GeometryResolvedPreviewPanel,
    GeometryRuntimeValidationError,
    GeometrySectionImage,
    GeometrySectionDiagnostic,
)
from CAD2BIM_backend.domain.geometry_inspection import GeometrySectionDisplay
from CAD2BIM_backend.infrastructure.geometry_runtime.codec import MeshData

_BACKGROUND = (248, 248, 245)
_EDGE = (65, 75, 82)
_MARKER_COLORS = {
    "model": (101, 76, 172),
    "family_internal": (193, 104, 24),
    "family_external": (0, 126, 116),
    "host_attachment": (90, 102, 118),
    "host_candidate": (46, 132, 77),
    "temporary": (31, 113, 204),
}
_MARKER_LABELS = {
    "model": "model",
    "family_internal": "internal",
    "family_external": "external",
    "host_attachment": "host",
    "host_candidate": "candidate",
    "temporary": "temporary",
}


def render_mesh(
    request: GeometryRenderRequest, mesh: MeshData, *, display_context_mesh: MeshData | None = None,
    reference_meshes: tuple[MeshData, ...] = (),
    source_mesh_pairs: tuple | None = None,
) -> GeometryPreview:
    """Every panel consumes this one already reopened mesh and exact scene."""
    if not mesh.vertices or not mesh.triangles:
        raise GeometryRuntimeValidationError("geometry has no renderable faces")
    if (request.display_context is None) != (display_context_mesh is None):
        raise GeometryRuntimeValidationError("render display context mesh is missing or unexpected")
    if len(reference_meshes) != len(request.reference_geometries):
        raise GeometryRuntimeValidationError("render reference meshes are missing or unexpected")
    try:
        with np.errstate(over="raise", divide="raise", invalid="raise"):
            return _render_mesh(request, mesh, display_context_mesh, reference_meshes, source_mesh_pairs)
    except (FloatingPointError, OverflowError) as exc:
        raise GeometryRuntimeValidationError(
            "preview coordinates or viewport exceed finite rendering arithmetic"
        ) from exc


def _render_mesh(request, mesh, display_context_mesh, reference_meshes, source_mesh_pairs=None):
    mesh_pairs = list(source_mesh_pairs) if source_mesh_pairs is not None else [(request.geometry, mesh, "Selected whole")]
    if display_context_mesh is not None:
        mesh_pairs.append((request.display_context, display_context_mesh,
                           "Display context; no independently queryable target in this response"))
    mesh_pairs.extend((geometry, part, f"Reference {request.scene.reference_geometries[index].reference_key!r}; query its exact resource version separately")
                      for index, (geometry, part) in enumerate(zip(request.reference_geometries, reference_meshes, strict=True)))
    warnings = []
    for geometry, display_mesh, label in mesh_pairs:
        for facts in display_mesh.display_quality_warnings:
            warning = GeometryDisplayQualityWarning(geometry_sha256=geometry.sha256, **facts)
            warnings.append(warning.model_copy(update={"message": f"{label}: {warning.message}"}))
    warnings = tuple(warnings)
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    triangles = np.asarray(mesh.triangles, dtype=np.int64)
    product_triangle_count = len(triangles)
    if display_context_mesh is not None:
        if not display_context_mesh.vertices or not display_context_mesh.triangles:
            raise GeometryRuntimeValidationError("display context has no renderable faces")
        context_vertices = np.asarray(display_context_mesh.vertices, dtype=np.float64)
        context_triangles = np.asarray(display_context_mesh.triangles, dtype=np.int64) + len(
            vertices
        )
        vertices = np.concatenate((vertices, context_vertices))
        triangles = np.concatenate((triangles, context_triangles))
    reference_triangle_start = len(triangles)
    for declaration, reference_mesh in zip(
        request.scene.reference_geometries if request.scene else (), reference_meshes, strict=True,
    ):
        if not reference_mesh.vertices or not reference_mesh.triangles:
            raise GeometryRuntimeValidationError("reference geometry has no renderable faces")
        transform = declaration.datum_transform
        reference_vertices = np.asarray(reference_mesh.vertices, dtype=np.float64)
        reference_vertices = reference_vertices @ np.asarray(transform.rotation).T + np.asarray(transform.translation_m)
        reference_triangles = np.asarray(reference_mesh.triangles, dtype=np.int64) + len(vertices)
        vertices = np.concatenate((vertices, reference_vertices))
        triangles = np.concatenate((triangles, reference_triangles))
    if not np.isfinite(vertices).all():
        raise GeometryRuntimeValidationError("preview vertices must be finite")
    overlays = () if request.scene is None else request.scene.interface_overlays
    overlay_by_id = {item.overlay_id: item for item in overlays}
    presentation = request.presentation
    panels = (
        (
            GeometryPreviewPanel(
                panel_key=request.view.value,
                camera=GeometryPreviewPresetFitCamera(view=request.view),
            ),
        )
        if presentation is None
        else presentation.panels
    )
    selected_ids = set()
    if presentation is not None and isinstance(
        presentation.overlay, GeometryPreviewInterfacesOverlayRequest
    ):
        focus = presentation.overlay.focus
        selected_ids = set(overlay_by_id) if focus.focus_kind == "all" else set(focus.overlay_ids)
    columns, rows = (1, 1) if len(panels) == 1 else (2, 1) if len(panels) == 2 else (2, 2)
    canvas = Image.new("RGB", (request.width_px, request.height_px), _BACKGROUND)
    normals, shades, edges = _surface_facts(vertices, triangles)
    if display_context_mesh is not None:
        context_light = shades[product_triangle_count:reference_triangle_start, 1].astype(float)
        shades[product_triangle_count:reference_triangle_start] = np.column_stack(
            (context_light * 0.64, context_light * 0.87, context_light * 0.70)
        ).astype(np.uint8)
    if reference_meshes:
        reference_light = shades[reference_triangle_start:, 1].astype(float)
        shades[reference_triangle_start:] = np.column_stack(
            (reference_light * 0.92, reference_light * 0.69, reference_light * 0.43)
        ).astype(np.uint8)
    straight_overlays = () if request.scene is None else request.scene.straight_profile_overlays
    source_paths = () if request.scene is None else request.scene.source_path_overlays
    framing_vertices = vertices
    if straight_overlays:
        framing_vertices = np.concatenate(
            (vertices, *(_straight_profile_points(item) for item in straight_overlays))
        )
    if source_paths:
        framing_vertices = np.concatenate(
            (framing_vertices, *(np.asarray(item.points_m, dtype=np.float64) for item in source_paths))
        )
    resolved = []
    for index, panel in enumerate(panels):
        column, row = index % columns, index // columns
        x0, y0 = column * request.width_px // columns, row * request.height_px // rows
        x1, y1 = (column + 1) * request.width_px // columns, (row + 1) * request.height_px // rows
        width, height = x1 - x0, y1 - y0
        camera = _resolve_camera(panel.camera, framing_vertices, overlay_by_id, width, height)
        projected = _project_vertices(vertices, camera, width, height)
        surface_owners = np.full((height, width), -1, dtype=np.int64)
        pixels, depth = _rasterize_surfaces(
            projected, triangles, shades, width, height, surface_owners
        )
        _draw_feature_edges(pixels, depth, projected, normals, edges, camera, surface_owners)
        panel_image = Image.fromarray(pixels)
        _draw_straight_profiles(panel_image, camera, straight_overlays)
        visible_ids = []
        for item in overlays:
            origin = _project_vertices(
                np.asarray([item.frame.translation_m]), camera, width, height
            )[0]
            if (
                item.overlay_id in selected_ids
                and 0 <= origin[0] < width
                and 0 <= origin[1] < height
            ):
                visible_ids.append(item.overlay_id)
        _draw_source_paths(
            panel_image, camera, source_paths,
            reserved_legend_rows=int(bool(visible_ids) and width >= 256) + 2 * int(bool(warnings)),
        )
        if (
            presentation is not None
            and isinstance(presentation.overlay, GeometryPreviewInterfacesOverlayRequest)
            and presentation.overlay.focus.focus_kind == "interface_pair"
        ):
            _draw_pair_connector(
                panel_image, camera, [overlay_by_id[key] for key in sorted(selected_ids)]
            )
        if visible_ids:
            _draw_overlays(panel_image, camera, tuple(overlay_by_id[key] for key in visible_ids))
        if presentation is not None:
            ImageDraw.Draw(panel_image).text(
                (8, 7),
                panel.panel_key.replace("_", " ")[:38],
                fill=(50, 62, 70),
                stroke_width=1,
                stroke_fill=_BACKGROUND,
            )
        canvas.paste(panel_image, (x0, y0))
        framing_projection = _project_vertices(framing_vertices, camera, width, height)
        clipped = bool(
            (framing_projection[:, 0] < 0).any()
            or (framing_projection[:, 0] > width).any()
            or (framing_projection[:, 1] < 0).any()
            or (framing_projection[:, 1] > height).any()
        )
        resolved.append(
            GeometryResolvedPreviewPanel(
                panel_key=panel.panel_key,
                x_px=x0,
                y_px=y0,
                width_px=width,
                height_px=height,
                camera=camera,
                geometry_clipped=clipped,
                visible_overlay_ids=tuple(visible_ids),
                hidden_overlay_ids=tuple(
                    item.overlay_id for item in overlays if item.overlay_id not in visible_ids
                ),
            )
        )
    if len(panels) > 1:
        draw = ImageDraw.Draw(canvas)
        draw.line(
            (request.width_px // 2, 0, request.width_px // 2, request.height_px - 1),
            fill=(215, 219, 216),
        )
        if rows == 2:
            draw.line(
                (0, request.height_px // 2, request.width_px - 1, request.height_px // 2),
                fill=(215, 219, 216),
            )
    if warnings:
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, request.height_px - 28, request.width_px, request.height_px), fill=(255, 233, 181))
        draw.text((5, request.height_px - 23), "Display quality warning - diagnostic image only", fill=(93, 51, 0))
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=False, compress_level=9)
    payload = output.getvalue()
    return GeometryPreview(
        view=request.view if presentation is None else None,
        width_px=request.width_px,
        height_px=request.height_px,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        resolved_panels=tuple(resolved),
        display_quality_warnings=warnings,
    )


def render_section_measurement(measurement, request):
    """Display sampled contours; measurement objects remain unchanged by cropping."""
    bounds = measurement.projected_bounds_uv_m
    display = request.observation.display
    if display is None:
        if bounds is None:
            bounds = (-.5, .5, -.5, .5)
        umin, umax, vmin, vmax = bounds
        padding = .08 * max(umax - umin, vmax - vmin, 1e-6)
        display = GeometrySectionDisplay(u_min_m=umin-padding, u_max_m=umax+padding,
                                         v_min_m=vmin-padding, v_max_m=vmax+padding)
    clipped = measurement.projected_bounds_uv_m is not None and (
        bounds[0] < display.u_min_m or bounds[1] > display.u_max_m
        or bounds[2] < display.v_min_m or bounds[3] > display.v_max_m
    )
    width, height = request.width_px, request.height_px
    left, top, right, bottom = 52, 58, width - 16, height - 66
    if right <= left or bottom <= top:
        raise GeometryRuntimeValidationError("section canvas is too small for axes and scale; increase its pixel dimensions")
    span_u, span_v = display.u_max_m-display.u_min_m, display.v_max_m-display.v_min_m
    scale = min((right-left)/span_u, (bottom-top)/span_v)
    if not math.isfinite(scale) or scale <= 0:
        raise GeometryRuntimeValidationError("section display exceeds finite pixel arithmetic")
    center_x, center_y = .5*(left+right), .5*(top+bottom)
    left, right = center_x-.5*span_u*scale, center_x+.5*span_u*scale
    top, bottom = center_y-.5*span_v*scale, center_y+.5*span_v*scale
    center_u = .5*display.u_min_m + .5*display.u_max_m
    center_v = .5*display.v_min_m + .5*display.v_max_m
    def screen(point):
        return ((left+right)/2+(point[0]-center_u)*scale,
                (top+bottom)/2-(point[1]-center_v)*scale)
    image = Image.new("RGB", (width, height), _BACKGROUND)
    colors = ((0,126,116), (101,76,172), (193,104,24), (31,113,204), (160,74,86))
    for item in measurement.objects:
        color = colors[item.object_index % len(colors)]
        draw = ImageDraw.Draw(image)
        for curve in item.intersection_curves:
            if len(curve.drawing_points_uv_m) < 2:
                raise GeometryRuntimeValidationError("section contact drawing is unavailable; retain the independently computed measurements")
            points = [screen(p) for p in curve.drawing_points_uv_m]
            for first, second in zip(points, points[1:]):
                line = _clip_section_segment(first, second, (left, top, right, bottom))
                if line is not None:
                    draw.line(line, fill=color, width=2)
        for point in item.intersection_points_uv_m:
            x,y = screen(point)
            if left <= x <= right and top <= y <= bottom:
                draw.ellipse((x-2,y-2,x+2,y+2), fill=color)
        for region in item.material_regions:
            mask = Image.new("L", (width, height), 0)
            mask_draw = ImageDraw.Draw(mask)
            outlines = []
            for boundary in sorted(region.boundaries, key=lambda b: b.role != "outer"):
                if any(len(curve.drawing_points_uv_m) < 2 for curve in boundary.curves):
                    raise GeometryRuntimeValidationError("section boundary drawing is unavailable; retain the independently computed measurements")
                points = [screen(p) for curve in boundary.curves for p in curve.drawing_points_uv_m]
                points = _clip_section_polygon(points, (left, top, right, bottom))
                if len(points) >= 3:
                    mask_draw.polygon(points, fill=255 if boundary.role == "outer" else 0)
                    outlines.append(points)
            tint = tuple(round(.55*c + .45*b) for c,b in zip(color, _BACKGROUND))
            image.paste(tint, (0,0), mask)
            draw = ImageDraw.Draw(image)
            for points in outlines:
                draw.line(points+[points[0]], fill=color, width=2)
    draw = ImageDraw.Draw(image)
    draw.rectangle((left,top,right,bottom), outline=(160,166,166))
    for axis, span in [("u", (display.u_min_m, display.u_max_m)), ("v", (display.v_min_m, display.v_max_m))]:
        for i in range(3):
            value = span[0] + (span[1]-span[0])*i/2
            if axis == "u":
                x,_ = screen((value,center_v))
                draw.text((x-18,bottom+5), f"{value:.4g}", fill=_EDGE)
            else:
                _,y = screen((center_u,value))
                draw.text((2,y-5), f"{value:.4g}", fill=_EDGE)
    bar_m = min(span_u, span_v)*.25
    bar_px = bar_m*scale
    draw.line((left,bottom-10,left+bar_px,bottom-10), fill=_EDGE, width=3)
    draw.text((left+3,bottom-25), f"{bar_m:.4g} m", fill=_EDGE)
    draw.text((5,4), f"Section {measurement.section_index} | offset {measurement.offset_m:.6g} m | {measurement.status}", fill=_EDGE)
    kind = request.selection.selection_kind
    key = getattr(request.selection, f"{kind}_key", None)
    label = kind if key is None else f"{kind}:{key}"
    caption = f"{label} | {request.coordinates.coordinate_space}"
    if draw.textlength(caption) > width-10:
        caption = f"{kind} | {request.coordinates.coordinate_space}; exact key accompanies image"
    draw.text((5,20), caption, fill=_EDGE)
    count = len(measurement.objects)
    draw.text((5,34), f"Material filled; holes unfilled; u/v metres | {count} {'object' if count == 1 else 'objects'}", fill=_EDGE)
    x = 5
    for item in measurement.objects[:2]:
        color = colors[item.object_index % len(colors)]
        label = f"{item.object_index}:{item.fragment_key or item.source_shape_kind}"
        if draw.textlength(label) > width/2-18:
            label = f"{item.object_index}: exact object key accompanies image"
        draw.rectangle((x,height-35,x+8,height-27), fill=color)
        draw.text((x+11,height-37), label, fill=_EDGE)
        x += width/2
    vector_label = lambda vector: '('+', '.join(f'{value:.6g}' for value in vector)+')'
    draw.text((5,height-19), f"O={vector_label(measurement.plane.origin_m)}; n={vector_label(measurement.plane.unit_normal)}; sampled", fill=_EDGE)
    draw.text((right-20,bottom+22), "+u", fill=_EDGE)
    draw.text((left-26,top-12), "+v", fill=_EDGE)
    diagnostics = ()
    if clipped:
        draw.text((width-115,5), "Display clipped", fill=(160,60,0))
        diagnostics = (GeometrySectionDiagnostic(code="DISPLAY_CLIPPED", message="Only the image viewport is clipped; all reported material measurements cover the complete selected-object section."),)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    payload = output.getvalue()
    return GeometrySectionImage(
        section_index=measurement.section_index,
        preview=GeometryPreview(width_px=width, height_px=height, payload=payload, sha256=hashlib.sha256(payload).hexdigest()),
        resolved_display=display, display_clipped=clipped, diagnostics=diagnostics,
    )


def _clip_section_segment(first, second, rectangle):
    if any(not math.isfinite(value) for point in (first, second) for value in point):
        raise GeometryRuntimeValidationError("section contact drawing exceeds finite pixel arithmetic")
    minimum, maximum = 0., 1.
    for axis, lower, upper in ((0, rectangle[0], rectangle[2]), (1, rectangle[1], rectangle[3])):
        delta = second[axis]-first[axis]
        if delta == 0:
            if first[axis] < lower or first[axis] > upper:
                return None
            continue
        entry, exit = (lower-first[axis])/delta, (upper-first[axis])/delta
        if entry > exit:
            entry, exit = exit, entry
        minimum, maximum = max(minimum, entry), min(maximum, exit)
        if minimum > maximum:
            return None
    return tuple(tuple(first[axis]+parameter*(second[axis]-first[axis]) for axis in range(2)) for parameter in (minimum, maximum))


def _clip_section_polygon(points, rectangle):
    """Clip drawing polygons before passing their bounded pixels to Pillow."""
    if any(not math.isfinite(x) or not math.isfinite(y) for x,y in points):
        raise GeometryRuntimeValidationError("section drawing exceeds finite pixel arithmetic")
    left,top,right,bottom = rectangle
    for axis,boundary,keep_greater in [(0,left,True),(0,right,False),(1,top,True),(1,bottom,False)]:
        output = []
        if not points:
            break
        previous = points[-1]
        previous_inside = previous[axis] >= boundary if keep_greater else previous[axis] <= boundary
        for current in points:
            inside = current[axis] >= boundary if keep_greater else current[axis] <= boundary
            if inside != previous_inside:
                fraction = (boundary-previous[axis])/(current[axis]-previous[axis])
                output.append(tuple(previous[i]+fraction*(current[i]-previous[i]) for i in range(2)))
            if inside:
                output.append(current)
            previous,previous_inside = current,inside
        points = output
    return points


def _preset_basis(view):
    """direction points camera-to-target; screen right is direction cross up."""
    directions = {
        GeometryPreviewView.FRONT: (0.0, 1.0, 0.0),
        GeometryPreviewView.BACK: (0.0, -1.0, 0.0),
        GeometryPreviewView.RIGHT: (-1.0, 0.0, 0.0),
        GeometryPreviewView.LEFT: (1.0, 0.0, 0.0),
        GeometryPreviewView.TOP: (0.0, 0.0, -1.0),
        GeometryPreviewView.BOTTOM: (0.0, 0.0, 1.0),
    }
    if view is GeometryPreviewView.ISOMETRIC:
        return np.asarray((-1.0, -1.0, -1.0)) / math.sqrt(3), np.asarray(
            (-1.0, -1.0, 2.0)
        ) / math.sqrt(6)
    up = (
        (0.0, 1.0, 0.0)
        if view in {GeometryPreviewView.TOP, GeometryPreviewView.BOTTOM}
        else (0.0, 0.0, 1.0)
    )
    return np.asarray(directions[view]), np.asarray(up)


def _resolve_camera(spec, vertices, overlay_by_id, width, height):
    aspect = width / height
    margin = min(min(width, height) // 4, max(16, min(width, height) // 12))
    extent = None
    if isinstance(spec, GeometryPreviewCustomCamera):
        direction, up = np.asarray(spec.direction), np.asarray(spec.up)
        target, extent, framing_points = np.asarray(spec.target_m), spec.vertical_extent_m, vertices
    elif isinstance(spec, GeometryPreviewInterfaceFocusCamera):
        frames = [overlay_by_id[key].frame for key in spec.overlay_ids]
        axis_x, axis_y, axis_z = np.asarray(frames[0].rotation).T
        if spec.orientation == "axial":
            direction, up = -axis_z, axis_y
        elif spec.orientation == "side_x":
            direction, up = -axis_x, axis_z
        elif spec.orientation == "side_y":
            direction, up = -axis_y, axis_z
        else:
            direction = -(axis_x + axis_y + axis_z) / math.sqrt(3)
            up = (-axis_x - axis_y + 2 * axis_z) / math.sqrt(6)
        origins = np.asarray([frame.translation_m for frame in frames])
        target = origins[0] + (origins[-1] - origins[0]) / 2
        # Viewport heuristic from actual nearby tessellation, never a BRep contact oracle.
        neighbors = []
        for origin in origins:
            distances = np.linalg.norm(vertices - origin, axis=1)
            neighbors.append(vertices[np.argsort(distances, kind="stable")[:32]])
        framing_points = np.concatenate([origins, *neighbors])
        extent = spec.vertical_extent_m
    else:
        direction, up = _preset_basis(spec.view)
        if isinstance(spec, GeometryPreviewPresetRegionCamera):
            target, extent = np.asarray(spec.target_m), spec.vertical_extent_m
        else:
            low, high = vertices.min(axis=0), vertices.max(axis=0)
            target = low + (high - low) / 2
        framing_points = vertices
    right = np.cross(direction, up)
    relative = framing_points - target
    horizontal, vertical = relative @ right, relative @ up
    if isinstance(spec, GeometryPreviewPresetFitCamera):
        dx, dy = (horizontal.min() + horizontal.max()) / 2, (vertical.min() + vertical.max()) / 2
        target, horizontal, vertical = target + right * dx + up * dy, horizontal - dx, vertical - dy
    if extent is None:
        extent = max(
            2 * float(np.max(np.abs(vertical))) * height / (height - 2 * margin),
            2 * float(np.max(np.abs(horizontal))) * width / (width - 2 * margin) / aspect,
        )
    if not math.isfinite(extent) or extent <= 0:
        raise GeometryRuntimeValidationError("selected preview viewport is degenerate")
    if isinstance(spec, GeometryPreviewInterfaceFocusCamera):
        origins = (
            np.asarray([overlay_by_id[key].frame.translation_m for key in spec.overlay_ids])
            - target
        )
        if (
            np.max(np.abs(origins @ up)) >= extent / 2
            or np.max(np.abs(origins @ right)) >= extent * aspect / 2
        ):
            raise GeometryPreviewRequestError(
                "interface focus extent does not contain both frames; increase "
                "vertical_extent_m or omit it for automatic framing"
            )
    return GeometryResolvedOrthographicCamera(
        target_m=tuple(target),
        direction=tuple(direction),
        up=tuple(up),
        right=tuple(right),
        vertical_extent_m=extent,
        horizontal_extent_m=extent * aspect,
    )


def _project_vertices(vertices, camera, width, height):
    relative = vertices - np.asarray(camera.target_m)
    return np.column_stack(
        (
            width / 2 + (relative @ camera.right) * width / camera.horizontal_extent_m,
            height / 2 - (relative @ camera.up) * height / camera.vertical_extent_m,
            relative @ -np.asarray(camera.direction),
        )
    )


def _surface_facts(vertices, triangles):
    normals = np.cross(
        vertices[triangles[:, 1]] - vertices[triangles[:, 0]],
        vertices[triangles[:, 2]] - vertices[triangles[:, 0]],
    )
    lengths = np.linalg.norm(normals, axis=1)
    normals = normals / np.where(lengths > 0, lengths, 1)[:, None]
    light = np.asarray((0.35, -0.45, 0.82))
    light = light / np.linalg.norm(light)
    base = 150 + np.rint(70 * np.clip(np.abs(normals @ light), 0, 1)).astype(np.int64)
    shades = np.column_stack((base - 28, base - 8, np.minimum(255, base + 16))).astype(np.uint8)
    # Exact-coordinate welding changes render adjacency only, not authoritative geometry.
    keys = [tuple(point) for point in vertices]
    edges = defaultdict(list)
    for index, (a, b, c) in enumerate(triangles.tolist()):
        if lengths[index] == 0:
            continue
        for start, end in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted((keys[start], keys[end])))].append((start, end, index))
    return normals, shades, edges


def _rasterize_surfaces(projected, triangles, shades, width, height, surface_owners=None):
    pixels = np.empty((height, width, 3), dtype=np.uint8)
    pixels[:] = _BACKGROUND
    depth = np.full((height, width), -np.inf, dtype=np.float64)
    # Compute screen bounds and pixel centres once, retaining triangle order
    # and the existing depth/barycentric tolerances for identical visibility.
    all_points = projected[triangles]
    lows = all_points[:, :, :2].min(axis=1)
    highs = all_points[:, :, :2].max(axis=1)
    in_view = (highs[:, 0] >= 0) & (highs[:, 1] >= 0) & (lows[:, 0] < width) & (lows[:, 1] < height)
    pixel_x = np.arange(width, dtype=np.float64) + 0.5
    pixel_y = np.arange(height, dtype=np.float64) + 0.5
    for triangle_index in np.flatnonzero(in_view):
        points, shade = all_points[triangle_index], shades[triangle_index]
        low, high = lows[triangle_index], highs[triangle_index]
        x0, y0 = int(max(0, math.floor(low[0]))), int(max(0, math.floor(low[1])))
        x1, y1 = int(min(width - 1, math.ceil(high[0]))), int(min(height - 1, math.ceil(high[1])))
        a, b, c = points
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < 1e-12:
            continue
        xx = pixel_x[x0 : x1 + 1][None, :]
        yy = pixel_y[y0 : y1 + 1][:, None]
        wa = ((b[1] - c[1]) * (xx - c[0]) + (c[0] - b[0]) * (yy - c[1])) / denominator
        wb = ((c[1] - a[1]) * (xx - c[0]) + (a[0] - c[0]) * (yy - c[1])) / denominator
        wc = 1 - wa - wb
        z = wa * a[2] + wb * b[2] + wc * c[2]
        current = depth[y0 : y1 + 1, x0 : x1 + 1]
        visible = (wa >= -1e-10) & (wb >= -1e-10) & (wc >= -1e-10) & (z > current)
        current[visible] = z[visible]
        pixels[y0 : y1 + 1, x0 : x1 + 1][visible] = shade
        if surface_owners is not None:
            surface_owners[y0 : y1 + 1, x0 : x1 + 1][visible] = triangle_index
    return pixels, depth


def _draw_feature_edges(pixels, depth, projected, normals, edges, camera, surface_owners):
    near, crease_cosine = -np.asarray(camera.direction), math.cos(math.radians(30))
    bias = max(1e-10, float(np.ptp(projected[:, 2])) * 1e-7)
    for incident in edges.values():
        feature = len(incident) == 1
        if not feature:
            normal = normals[incident[0][2]]
            feature = any(
                abs(float(normal @ normals[item[2]])) < crease_cosine
                or float(normal @ near) * float(normals[item[2]] @ near) < -1e-12
                for item in incident[1:]
            )
        if feature:
            a, b, _ = incident[0]
            _depth_line(
                pixels,
                depth,
                projected[a],
                projected[b],
                bias,
                surface_owners,
                tuple(item[2] for item in incident),
            )


def _depth_line(pixels, depth, start, end, bias, surface_owners, incident_triangles):
    """Clip before sampling so microscopic regions cannot allocate enormous lines."""
    height, width = depth.shape
    clipped = _clip_screen_segment(start, end, width, height)
    if clipped is None:
        return
    clipped_start, clipped_end = clipped
    count = max(2, int(np.max(np.abs(clipped_end[:2] - clipped_start[:2])) * 2) + 1)
    values = clipped_start + np.linspace(0, 1, count)[:, None] * (clipped_end - clipped_start)
    xs = np.clip(np.floor(values[:, 0]).astype(int), 0, width - 1)
    ys = np.clip(np.floor(values[:, 1]).astype(int), 0, height - 1)
    visible = values[:, 2] >= depth[ys, xs] - bias
    # An edge sample and the owning surface's pixel centre are different points.
    # The same visible triangle must not falsely occlude its own sloping edge.
    # Ownership avoids a broad depth bias that would expose unrelated rear edges.
    visible |= np.isin(surface_owners[ys, xs], incident_triangles)
    pixels[ys[visible], xs[visible]] = _EDGE


def _draw_pair_connector(image, camera, overlays):
    points = _project_vertices(
        np.asarray([item.frame.translation_m for item in overlays]), camera, *image.size
    )[:, :2]
    start, end = points
    delta, distance = end - start, float(np.linalg.norm(end - start))
    if distance == 0 or distance > 8 * max(image.size):
        return
    draw = ImageDraw.Draw(image)
    for offset in range(0, math.ceil(distance), 9):
        a, b = (
            start + delta * (offset / distance),
            start + delta * (min(offset + 4, distance) / distance),
        )
        draw.line((tuple(a), tuple(b)), fill=(110, 115, 118), width=1)


def _draw_overlays(image, camera, overlays):
    draw = ImageDraw.Draw(image)
    width, height = image.size
    length = min(camera.horizontal_extent_m, camera.vertical_extent_m) * 0.10
    label_boxes = []
    for item in overlays:
        color = _MARKER_COLORS[item.marker_kind.value]
        rotation, origin = np.asarray(item.frame.rotation), np.asarray(item.frame.translation_m)
        axis_x, axis_z = rotation[:, 0], rotation[:, 2]
        points = _project_vertices(
            np.asarray(
                [
                    origin,
                    origin + axis_z * length,
                    origin + axis_x * length * 0.65,
                ]
            ),
            camera,
            width,
            height,
        )
        center, z_tip, x_tip = points[:, :2]
        _draw_profile(draw, item, camera, width, height, color)
        _arrow(draw, center, z_tip, color)
        _arrow(draw, center, x_tip, (185, 64, 65), width=1)
        x, y = center
        if float(np.linalg.norm(z_tip - center)) < 2:
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), outline=color, width=2)
            if float(axis_z @ -np.asarray(camera.direction)) >= 0:
                draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=color)
            else:
                draw.line((x - 2, y - 2, x + 2, y + 2), fill=color)
                draw.line((x - 2, y + 2, x + 2, y - 2), fill=color)
        else:
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
        label = item.label.encode("ascii", "replace").decode()[:38]
        bounds = draw.textbbox((0, 0), label)
        label_width, label_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        label_x = max(4, min(width - label_width - 4, x + 6))
        label_y = max(20, min(height - label_height - 22, y - 14))
        for _ in range(len(label_boxes)):
            if not any(
                label_x < right
                and label_x + label_width > left
                and label_y < bottom
                and label_y + label_height > top
                for left, top, right, bottom in label_boxes
            ):
                break
            label_y = min(height - label_height - 22, label_y + label_height + 5)
        label_boxes.append((label_x, label_y, label_x + label_width, label_y + label_height))
        draw.text(
            (label_x, label_y),
            label,
            fill=color,
            stroke_width=1,
            stroke_fill=_BACKGROUND,
        )
    if width >= 256:
        x = 8
        for kind in dict.fromkeys(item.marker_kind.value for item in overlays):
            label = _MARKER_LABELS[kind]
            draw.rectangle((x, height - 15, x + 7, height - 8), fill=_MARKER_COLORS[kind])
            draw.text((x + 11, height - 17), label, fill=(58, 66, 72))
            x += 20 + len(label) * 6
            if x > width - 70:
                break


def _arrow(draw, start, end, color, width=2):
    delta = end - start
    length = float(np.linalg.norm(delta))
    if length < 2:
        return
    direction = delta / length
    normal = np.asarray((-direction[1], direction[0]))
    draw.line((tuple(start), tuple(end)), fill=color, width=width)
    size = min(6, length / 3)
    left, right = (
        end - direction * size + normal * size / 2,
        end - direction * size - normal * size / 2,
    )
    draw.polygon((tuple(end), tuple(left), tuple(right)), fill=color)


def _draw_profile(draw, item, camera, width, height, color):
    profile = item.profile
    if profile is None:
        return
    local = _profile_local_points(profile)
    world = np.column_stack((local, np.zeros(len(local)))) @ np.asarray(item.frame.rotation).T
    world += np.asarray(item.frame.translation_m)
    points = _project_vertices(world, camera, width, height)[:, :2]
    if not np.isfinite(points).all():
        raise GeometryRuntimeValidationError("preview profile exceeds finite drawing arithmetic")
    for a, b in zip(points, points[1:]):
        clipped = _clip_screen_segment(a, b, width, height)
        if clipped is not None:
            draw.line(tuple(tuple(point) for point in clipped), fill=color, width=1)


def _profile_local_points(profile):
    if isinstance(profile, CircularInterfaceProfile):
        angles = np.linspace(0, 2 * math.pi, 65)
        local = np.column_stack((np.cos(angles), np.sin(angles))) * profile.diameter_m / 2
    elif isinstance(profile, RectangleInterfaceProfile):
        w, h = profile.width_m / 2, profile.height_m / 2
        local = np.asarray(((-w, -h), (w, -h), (w, h), (-w, h), (-w, -h)))
    elif isinstance(profile, PolygonInterfaceProfile):
        local = np.asarray((*profile.points_m, profile.points_m[0]))
    else:
        raise GeometryRuntimeValidationError("preview interface profile is unsupported")
    return local


def _straight_profile_points(item):
    section = _profile_local_points(item.profile)
    local = np.concatenate(
        (
            np.column_stack((section, np.zeros(len(section)))),
            np.column_stack((section, np.full(len(section), item.length_m))),
        )
    )
    return local @ np.asarray(item.frame.rotation).T + np.asarray(item.frame.translation_m)


def _draw_source_paths(image, camera, paths, *, reserved_legend_rows=0):
    """Display exact ordered source points as explanatory, non-solid dashed lines."""
    if not paths:
        return
    draw = ImageDraw.Draw(image)
    width, height = image.size
    color = (119, 146, 184)
    for index, item in enumerate(paths):
        projected = _project_vertices(
            np.asarray(item.points_m, dtype=np.float64), camera, width, height,
        )[:, :2]
        for start, end in zip(projected, projected[1:]):
            clipped = _clip_screen_segment(start, end, width, height)
            if clipped is None:
                continue
            a, b = clipped
            distance = float(np.linalg.norm(b - a))
            if distance <= 0:
                continue
            for offset in range(0, math.ceil(distance), 10):
                line_start = a + (b - a) * (offset / distance)
                line_end = a + (b - a) * (min(offset + 5, distance) / distance)
                draw.line((tuple(line_start), tuple(line_end)), fill=color, width=2)
        draw.text(
            (8, height - 17 * (len(paths) - index + reserved_legend_rows)),
            f"source path: {item.source_label}"[:120],
            fill=(64, 84, 121), stroke_width=1, stroke_fill=_BACKGROUND,
        )


def _draw_straight_profiles(image, camera, overlays):
    if not overlays:
        return
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for item in overlays:
        if item.display_kind != "original":
            continue
        points = _project_vertices(_straight_profile_points(item), camera, width, height)[:, :2]
        count = len(points) // 2
        segments = list(zip(points[: count - 1], points[1:count]))
        segments += list(zip(points[count:-1], points[count + 1 :]))
        segments += [
            (points[i], points[count + i]) for i in range(0, count - 1, max(1, (count - 1) // 4))
        ]
        for start, end in segments:
            clipped = _clip_screen_segment(start, end, width, height)
            if clipped is None:
                continue
            a, b = clipped
            distance = float(np.linalg.norm(b - a))
            if distance == 0:
                continue
            for offset in range(0, math.ceil(distance), 8):
                line_start = a + (b - a) * (offset / distance)
                line_end = a + (b - a) * (min(offset + 3, distance) / distance)
                draw.line((tuple(line_start), tuple(line_end)), fill=(126, 135, 130), width=1)
    kinds = {item.display_kind for item in overlays}
    labels = []
    if "original" in kinds:
        labels.append("original host: dashed")
    if "candidate" in kinds:
        labels.append("candidate: green")
    if "illustrative" in kinds:
        labels.append("connection context: green" if any(
            item.recipe_key.startswith("context:") for item in overlays
        ) else "illustrative host: green")
    draw.text(
        (8, 23), " | ".join(labels), fill=(69, 98, 76), stroke_width=1, stroke_fill=_BACKGROUND
    )


def _clip_screen_segment(start, end, width, height):
    """Bound profile drawing in screen space without dropping crossing segments."""
    delta, t0, t1 = end - start, 0.0, 1.0
    for origin, change, maximum in (
        (start[0], delta[0], width - 1),
        (start[1], delta[1], height - 1),
    ):
        if change == 0:
            if origin < 0 or origin > maximum:
                return None
            continue
        lo, hi = sorted((-origin / change, (maximum - origin) / change))
        t0, t1 = max(t0, lo), min(t1, hi)
        if t0 > t1:
            return None
    return start + delta * t0, start + delta * t1


__all__ = ["render_mesh"]
