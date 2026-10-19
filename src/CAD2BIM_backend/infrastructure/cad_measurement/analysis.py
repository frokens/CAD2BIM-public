from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pyarrow as pa
from scipy.spatial import cKDTree
from shapely.geometry import Point, box

from CAD2BIM_backend.infrastructure.cad_measurement import kernel as k


@dataclass(frozen=True, slots=True)
class DistributionSummary:
    metric: str
    scope_key: str | None
    unit: str
    count: int
    minimum: float | None
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None
    maximum: float | None


@dataclass(frozen=True, slots=True)
class ToleranceProbe:
    relation_family: str
    scope_key: str | None
    parameter_name: str
    candidate_value: float
    resulting_relation_count: int
    resulting_ambiguous_subject_count: int
    resulting_blocker_count: int


@dataclass(frozen=True, slots=True)
class PointScopeTrialAnalysis:
    scope_key: str
    selected_point_count: int
    candidate_count: int
    distinct_target_count: int
    unmatched_count: int
    ambiguous_count: int


@dataclass(frozen=True, slots=True)
class DiagnosticSample:
    diagnostic_code: str
    relation_family: str
    scope_key: str | None
    source_entity_key: str | None
    atomic_edge_key: str | None
    measured_value: float | None
    threshold_name: str | None
    threshold_value: float | None


@dataclass(frozen=True, slots=True)
class TrialAnalysis:
    trial_key: str
    compute_blocked: bool
    blocker_count: int
    quantized_endpoint_group_count: int
    quantization_collapse_edge_count: int
    endpoint_pair_candidate_count: int
    endpoint_on_path_candidate_count: int
    endpoint_on_path_pass_guard_count: int
    point_to_path_candidate_count: int
    point_distinct_target_count: int
    point_unmatched_count: int
    point_ambiguous_count: int
    curve_turn_count: int
    curve_within_straight_tolerance_count: int
    planar_intersection_count: int
    point_scope_results: tuple[PointScopeTrialAnalysis, ...]


@dataclass(frozen=True, slots=True)
class ToleranceAnalysis:
    coverage: k.AnalysisCoverage
    distributions: tuple[DistributionSummary, ...]
    trials: tuple[TrialAnalysis, ...]
    generated_probes: tuple[ToleranceProbe, ...]
    diagnostic_samples: tuple[DiagnosticSample, ...]
    varying_parameter: str | None
    stable_interval_trial_keys: tuple[str, str] | None
    suggested_starting_trial_key: str | None
    suggestion_reason: str

    @property
    def recommendation_reason(self) -> str:
        return self.suggestion_reason

    def to_domain_payload(self) -> dict[str, Any]:
        return {
            "coverage": asdict(self.coverage),
            "distributions": [asdict(item) for item in self.distributions],
            "generated_probes": [asdict(item) for item in self.generated_probes],
            "trial_results": [asdict(item) for item in self.trials],
            "diagnostic_samples": [asdict(item) for item in self.diagnostic_samples],
            "suggested_starting_trial": self.suggested_starting_trial_key,
            "suggestion_reason": self.suggestion_reason,
        }


def analyze_tolerance_candidates(
    *,
    entities: Sequence[Mapping[str, Any]] | pa.Table,
    geometry_elements: Sequence[Mapping[str, Any]] | pa.Table,
    relationships: Sequence[Mapping[str, Any]] | pa.Table,
    path_layers: Sequence[str],
    point_groups: Sequence[k.PointGroup],
    source_unit_to_m: float,
    trials: Sequence[k.NamedToleranceTrial],
    candidate_budget: int,
) -> ToleranceAnalysis:
    if isinstance(candidate_budget, bool) or candidate_budget <= 0:
        raise k.GeometryInputError("candidate_budget must be a positive integer")
    if len(trials) > 8 or len({trial.trial_key for trial in trials}) != len(trials):
        raise k.GeometryInputError("analyzer requires 0..8 unique trial_key values")
    group_keys = {group.scope_key for group in point_groups}
    if len(group_keys) != len(point_groups):
        raise k.GeometryInputError("point group scope_key values must be unique")
    for trial in trials:
        keys = {radius.scope_key for radius in trial.point_search_radii}
        if len(keys) != len(trial.point_search_radii) or keys != group_keys:
            raise k.GeometryInputError("trial point_search_radii must exactly match point_groups")
    entity_rows, geometry_rows, relation_rows = map(
        k._row_dicts, (entities, geometry_elements, relationships)
    )
    prepared_base = k._prepare(
        entities=entity_rows,
        geometry_elements=geometry_rows,
        relationships=relation_rows,
        path_layers=path_layers,
        point_scopes=point_groups,
        source_unit_to_m=source_unit_to_m,
        quantization_m=None,
    )
    distributions = _base_distributions(prepared_base, point_groups)
    if not trials:
        return ToleranceAnalysis(
            prepared_base.coverage,
            distributions,
            (),
            _generate_probes(prepared_base, candidate_budget),
            _diagnostic_samples(prepared_base.diagnostics),
            None,
            None,
            None,
            "no_explicit_trials",
        )
    analyses, bundles, prepared_trials = [], [], []
    for trial in trials:
        radii = {item.scope_key: item.search_radius_m for item in trial.point_search_radii}
        scopes = tuple(
            k.PointScope(group.scope_key, group.point_layers, radii[group.scope_key])
            for group in point_groups
        )
        prepared = k._requantize_prepared(
            prepared_base,
            trial.tolerances.endpoint_quantization_m,
        )
        bundle = k._evaluate(
            prepared, scopes, trial.tolerances, k._CandidateBudget(candidate_budget)
        )
        prepared_trials.append(prepared)
        bundles.append(bundle)
        analyses.append(_trial_analysis(trial.trial_key, prepared, bundle, scopes))
    varying = _single_varying_parameter(trials)
    interval, suggestion, reason = _recommend(trials, analyses, varying)
    return ToleranceAnalysis(
        prepared_trials[0].coverage,
        distributions,
        tuple(analyses),
        (),
        _diagnostic_samples(bundles[0].tolerance_diagnostics),
        varying,
        interval,
        suggestion,
        reason,
    )


def _generate_probes(prepared: k._Prepared, candidate_budget: int) -> tuple[ToleranceProbe, ...]:
    endpoints = [point for edge in prepared.edges for point in (edge.start, edge.end)]
    endpoint_values = _nearest_distances(endpoints)
    turns = [row["deflection_3d_deg"] for row in k._curve_turns(prepared.edges, 180.0)]
    lengths = [edge.length for edge in prepared.edges]
    blocker_count = sum(row["severity"] == "blocker" for row in prepared.diagnostics)
    probes: list[ToleranceProbe] = []
    for value in _probe_values(endpoint_values):
        probes.append(
            ToleranceProbe(
                "endpoint_pairs",
                None,
                "endpoint_snap_candidate_m",
                value,
                _count_endpoint_pairs(endpoints, value, candidate_budget),
                0,
                blocker_count,
            )
        )
    for value in _probe_values(turns):
        probes.append(
            ToleranceProbe(
                "curve_turns",
                None,
                "straight_turn_tolerance_deg",
                value,
                sum(angle <= value for angle in turns),
                0,
                blocker_count,
            )
        )
    for value in _probe_values(lengths):
        groups: set[tuple[int, int, int]] = set()
        collapse_count = 0
        for edge in prepared.edges:
            try:
                first, second = (
                    k._quantized_point(edge.start, value),
                    k._quantized_point(edge.end, value),
                )
            except k.GeometryInputError:
                collapse_count += 1
                continue
            groups.update((first, second))
            collapse_count += first == second
        probes.append(
            ToleranceProbe(
                "endpoint_quantization",
                None,
                "endpoint_quantization_m",
                value,
                len(groups),
                0,
                blocker_count + collapse_count,
            )
        )
    return tuple(probes)


def _trial_analysis(
    trial_key: str,
    prepared: k._Prepared,
    bundle: k.MeasurementBundle,
    scopes: Sequence[k.PointScope],
) -> TrialAnalysis:
    blockers = [row for row in bundle.tolerance_diagnostics if row["severity"] == "blocker"]
    results = []
    for scope in sorted(scopes, key=lambda item: item.scope_key):
        selected = {
            point.entity_key for point in prepared.points if point.layer in set(scope.point_layers)
        }
        rows = [row for row in bundle.point_to_path if row["scope_key"] == scope.scope_key]
        matched = {row["point_entity_key"] for row in rows}
        targets = {(row["point_entity_key"], row["target_key"]) for row in rows}
        ambiguous = {
            row["point_entity_key"]
            for row in rows
            if row["distinct_target_count_within_margin"] > 1
        }
        results.append(
            PointScopeTrialAnalysis(
                scope.scope_key,
                len(selected),
                len(rows),
                len(targets),
                len(selected - matched),
                len(ambiguous),
            )
        )
    return TrialAnalysis(
        trial_key,
        bool(blockers),
        sum(int(row.get("count") or 1) for row in blockers),
        len({row["quantized_endpoint_key"] for row in bundle.endpoints}),
        sum(row["diagnostic_code"] == "QUANTIZATION_COLLAPSE" for row in blockers),
        len(bundle.endpoint_pairs),
        len(bundle.endpoint_on_path),
        sum(row["passes_host_endpoint_guard"] for row in bundle.endpoint_on_path),
        len(bundle.point_to_path),
        sum(item.distinct_target_count for item in results),
        sum(item.unmatched_count for item in results),
        sum(item.ambiguous_count for item in results),
        len(bundle.curve_turns),
        sum(row["within_straight_tolerance"] for row in bundle.curve_turns),
        len(bundle.planar_intersections),
        tuple(results),
    )


def _base_distributions(
    prepared: k._Prepared,
    point_groups: Sequence[k.PointGroup],
) -> tuple[DistributionSummary, ...]:
    endpoints = [point for edge in prepared.edges for point in (edge.start, edge.end)]
    turns = [row["deflection_3d_deg"] for row in k._curve_turns(prepared.edges, 180.0)]
    result = [
        _distribution("nearest_endpoint_distance", "metre", _nearest_distances(endpoints)),
        _distribution("atomic_edge_length", "metre", (edge.length for edge in prepared.edges)),
        _distribution("curve_deflection", "degree", turns),
    ]
    point_distances = _nearest_point_to_path_distances(prepared.points, prepared.edges)
    for group in sorted(point_groups, key=lambda item: item.scope_key):
        layers = set(group.point_layers)
        distances = [
            point_distances[point.entity_key]
            for point in prepared.points
            if point.layer in layers and point.entity_key in point_distances
        ]
        result.append(
            _distribution("nearest_point_to_path_distance", "metre", distances, group.scope_key)
        )
    return tuple(result)


def _nearest_point_to_path_distances(
    points: Sequence[k._Point], edges: Sequence[k._Edge]
) -> dict[str, float]:
    """Return exact 3D nearest-edge distances using one shared 2D broad-phase index.

    A first XY-nearest edge supplies a finite 3D upper bound. Any edge that could
    improve that bound must have an XY distance no greater than the bound, so its
    bounding box intersects the corresponding square and is returned by STRtree.
    Final ranking always uses the shared exact 3D projection.
    """

    _, tree = k._edge_tree(edges)
    if tree is None:
        return {}
    result: dict[str, float] = {}
    for point in points:
        x, y = map(float, point.point[:2])
        nearest_index = int(tree.nearest(Point(x, y)))
        best = k._project_3d(point.point, edges[nearest_index])[2]
        candidates = tree.query(box(x - best, y - best, x + best, y + best))
        for index in sorted(int(value) for value in candidates):
            best = min(best, k._project_3d(point.point, edges[index])[2])
        result[point.entity_key] = best
    return result


def _diagnostic_samples(rows: Sequence[Mapping[str, Any]]) -> tuple[DiagnosticSample, ...]:
    return tuple(
        DiagnosticSample(
            str(row["diagnostic_code"]),
            str(row["relation_family"]),
            row.get("scope_key"),
            row.get("source_entity_key"),
            row.get("atomic_edge_key"),
            row.get("measured_value"),
            row.get("threshold_name"),
            row.get("threshold_value"),
        )
        for row in rows
        if row["row_kind"] == "sample"
    )[:32]


def _single_varying_parameter(trials: Sequence[k.NamedToleranceTrial]) -> str | None:
    dimensions = [
        field.name
        for field in fields(k.MeasurementTolerances)
        if len({getattr(trial.tolerances, field.name) for trial in trials}) > 1
    ]
    for key in sorted(
        {radius.scope_key for trial in trials for radius in trial.point_search_radii}
    ):
        values = [
            next(
                radius.search_radius_m
                for radius in trial.point_search_radii
                if radius.scope_key == key
            )
            for trial in trials
        ]
        if len(set(values)) > 1:
            dimensions.append(f"point_search_radius:{key}")
    if len(dimensions) != 1:
        return None
    name = dimensions[0]
    values = (
        [
            next(
                radius.search_radius_m
                for radius in trial.point_search_radii
                if radius.scope_key == name.split(":", 1)[1]
            )
            for trial in trials
        ]
        if name.startswith("point_search_radius:")
        else [getattr(trial.tolerances, name) for trial in trials]
    )
    return name if all(first < second for first, second in zip(values, values[1:])) else None


def _recommend(
    trials: Sequence[k.NamedToleranceTrial],
    analyses: Sequence[TrialAnalysis],
    varying: str | None,
) -> tuple[tuple[str, str] | None, str | None, str]:
    if varying is None:
        count = _varying_dimension_count(trials)
        if len(trials) < 2 or count == 0:
            return None, None, "no_stable_interval"
        return None, None, "multivariate_trials" if count > 1 else "nonmonotone_family"
    if any(item.blocker_count for item in analyses):
        return None, None, "blocker_present"
    metric = {
        "endpoint_quantization_m": "quantized_endpoint_group_count",
        "endpoint_snap_candidate_m": "endpoint_pair_candidate_count",
        "endpoint_on_path_candidate_m": "endpoint_on_path_candidate_count",
        "host_endpoint_guard_m": "endpoint_on_path_pass_guard_count",
        "point_target_uniqueness_margin_m": "point_ambiguous_count",
        "straight_turn_tolerance_deg": "curve_within_straight_tolerance_count",
    }.get(varying)
    if varying.startswith("point_search_radius:"):
        key = varying.split(":", 1)[1]
        counts = [
            next(
                scope.candidate_count
                for scope in item.point_scope_results
                if scope.scope_key == key
            )
            for item in analyses
        ]
    elif metric:
        counts = [getattr(item, metric) for item in analyses]
    else:
        return None, None, "no_stable_interval"
    if not (
        all(a <= b for a, b in zip(counts, counts[1:]))
        or all(a >= b for a, b in zip(counts, counts[1:]))
    ):
        return None, None, "nonmonotone_family"
    for index, (first, second) in enumerate(zip(counts, counts[1:])):
        if first == second:
            interval = (trials[index].trial_key, trials[index + 1].trial_key)
            return interval, trials[index].trial_key, "stable_single_parameter_interval"
    return None, None, "no_stable_interval"


def _varying_dimension_count(trials: Sequence[k.NamedToleranceTrial]) -> int:
    count = sum(
        len({getattr(trial.tolerances, field.name) for trial in trials}) > 1
        for field in fields(k.MeasurementTolerances)
    )
    for key in {radius.scope_key for trial in trials for radius in trial.point_search_radii}:
        count += (
            len(
                {
                    next(
                        radius.search_radius_m
                        for radius in trial.point_search_radii
                        if radius.scope_key == key
                    )
                    for trial in trials
                }
            )
            > 1
        )
    return count


def _probe_values(values: Sequence[float]) -> tuple[float, ...]:
    if not values:
        return ()
    return tuple(
        float(value)
        for value in np.unique(np.quantile(np.asarray(sorted(values)), (0.25, 0.5, 0.75)))
    )


def _nearest_distances(points: Sequence[np.ndarray]) -> list[float]:
    if len(points) < 2:
        return []
    values, _ = cKDTree(np.asarray(points)).query(np.asarray(points), k=2)
    return [float(value) for value in values[:, 1] if value > 0.0 and math.isfinite(value)]


def _count_endpoint_pairs(points: Sequence[np.ndarray], threshold: float, budget: int) -> int:
    if len(points) < 2:
        return 0
    tree, coordinates, count = cKDTree(np.asarray(points)), np.asarray(points), 0
    for index, neighbors in enumerate(tree.query_ball_point(coordinates, threshold)):
        count += sum(int(other) > index for other in neighbors)
        if count > budget:
            raise k.GeometryBudgetError(limit=budget, observed=count)
    return count


def _distribution(
    metric: str,
    unit: str,
    values: Iterable[float],
    scope_key: str | None = None,
) -> DistributionSummary:
    array = np.asarray(list(values), dtype=np.float64)
    if not len(array):
        return DistributionSummary(metric, scope_key, unit, 0, None, None, None, None, None, None)
    return DistributionSummary(
        metric,
        scope_key,
        unit,
        len(array),
        float(array.min()),
        *[float(np.quantile(array, value)) for value in (0.5, 0.9, 0.95, 0.99)],
        float(array.max()),
    )


__all__ = [
    "DiagnosticSample",
    "DistributionSummary",
    "PointScopeTrialAnalysis",
    "ToleranceAnalysis",
    "ToleranceProbe",
    "TrialAnalysis",
    "analyze_tolerance_candidates",
]
