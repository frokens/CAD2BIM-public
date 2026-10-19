"""Direct CAD provider response contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictFloat, field_validator

from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_PAYLOAD_NAMES,
    CadLayerName,
    CadPointScope,
    CadToleranceSpec,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract, _non_blank_no_nul
from CAD2BIM_mcp.direct.transform.contracts import ParquetOperationOutputDeclaration


class CadMeasurementOperationDetail(DirectContract):
    detail_kind: Literal["cad_measurement"]
    engine: Literal["cad_measurement"]
    source_package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    source_unit_to_m: StrictFloat = Field(gt=0)
    path_layers: tuple[CadLayerName, ...] = Field(min_length=1, max_length=100)
    point_scopes: tuple[CadPointScope, ...] = Field(max_length=16)
    tolerance_spec: CadToleranceSpec
    outputs: tuple[ParquetOperationOutputDeclaration, ...] = Field(min_length=10, max_length=10)
    output_package_name: str = Field(min_length=1, max_length=255)
    code_available: Literal[True]

    _package_name = field_validator("output_package_name")(_non_blank_no_nul)

    @field_validator("outputs")
    @classmethod
    def validate_fixed_outputs(
        cls,
        value: tuple[ParquetOperationOutputDeclaration, ...],
    ) -> tuple[ParquetOperationOutputDeclaration, ...]:
        if tuple(item.payload_name for item in value) != CAD_MEASUREMENT_PAYLOAD_NAMES:
            raise ValueError("CAD measurement outputs must be the fixed ten Payloads in order")
        return value


class UnsupportedEntityTypeSummary(DirectContract):
    entity_type: str = Field(min_length=1, max_length=120)
    count: int = Field(ge=1)


class CadL0Summary(DirectContract):
    parse_complete: Literal[True] = True
    observed_sections: tuple[str, ...]
    record_count: int = Field(ge=0)
    raw_tag_count: int = Field(ge=0)
    entity_count: int = Field(ge=0)
    geometry_element_count: int = Field(ge=0)
    xdata_tag_count: int = Field(ge=0)
    relationship_count: int = Field(ge=0)
    typed_geometry_entity_count: int = Field(ge=0)
    raw_only_entity_count: int = Field(ge=0)
    unsupported_entity_types: tuple[UnsupportedEntityTypeSummary, ...]
