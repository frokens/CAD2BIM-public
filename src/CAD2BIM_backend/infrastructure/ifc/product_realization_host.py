"""Conservative read-only IfcOpenShell extraction of realization host facts."""

from __future__ import annotations

import math
import hashlib
from typing import Any

import ifcopenshell.util.unit
import numpy as np
import numpy.typing as npt

from CAD2BIM_backend.domain.ifc_model import IfcModelValidationError
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    IfcSpatialPlacementInvalidError,
    IfcSpatialStructureIntegrityError,
)
from CAD2BIM_backend.domain.geometry_frame import (
    RigidTransform3D,
    rigid_transform_from_axis_reference,
)
from CAD2BIM_backend.domain.product_realization_host import (
    CircularConnectionProfile,
    ProductRealizationAttachmentFact,
    ProductRealizationInterfaceBinding,
    ProductRealizationSectionedStraightHostFact,
    CurrentIfcAxisBoundary,
    CurrentIfcProductBoundarySelector,
    CurrentIfcStraightBody,
    CurrentIfcStraightBodySection,
    MAX_CURRENT_IFC_BODY_SECTIONS,
    current_ifc_body_section_key,
    ProductRealizationHostError,
    ProductRealizationHostErrorCode,
    ResolvedCurrentIfcHostFact,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.placement import (
    placement_matrix,
    project_world_placement,
)


_GEOMETRY_TOLERANCE = 1e-9
_MAX_HOST_PRODUCTS = 100_000


class IfcOpenShellProductRealizationHostFactExtractor:
    """Resolve straight pipe boundaries without mutating or writing the IFC."""

    def __init__(self, codec: IfcOpenShellModelCodec | None = None) -> None:
        self._codec = codec or IfcOpenShellModelCodec()

    def extract(
        self,
        content: bytes,
        selectors: tuple[CurrentIfcProductBoundarySelector, ...],
    ) -> tuple[ResolvedCurrentIfcHostFact, ...]:
        try:
            model = self._codec.private_copy(content).model
        except (IfcModelValidationError, RuntimeError, ValueError, TypeError) as exc:
            raise ProductRealizationHostError(
                ProductRealizationHostErrorCode.IFC_CONTENT_INVALID,
                "The pinned IFC bytes could not be opened for host fact extraction",
            ) from exc
        return self.extract_from_model(model, selectors, ifc_sha256=hashlib.sha256(content).hexdigest())

    def extract_from_model(
        self,
        model: Any,
        selectors: tuple[CurrentIfcProductBoundarySelector, ...],
        *,
        ifc_sha256: str,
    ) -> tuple[ResolvedCurrentIfcHostFact, ...]:
        """Read one already-private candidate using the same exact geometry rules."""
        unit_scale = _project_length_unit_scale(model)
        by_global_id = _product_index(model)
        project_global_id = model.by_type("IfcProject")[0].GlobalId
        bodies: dict[str, CurrentIfcStraightBody] = {}
        selected: list[tuple[CurrentIfcProductBoundarySelector, CurrentIfcStraightBody, CurrentIfcStraightBodySection, bool]] = []
        for selector in selectors:
            body = bodies.get(selector.product_global_id)
            if body is None:
                body = _resolve_current_body(
                    _unique_product(by_global_id, selector.product_global_id),
                    unit_scale=unit_scale, project_global_id=project_global_id, ifc_sha256=ifc_sha256,
                )
                bodies[selector.product_global_id] = body
            section, remapped = _select_body_section(body, selector)
            selected.append((selector, body, section, remapped))
        editable: dict[str, ProductRealizationSectionedStraightHostFact] = {}
        for selector, body, _section, _remapped in selected:
            if selector.edit_permission is not None and selector.attachment_key not in editable:
                editable[selector.attachment_key] = _editable_body_attachment(
                    body, [item for item in selected if item[0].attachment_key == selector.attachment_key],
                )
        resolved: list[ResolvedCurrentIfcHostFact] = []
        for selector, body, section, remapped in selected:
            first = selector.boundary is CurrentIfcAxisBoundary.AXIS_START
            delta = tuple(b - a for a, b in zip(body.centerline_start_m, body.centerline_end_m))
            length = math.hypot(*delta)
            tangent = tuple(v / length for v in delta)
            attachment = editable.get(selector.attachment_key)
            if attachment is None:
                attachment = ProductRealizationAttachmentFact(
                    attachment_key=selector.attachment_key, source_kind="current_ifc",
                    project_world_frame=_boundary_frame(
                        section.centerline_start_m if first else section.centerline_end_m,
                        tuple(-v for v in tangent) if first else tangent,
                    ),
                    profile=CircularConnectionProfile(outer_diameter_m=body.profile.diameter_m),
                )
            resolved.append(ResolvedCurrentIfcHostFact(
                product_global_id=selector.product_global_id, boundary=selector.boundary,
                attachment=attachment,
                interface_binding=ProductRealizationInterfaceBinding(
                    frame_relation=selector.frame_relation,
                    interface_key=selector.interface_key, attachment_key=selector.attachment_key,
                    host_boundary_key=_boundary_key(section, selector.boundary) if selector.edit_permission else None,
                ),
                body=body, body_section_ref=body.section_reference(section), body_reference_remapped=remapped,
            ))
        return tuple(resolved)


def _product_index(model: Any) -> dict[str, list[Any]]:
    products = tuple(model.by_type("IfcProduct"))
    if len(products) > _MAX_HOST_PRODUCTS:
        raise ProductRealizationHostError(ProductRealizationHostErrorCode.IFC_CONTENT_INVALID,
                                          "The pinned IFC exceeds the bounded host product scan")
    result: dict[str, list[Any]] = {}
    for product in products:
        global_id = getattr(product, "GlobalId", None)
        if isinstance(global_id, str):
            result.setdefault(global_id, []).append(product)
    return result


def _unique_product(index: dict[str, list[Any]], global_id: str) -> Any:
    matches = index.get(global_id, ())
    if not matches:
        raise ProductRealizationHostError(ProductRealizationHostErrorCode.PRODUCT_NOT_FOUND,
                                          "The selected IFC product GlobalId does not exist")
    if len(matches) != 1:
        raise ProductRealizationHostError(ProductRealizationHostErrorCode.PRODUCT_AMBIGUOUS,
                                          "The selected IFC product GlobalId is not unique")
    return matches[0]


def inspect_current_straight_body(model: Any, product_global_id: str, *, ifc_sha256: str) -> CurrentIfcStraightBody:
    """Shared read-only projection for Query and private candidate consumers."""
    scale = _project_length_unit_scale(model)
    return _resolve_current_body(
        _unique_product(_product_index(model), product_global_id), unit_scale=scale,
        project_global_id=model.by_type("IfcProject")[0].GlobalId, ifc_sha256=ifc_sha256,
    )


def _project_length_unit_scale(model: Any) -> float:
    projects = tuple(model.by_type("IfcProject"))
    if len(projects) != 1:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.IFC_CONTENT_INVALID,
            "The pinned IFC must contain one project for length-unit resolution",
        )
    assignment = getattr(projects[0], "UnitsInContext", None)
    length_units = tuple(
        unit
        for unit in tuple(getattr(assignment, "Units", ()) or ())
        if str(getattr(unit, "UnitType", "") or "") == "LENGTHUNIT"
    )
    if len(length_units) != 1:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.IFC_CONTENT_INVALID,
            "The pinned IFC must contain one project length unit",
        )
    try:
        unit_scale = float(ifcopenshell.util.unit.calculate_unit_scale(model))
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.IFC_CONTENT_INVALID,
            "The pinned IFC project length unit could not be resolved",
        ) from exc
    if not math.isfinite(unit_scale) or unit_scale <= 0.0:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.IFC_CONTENT_INVALID,
            "The pinned IFC has no usable project length unit",
        )
    return unit_scale


def _resolve_current_body(
    product: Any,
    *,
    unit_scale: float,
    project_global_id: str,
    ifc_sha256: str,
) -> CurrentIfcStraightBody:
    if str(product.is_a()) != "IfcPipeSegment":
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.PRODUCT_CLASS_UNSUPPORTED,
            "Only IfcPipeSegment is supported as a CURRENT IFC host",
        )

    _axis_item, local_start, local_end = _axis(product)
    try:
        world_matrix = placement_matrix(project_world_placement(product))
    except (IfcSpatialPlacementInvalidError, IfcSpatialStructureIntegrityError) as exc:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.PLACEMENT_INVALID,
            "The selected IFC host has no trustworthy project-world placement",
        ) from exc

    start = _transform_point(world_matrix, local_start, unit_scale=unit_scale)
    end = _transform_point(world_matrix, local_end, unit_scale=unit_scale)
    tangent = _normalize(
        tuple(end[index] - start[index] for index in range(3)),
        code=ProductRealizationHostErrorCode.AXIS_INVALID,
        message="The selected IFC host Axis has coincident boundaries",
    )
    diameter, inner_diameter, intervals = _body_sections(
        product, axis_start=start, axis_end=end, world_matrix=world_matrix, unit_scale=unit_scale,
    )
    signature = sha256_digest({
        "project_global_id": project_global_id, "product_global_id": product.GlobalId,
        "centerline_start_m": start, "centerline_end_m": end,
        "diameter_m": diameter, "inner_diameter_m": inner_diameter,
    })
    frame = _boundary_frame(start, tangent)
    return CurrentIfcStraightBody(
        ifc_sha256=ifc_sha256, project_global_id=project_global_id, product_global_id=product.GlobalId,
        geometry_signature=signature,
        body_digest=sha256_digest({"geometry_signature": signature, "station_intervals_m": intervals}),
        centerline_start_m=start, centerline_end_m=end,
        profile_reference_direction=tuple(row[0] for row in frame.rotation),
        profile={"kind": "circular", "diameter_m": diameter}, inner_diameter_m=inner_diameter,
        sections=tuple(CurrentIfcStraightBodySection(
            section_key=current_ifc_body_section_key(signature, interval),
            station_interval_m=interval,
            centerline_start_m=tuple(a + interval[0] * t for a, t in zip(start, tangent)),
            centerline_end_m=tuple(a + interval[1] * t for a, t in zip(start, tangent)),
        ) for interval in intervals),
    )


def _select_body_section(
    body: CurrentIfcStraightBody, selector: CurrentIfcProductBoundarySelector,
) -> tuple[CurrentIfcStraightBodySection, bool]:
    reference = selector.body_section_ref
    if reference is None:
        if len(body.sections) != 1:
            raise ProductRealizationHostError(
                ProductRealizationHostErrorCode.BODY_SECTION_AMBIGUOUS,
                "The CURRENT Body has multiple sections; use ctx.host_sections(product_global_id) to select an actual section reference",
            )
        return body.sections[0], False
    if (reference.project_global_id != body.project_global_id
        or reference.product_global_id != body.product_global_id
        or reference.geometry_signature != body.geometry_signature):
        raise ProductRealizationHostError(ProductRealizationHostErrorCode.BODY_REFERENCE_STALE,
                                          "The referenced Host identity, Axis, placement or profile changed; rediscover its current Body")
    if reference.ifc_sha256 == body.ifc_sha256 and reference.body_digest != body.body_digest:
        raise ProductRealizationHostError(ProductRealizationHostErrorCode.BODY_REFERENCE_STALE,
                                          "The Body reference disagrees with its exact IFC snapshot")
    matches = [section for section in body.sections if section.section_key == reference.section_key]
    if not matches and reference.ifc_sha256 != body.ifc_sha256:
        side = 0 if selector.boundary is CurrentIfcAxisBoundary.AXIS_START else 1
        old_low, old_high = reference.station_interval_m
        matches = [section for section in body.sections
                   if old_low <= section.station_interval_m[0] < section.station_interval_m[1] <= old_high
                   and section.station_interval_m[side] == reference.station_interval_m[side]]
    if len(matches) != 1:
        raise ProductRealizationHostError(ProductRealizationHostErrorCode.BODY_REFERENCE_STALE,
                                          "The selected Body boundary no longer maps uniquely to retained material; rediscover and select the current section")
    return matches[0], body.section_reference(matches[0]) != reference


def _boundary_key(section: CurrentIfcStraightBodySection, boundary: CurrentIfcAxisBoundary) -> str:
    return ("start_" if boundary is CurrentIfcAxisBoundary.AXIS_START else "end_") + section.section_key


def _merged_intervals(intervals: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    result: list[tuple[float, float]] = []
    for low, high in sorted(intervals):
        if result and low <= result[-1][1]:
            result[-1] = (result[-1][0], max(high, result[-1][1]))
        else:
            result.append((low, high))
    return tuple(result)


def _editable_body_attachment(
    body: CurrentIfcStraightBody,
    selected: list[tuple[CurrentIfcProductBoundarySelector, CurrentIfcStraightBody, CurrentIfcStraightBodySection, bool]],
) -> ProductRealizationSectionedStraightHostFact:
    editable: list[tuple[float, float]] = []
    protected: list[tuple[float, float]] = []
    boundaries = []
    for selector, selected_body, section, _remapped in selected:
        permission = selector.edit_permission
        if permission is None or selected_body != body:
            raise ProductRealizationHostError(ProductRealizationHostErrorCode.EDIT_PERMISSION_INVALID,
                                              "One editable attachment must describe one CURRENT pipe with explicit boundary permissions")
        low, high = permission.connection_station_interval_m
        body_low, body_high = section.station_interval_m
        if not body_low <= low <= high <= body_high:
            raise ProductRealizationHostError(ProductRealizationHostErrorCode.EDIT_PERMISSION_INVALID,
                                              "Connection permission must lie inside the selected actual Body section, without crossing a gap")
        first = selector.boundary is CurrentIfcAxisBoundary.AXIS_START
        removable = (body_low, high) if first else (low, body_high)
        if removable[0] < removable[1]:
            editable.append(removable)
        protected.extend(permission.required_retained_station_intervals_m)
        boundaries.append({
            "boundary_key": _boundary_key(section, selector.boundary),
            "retained_side": "after" if first else "before",
            "connection_station_interval_m": (low, high),
        })
    try:
        return ProductRealizationSectionedStraightHostFact(
            attachment_key=selected[0][0].attachment_key, source_kind="current_ifc",
            centerline_start_m=body.centerline_start_m, centerline_end_m=body.centerline_end_m,
            profile_reference_direction=body.profile_reference_direction, profile=body.profile,
            body_station_intervals_m=tuple(section.station_interval_m for section in body.sections),
            editable_station_intervals_m=_merged_intervals(editable),
            required_retained_station_intervals_m=_merged_intervals(protected), boundaries=boundaries,
        )
    except ValueError as exc:
        raise ProductRealizationHostError(ProductRealizationHostErrorCode.EDIT_PERMISSION_INVALID,
                                          "CURRENT Body permissions or boundary mappings are inconsistent with actual retained material") from exc


def _axis(product: Any) -> tuple[Any, tuple[float, float, float], tuple[float, float, float]]:
    product_shape = getattr(product, "Representation", None)
    representations = tuple(getattr(product_shape, "Representations", ()) or ())
    axes = tuple(
        representation
        for representation in representations
        if str(getattr(representation, "RepresentationIdentifier", "") or "") == "Axis"
    )
    if not axes:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_MISSING,
            "The selected IFC host has no Axis representation",
        )
    if len(axes) != 1:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_AMBIGUOUS,
            "The selected IFC host has multiple Axis representations",
        )
    representation = axes[0]
    if str(getattr(representation, "RepresentationType", "") or "") != "Curve3D":
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_UNSUPPORTED,
            "The selected IFC host Axis must be a Curve3D",
        )
    items = tuple(getattr(representation, "Items", ()) or ())
    if not items:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_MISSING,
            "The selected IFC host Axis representation is empty",
        )
    if len(items) != 1:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_AMBIGUOUS,
            "The selected IFC host Axis representation must contain one curve",
        )
    start, end = _straight_curve_endpoints(items[0])
    return items[0], start, end


def _straight_curve_endpoints(
    curve: Any,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if curve.is_a("IfcPolyline"):
        raw_points = tuple(getattr(curve, "Points", ()) or ())
        if len(raw_points) != 2:
            raise ProductRealizationHostError(
                ProductRealizationHostErrorCode.AXIS_UNSUPPORTED,
                "Only a two-point IfcPolyline host Axis is supported",
            )
        points = tuple(getattr(point, "Coordinates", ()) for point in raw_points)
    elif curve.is_a("IfcIndexedPolyCurve"):
        segments = tuple(getattr(curve, "Segments", ()) or ())
        coord_list = tuple(getattr(getattr(curve, "Points", None), "CoordList", ()) or ())
        if segments or len(coord_list) != 2:
            raise ProductRealizationHostError(
                ProductRealizationHostErrorCode.AXIS_UNSUPPORTED,
                "Only an unsegmented two-point IfcIndexedPolyCurve host Axis is supported",
            )
        points = coord_list
    else:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_UNSUPPORTED,
            "The selected IFC host Axis curve type is not supported",
        )
    try:
        converted = tuple(tuple(float(value) for value in point) for point in points)
    except (TypeError, ValueError) as exc:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_INVALID,
            "The selected IFC host Axis coordinates are invalid",
        ) from exc
    if len(converted) != 2 or any(len(point) != 3 for point in converted):
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_INVALID,
            "The selected IFC host Axis must have two finite 3D endpoints",
        )
    if not all(math.isfinite(value) for point in converted for value in point):
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_INVALID,
            "The selected IFC host Axis must have two finite 3D endpoints",
        )
    return converted[0], converted[1]


def _body_sections(
    product: Any,
    *,
    axis_start: tuple[float, float, float],
    axis_end: tuple[float, float, float],
    world_matrix: npt.NDArray[np.float64],
    unit_scale: float,
) -> tuple[float, float | None, tuple[tuple[float, float], ...]]:
    product_shape = getattr(product, "Representation", None)
    representations = tuple(getattr(product_shape, "Representations", ()) or ())
    bodies = tuple(
        representation
        for representation in representations
        if str(getattr(representation, "RepresentationIdentifier", "") or "") == "Body"
    )
    if not bodies:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.PROFILE_MISSING,
            "The selected IFC host has no circular Body profile",
        )
    if len(bodies) != 1:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.PROFILE_AMBIGUOUS,
            "The selected IFC host has multiple Body representations",
        )
    items = tuple(getattr(bodies[0], "Items", ()) or ())
    if not items:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.PROFILE_MISSING,
            "The selected IFC host Body is empty",
        )
    if len(items) > MAX_CURRENT_IFC_BODY_SECTIONS:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.BODY_UNSUPPORTED,
            "The selected IFC Body exceeds the supported straight-section count",
        )
    length = math.dist(axis_start, axis_end)
    tangent = tuple((b-a)/length for a, b in zip(axis_start, axis_end))
    profiles: list[tuple[float, float | None]] = []
    intervals = []
    for solid in items:
        if str(solid.is_a()) != "IfcSweptDiskSolid" or solid.StartParam is not None or solid.EndParam is not None:
            raise ProductRealizationHostError(
                ProductRealizationHostErrorCode.BODY_UNSUPPORTED,
                "CURRENT straight Body requires IfcSweptDiskSolid items with bounded two-point Directrix and omitted StartParam/EndParam",
            )
        try:
            diameter = 2.0 * float(solid.Radius) * unit_scale
            inner = 2.0 * float(solid.InnerRadius) * unit_scale if solid.InnerRadius is not None else None
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProductRealizationHostError(ProductRealizationHostErrorCode.PROFILE_INVALID,
                                              "The selected IFC Body radii are invalid") from exc
        if not math.isfinite(diameter) or diameter <= 0 or (inner is not None and (not math.isfinite(inner) or not 0 < inner < diameter)):
            raise ProductRealizationHostError(ProductRealizationHostErrorCode.PROFILE_INVALID,
                                              "The selected IFC Body requires finite positive outer radius and a smaller positive inner radius")
        profiles.append((diameter, inner))
        try:
            local_points = _straight_curve_endpoints(solid.Directrix)
        except ProductRealizationHostError as exc:
            raise ProductRealizationHostError(ProductRealizationHostErrorCode.BODY_UNSUPPORTED,
                                              "The selected IFC Body Directrix must be a supported two-point straight curve") from exc
        stations = []
        for point in local_points:
            world = _transform_point(world_matrix, point, unit_scale=unit_scale)
            station = sum((v-a)*t for v, a, t in zip(world, axis_start, tangent))
            projected = tuple(a+station*t for a, t in zip(axis_start, tangent))
            if (math.dist(world, projected) > _GEOMETRY_TOLERANCE
                or station < -_GEOMETRY_TOLERANCE or station > length + _GEOMETRY_TOLERANCE):
                raise ProductRealizationHostError(ProductRealizationHostErrorCode.PROFILE_INVALID,
                                                  "The selected IFC Body must be collinear with and contained in the original finite Axis")
            if abs(station) <= _GEOMETRY_TOLERANCE:
                station = 0.0
            elif abs(station-length) <= _GEOMETRY_TOLERANCE:
                station = length
            else:
                station = round(station, 12)
            stations.append(station)
        low, high = sorted(stations)
        if high-low <= _GEOMETRY_TOLERANCE:
            raise ProductRealizationHostError(ProductRealizationHostErrorCode.BODY_INVALID,
                                              "The selected IFC Body section has no trustworthy positive length")
        intervals.append((low, high))
    diameter, inner = profiles[0]
    if any(profile != profiles[0] for profile in profiles[1:]):
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.PROFILE_AMBIGUOUS,
            "The selected IFC Body sections have different circular profiles",
        )
    intervals.sort()
    if any(right[0]-left[1] <= _GEOMETRY_TOLERANCE for left, right in zip(intervals, intervals[1:])):
        raise ProductRealizationHostError(ProductRealizationHostErrorCode.PROFILE_AMBIGUOUS,
                                          "The selected IFC Body items overlap, duplicate or touch; no artificial internal boundary is inferred")
    return diameter, inner, tuple(intervals)


def _transform_point(
    matrix: npt.NDArray[np.float64],
    point: tuple[float, float, float],
    *,
    unit_scale: float,
) -> tuple[float, float, float]:
    value = matrix @ np.asarray((*point, 1.0), dtype=float)
    result = tuple(float(value[index]) * unit_scale for index in range(3))
    if not all(math.isfinite(item) for item in result):
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.PLACEMENT_INVALID,
            "The selected IFC host project-world Axis is not finite",
        )
    return result


def _boundary_frame(
    origin: tuple[float, float, float],
    outward: tuple[float, float, float],
) -> RigidTransform3D:
    z_axis = _normalize(
        outward,
        code=ProductRealizationHostErrorCode.AXIS_INVALID,
        message="The selected IFC host boundary direction is invalid",
    )
    x_axis: tuple[float, float, float] | None = None
    for reference in ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
        projected = tuple(
            reference[index]
            - sum(reference[item] * z_axis[item] for item in range(3)) * z_axis[index]
            for index in range(3)
        )
        norm = math.sqrt(sum(item * item for item in projected))
        if norm > _GEOMETRY_TOLERANCE:
            x_axis = tuple(item / norm for item in projected)
            break
    if x_axis is None:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_INVALID,
            "The selected IFC host boundary frame could not be constructed",
        )
    try:
        return rigid_transform_from_axis_reference(origin, z_axis, x_axis)
    except ValueError as exc:
        raise ProductRealizationHostError(
            ProductRealizationHostErrorCode.AXIS_INVALID,
            "The selected IFC host boundary frame could not be constructed",
        ) from exc


def _normalize(
    value: tuple[float, float, float],
    *,
    code: ProductRealizationHostErrorCode,
    message: str,
) -> tuple[float, float, float]:
    norm = math.sqrt(sum(item * item for item in value))
    if not math.isfinite(norm) or norm <= _GEOMETRY_TOLERANCE:
        raise ProductRealizationHostError(code, message)
    return tuple(item / norm for item in value)


__all__ = ["IfcOpenShellProductRealizationHostFactExtractor", "inspect_current_straight_body"]
