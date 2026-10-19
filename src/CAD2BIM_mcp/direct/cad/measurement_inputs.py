"""Provider-safe Direct inputs for CAD tolerance and measurement tools."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, TypeAdapter, model_validator

from CAD2BIM_backend.domain.cad_measurement import (
    CadAnalyzeToleranceCandidatesCommand,
    CadComputeMeasurementsCommand,
    CadPointGroup,
    CadPointSearchRadius,
    CadPointScope,
)
from CAD2BIM_mcp.direct.transform.common_inputs import OutputPackageDeclaration
from CAD2BIM_mcp.direct.transform.inputs import DirectDataInput
from CAD2BIM_mcp.tool_docs import cad as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs
from CAD2BIM_mcp.tool_docs.cad import (
    CAD_ANALYZE_TOLERANCE_CANDIDATES_DESCRIPTION,
    CAD_COMPUTE_MEASUREMENTS_DESCRIPTION,
)


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_SAFE_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
CadLayerNameInput = Annotated[str, Field(min_length=1, max_length=255)]


class DirectCadPointScope(DirectDataInput):
    scope_key: str = Field(
        pattern=_SAFE_KEY_PATTERN,
        max_length=63,
        description=(_tool_docs.DIRECT_CAD_POINT_SCOPE_SCOPE_KEY_DESCRIPTION),
    )
    point_layers: tuple[CadLayerNameInput, ...] = Field(
        min_length=1,
        max_length=100,
        description=(_tool_docs.DIRECT_CAD_POINT_SCOPE_POINT_LAYERS_DESCRIPTION),
    )
    search_radius_m: float = Field(
        strict=True,
        gt=0,
        description=(_tool_docs.DIRECT_CAD_POINT_SCOPE_SEARCH_RADIUS_M_DESCRIPTION),
    )

    @model_validator(mode="after")
    def validate_domain_contract(self) -> "DirectCadPointScope":
        _DOMAIN_POINT_SCOPE.validate_python(self.model_dump(mode="python"))
        return self


class DirectCadPointGroup(DirectDataInput):
    scope_key: str = Field(
        pattern=_SAFE_KEY_PATTERN,
        max_length=63,
        description=(_tool_docs.DIRECT_CAD_POINT_GROUP_SCOPE_KEY_DESCRIPTION),
    )
    point_layers: tuple[CadLayerNameInput, ...] = Field(
        min_length=1,
        max_length=100,
        description=(_tool_docs.DIRECT_CAD_POINT_GROUP_POINT_LAYERS_DESCRIPTION),
    )

    @model_validator(mode="after")
    def validate_domain_contract(self) -> "DirectCadPointGroup":
        _DOMAIN_POINT_GROUP.validate_python(self.model_dump(mode="python"))
        return self


class DirectCadPointSearchRadius(DirectDataInput):
    scope_key: str = Field(
        pattern=_SAFE_KEY_PATTERN,
        max_length=63,
        description=_tool_docs.DIRECT_CAD_POINT_SEARCH_RADIUS_SCOPE_KEY_DESCRIPTION,
    )
    search_radius_m: float = Field(
        strict=True,
        gt=0,
        description=(_tool_docs.DIRECT_CAD_POINT_SEARCH_RADIUS_SEARCH_RADIUS_M_DESCRIPTION),
    )

    @model_validator(mode="after")
    def validate_domain_contract(self) -> "DirectCadPointSearchRadius":
        _DOMAIN_POINT_RADIUS.validate_python(self.model_dump(mode="python"))
        return self


class DirectCadToleranceSpec(DirectDataInput):
    endpoint_quantization_m: float = Field(
        strict=True,
        gt=0,
        description=(_tool_docs.DIRECT_CAD_TOLERANCE_SPEC_ENDPOINT_QUANTIZATION_M_DESCRIPTION),
    )
    endpoint_snap_candidate_m: float = Field(
        strict=True,
        gt=0,
        description=_tool_docs.DIRECT_CAD_TOLERANCE_SPEC_ENDPOINT_SNAP_CANDIDATE_M_DESCRIPTION,
    )
    endpoint_on_path_candidate_m: float = Field(
        strict=True,
        gt=0,
        description=_tool_docs.DIRECT_CAD_TOLERANCE_SPEC_ENDPOINT_ON_PATH_CANDIDATE_M_DESCRIPTION,
    )
    host_endpoint_guard_m: float = Field(
        strict=True,
        ge=0,
        description=(_tool_docs.DIRECT_CAD_TOLERANCE_SPEC_HOST_ENDPOINT_GUARD_M_DESCRIPTION),
    )
    point_target_uniqueness_margin_m: float = Field(
        strict=True,
        ge=0,
        description=(
            _tool_docs.DIRECT_CAD_TOLERANCE_SPEC_POINT_TARGET_UNIQUENESS_MARGIN_M_DESCRIPTION
        ),
    )
    straight_turn_tolerance_deg: float = Field(
        strict=True,
        ge=0,
        le=180,
        description=_tool_docs.DIRECT_CAD_TOLERANCE_SPEC_STRAIGHT_TURN_TOLERANCE_DEG_DESCRIPTION,
    )


class DirectCadToleranceTrial(DirectDataInput):
    trial_key: str = Field(
        pattern=_SAFE_KEY_PATTERN,
        max_length=63,
        description=_tool_docs.DIRECT_CAD_TOLERANCE_TRIAL_TRIAL_KEY_DESCRIPTION,
    )
    tolerance_spec: DirectCadToleranceSpec = Field(
        description=_tool_docs.DIRECT_CAD_TOLERANCE_TRIAL_TOLERANCE_SPEC_DESCRIPTION
    )
    point_search_radii: tuple[DirectCadPointSearchRadius, ...] = Field(
        max_length=16,
        description=(_tool_docs.DIRECT_CAD_TOLERANCE_TRIAL_POINT_SEARCH_RADII_DESCRIPTION),
    )


class _DirectCadMeasurementRoot(DirectDataInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=(_tool_docs.DIRECT_CAD_MEASUREMENT_ROOT_PACKAGE_ALIAS_DESCRIPTION),
    )
    source_unit_to_m: float = Field(
        strict=True,
        gt=0,
        description=(_tool_docs.DIRECT_CAD_MEASUREMENT_ROOT_SOURCE_UNIT_TO_M_DESCRIPTION),
    )
    path_layers: tuple[CadLayerNameInput, ...] = Field(
        min_length=1,
        max_length=100,
        description=(_tool_docs.DIRECT_CAD_MEASUREMENT_ROOT_PATH_LAYERS_DESCRIPTION),
    )


class DirectCadAnalyzeToleranceCandidatesInput(_DirectCadMeasurementRoot):
    point_groups: tuple[DirectCadPointGroup, ...] = Field(
        default=(),
        max_length=16,
        description=(
            _tool_docs.DIRECT_CAD_ANALYZE_TOLERANCE_CANDIDATES_INPUT_POINT_GROUPS_DESCRIPTION
        ),
    )
    trials: tuple[DirectCadToleranceTrial, ...] = Field(
        default=(),
        max_length=8,
        description=(_tool_docs.DIRECT_CAD_ANALYZE_TOLERANCE_CANDIDATES_INPUT_TRIALS_DESCRIPTION),
    )

    @model_validator(mode="after")
    def validate_domain_contract(self) -> "DirectCadAnalyzeToleranceCandidatesInput":
        _DOMAIN_ANALYZE.validate_python(_without_direct_session(self))
        return self


class DirectCadComputeMeasurementsInput(_DirectCadMeasurementRoot):
    point_scopes: tuple[DirectCadPointScope, ...] = Field(
        default=(),
        max_length=16,
        description=(_tool_docs.DIRECT_CAD_COMPUTE_MEASUREMENTS_INPUT_POINT_SCOPES_DESCRIPTION),
    )
    tolerance_spec: DirectCadToleranceSpec = Field(
        description=(_tool_docs.DIRECT_CAD_COMPUTE_MEASUREMENTS_INPUT_TOLERANCE_SPEC_DESCRIPTION)
    )
    output_package: OutputPackageDeclaration = Field(
        description=(_tool_docs.DIRECT_CAD_COMPUTE_MEASUREMENTS_INPUT_OUTPUT_PACKAGE_DESCRIPTION)
    )

    @model_validator(mode="after")
    def validate_domain_contract(self) -> "DirectCadComputeMeasurementsInput":
        _DOMAIN_COMPUTE.validate_python(_without_direct_session(self))
        return self


_DOMAIN_POINT_SCOPE = TypeAdapter(CadPointScope)
_DOMAIN_POINT_GROUP = TypeAdapter(CadPointGroup)
_DOMAIN_POINT_RADIUS = TypeAdapter(CadPointSearchRadius)
_DOMAIN_ANALYZE = TypeAdapter(CadAnalyzeToleranceCandidatesCommand)
_DOMAIN_COMPUTE = TypeAdapter(CadComputeMeasurementsCommand)


def _without_direct_session(value: DirectDataInput) -> dict[str, object]:
    return value.model_dump(mode="python", exclude={"direct_session_alias"})


def to_domain_cad_analyze_tolerance_candidates_command(
    value: DirectCadAnalyzeToleranceCandidatesInput,
) -> CadAnalyzeToleranceCandidatesCommand:
    """Remove the Direct scope alias and return the entry-neutral analyzer command."""

    return _DOMAIN_ANALYZE.validate_python(_without_direct_session(value))


def to_domain_cad_compute_measurements_command(
    value: DirectCadComputeMeasurementsInput,
) -> CadComputeMeasurementsCommand:
    """Remove the Direct scope alias and return the entry-neutral measurement command."""

    return _DOMAIN_COMPUTE.validate_python(_without_direct_session(value))


__all__ = [
    "CAD_ANALYZE_TOLERANCE_CANDIDATES_DESCRIPTION",
    "CAD_COMPUTE_MEASUREMENTS_DESCRIPTION",
    "CadLayerNameInput",
    "DirectCadAnalyzeToleranceCandidatesInput",
    "DirectCadComputeMeasurementsInput",
    "DirectCadPointGroup",
    "DirectCadPointSearchRadius",
    "DirectCadPointScope",
    "DirectCadToleranceSpec",
    "DirectCadToleranceTrial",
    "to_domain_cad_analyze_tolerance_candidates_command",
    "to_domain_cad_compute_measurements_command",
]
