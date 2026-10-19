from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import numpy as np
import pyarrow as pa
from ezdxf.math import OCS, Vec3
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree

from CAD2BIM_backend.infrastructure.cad_measurement.schemas import (
    MEASUREMENT_PAYLOAD_SCHEMAS,
)


_PATH_TYPES = frozenset({"LINE", "LWPOLYLINE", "POLYLINE"})
_POINT_TYPE = "POINT"
_EPS = 1e-12
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class GeometryInputError(ValueError):
    """The caller supplied an invalid pure-kernel input."""

    def __init__(self, message: str, *, code: str = "INVALID_ARGUMENT") -> None:
        self.code = code
        super().__init__(message)


class GeometryBlockerError(RuntimeError):
    """Selected source geometry cannot produce an atomic complete result."""

    def __init__(self, diagnostics: Sequence[Mapping[str, Any]]) -> None:
        self.diagnostics = tuple(dict(row) for row in diagnostics)
        codes = sorted(
            {str(row["diagnostic_code"]) for row in diagnostics if row["severity"] == "blocker"}
        )
        mapping = {
            "UNSUPPORTED_GEOMETRY": "CAD_GEOMETRY_UNSUPPORTED",
            "NONZERO_BULGE": "CAD_NONZERO_BULGE_UNSUPPORTED",
            "NONFINITE_COORDINATE": "CAD_GEOMETRY_NONFINITE",
            "ZERO_LENGTH_EDGE": "CAD_ZERO_LENGTH_EDGE",
            "QUANTIZATION_RANGE_EXCEEDED": "CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED",
            "QUANTIZATION_COLLAPSE": "CAD_ENDPOINT_QUANTIZATION_COLLAPSE",
        }
        self.code = (
            mapping.get(codes[0], "CAD_GEOMETRY_UNSUPPORTED")
            if len(codes) == 1
            else "CAD_GEOMETRY_UNSUPPORTED"
        )
        super().__init__("geometry blockers: " + ", ".join(codes))


class GeometryBudgetError(RuntimeError):
    """Complete candidate output exceeded the caller's server-owned budget."""

    def __init__(self, *, limit: int, observed: int) -> None:
        self.limit = limit
        self.observed = observed
        self.code = "CAD_MEASUREMENT_CANDIDATE_BUDGET_EXCEEDED"
        super().__init__(f"geometry candidate budget exceeded: {observed} > {limit}")


@dataclass(frozen=True, slots=True)
class PointScope:
    scope_key: str
    point_layers: tuple[str, ...]
    search_radius_m: float

    def __post_init__(self) -> None:
        if not self.scope_key:
            raise GeometryInputError("point scope_key must not be empty")
        if not self.point_layers or any(not layer for layer in self.point_layers):
            raise GeometryInputError("point scope point_layers must not be empty")
        _require_positive_finite(self.search_radius_m, "search_radius_m")


@dataclass(frozen=True, slots=True)
class PointGroup:
    scope_key: str
    point_layers: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.scope_key
            or not self.point_layers
            or any(not layer for layer in self.point_layers)
        ):
            raise GeometryInputError("point group requires scope_key and point_layers")


@dataclass(frozen=True, slots=True)
class PointSearchRadius:
    scope_key: str
    search_radius_m: float

    def __post_init__(self) -> None:
        if not self.scope_key:
            raise GeometryInputError("point search radius requires scope_key")
        _require_positive_finite(self.search_radius_m, "search_radius_m")


@dataclass(frozen=True, slots=True)
class MeasurementTolerances:
    endpoint_quantization_m: float
    endpoint_snap_candidate_m: float
    endpoint_on_path_candidate_m: float
    host_endpoint_guard_m: float
    point_target_uniqueness_margin_m: float
    straight_turn_tolerance_deg: float

    def __post_init__(self) -> None:
        _require_positive_finite(self.endpoint_quantization_m, "endpoint_quantization_m")
        _require_positive_finite(self.endpoint_snap_candidate_m, "endpoint_snap_candidate_m")
        _require_positive_finite(self.endpoint_on_path_candidate_m, "endpoint_on_path_candidate_m")
        _require_nonnegative_finite(self.host_endpoint_guard_m, "host_endpoint_guard_m")
        _require_nonnegative_finite(
            self.point_target_uniqueness_margin_m, "point_target_uniqueness_margin_m"
        )
        _require_nonnegative_finite(self.straight_turn_tolerance_deg, "straight_turn_tolerance_deg")
        if self.straight_turn_tolerance_deg > 180.0:
            raise GeometryInputError("straight_turn_tolerance_deg must not exceed 180")


@dataclass(frozen=True, slots=True)
class NamedToleranceTrial:
    trial_key: str
    tolerances: MeasurementTolerances
    point_search_radii: tuple[PointSearchRadius, ...]

    def __post_init__(self) -> None:
        if not self.trial_key:
            raise GeometryInputError("trial_key must not be empty")


@dataclass(frozen=True, slots=True)
class AnalysisCoverage:
    declared_path_layer_count: int
    declared_point_group_count: int
    selected_path_entity_count: int
    selected_point_entity_count: int
    atomic_edge_count: int
    endpoint_count: int
    ignored_non_path_entity_count: int
    ignored_non_point_entity_count: int
    unsupported_geometry_count: int
    nonzero_bulge_count: int
    nonfinite_coordinate_count: int
    zero_length_edge_count: int
    compute_blocked: bool


@dataclass(frozen=True, slots=True)
class MeasurementBundle:
    atomic_edges: tuple[Mapping[str, Any], ...]
    endpoints: tuple[Mapping[str, Any], ...]
    endpoint_pairs: tuple[Mapping[str, Any], ...]
    endpoint_on_path: tuple[Mapping[str, Any], ...]
    planar_intersections: tuple[Mapping[str, Any], ...]
    point_to_path: tuple[Mapping[str, Any], ...]
    curve_turns: tuple[Mapping[str, Any], ...]
    entity_bounds: tuple[Mapping[str, Any], ...]
    local_frames: tuple[Mapping[str, Any], ...]
    tolerance_diagnostics: tuple[Mapping[str, Any], ...]

    def to_arrow_tables(self) -> dict[str, pa.Table]:
        return {
            name: pa.Table.from_pylist([dict(row) for row in getattr(self, name)], schema=schema)
            for name, schema in MEASUREMENT_PAYLOAD_SCHEMAS.items()
        }


@dataclass(frozen=True, slots=True)
class _Entity:
    key: str
    document_key: str
    entity_type: str
    layer: str
    entity_scope: str
    extrusion: tuple[float, float, float]
    typed_geometry_supported: bool
    unsupported_reason: str | None


@dataclass(frozen=True, slots=True)
class _Edge:
    key: str
    document_key: str
    source_key: str
    source_type: str
    layer: str
    ordinal: int
    closed_edge: bool
    start_geometry_key: str | None
    end_geometry_key: str | None
    start_raw: np.ndarray
    end_raw: np.ndarray
    start_source: np.ndarray
    end_source: np.ndarray
    start: np.ndarray
    end: np.ndarray
    direction: np.ndarray
    length: float
    start_q: tuple[int, int, int]
    end_q: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _Endpoint:
    key: str
    edge: _Edge
    side: str
    source: np.ndarray
    point: np.ndarray
    tangent: np.ndarray
    q: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _Point:
    entity_key: str
    geometry_element_key: str
    layer: str
    source: np.ndarray
    point: np.ndarray


@dataclass(frozen=True, slots=True)
class _RawEdge:
    entity: _Entity
    ordinal: int
    is_closing: bool
    start_geometry_key: str | None
    end_geometry_key: str | None
    start_raw: np.ndarray
    end_raw: np.ndarray
    start_wcs_source: np.ndarray
    end_wcs_source: np.ndarray


@dataclass(frozen=True, slots=True)
class _Prepared:
    entities: Mapping[str, _Entity]
    edges: tuple[_Edge, ...]
    points: tuple[_Point, ...]
    diagnostics: tuple[dict[str, Any], ...]
    coverage: AnalysisCoverage


@dataclass(slots=True)
class _CandidateBudget:
    limit: int
    observed: int = 0

    def consume(self) -> None:
        self.observed += 1
        if self.observed > self.limit:
            raise GeometryBudgetError(limit=self.limit, observed=self.observed)


def quantize_ties_to_even(value_m: float, quantum_m: float) -> int:
    """Return an explicit decimal ties-to-even grid coordinate."""

    _require_finite(value_m, "value_m")
    _require_positive_finite(quantum_m, "quantum_m")
    try:
        ratio = Decimal(str(value_m)) / Decimal(str(quantum_m))
        result = int(ratio.to_integral_value(rounding=ROUND_HALF_EVEN))
        if not _INT64_MIN <= result <= _INT64_MAX:
            raise GeometryInputError(
                "quantized endpoint coordinate exceeds int64",
                code="CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED",
            )
        return result
    except (InvalidOperation, OverflowError) as exc:
        raise GeometryInputError(
            "coordinate cannot be quantized into int64",
            code="CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED",
        ) from exc


def compute_measurements(
    *,
    entities: Sequence[Mapping[str, Any]] | pa.Table,
    geometry_elements: Sequence[Mapping[str, Any]] | pa.Table,
    relationships: Sequence[Mapping[str, Any]] | pa.Table,
    path_layers: Sequence[str],
    point_scopes: Sequence[PointScope],
    source_unit_to_m: float,
    tolerances: MeasurementTolerances,
    candidate_budget: int,
    fail_on_blockers: bool = True,
) -> MeasurementBundle:
    """Compute the complete deterministic M3 geometry fact bundle.

    The function is pure with respect to product state: it reads in-memory L0 rows and
    returns immutable rows. It never publishes, truncates, repairs, or classifies geometry.
    """

    if isinstance(candidate_budget, bool) or candidate_budget <= 0:
        raise GeometryInputError("candidate_budget must be a positive integer")
    prepared = _prepare(
        entities=_row_dicts(entities),
        geometry_elements=_row_dicts(geometry_elements),
        relationships=_row_dicts(relationships),
        path_layers=path_layers,
        point_scopes=point_scopes,
        source_unit_to_m=source_unit_to_m,
        quantization_m=tolerances.endpoint_quantization_m,
    )
    blockers = [row for row in prepared.diagnostics if row["severity"] == "blocker"]
    if fail_on_blockers and blockers:
        raise GeometryBlockerError(blockers)
    bundle = _evaluate(prepared, point_scopes, tolerances, _CandidateBudget(candidate_budget))
    return bundle


def _prepare(
    *,
    entities: list[dict[str, Any]],
    geometry_elements: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    path_layers: Sequence[str],
    point_scopes: Sequence[PointScope | PointGroup],
    source_unit_to_m: float,
    quantization_m: float | None,
) -> _Prepared:
    _require_positive_finite(source_unit_to_m, "source_unit_to_m")
    if not path_layers or any(not layer for layer in path_layers):
        raise GeometryInputError("path_layers must not be empty")
    if len(set(path_layers)) != len(path_layers):
        raise GeometryInputError("path_layers must not contain duplicates")
    if len({scope.scope_key for scope in point_scopes}) != len(point_scopes):
        raise GeometryInputError("point scope_key values must be unique")

    entity_map: dict[str, _Entity] = {}
    for row in entities:
        key = str(row.get("entity_key") or "")
        if not key or key in entity_map:
            raise GeometryInputError("L0 entities require unique non-empty entity_key")
        entity_map[key] = _Entity(
            key=key,
            document_key=str(row.get("document_key") or "doc_0001"),
            entity_type=str(row.get("entity_type") or ""),
            layer=str(row.get("layer") or ""),
            entity_scope=str(row.get("entity_scope") or "top_level_modelspace"),
            extrusion=(
                _optional_float(row.get("extrusion_x"), 0.0),
                _optional_float(row.get("extrusion_y"), 0.0),
                _optional_float(row.get("extrusion_z"), 1.0),
            ),
            typed_geometry_supported=bool(row.get("typed_geometry_supported", False)),
            unsupported_reason=(
                str(row["unsupported_reason"]) if row.get("unsupported_reason") else None
            ),
        )

    geometry_by_entity: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in geometry_elements:
        entity_key = str(row.get("entity_key") or "")
        if entity_key not in entity_map:
            raise GeometryInputError(f"geometry references unknown entity {entity_key!r}")
        geometry_by_entity[entity_key].append(row)
    for rows in geometry_by_entity.values():
        rows.sort(
            key=lambda row: (
                int(row.get("ordinal") or 0),
                str(row.get("geometry_element_key") or ""),
            )
        )

    declared_layers = set(path_layers) | {
        layer for scope in point_scopes for layer in scope.point_layers
    }
    available_layers = {
        entity.layer
        for entity in entity_map.values()
        if entity.entity_scope == "top_level_modelspace"
    }
    missing_layers = sorted(declared_layers - available_layers)
    if missing_layers:
        raise GeometryInputError(
            "declared CAD layer not found: " + ", ".join(missing_layers),
            code="CAD_LAYER_NOT_FOUND",
        )

    polyline_children: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in relationships:
        if row.get("relation_kind") != "polyline_vertex":
            continue
        parent = str(row.get("subject_key") or "")
        child = str(row.get("resolved_target_key") or "")
        if parent in entity_map and child in entity_map:
            polyline_children[parent].append((str(row.get("relationship_key") or ""), child))
    for parent in polyline_children:
        polyline_children[parent].sort(key=lambda item: (item[0], item[1]))

    selected_path = sorted(
        (
            entity
            for entity in entity_map.values()
            if entity.layer in set(path_layers)
            and entity.entity_type in _PATH_TYPES
            and entity.entity_scope == "top_level_modelspace"
        ),
        key=lambda entity: entity.key,
    )
    point_layers = {layer for scope in point_scopes for layer in scope.point_layers}
    selected_points = sorted(
        (
            entity
            for entity in entity_map.values()
            if entity.entity_type == _POINT_TYPE
            and entity.layer in point_layers
            and entity.entity_scope == "top_level_modelspace"
        ),
        key=lambda entity: entity.key,
    )
    if not selected_path:
        raise GeometryInputError(
            "declared path layers contain no supported path entity type",
            code="CAD_PATH_SCOPE_EMPTY",
        )

    diagnostics: list[dict[str, Any]] = []
    raw_edges: list[_RawEdge] = []
    for entity in selected_path:
        if entity.entity_type not in _PATH_TYPES or not entity.typed_geometry_supported:
            _add_diagnostic(
                diagnostics,
                code="UNSUPPORTED_GEOMETRY",
                category="source_geometry",
                severity="blocker",
                count=1,
                sample_key=entity.key,
                message=entity.unsupported_reason or f"unsupported path type {entity.entity_type}",
            )
            continue
        try:
            raw_edges.extend(_expand_entity_edges(entity, geometry_by_entity, polyline_children))
        except _SourceBlocker as exc:
            _add_diagnostic(
                diagnostics,
                code=exc.code,
                category="source_geometry",
                severity="blocker",
                count=1,
                sample_key=entity.key,
                message=exc.message,
            )

    edges: list[_Edge] = []
    for raw_edge in raw_edges:
        quantization_failed = False
        entity = raw_edge.entity
        ordinal = raw_edge.ordinal
        closed_edge = raw_edge.is_closing
        start_source = raw_edge.start_wcs_source
        end_source = raw_edge.end_wcs_source
        key = f"edge:{entity.key}:{ordinal:06d}"
        if not _finite_vector(start_source) or not _finite_vector(end_source):
            _add_diagnostic(
                diagnostics,
                code="NONFINITE_COORDINATE",
                category="source_geometry",
                severity="blocker",
                count=1,
                sample_key=key,
                message="atomic edge contains a non-finite coordinate",
            )
            continue
        start = start_source * source_unit_to_m
        end = end_source * source_unit_to_m
        vector = end - start
        length = float(np.linalg.norm(vector))
        if length == 0.0:
            _add_diagnostic(
                diagnostics,
                code="ZERO_LENGTH_EDGE",
                category="source_geometry",
                severity="blocker",
                count=1,
                sample_key=key,
                message="atomic edge has exactly zero 3D length",
            )
            continue
        try:
            start_q = _quantized_point(start, quantization_m) if quantization_m else (0, 0, 0)
            end_q = _quantized_point(end, quantization_m) if quantization_m else (0, 0, 0)
        except GeometryInputError as exc:
            if exc.code != "CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED":
                raise
            _add_diagnostic(
                diagnostics,
                code="QUANTIZATION_RANGE_EXCEEDED",
                category="quantization",
                severity="blocker",
                count=1,
                threshold_m=quantization_m,
                sample_key=key,
                threshold_name="endpoint_quantization_m",
                message=str(exc),
            )
            start_q = end_q = (0, 0, 0)
            quantization_failed = True
        if quantization_m is not None and not quantization_failed and start_q == end_q:
            _add_diagnostic(
                diagnostics,
                code="QUANTIZATION_COLLAPSE",
                category="quantization",
                severity="blocker",
                count=1,
                threshold_m=quantization_m,
                threshold_name="endpoint_quantization_m",
                sample_key=key,
                measured_value=length,
                message="nonzero atomic edge endpoints occupy the same quantized cell",
            )
        edges.append(
            _Edge(
                key=key,
                document_key=entity.document_key,
                source_key=entity.key,
                source_type=entity.entity_type,
                layer=entity.layer,
                ordinal=ordinal,
                closed_edge=closed_edge,
                start_geometry_key=raw_edge.start_geometry_key,
                end_geometry_key=raw_edge.end_geometry_key,
                start_raw=raw_edge.start_raw,
                end_raw=raw_edge.end_raw,
                start_source=start_source,
                end_source=end_source,
                start=start,
                end=end,
                direction=vector / length,
                length=length,
                start_q=start_q,
                end_q=end_q,
            )
        )

    points: list[_Point] = []
    for entity in selected_points:
        rows = geometry_by_entity.get(entity.key, [])
        row = next(
            (item for item in rows if item.get("geometry_component_kind") == "point_location"), None
        )
        if not entity.typed_geometry_supported or row is None:
            _add_diagnostic(
                diagnostics,
                code="UNSUPPORTED_GEOMETRY",
                category="source_geometry",
                severity="blocker",
                count=1,
                sample_key=entity.key,
                message=entity.unsupported_reason or "POINT has no typed location",
            )
            continue
        source = _row_point(row)
        if not _finite_vector(source):
            _add_diagnostic(
                diagnostics,
                code="NONFINITE_COORDINATE",
                category="source_geometry",
                severity="blocker",
                count=1,
                sample_key=entity.key,
                message="POINT contains a non-finite coordinate",
            )
            continue
        points.append(
            _Point(
                entity.key,
                str(row.get("geometry_element_key") or ""),
                entity.layer,
                source,
                source * source_unit_to_m,
            )
        )

    blocker_counts: defaultdict[str, int] = defaultdict(int)
    for row in diagnostics:
        if row["severity"] == "blocker":
            blocker_counts[str(row["diagnostic_code"])] += int(row.get("count") or 1)
    source_blocked = any(blocker_counts.values())
    coverage = AnalysisCoverage(
        declared_path_layer_count=len(path_layers),
        declared_point_group_count=len(point_scopes),
        selected_path_entity_count=len(selected_path),
        selected_point_entity_count=len(selected_points),
        atomic_edge_count=len(edges),
        endpoint_count=2 * len(edges),
        ignored_non_path_entity_count=sum(
            entity.layer in set(path_layers)
            and entity.entity_scope == "top_level_modelspace"
            and entity.entity_type not in _PATH_TYPES
            for entity in entity_map.values()
        ),
        ignored_non_point_entity_count=sum(
            entity.layer in point_layers
            and entity.entity_scope == "top_level_modelspace"
            and entity.entity_type != _POINT_TYPE
            for entity in entity_map.values()
        ),
        unsupported_geometry_count=blocker_counts["UNSUPPORTED_GEOMETRY"],
        nonzero_bulge_count=blocker_counts["NONZERO_BULGE"],
        nonfinite_coordinate_count=blocker_counts["NONFINITE_COORDINATE"],
        zero_length_edge_count=blocker_counts["ZERO_LENGTH_EDGE"],
        compute_blocked=source_blocked,
    )
    return _Prepared(
        entities=MappingProxyType(entity_map),
        edges=tuple(sorted(edges, key=lambda edge: edge.key)),
        points=tuple(sorted(points, key=lambda point: point.entity_key)),
        diagnostics=tuple(sorted(diagnostics, key=lambda row: row["diagnostic_key"])),
        coverage=coverage,
    )


def _requantize_prepared(prepared: _Prepared, quantization_m: float) -> _Prepared:
    """Apply one analyzer trial quantum without reparsing and expanding L0 geometry."""

    _require_positive_finite(quantization_m, "endpoint_quantization_m")
    diagnostics = [
        dict(row) for row in prepared.diagnostics if row["relation_family"] != "quantization"
    ]
    edges: list[_Edge] = []
    quantization_blocked = False
    for edge in prepared.edges:
        quantization_failed = False
        try:
            start_q = _quantized_point(edge.start, quantization_m)
            end_q = _quantized_point(edge.end, quantization_m)
        except GeometryInputError as exc:
            if exc.code != "CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED":
                raise
            _add_diagnostic(
                diagnostics,
                code="QUANTIZATION_RANGE_EXCEEDED",
                category="quantization",
                severity="blocker",
                count=1,
                threshold_m=quantization_m,
                sample_key=edge.key,
                threshold_name="endpoint_quantization_m",
                message=str(exc),
            )
            start_q = end_q = (0, 0, 0)
            quantization_failed = True
            quantization_blocked = True
        if not quantization_failed and start_q == end_q:
            _add_diagnostic(
                diagnostics,
                code="QUANTIZATION_COLLAPSE",
                category="quantization",
                severity="blocker",
                count=1,
                threshold_m=quantization_m,
                threshold_name="endpoint_quantization_m",
                sample_key=edge.key,
                measured_value=edge.length,
                message="nonzero atomic edge endpoints occupy the same quantized cell",
            )
            quantization_blocked = True
        edges.append(replace(edge, start_q=start_q, end_q=end_q))
    return replace(
        prepared,
        edges=tuple(edges),
        diagnostics=tuple(sorted(diagnostics, key=lambda row: row["diagnostic_key"])),
        coverage=replace(
            prepared.coverage,
            compute_blocked=prepared.coverage.compute_blocked or quantization_blocked,
        ),
    )


class _SourceBlocker(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _expand_entity_edges(
    entity: _Entity,
    geometry_by_entity: Mapping[str, list[dict[str, Any]]],
    polyline_children: Mapping[str, list[tuple[str, str]]],
) -> list[_RawEdge]:
    own = geometry_by_entity.get(entity.key, [])
    if entity.entity_type == "LINE":
        start_row = next(
            (row for row in own if row.get("geometry_component_kind") == "line_start"), None
        )
        end_row = next(
            (row for row in own if row.get("geometry_component_kind") == "line_end"), None
        )
        if start_row is None or end_row is None:
            raise _SourceBlocker("UNSUPPORTED_GEOMETRY", "LINE is missing typed endpoints")
        start = _row_point(start_row)
        end = _row_point(end_row)
        return [
            _RawEdge(
                entity,
                0,
                False,
                str(start_row.get("geometry_element_key") or "") or None,
                str(end_row.get("geometry_element_key") or "") or None,
                start,
                end,
                start,
                end,
            )
        ]

    if entity.entity_type == "LWPOLYLINE":
        vertex_rows = [
            row for row in own if row.get("geometry_component_kind") == "polyline_vertex"
        ]
        if len(vertex_rows) < 2:
            raise _SourceBlocker("UNSUPPORTED_GEOMETRY", "LWPOLYLINE has fewer than two vertices")
        _reject_bulge(vertex_rows)
        raw_points = [_row_point(row) for row in vertex_rows]
        points = [_ocs_to_wcs(point, entity.extrusion) for point in raw_points]
        geometry_keys = [str(row.get("geometry_element_key") or "") or None for row in vertex_rows]
        closed = bool(vertex_rows[0].get("closed"))
        return _segments(entity, raw_points, points, geometry_keys, closed)

    origin = next(
        (row for row in own if row.get("geometry_component_kind") == "polyline_origin"), None
    )
    children = polyline_children.get(entity.key, [])
    if origin is None or len(children) < 2:
        raise _SourceBlocker("UNSUPPORTED_GEOMETRY", "POLYLINE source sequence is incomplete")
    vertex_rows: list[dict[str, Any]] = []
    for _, child_key in children:
        child_rows = geometry_by_entity.get(child_key, [])
        vertex = next(
            (row for row in child_rows if row.get("geometry_component_kind") == "polyline_vertex"),
            None,
        )
        if vertex is None:
            raise _SourceBlocker("UNSUPPORTED_GEOMETRY", "POLYLINE child is missing typed vertex")
        vertex_rows.append(vertex)
    _reject_bulge(vertex_rows)
    flags = int(origin.get("flags") or 0)
    if flags & 8:
        raw_points = [_row_point(row) for row in vertex_rows]
        points = raw_points
    else:
        elevation = _optional_float(origin.get("z"), 0.0)
        raw_points = [
            np.asarray((float(row["x"]), float(row["y"]), elevation)) for row in vertex_rows
        ]
        points = [_ocs_to_wcs(point, entity.extrusion) for point in raw_points]
    geometry_keys = [str(row.get("geometry_element_key") or "") or None for row in vertex_rows]
    return _segments(entity, raw_points, points, geometry_keys, bool(origin.get("closed")))


def _reject_bulge(rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        bulge = row.get("bulge")
        if bulge is None:
            continue
        value = float(bulge)
        if not math.isfinite(value):
            raise _SourceBlocker("NONFINITE_COORDINATE", "polyline bulge is non-finite")
        if value != 0.0:
            raise _SourceBlocker("NONZERO_BULGE", "nonzero polyline bulge is unsupported")


def _segments(
    entity: _Entity,
    raw_points: Sequence[np.ndarray],
    points: Sequence[np.ndarray],
    geometry_keys: Sequence[str | None],
    closed: bool,
) -> list[_RawEdge]:
    pairs = [
        (
            raw_points[index],
            raw_points[index + 1],
            points[index],
            points[index + 1],
            geometry_keys[index],
            geometry_keys[index + 1],
            False,
        )
        for index in range(len(points) - 1)
    ]
    if closed:
        pairs.append(
            (
                raw_points[-1],
                raw_points[0],
                points[-1],
                points[0],
                geometry_keys[-1],
                geometry_keys[0],
                True,
            )
        )
    return [
        _RawEdge(entity, ordinal, closing, start_key, end_key, start_raw, end_raw, start, end)
        for ordinal, (start_raw, end_raw, start, end, start_key, end_key, closing) in enumerate(
            pairs
        )
    ]


def _ocs_to_wcs(point: np.ndarray, extrusion: tuple[float, float, float]) -> np.ndarray:
    if not _finite_vector(np.asarray(extrusion)) or float(np.linalg.norm(extrusion)) == 0.0:
        raise _SourceBlocker("NONFINITE_COORDINATE", "invalid extrusion vector")
    value = OCS(extrusion).to_wcs(Vec3(*point))
    return np.asarray((value.x, value.y, value.z), dtype=np.float64)


def _evaluate(
    prepared: _Prepared,
    point_scopes: Sequence[PointScope],
    tolerances: MeasurementTolerances,
    budget: _CandidateBudget,
) -> MeasurementBundle:
    edges = prepared.edges
    endpoints = tuple(
        endpoint
        for edge in edges
        for endpoint in (
            _Endpoint(
                f"endpoint:{edge.key}:start",
                edge,
                "start",
                edge.start_raw,
                edge.start,
                edge.direction,
                edge.start_q,
            ),
            _Endpoint(
                f"endpoint:{edge.key}:end",
                edge,
                "end",
                edge.end_raw,
                edge.end,
                -edge.direction,
                edge.end_q,
            ),
        )
    )
    atomic_rows = tuple(_atomic_edge_row(edge) for edge in edges)
    endpoint_rows = tuple(
        _endpoint_row(endpoint, tolerances.endpoint_quantization_m) for endpoint in endpoints
    )
    pair_rows = _endpoint_pairs(endpoints, tolerances.endpoint_snap_candidate_m, budget)
    eop_rows = _endpoint_on_path(
        endpoints,
        edges,
        tolerances.endpoint_on_path_candidate_m,
        tolerances.host_endpoint_guard_m,
        budget,
    )
    intersection_rows = _planar_intersections(edges, budget)
    point_rows = _point_to_path(
        prepared.points,
        point_scopes,
        edges,
        tolerances.point_target_uniqueness_margin_m,
        tolerances.endpoint_quantization_m,
        budget,
    )
    turn_rows = _curve_turns(edges, tolerances.straight_turn_tolerance_deg)
    bounds_rows = _entity_bounds(prepared, edges)
    frame_rows = _local_frames(edges)
    diagnostics = list(prepared.diagnostics)
    _coverage_diagnostics(
        diagnostics,
        edges=edges,
        endpoints=endpoints,
        endpoint_pairs=pair_rows,
        endpoint_on_path=eop_rows,
        planar_intersections=intersection_rows,
        point_to_path=point_rows,
        tolerances=tolerances,
    )
    return MeasurementBundle(
        atomic_edges=atomic_rows,
        endpoints=endpoint_rows,
        endpoint_pairs=pair_rows,
        endpoint_on_path=eop_rows,
        planar_intersections=intersection_rows,
        point_to_path=point_rows,
        curve_turns=turn_rows,
        entity_bounds=bounds_rows,
        local_frames=frame_rows,
        tolerance_diagnostics=tuple(sorted(diagnostics, key=lambda row: row["diagnostic_key"])),
    )


def _atomic_edge_row(edge: _Edge) -> Mapping[str, Any]:
    return _frozen_row(
        atomic_edge_key=edge.key,
        document_key=edge.document_key,
        source_entity_key=edge.source_key,
        source_entity_type=edge.source_type,
        source_layer=edge.layer,
        segment_ordinal=edge.ordinal,
        source_start_geometry_element_key=edge.start_geometry_key,
        source_end_geometry_element_key=edge.end_geometry_key,
        start_endpoint_key=f"endpoint:{edge.key}:start",
        end_endpoint_key=f"endpoint:{edge.key}:end",
        start_x_m=float(edge.start[0]),
        start_y_m=float(edge.start[1]),
        start_z_m=float(edge.start[2]),
        end_x_m=float(edge.end[0]),
        end_y_m=float(edge.end[1]),
        end_z_m=float(edge.end[2]),
        unit_x=float(edge.direction[0]),
        unit_y=float(edge.direction[1]),
        unit_z=float(edge.direction[2]),
        length_3d_m=edge.length,
        length_xy_m=float(np.linalg.norm(edge.end[:2] - edge.start[:2])),
        is_closing_segment=edge.closed_edge,
        would_collapse_under_quantization=edge.start_q == edge.end_q,
    )


def _endpoint_row(endpoint: _Endpoint, quantum: float) -> Mapping[str, Any]:
    grid = np.asarray(endpoint.q, dtype=np.float64) * quantum
    return _frozen_row(
        endpoint_key=endpoint.key,
        atomic_edge_key=endpoint.edge.key,
        document_key=endpoint.edge.document_key,
        source_entity_key=endpoint.edge.source_key,
        source_geometry_element_key=(
            endpoint.edge.start_geometry_key
            if endpoint.side == "start"
            else endpoint.edge.end_geometry_key
        ),
        side=endpoint.side,
        raw_x=float(endpoint.source[0]),
        raw_y=float(endpoint.source[1]),
        raw_z=float(endpoint.source[2]),
        x_m=float(endpoint.point[0]),
        y_m=float(endpoint.point[1]),
        z_m=float(endpoint.point[2]),
        inward_tangent_x=float(endpoint.tangent[0]),
        inward_tangent_y=float(endpoint.tangent[1]),
        inward_tangent_z=float(endpoint.tangent[2]),
        quantized_ix=endpoint.q[0],
        quantized_iy=endpoint.q[1],
        quantized_iz=endpoint.q[2],
        quantized_endpoint_key=_qkey(endpoint.q),
        quantized_x_m=float(grid[0]),
        quantized_y_m=float(grid[1]),
        quantized_z_m=float(grid[2]),
        quantization_deviation_3d_m=float(np.linalg.norm(endpoint.point - grid)),
    )


def _endpoint_pairs(
    endpoints: Sequence[_Endpoint], threshold: float, budget: _CandidateBudget
) -> tuple[Mapping[str, Any], ...]:
    if len(endpoints) < 2:
        return ()
    coordinates = np.asarray([endpoint.point for endpoint in endpoints])
    tree = cKDTree(coordinates)
    rows: list[Mapping[str, Any]] = []
    for first, neighbors in enumerate(tree.query_ball_point(coordinates, threshold)):
        for second in sorted(int(value) for value in neighbors if int(value) > first):
            a, b = sorted((endpoints[first], endpoints[second]), key=lambda endpoint: endpoint.key)
            delta = b.point - a.point
            distance = float(np.linalg.norm(delta))
            if distance <= threshold:
                budget.consume()
                rows.append(
                    _frozen_row(
                        endpoint_pair_key=_relation_key("endpoint-pair", a.key, b.key),
                        endpoint_a_key=a.key,
                        endpoint_b_key=b.key,
                        atomic_edge_a_key=a.edge.key,
                        atomic_edge_b_key=b.edge.key,
                        source_entity_a_key=a.edge.source_key,
                        source_entity_b_key=b.edge.source_key,
                        quantized_endpoint_a_key=_qkey(a.q),
                        quantized_endpoint_b_key=_qkey(b.q),
                        delta_x_m=float(delta[0]),
                        delta_y_m=float(delta[1]),
                        delta_z_m=float(delta[2]),
                        distance_3d_m=distance,
                        distance_xy_m=float(np.linalg.norm(delta[:2])),
                        same_quantized_endpoint=a.q == b.q,
                        same_atomic_edge=a.edge.key == b.edge.key,
                        same_source_entity=a.edge.source_key == b.edge.source_key,
                    )
                )
    return tuple(sorted(rows, key=lambda row: row["endpoint_pair_key"]))


def _edge_tree(edges: Sequence[_Edge]) -> tuple[list[Any], STRtree | None]:
    lines = [
        Point(edge.start[:2])
        if np.array_equal(edge.start[:2], edge.end[:2])
        else LineString((edge.start[:2], edge.end[:2]))
        for edge in edges
    ]
    return lines, STRtree(lines) if lines else None


def _endpoint_on_path(
    endpoints: Sequence[_Endpoint],
    edges: Sequence[_Edge],
    threshold: float,
    guard: float,
    budget: _CandidateBudget,
) -> tuple[Mapping[str, Any], ...]:
    lines, tree = _edge_tree(edges)
    if tree is None:
        return ()
    rows: list[Mapping[str, Any]] = []
    for endpoint in endpoints:
        x, y = endpoint.point[:2]
        candidates = tree.query(box(x - threshold, y - threshold, x + threshold, y + threshold))
        for index in sorted(int(value) for value in candidates):
            host = edges[index]
            if host.key == endpoint.edge.key:
                continue
            parameter, projection, distance = _project_3d(endpoint.point, host)
            if not (0.0 < parameter < 1.0) or distance > threshold:
                continue
            delta = endpoint.point - projection
            start_distance = float(np.linalg.norm(projection - host.start))
            end_distance = float(np.linalg.norm(projection - host.end))
            adjacent = endpoint.edge.source_key == host.source_key and _source_edges_adjacent(
                endpoint.edge, host, edges
            )
            key = _relation_key("endpoint-on-path", endpoint.key, host.key)
            budget.consume()
            rows.append(
                _frozen_row(
                    endpoint_on_path_key=key,
                    branch_endpoint_key=endpoint.key,
                    branch_atomic_edge_key=endpoint.edge.key,
                    branch_source_entity_key=endpoint.edge.source_key,
                    host_atomic_edge_key=host.key,
                    host_source_entity_key=host.source_key,
                    host_parameter=parameter,
                    projection_x_m=float(projection[0]),
                    projection_y_m=float(projection[1]),
                    projection_z_m=float(projection[2]),
                    distance_3d_m=distance,
                    distance_xy_m=float(np.linalg.norm(delta[:2])),
                    signed_z_difference_m=float(delta[2]),
                    distance_to_host_start_m=start_distance,
                    distance_to_host_end_m=end_distance,
                    passes_host_endpoint_guard=min(start_distance, end_distance) >= guard,
                    branch_tangent_x=float(endpoint.tangent[0]),
                    branch_tangent_y=float(endpoint.tangent[1]),
                    branch_tangent_z=float(endpoint.tangent[2]),
                    host_tangent_x=float(host.direction[0]),
                    host_tangent_y=float(host.direction[1]),
                    host_tangent_z=float(host.direction[2]),
                    same_source_entity=endpoint.edge.source_key == host.source_key,
                    source_edges_adjacent=adjacent,
                )
            )
    return tuple(sorted(rows, key=lambda row: row["endpoint_on_path_key"]))


def _source_edges_adjacent(first: _Edge, second: _Edge, edges: Sequence[_Edge]) -> bool:
    source = [edge for edge in edges if edge.source_key == first.source_key]
    ordinals = sorted(edge.ordinal for edge in source)
    if abs(first.ordinal - second.ordinal) == 1:
        return True
    return bool(
        source
        and any(edge.closed_edge for edge in source)
        and {first.ordinal, second.ordinal} == {ordinals[0], ordinals[-1]}
    )


def _planar_intersections(
    edges: Sequence[_Edge], budget: _CandidateBudget
) -> tuple[Mapping[str, Any], ...]:
    lines, tree = _edge_tree(edges)
    if tree is None:
        return ()
    rows: list[Mapping[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for first_index, line in enumerate(lines):
        for second_index in sorted(int(value) for value in tree.query(line)):
            pair = (min(first_index, second_index), max(first_index, second_index))
            if pair[0] == pair[1] or pair in seen:
                continue
            seen.add(pair)
            a, b = edges[pair[0]], edges[pair[1]]
            intersection = lines[pair[0]].intersection(lines[pair[1]])
            for component_index, (kind, start_xy, end_xy) in enumerate(
                _intersection_components(intersection)
            ):
                budget.consume()
                if end_xy is None:
                    end_xy = start_xy
                ta = _parameter_xy(start_xy, a)
                tb = _parameter_xy(start_xy, b)
                ta_end = _parameter_xy(end_xy, a)
                tb_end = _parameter_xy(end_xy, b)
                if ta > ta_end:
                    start_xy, end_xy = end_xy, start_xy
                    ta, ta_end, tb, tb_end = ta_end, ta, tb_end, tb
                za, zb = _z_at(a, ta), _z_at(b, tb)
                za_end, zb_end = _z_at(a, ta_end), _z_at(b, tb_end)
                point_intersection = start_xy == end_xy
                intersection_kind = (
                    "endpoint_touch"
                    if point_intersection
                    and any(value <= _EPS or value >= 1.0 - _EPS for value in (ta, tb))
                    else ("proper_crossing" if point_intersection else "collinear_overlap")
                )
                rows.append(
                    _frozen_row(
                        planar_intersection_key=_relation_key(
                            "planar-intersection", a.key, b.key, str(component_index)
                        ),
                        atomic_edge_a_key=a.key,
                        atomic_edge_b_key=b.key,
                        source_entity_a_key=a.source_key,
                        source_entity_b_key=b.source_key,
                        intersection_kind=intersection_kind,
                        xy_start_x_m=start_xy[0],
                        xy_start_y_m=start_xy[1],
                        xy_end_x_m=end_xy[0],
                        xy_end_y_m=end_xy[1],
                        a_parameter_start=ta,
                        a_parameter_end=ta_end,
                        b_parameter_start=tb,
                        b_parameter_end=tb_end,
                        a_z_start_m=za,
                        a_z_end_m=za_end,
                        b_z_start_m=zb,
                        b_z_end_m=zb_end,
                        signed_z_gap_start_m=za - zb,
                        signed_z_gap_end_m=za_end - zb_end,
                        absolute_z_gap_start_m=abs(za - zb),
                        absolute_z_gap_end_m=abs(za_end - zb_end),
                        same_source_entity=a.source_key == b.source_key,
                        source_edges_adjacent=(
                            a.source_key == b.source_key and _source_edges_adjacent(a, b, edges)
                        ),
                    )
                )
    return tuple(sorted(rows, key=lambda row: row["planar_intersection_key"]))


def _intersection_components(
    geometry: Any,
) -> list[tuple[str, tuple[float, float], tuple[float, float] | None]]:
    if geometry.is_empty:
        return []
    kind = geometry.geom_type
    if kind == "Point":
        return [("point", (float(geometry.x), float(geometry.y)), None)]
    if kind == "LineString":
        coordinates = list(geometry.coords)
        start = tuple(map(float, coordinates[0][:2]))
        end = tuple(map(float, coordinates[-1][:2]))
        return [("overlap", min(start, end), max(start, end))]
    if kind in {"MultiPoint", "MultiLineString", "GeometryCollection"}:
        components: list[tuple[str, tuple[float, float], tuple[float, float] | None]] = []
        for child in geometry.geoms:
            components.extend(_intersection_components(child))
        return sorted(components, key=lambda item: (item[0], item[1], item[2] or item[1]))
    return []


def _point_to_path(
    points: Sequence[_Point],
    scopes: Sequence[PointScope],
    edges: Sequence[_Edge],
    uniqueness_margin: float,
    endpoint_quantization_m: float,
    budget: _CandidateBudget,
) -> tuple[Mapping[str, Any], ...]:
    _, tree = _edge_tree(edges)
    if tree is None:
        return ()
    rows: list[dict[str, Any]] = []
    for scope in sorted(scopes, key=lambda item: item.scope_key):
        layers = set(scope.point_layers)
        for point in (item for item in points if item.layer in layers):
            x, y = point.point[:2]
            candidate_indices = tree.query(
                box(
                    x - scope.search_radius_m,
                    y - scope.search_radius_m,
                    x + scope.search_radius_m,
                    y + scope.search_radius_m,
                )
            )
            point_rows: list[dict[str, Any]] = []
            for index in sorted(int(value) for value in candidate_indices):
                host = edges[index]
                parameter, projection, distance = _project_3d(point.point, host)
                if distance > scope.search_radius_m:
                    continue
                budget.consume()
                delta = point.point - projection
                start_distance = float(np.linalg.norm(projection - host.start))
                end_distance = float(np.linalg.norm(projection - host.end))
                nearest_side = "start" if start_distance <= end_distance else "end"
                endpoint_targets = [
                    (start_distance, _qkey(host.start_q))
                    for _ in (0,)
                    if start_distance <= endpoint_quantization_m
                ] + [
                    (end_distance, _qkey(host.end_q))
                    for _ in (0,)
                    if end_distance <= endpoint_quantization_m
                ]
                candidate_q = (
                    min(endpoint_targets, key=lambda item: (item[0], item[1]))[1]
                    if endpoint_targets
                    else None
                )
                target_kind = "quantized_endpoint" if candidate_q else "edge_interior"
                target = candidate_q or f"edge-interior:{host.key}"
                point_rows.append(
                    {
                        "point_to_path_key": _relation_key(
                            "point-to-path", scope.scope_key, point.entity_key, host.key
                        ),
                        "scope_key": scope.scope_key,
                        "point_entity_key": point.entity_key,
                        "point_geometry_element_key": point.geometry_element_key,
                        "host_atomic_edge_key": host.key,
                        "host_source_entity_key": host.source_key,
                        "point_x_m": float(point.point[0]),
                        "point_y_m": float(point.point[1]),
                        "point_z_m": float(point.point[2]),
                        "host_parameter": parameter,
                        "projection_x_m": float(projection[0]),
                        "projection_y_m": float(projection[1]),
                        "projection_z_m": float(projection[2]),
                        "distance_3d_m": distance,
                        "distance_xy_m": float(np.linalg.norm(delta[:2])),
                        "signed_z_difference_m": float(delta[2]),
                        "nearest_host_endpoint_side": nearest_side,
                        "host_tangent_x": float(host.direction[0]),
                        "host_tangent_y": float(host.direction[1]),
                        "host_tangent_z": float(host.direction[2]),
                        "host_start_quantized_endpoint_key": _qkey(host.start_q),
                        "host_end_quantized_endpoint_key": _qkey(host.end_q),
                        "distance_to_host_start_m": start_distance,
                        "distance_to_host_end_m": end_distance,
                        "candidate_quantized_endpoint_key": candidate_q,
                        "target_kind": target_kind,
                        "target_key": target,
                    }
                )
            point_rows.sort(
                key=lambda row: (
                    row["distance_3d_m"],
                    row["target_key"],
                    row["host_atomic_edge_key"],
                )
            )
            target_minima: dict[str, float] = {}
            for row in point_rows:
                target_minima[row["target_key"]] = min(
                    row["distance_3d_m"], target_minima.get(row["target_key"], math.inf)
                )
            ordered_targets = sorted(target_minima, key=lambda key: (target_minima[key], key))
            target_rank = {key: index + 1 for index, key in enumerate(ordered_targets)}
            best_distance = target_minima[ordered_targets[0]] if ordered_targets else None
            next_distance = target_minima[ordered_targets[1]] if len(ordered_targets) > 1 else None
            gap = (
                next_distance - best_distance
                if next_distance is not None and best_distance is not None
                else None
            )
            for rank, row in enumerate(point_rows, start=1):
                current_target_rank = target_rank[row["target_key"]]
                target_distance = target_minima[row["target_key"]]
                within_margin = bool(
                    best_distance is not None
                    and target_distance <= best_distance + uniqueness_margin
                )
                within_count = (
                    sum(
                        distance <= best_distance + uniqueness_margin
                        for distance in target_minima.values()
                    )
                    if best_distance is not None
                    else 0
                )
                row.update(
                    raw_candidate_rank=rank,
                    distinct_target_rank=current_target_rank,
                    distance_gap_to_best_m=row["distance_3d_m"] - best_distance,
                    best_to_second_distinct_target_gap_m=gap,
                    within_uniqueness_margin=within_margin,
                    distinct_target_count_within_margin=within_count,
                )
                rows.append(row)
    return tuple(
        _frozen_row(**row) for row in sorted(rows, key=lambda item: item["point_to_path_key"])
    )


def _curve_turns(
    edges: Sequence[_Edge], straight_tolerance_deg: float
) -> tuple[Mapping[str, Any], ...]:
    by_source: defaultdict[str, list[_Edge]] = defaultdict(list)
    for edge in edges:
        by_source[edge.source_key].append(edge)
    rows: list[Mapping[str, Any]] = []
    for source_key, source_edges in sorted(by_source.items()):
        source_edges.sort(key=lambda edge: edge.ordinal)
        pairs = list(zip(source_edges, source_edges[1:]))
        if source_edges and any(edge.closed_edge for edge in source_edges):
            pairs.append((source_edges[-1], source_edges[0]))
        for incoming, outgoing in pairs:
            closure = (
                outgoing.ordinal == source_edges[0].ordinal
                and incoming.ordinal == source_edges[-1].ordinal
            )
            turn_3d = _angle_degrees(incoming.direction, outgoing.direction)
            cross = np.cross(incoming.direction, outgoing.direction)
            xy_signed = _signed_xy_turn_degrees(incoming.direction, outgoing.direction)
            rows.append(
                _frozen_row(
                    curve_turn_key=_relation_key("curve-turn", incoming.key, outgoing.key),
                    document_key=incoming.document_key,
                    source_entity_key=source_key,
                    vertex_ordinal=outgoing.ordinal,
                    incoming_atomic_edge_key=incoming.key,
                    outgoing_atomic_edge_key=outgoing.key,
                    vertex_x_m=float(incoming.end[0]),
                    vertex_y_m=float(incoming.end[1]),
                    vertex_z_m=float(incoming.end[2]),
                    incoming_tangent_x=float(incoming.direction[0]),
                    incoming_tangent_y=float(incoming.direction[1]),
                    incoming_tangent_z=float(incoming.direction[2]),
                    outgoing_tangent_x=float(outgoing.direction[0]),
                    outgoing_tangent_y=float(outgoing.direction[1]),
                    outgoing_tangent_z=float(outgoing.direction[2]),
                    dot_product=float(np.dot(incoming.direction, outgoing.direction)),
                    cross_x=float(cross[0]),
                    cross_y=float(cross[1]),
                    cross_z=float(cross[2]),
                    deflection_3d_deg=turn_3d,
                    deflection_xy_deg=abs(xy_signed) if xy_signed is not None else None,
                    signed_xy_turn_deg=xy_signed,
                    within_straight_tolerance=turn_3d <= straight_tolerance_deg,
                    is_closing_vertex=closure,
                )
            )
    return tuple(sorted(rows, key=lambda row: row["curve_turn_key"]))


def _entity_bounds(prepared: _Prepared, edges: Sequence[_Edge]) -> tuple[Mapping[str, Any], ...]:
    coordinates: defaultdict[str, list[np.ndarray]] = defaultdict(list)
    geometry_keys: defaultdict[str, set[str]] = defaultdict(set)
    for edge in edges:
        coordinates[edge.source_key].extend((edge.start, edge.end))
        geometry_keys[edge.source_key].update(
            key for key in (edge.start_geometry_key, edge.end_geometry_key) if key
        )
    for point in prepared.points:
        coordinates[point.entity_key].append(point.point)
        geometry_keys[point.entity_key].add(point.geometry_element_key)
    rows: list[Mapping[str, Any]] = []
    for key, points in sorted(coordinates.items()):
        values = np.asarray(points)
        minimum = values.min(axis=0)
        maximum = values.max(axis=0)
        center = (minimum + maximum) / 2.0
        size = maximum - minimum
        entity = prepared.entities[key]
        rows.append(
            _frozen_row(
                document_key=entity.document_key,
                source_entity_key=key,
                source_entity_type=entity.entity_type,
                source_layer=entity.layer,
                selected_as_path=bool(
                    coordinates[key] and key in {edge.source_key for edge in edges}
                ),
                selected_as_point=key in {point.entity_key for point in prepared.points},
                geometry_element_count=len(geometry_keys[key]),
                min_x_m=float(minimum[0]),
                min_y_m=float(minimum[1]),
                min_z_m=float(minimum[2]),
                max_x_m=float(maximum[0]),
                max_y_m=float(maximum[1]),
                max_z_m=float(maximum[2]),
                center_x_m=float(center[0]),
                center_y_m=float(center[1]),
                center_z_m=float(center[2]),
                size_x_m=float(size[0]),
                size_y_m=float(size[1]),
                size_z_m=float(size[2]),
            )
        )
    return tuple(rows)


def _local_frames(
    edges: Sequence[_Edge],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(_frame_row(edge.key, edge.start, edge.direction) for edge in edges)


def _frame_row(key: str, origin: np.ndarray, x_axis: np.ndarray) -> Mapping[str, Any]:
    candidates = (
        np.asarray((0.0, 0.0, 1.0)),
        np.asarray((0.0, 1.0, 0.0)),
        np.asarray((1.0, 0.0, 0.0)),
    )
    reference = min(
        enumerate(candidates), key=lambda item: (abs(float(np.dot(x_axis, item[1]))), item[0])
    )[1]
    y_axis = np.cross(reference, x_axis)
    y_axis = y_axis / np.linalg.norm(y_axis)
    z_axis = np.cross(x_axis, y_axis)
    return _frozen_row(
        frame_key=f"frame:{key}",
        atomic_edge_key=key,
        origin_x_m=float(origin[0]),
        origin_y_m=float(origin[1]),
        origin_z_m=float(origin[2]),
        x_axis_x=float(x_axis[0]),
        x_axis_y=float(x_axis[1]),
        x_axis_z=float(x_axis[2]),
        y_axis_x=float(y_axis[0]),
        y_axis_y=float(y_axis[1]),
        y_axis_z=float(y_axis[2]),
        z_axis_x=float(z_axis[0]),
        z_axis_y=float(z_axis[1]),
        z_axis_z=float(z_axis[2]),
    )


def _coverage_diagnostics(
    diagnostics: list[dict[str, Any]],
    *,
    edges: Sequence[_Edge],
    endpoints: Sequence[_Endpoint],
    endpoint_pairs: Sequence[Mapping[str, Any]],
    endpoint_on_path: Sequence[Mapping[str, Any]],
    planar_intersections: Sequence[Mapping[str, Any]],
    point_to_path: Sequence[Mapping[str, Any]],
    tolerances: MeasurementTolerances,
) -> None:
    facts = (
        ("atomic_edges", len(edges), None),
        ("endpoints", len(endpoints), None),
        ("endpoint_pairs", len(endpoint_pairs), tolerances.endpoint_snap_candidate_m),
        ("endpoint_on_path", len(endpoint_on_path), tolerances.endpoint_on_path_candidate_m),
        ("planar_intersections", len(planar_intersections), None),
        ("point_to_path", len(point_to_path), None),
    )
    for code, count, threshold in facts:
        _add_diagnostic(
            diagnostics,
            code=code.upper() + "_COMPLETE",
            category="coverage",
            severity="info",
            count=count,
            threshold_m=threshold,
            message=f"complete untruncated {code} output",
        )


def _add_diagnostic(
    rows: list[dict[str, Any]],
    *,
    code: str,
    category: str,
    severity: str,
    count: int,
    message: str,
    threshold_m: float | None = None,
    sample_key: str | None = None,
    threshold_name: str | None = None,
    measured_value: float | None = None,
    scope_key: str | None = None,
) -> None:
    identity = f"{category}|{code}|{sample_key or ''}|{threshold_m!r}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    rows.append(
        {
            "diagnostic_key": f"diagnostic:{digest}",
            "row_kind": "sample" if sample_key else "summary",
            "diagnostic_code": code,
            "severity": severity,
            "relation_family": category,
            "scope_key": scope_key,
            "count": count if sample_key is None else None,
            "sample_ordinal": 0 if sample_key else None,
            "source_entity_key": (
                sample_key if sample_key and not sample_key.startswith("edge:") else None
            ),
            "atomic_edge_key": sample_key
            if sample_key and sample_key.startswith("edge:")
            else None,
            "measured_value": measured_value,
            "threshold_name": threshold_name,
            "threshold_value": threshold_m,
        }
    )


def _project_3d(point: np.ndarray, edge: _Edge) -> tuple[float, np.ndarray, float]:
    vector = edge.end - edge.start
    parameter = float(np.dot(point - edge.start, vector) / np.dot(vector, vector))
    parameter = min(1.0, max(0.0, parameter))
    projection = edge.start + parameter * vector
    return parameter, projection, float(np.linalg.norm(point - projection))


def _parameter_xy(point: tuple[float, float], edge: _Edge) -> float:
    vector = edge.end[:2] - edge.start[:2]
    denominator = float(np.dot(vector, vector))
    if denominator == 0.0:
        return 0.0
    return min(
        1.0, max(0.0, float(np.dot(np.asarray(point) - edge.start[:2], vector) / denominator))
    )


def _z_at(edge: _Edge, parameter: float) -> float:
    return float(edge.start[2] + parameter * (edge.end[2] - edge.start[2]))


def _angle_degrees(first: np.ndarray, second: np.ndarray) -> float:
    cosine = float(np.clip(np.dot(first, second), -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def _angle_xy_degrees(first: np.ndarray, second: np.ndarray) -> float | None:
    first_xy = first[:2]
    second_xy = second[:2]
    first_length = float(np.linalg.norm(first_xy))
    second_length = float(np.linalg.norm(second_xy))
    if first_length == 0.0 or second_length == 0.0:
        return None
    cosine = float(np.clip(np.dot(first_xy, second_xy) / (first_length * second_length), -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def _quantized_point(point: np.ndarray, quantum: float) -> tuple[int, int, int]:
    return tuple(quantize_ties_to_even(float(value), quantum) for value in point)  # type: ignore[return-value]


def _qkey(value: tuple[int, int, int]) -> str:
    return f"q:{value[0]}:{value[1]}:{value[2]}"


def _relation_key(prefix: str, *identity: str) -> str:
    digest = hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _signed_xy_turn_degrees(first: np.ndarray, second: np.ndarray) -> float | None:
    if np.linalg.norm(first[:2]) == 0.0 or np.linalg.norm(second[:2]) == 0.0:
        return None
    cross = first[0] * second[1] - first[1] * second[0]
    dot = float(np.dot(first[:2], second[:2]))
    return math.degrees(math.atan2(float(cross), dot))


def _row_point(row: Mapping[str, Any]) -> np.ndarray:
    try:
        return np.asarray(
            (float(row["x"]), float(row["y"]), float(row.get("z") or 0.0)), dtype=np.float64
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _SourceBlocker(
            "NONFINITE_COORDINATE", "typed geometry coordinate is missing or invalid"
        ) from exc


def _row_dicts(rows: Sequence[Mapping[str, Any]] | pa.Table) -> list[dict[str, Any]]:
    if isinstance(rows, pa.Table):
        return rows.to_pylist()
    return [dict(row) for row in rows]


def _frozen_row(**values: Any) -> Mapping[str, Any]:
    return MappingProxyType(values)


def _finite_vector(value: np.ndarray) -> bool:
    return bool(np.all(np.isfinite(value)))


def _optional_float(value: Any, default: float) -> float:
    return default if value is None else float(value)


def _require_finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise GeometryInputError(f"{name} must be finite")


def _require_positive_finite(value: float, name: str) -> None:
    _require_finite(value, name)
    if value <= 0.0:
        raise GeometryInputError(f"{name} must be positive")


def _require_nonnegative_finite(value: float, name: str) -> None:
    _require_finite(value, name)
    if value < 0.0:
        raise GeometryInputError(f"{name} must be nonnegative")
