"""Entry-neutral contracts for M3 CAD tolerance analysis and measurement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from CAD2BIM_backend.domain.data_transform import OutputPackageDeclaration
from CAD2BIM_backend.domain.runtime_execution import CAD_ANALYSIS_EXECUTION, CAD_MEASUREMENT_EXECUTION


_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_SAFE_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
CAD_MEASUREMENT_PAYLOAD_NAMES = (
    "atomic_edges",
    "endpoints",
    "endpoint_pairs",
    "endpoint_on_path",
    "planar_intersections",
    "point_to_path",
    "curve_turns",
    "entity_bounds",
    "local_frames",
    "tolerance_diagnostics",
)


@dataclass(frozen=True, slots=True)
class CadMeasurementBudget:
    total_timeout_seconds: int
    worker_wall_seconds: int
    worker_cpu_seconds: int
    worker_rss_bytes: int
    worker_scratch_bytes: int
    worker_threads: int
    max_concurrent_workers: int
    capacity_wait_seconds: int
    response_bytes: int
    warm_p95_seconds: int
    input_rows: int = 2_000_000
    input_parquet_bytes: int = 256 * 1024**2
    input_arrow_bytes: int = 512 * 1024**2
    output_rows: int | None = None
    output_arrow_bytes: int | None = None
    output_parquet_bytes: int | None = None
    output_payload_parquet_bytes: int | None = None
    candidate_count: int | None = None


CAD_TOLERANCE_ANALYZER_BUDGET = CadMeasurementBudget(
    total_timeout_seconds=int(CAD_ANALYSIS_EXECUTION.total_seconds),
    worker_wall_seconds=90,
    worker_cpu_seconds=180,
    worker_rss_bytes=4 * 1024**3,
    worker_scratch_bytes=4 * 1024**3,
    worker_threads=2,
    max_concurrent_workers=1,
    capacity_wait_seconds=int(CAD_ANALYSIS_EXECUTION.queue_seconds),
    response_bytes=256 * 1024,
    warm_p95_seconds=30,
)
CAD_MEASUREMENT_COMPUTE_BUDGET = CadMeasurementBudget(
    total_timeout_seconds=int(CAD_MEASUREMENT_EXECUTION.total_seconds),
    worker_wall_seconds=150,
    worker_cpu_seconds=300,
    worker_rss_bytes=4 * 1024**3,
    worker_scratch_bytes=4 * 1024**3,
    worker_threads=2,
    max_concurrent_workers=1,
    capacity_wait_seconds=int(CAD_MEASUREMENT_EXECUTION.queue_seconds),
    response_bytes=64 * 1024,
    warm_p95_seconds=60,
    output_rows=2_000_000,
    output_arrow_bytes=512 * 1024**2,
    output_parquet_bytes=256 * 1024**2,
    output_payload_parquet_bytes=100 * 1024**2,
    candidate_count=2_000_000,
)


class CadMeasurementErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    DIRECT_SESSION_NOT_FOUND = "DIRECT_SESSION_NOT_FOUND"
    PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"
    PACKAGE_KIND_MISMATCH = "PACKAGE_KIND_MISMATCH"
    PACKAGE_INTEGRITY_ERROR = "PACKAGE_INTEGRITY_ERROR"
    CAD_LAYER_NOT_FOUND = "CAD_LAYER_NOT_FOUND"
    CAD_PATH_SCOPE_EMPTY = "CAD_PATH_SCOPE_EMPTY"
    CAD_MEASUREMENT_INPUT_BUDGET_EXCEEDED = "CAD_MEASUREMENT_INPUT_BUDGET_EXCEEDED"
    CAD_GEOMETRY_UNSUPPORTED = "CAD_GEOMETRY_UNSUPPORTED"
    CAD_NONZERO_BULGE_UNSUPPORTED = "CAD_NONZERO_BULGE_UNSUPPORTED"
    CAD_GEOMETRY_NONFINITE = "CAD_GEOMETRY_NONFINITE"
    CAD_ZERO_LENGTH_EDGE = "CAD_ZERO_LENGTH_EDGE"
    CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED = "CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED"
    CAD_ENDPOINT_QUANTIZATION_COLLAPSE = "CAD_ENDPOINT_QUANTIZATION_COLLAPSE"
    CAD_MEASUREMENT_CANDIDATE_BUDGET_EXCEEDED = "CAD_MEASUREMENT_CANDIDATE_BUDGET_EXCEEDED"
    CAPACITY_BUSY = "CAPACITY_BUSY"
    TIMEOUT = "TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    WORKER_FAILED = "WORKER_FAILED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    OUTPUT_BUDGET_EXCEEDED = "OUTPUT_BUDGET_EXCEEDED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    PUBLICATION_UNCERTAIN = "PUBLICATION_UNCERTAIN"


PositiveFiniteFloat = Annotated[StrictFloat, Field(gt=0)]
NonNegativeFiniteFloat = Annotated[StrictFloat, Field(ge=0)]
TurnToleranceDegrees = Annotated[StrictFloat, Field(ge=0, le=180)]
NonNegativeInt64 = Annotated[StrictInt, Field(ge=0, le=9_223_372_036_854_775_807)]
CadLayerName = Annotated[str, Field(min_length=1, max_length=255)]
ToleranceParameterName = Literal[
    "endpoint_quantization_m",
    "endpoint_snap_candidate_m",
    "endpoint_on_path_candidate_m",
    "host_endpoint_guard_m",
    "point_target_uniqueness_margin_m",
    "straight_turn_tolerance_deg",
]


class CadMeasurementModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CadPointScope(CadMeasurementModel):
    scope_key: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    point_layers: tuple[CadLayerName, ...] = Field(min_length=1, max_length=100)
    search_radius_m: PositiveFiniteFloat

    @field_validator("point_layers")
    @classmethod
    def validate_point_layers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_layers(value, "point_layers")


class CadPointGroup(CadMeasurementModel):
    scope_key: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    point_layers: tuple[CadLayerName, ...] = Field(min_length=1, max_length=100)

    @field_validator("point_layers")
    @classmethod
    def validate_point_layers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_layers(value, "point_layers")


class CadPointSearchRadius(CadMeasurementModel):
    scope_key: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    search_radius_m: PositiveFiniteFloat


class CadToleranceSpec(CadMeasurementModel):
    endpoint_quantization_m: PositiveFiniteFloat
    endpoint_snap_candidate_m: PositiveFiniteFloat
    endpoint_on_path_candidate_m: PositiveFiniteFloat
    host_endpoint_guard_m: NonNegativeFiniteFloat
    point_target_uniqueness_margin_m: NonNegativeFiniteFloat
    straight_turn_tolerance_deg: TurnToleranceDegrees


class CadToleranceTrial(CadMeasurementModel):
    trial_key: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    tolerance_spec: CadToleranceSpec
    point_search_radii: tuple[CadPointSearchRadius, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def validate_radius_keys(self) -> "CadToleranceTrial":
        keys = [radius.scope_key for radius in self.point_search_radii]
        if len(keys) != len(set(keys)):
            raise ValueError("point_search_radii scope_key values must be unique")
        return self


class _CadMeasurementCommand(CadMeasurementModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    source_unit_to_m: PositiveFiniteFloat
    path_layers: tuple[CadLayerName, ...] = Field(min_length=1, max_length=100)

    @field_validator("path_layers")
    @classmethod
    def validate_path_layers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_layers(value, "path_layers")


class CadAnalyzeToleranceCandidatesCommand(_CadMeasurementCommand):
    point_groups: tuple[CadPointGroup, ...] = Field(default=(), max_length=16)
    trials: tuple[CadToleranceTrial, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_group_and_trial_keys(self) -> "CadAnalyzeToleranceCandidatesCommand":
        group_keys = [group.scope_key for group in self.point_groups]
        if len(group_keys) != len(set(group_keys)):
            raise ValueError("point group scope_key values must be unique")
        keys = [trial.trial_key for trial in self.trials]
        if len(keys) != len(set(keys)):
            raise ValueError("trial_key values must be unique")
        expected = set(group_keys)
        if any(
            {radius.scope_key for radius in trial.point_search_radii} != expected
            for trial in self.trials
        ):
            raise ValueError(
                "each trial point_search_radii scope_key set must exactly match point_groups"
            )
        return self


class CadComputeMeasurementsCommand(_CadMeasurementCommand):
    point_scopes: tuple[CadPointScope, ...] = Field(default=(), max_length=16)
    tolerance_spec: CadToleranceSpec
    output_package: OutputPackageDeclaration

    @model_validator(mode="after")
    def validate_scope_keys(self) -> "CadComputeMeasurementsCommand":
        keys = [scope.scope_key for scope in self.point_scopes]
        if len(keys) != len(set(keys)):
            raise ValueError("point scope_key values must be unique")
        return self


class CadToleranceAnalysisCoverage(CadMeasurementModel):
    declared_path_layer_count: NonNegativeInt64
    declared_point_group_count: NonNegativeInt64
    selected_path_entity_count: NonNegativeInt64
    selected_point_entity_count: NonNegativeInt64
    atomic_edge_count: NonNegativeInt64
    endpoint_count: NonNegativeInt64
    ignored_non_path_entity_count: NonNegativeInt64
    ignored_non_point_entity_count: NonNegativeInt64
    unsupported_geometry_count: NonNegativeInt64
    nonzero_bulge_count: NonNegativeInt64
    nonfinite_coordinate_count: NonNegativeInt64
    zero_length_edge_count: NonNegativeInt64
    compute_blocked: StrictBool


class CadToleranceDistribution(CadMeasurementModel):
    metric: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    scope_key: str | None = Field(default=None, pattern=_SAFE_KEY_PATTERN, max_length=63)
    unit: Literal["metre", "degree"]
    count: NonNegativeInt64
    minimum: StrictFloat | None = None
    p50: StrictFloat | None = None
    p90: StrictFloat | None = None
    p95: StrictFloat | None = None
    p99: StrictFloat | None = None
    maximum: StrictFloat | None = None


class CadGeneratedToleranceProbe(CadMeasurementModel):
    relation_family: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    scope_key: str | None = Field(default=None, pattern=_SAFE_KEY_PATTERN, max_length=63)
    parameter_name: ToleranceParameterName
    candidate_value: NonNegativeFiniteFloat
    resulting_relation_count: NonNegativeInt64
    resulting_ambiguous_subject_count: NonNegativeInt64
    resulting_blocker_count: NonNegativeInt64


class CadPointScopeTrialResult(CadMeasurementModel):
    scope_key: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    selected_point_count: NonNegativeInt64
    candidate_count: NonNegativeInt64
    distinct_target_count: NonNegativeInt64
    unmatched_count: NonNegativeInt64
    ambiguous_count: NonNegativeInt64


class CadToleranceTrialResult(CadMeasurementModel):
    trial_key: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    compute_blocked: StrictBool
    blocker_count: NonNegativeInt64
    quantized_endpoint_group_count: NonNegativeInt64
    quantization_collapse_edge_count: NonNegativeInt64
    endpoint_pair_candidate_count: NonNegativeInt64
    endpoint_on_path_candidate_count: NonNegativeInt64
    endpoint_on_path_pass_guard_count: NonNegativeInt64
    point_to_path_candidate_count: NonNegativeInt64
    point_distinct_target_count: NonNegativeInt64
    point_unmatched_count: NonNegativeInt64
    point_ambiguous_count: NonNegativeInt64
    curve_turn_count: NonNegativeInt64
    curve_within_straight_tolerance_count: NonNegativeInt64
    planar_intersection_count: NonNegativeInt64
    point_scope_results: tuple[CadPointScopeTrialResult, ...] = Field(max_length=16)


class CadToleranceDiagnosticSample(CadMeasurementModel):
    diagnostic_code: str = Field(min_length=1, max_length=120)
    relation_family: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=63)
    scope_key: str | None = Field(default=None, pattern=_SAFE_KEY_PATTERN, max_length=63)
    source_entity_key: str | None = Field(default=None, min_length=1, max_length=255)
    atomic_edge_key: str | None = Field(default=None, min_length=1, max_length=255)
    measured_value: StrictFloat | None = None
    threshold_name: ToleranceParameterName | None = None
    threshold_value: NonNegativeFiniteFloat | None = None


class CadToleranceAnalysisResult(CadMeasurementModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    document_key: str = Field(min_length=1, max_length=255)
    source_unit_to_m: PositiveFiniteFloat
    coverage: CadToleranceAnalysisCoverage
    distributions: tuple[CadToleranceDistribution, ...] = Field(max_length=32)
    generated_probes: tuple[CadGeneratedToleranceProbe, ...] = Field(max_length=64)
    trial_results: tuple[CadToleranceTrialResult, ...] = Field(max_length=8)
    diagnostic_samples: tuple[CadToleranceDiagnosticSample, ...] = Field(max_length=32)
    suggested_starting_trial: str | None = Field(
        default=None,
        pattern=_SAFE_KEY_PATTERN,
        max_length=63,
    )
    suggestion_reason: Literal[
        "stable_single_parameter_interval",
        "no_explicit_trials",
        "multivariate_trials",
        "nonmonotone_family",
        "blocker_present",
        "no_stable_interval",
    ]


def _validate_layers(value: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} values must be unique")
    for layer in value:
        if not layer.strip():
            raise ValueError(f"{field_name} values must not be blank")
        if "\x00" in layer:
            raise ValueError(f"{field_name} values must not contain NUL")
        if len(layer) > 255:
            raise ValueError(f"{field_name} values must be at most 255 characters")
    return value


__all__ = [
    "CAD_MEASUREMENT_COMPUTE_BUDGET",
    "CAD_MEASUREMENT_PAYLOAD_NAMES",
    "CAD_TOLERANCE_ANALYZER_BUDGET",
    "CadAnalyzeToleranceCandidatesCommand",
    "CadComputeMeasurementsCommand",
    "CadGeneratedToleranceProbe",
    "CadLayerName",
    "CadMeasurementModel",
    "CadMeasurementBudget",
    "CadMeasurementErrorCode",
    "CadPointGroup",
    "CadPointSearchRadius",
    "CadPointScope",
    "CadPointScopeTrialResult",
    "CadToleranceAnalysisCoverage",
    "CadToleranceAnalysisResult",
    "CadToleranceDiagnosticSample",
    "CadToleranceDistribution",
    "CadToleranceSpec",
    "CadToleranceTrial",
    "CadToleranceTrialResult",
]
