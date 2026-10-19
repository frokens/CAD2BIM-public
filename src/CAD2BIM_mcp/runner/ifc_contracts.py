"""Runner IFC entry contracts without Direct Session identity fields."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from CAD2BIM_mcp.direct.ifc.query.contracts import IfcQueryExactInput, IfcQuerySourceEdit
from CAD2BIM_mcp.direct.ifc.spatial.contracts import IfcSpatialStructureListQueryInput


class _RunnerIfcInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunnerIfcQueryFreshInput(_RunnerIfcInput):
    mode: Literal["fresh"]
    inputs: tuple[IfcQueryExactInput, ...] = Field(default=(), max_length=16)
    code: str = Field(min_length=1, max_length=65_536)
    page_size: int = Field(default=100, strict=True, ge=1, le=200)

    @field_validator("code")
    @classmethod
    def require_source(cls, value: str) -> str:
        if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > 65_536:
            raise ValueError("IFC query source must be nonblank bounded UTF-8 text")
        return value

    @model_validator(mode="after")
    def require_unique_inputs(self):
        names = [item.input_name for item in self.inputs]
        payloads = [(item.package_alias, item.payload_name) for item in self.inputs]
        if len(names) != len(set(names)) or len(payloads) != len(set(payloads)):
            raise ValueError("IFC query input names and exact Payload bindings must be unique")
        return self


class RunnerIfcQueryRepairInput(_RunnerIfcInput):
    mode: Literal["repair"]
    parent_attempt_alias: str = Field(pattern=r"^cur\..+$", max_length=16_384)

    edits: tuple[IfcQuerySourceEdit, ...] = Field(min_length=1, max_length=16)
    page_size: int = Field(default=100, strict=True, ge=1, le=200)


class RunnerIfcQueryCursorInput(_RunnerIfcInput):
    mode: Literal["cursor"]
    cursor: str = Field(pattern=r"^cur\..+$", max_length=16_384)


RunnerIfcQueryInput: TypeAlias = (
    RunnerIfcQueryFreshInput | RunnerIfcQueryRepairInput | RunnerIfcQueryCursorInput
)
_QUERY_INPUT = TypeAdapter(RunnerIfcQueryInput)


def validate_runner_ifc_query(value: object) -> RunnerIfcQueryInput:
    return _QUERY_INPUT.validate_python(value)


class RunnerIfcSpatialListContinuationInput(_RunnerIfcInput):
    cursor: str = Field(pattern=r"^cur\..+$", max_length=16_384)


RunnerIfcSpatialListInput: TypeAlias = (
    IfcSpatialStructureListQueryInput | RunnerIfcSpatialListContinuationInput
)
_SPATIAL_LIST_INPUT = TypeAdapter(RunnerIfcSpatialListInput)


def validate_runner_ifc_spatial_list(value: object) -> RunnerIfcSpatialListInput:
    return _SPATIAL_LIST_INPUT.validate_python(value)


__all__ = [
    "RunnerIfcQueryCursorInput",
    "RunnerIfcQueryFreshInput",
    "RunnerIfcQueryInput",
    "RunnerIfcQueryRepairInput",
    "RunnerIfcSpatialListContinuationInput",
    "RunnerIfcSpatialListInput",
    "validate_runner_ifc_query",
    "validate_runner_ifc_spatial_list",
]
