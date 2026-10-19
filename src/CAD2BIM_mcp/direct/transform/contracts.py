"""Direct data transformation provider response contracts."""

from __future__ import annotations

from CAD2BIM_backend.domain.data_transform import TransformMetadataPage

import hashlib
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from CAD2BIM_mcp.direct.contract_base import (
    _MAX_MARKDOWN_BYTES,
    DirectContract,
    LogicalKeyField,
    NonNegativeInt64,
    _non_blank_no_nul,
)
from CAD2BIM_mcp.direct.transform.common_inputs import ResolvedTransformInputBinding, TransformParameter
from CAD2BIM_mcp.direct.transform.inputs import InlineTable
from CAD2BIM_mcp.tool_docs import data_transform as _tool_docs


class JsonOperationOutputDeclaration(DirectContract):
    payload_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$", max_length=120)
    format: Literal["json"]
    description: str = Field(min_length=1, max_length=500)
    json_kind: Literal["document", "records"]

    _description = field_validator("description")(_non_blank_no_nul)


class ParquetOperationOutputDeclaration(DirectContract):
    payload_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$", max_length=120)
    format: Literal["parquet"]
    description: str = Field(min_length=1, max_length=500)
    logical_key_fields: tuple[LogicalKeyField, ...] = Field(min_length=1, max_length=16)

    _description = field_validator("description")(_non_blank_no_nul)

    @field_validator("logical_key_fields")
    @classmethod
    def validate_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("logical_key_fields must be unique")
        return value


class IfcReadSourceSummary(DirectContract):
    revision_no: int = Field(ge=1, description=_tool_docs.IFC_REVISION_SUMMARY_DESCRIPTION)


class DataTransformOperationDetail(DirectContract):
    detail_kind: Literal["data_transform"]
    engine: Literal["duckdb_sql", "python_arrow"]
    inputs: TransformMetadataPage
    ifc_source: IfcReadSourceSummary | None = None
    parameters: TransformMetadataPage
    inline_tables: TransformMetadataPage
    outputs: TransformMetadataPage
    output_package_name: str = Field(min_length=1, max_length=255)
    code_available: bool
    code_locator: str | None = None
    repair_parent_operation_alias: str | None = Field(
        default=None,
        pattern=r"^op_[0-9]{3,}$",
        max_length=32,
        description=(
            _tool_docs.DATA_TRANSFORM_OPERATION_DETAIL_REPAIR_PARENT_OPERATION_ALIAS_DESCRIPTION
        ),
    )

    _package_name = field_validator("output_package_name")(_non_blank_no_nul)

    @model_validator(mode="after")
    def validate_read_sources(self) -> "DataTransformOperationDetail":
        if self.inputs.total_count == 0 and self.inline_tables.total_count == 0 and self.ifc_source is None:
            raise ValueError("derived operation requires Package inputs or an exact IFC source")
        if self.ifc_source is not None and self.engine != "python_arrow":
            raise ValueError("IFC sources require Python")
        return self


class DataTransformOutputResult(DirectContract):
    payload_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$", max_length=120)
    format: Literal["parquet"]
    row_count: Annotated[StrictInt, Field(ge=0, le=2_000_000)]
    logical_key_fields: tuple[LogicalKeyField, ...] = Field(min_length=1, max_length=16)

    @field_validator("logical_key_fields")
    @classmethod
    def validate_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("logical_key_fields must be unique")
        return value


class JsonDataTransformOutputResult(DirectContract):
    payload_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$", max_length=120)
    format: Literal["json"]
    json_kind: Literal["document", "records"]
    size_bytes: NonNegativeInt64


PublishedDataTransformOutput: TypeAlias = DataTransformOutputResult | JsonDataTransformOutputResult


class DataTransformResult(DirectContract):
    __doc__ = _tool_docs.DATA_TRANSFORM_RESULT_DESCRIPTION

    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    package_name: str = Field(min_length=1, max_length=255)
    output: PublishedDataTransformOutput
    ifc_source: IfcReadSourceSummary | None = None

    _package_name = field_validator("package_name")(_non_blank_no_nul)


class MultiOutputDataTransformResult(DirectContract):
    __doc__ = _tool_docs.MULTI_OUTPUT_DATA_TRANSFORM_RESULT_DESCRIPTION

    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    package_name: str = Field(min_length=1, max_length=255)
    outputs: tuple[PublishedDataTransformOutput, ...] = Field(min_length=2, max_length=16)
    ifc_source: IfcReadSourceSummary | None = None

    _package_name = field_validator("package_name")(_non_blank_no_nul)

    @field_validator("outputs")
    @classmethod
    def validate_output_names(
        cls,
        value: tuple[DataTransformOutputResult, ...],
    ) -> tuple[DataTransformOutputResult, ...]:
        if len({item.payload_name for item in value}) != len(value):
            raise ValueError("output payload names must be unique")
        return value


DataTransformToolResult: TypeAlias = DataTransformResult | MultiOutputDataTransformResult


class DataRepairTransformResult(DataTransformResult):
    __doc__ = _tool_docs.DATA_REPAIR_TRANSFORM_RESULT_DESCRIPTION

    repair_parent_operation_alias: str = Field(
        pattern=r"^op_[0-9]{3,}$",
        max_length=32,
        description=(
            _tool_docs.DATA_REPAIR_TRANSFORM_RESULT_REPAIR_PARENT_OPERATION_ALIAS_DESCRIPTION
        ),
    )
    engine: Literal["duckdb_sql", "python_arrow"]
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MultiOutputDataRepairTransformResult(MultiOutputDataTransformResult):
    __doc__ = _tool_docs.MULTI_OUTPUT_DATA_REPAIR_TRANSFORM_RESULT_DESCRIPTION

    repair_parent_operation_alias: str = Field(
        pattern=r"^op_[0-9]{3,}$",
        max_length=32,
        description=(
            _tool_docs.DATA_REPAIR_TRANSFORM_RESULT_REPAIR_PARENT_OPERATION_ALIAS_DESCRIPTION
        ),
    )
    engine: Literal["duckdb_sql", "python_arrow"]
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


DataRepairTransformToolResult: TypeAlias = (
    DataRepairTransformResult | MultiOutputDataRepairTransformResult
)

DataTransformWriteToolResult: TypeAlias = (
    DataTransformResult
    | MultiOutputDataTransformResult
    | DataRepairTransformResult
    | MultiOutputDataRepairTransformResult
)


class _TransformationCodeResult(DirectContract):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    view_mode: Literal["full"] = "full"
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    total_lines: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    code: str = Field(min_length=1, max_length=_MAX_MARKDOWN_BYTES)

    @model_validator(mode="before")
    @classmethod
    def populate_exact_metadata(cls, value: object) -> object:
        # Keep old full-source constructors valid while making exact metadata
        # required in the serialized Provider contract.
        if isinstance(value, dict) and isinstance(value.get("code"), str):
            materialized = dict(value)
            code = materialized["code"]
            materialized.setdefault("code_sha256", hashlib.sha256(code.encode("utf-8")).hexdigest())
            materialized.setdefault("total_lines", max(1, len(code.splitlines())))
            return materialized
        return value

    @model_validator(mode="after")
    def validate_code(self) -> _TransformationCodeResult:
        encoded = self.code.encode("utf-8")
        if len(encoded) > _MAX_MARKDOWN_BYTES:
            raise ValueError("code exceeds 65,536 UTF-8 bytes")
        if not self.code.strip() or "\x00" in self.code:
            raise ValueError("code must be non-blank and contain no NUL")
        if self.size_bytes != len(encoded):
            raise ValueError("size_bytes must equal the exact UTF-8 code size")
        if self.code_sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError("code_sha256 must identify the exact returned source")
        if self.total_lines != max(1, len(self.code.splitlines())):
            raise ValueError("total_lines must equal the exact source line count")
        return self


class _SqlTransformationCodeResult(_TransformationCodeResult):
    engine: Literal["duckdb_sql"]
    language: Literal["sql"]
    media_type: Literal["application/sql"]


class _PythonTransformationCodeResult(_TransformationCodeResult):
    engine: Literal["python_arrow"]
    language: Literal["python"]
    media_type: Literal["text/x-python"]


class _CadMeasurementTransformationCodeResult(_TransformationCodeResult):
    engine: Literal["cad_measurement"]
    language: Literal["python"]
    media_type: Literal["text/x-python"]


class SucceededSqlTransformationCodeResult(_SqlTransformationCodeResult):
    operation_outcome: Literal["succeeded"]
    result_package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)


class FailedSqlTransformationCodeResult(_SqlTransformationCodeResult):
    operation_outcome: Literal["failed"]


class SucceededPythonTransformationCodeResult(_PythonTransformationCodeResult):
    operation_outcome: Literal["succeeded"]
    result_package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)


class FailedPythonTransformationCodeResult(_PythonTransformationCodeResult):
    operation_outcome: Literal["failed"]


class SucceededCadMeasurementTransformationCodeResult(_CadMeasurementTransformationCodeResult):
    operation_outcome: Literal["succeeded"]
    result_package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)


class FailedCadMeasurementTransformationCodeResult(_CadMeasurementTransformationCodeResult):
    operation_outcome: Literal["failed"]


class TransformationCodeLine(DirectContract):
    line_number: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    text: str = Field(max_length=_MAX_MARKDOWN_BYTES)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > _MAX_MARKDOWN_BYTES:
            raise ValueError("source line is invalid or exceeds 65,536 UTF-8 bytes")
        return value


class TransformationCodeSearchMatch(DirectContract):
    line: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    column: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    end_line: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    end_column: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    excerpt_start_line: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    excerpt_end_line: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    excerpt: str = Field(min_length=1, max_length=_MAX_MARKDOWN_BYTES)

    @field_validator("excerpt")
    @classmethod
    def validate_excerpt(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > _MAX_MARKDOWN_BYTES:
            raise ValueError("search excerpt is invalid or exceeds 65,536 UTF-8 bytes")
        return value


class _TransformationCodePartialResult(DirectContract):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    operation_outcome: Literal["succeeded", "failed"]
    engine: Literal["duckdb_sql", "python_arrow", "cad_measurement"]
    language: Literal["sql", "python"]
    media_type: Literal["application/sql", "text/x-python"]
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    total_lines: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]

    @model_validator(mode="after")
    def validate_engine_triplet(self) -> _TransformationCodePartialResult:
        expected = {
            "duckdb_sql": ("sql", "application/sql"),
            "python_arrow": ("python", "text/x-python"),
            "cad_measurement": ("python", "text/x-python"),
        }[self.engine]
        if (self.language, self.media_type) != expected:
            raise ValueError("engine, language, and media_type do not match")
        return self


class SucceededTransformationCodeLinesResult(_TransformationCodePartialResult):
    operation_outcome: Literal["succeeded"]
    result_package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    view_mode: Literal["lines"]
    start_line: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    end_line: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    returned_line_count: Annotated[StrictInt, Field(ge=1, le=200)]
    lines: tuple[TransformationCodeLine, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_lines(self) -> SucceededTransformationCodeLinesResult:
        if self.returned_line_count != len(self.lines):
            raise ValueError("returned_line_count must equal lines length")
        if self.end_line != self.start_line + self.returned_line_count - 1:
            raise ValueError("line range is inconsistent")
        if tuple(item.line_number for item in self.lines) != tuple(
            range(self.start_line, self.end_line + 1)
        ):
            raise ValueError("line numbers must be complete and contiguous")
        return self


class FailedTransformationCodeLinesResult(_TransformationCodePartialResult):
    operation_outcome: Literal["failed"]
    view_mode: Literal["lines"]
    start_line: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    end_line: Annotated[StrictInt, Field(ge=1, le=_MAX_MARKDOWN_BYTES)]
    returned_line_count: Annotated[StrictInt, Field(ge=1, le=200)]
    lines: tuple[TransformationCodeLine, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_lines(self) -> FailedTransformationCodeLinesResult:
        if self.returned_line_count != len(self.lines):
            raise ValueError("returned_line_count must equal lines length")
        if self.end_line != self.start_line + self.returned_line_count - 1:
            raise ValueError("line range is inconsistent")
        if tuple(item.line_number for item in self.lines) != tuple(
            range(self.start_line, self.end_line + 1)
        ):
            raise ValueError("line numbers must be complete and contiguous")
        return self


class SucceededTransformationCodeSearchResult(_TransformationCodePartialResult):
    operation_outcome: Literal["succeeded"]
    result_package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    view_mode: Literal["search"]
    total_match_count: Annotated[StrictInt, Field(ge=0)]
    returned_count: Annotated[StrictInt, Field(ge=0, le=50)]
    complete: StrictBool
    matches: tuple[TransformationCodeSearchMatch, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_matches(self) -> SucceededTransformationCodeSearchResult:
        if self.returned_count != len(self.matches):
            raise ValueError("returned_count must equal matches length")
        if self.complete != (self.returned_count == self.total_match_count):
            raise ValueError("complete must identify whether every literal match was returned")
        return self


class FailedTransformationCodeSearchResult(_TransformationCodePartialResult):
    operation_outcome: Literal["failed"]
    view_mode: Literal["search"]
    total_match_count: Annotated[StrictInt, Field(ge=0)]
    returned_count: Annotated[StrictInt, Field(ge=0, le=50)]
    complete: StrictBool
    matches: tuple[TransformationCodeSearchMatch, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_matches(self) -> FailedTransformationCodeSearchResult:
        if self.returned_count != len(self.matches):
            raise ValueError("returned_count must equal matches length")
        if self.complete != (self.returned_count == self.total_match_count):
            raise ValueError("complete must identify whether every literal match was returned")
        return self


SucceededTransformationCodeResult: TypeAlias = (
    SucceededSqlTransformationCodeResult
    | SucceededPythonTransformationCodeResult
    | SucceededCadMeasurementTransformationCodeResult
    | SucceededTransformationCodeLinesResult
    | SucceededTransformationCodeSearchResult
)
FailedTransformationCodeResult: TypeAlias = (
    FailedSqlTransformationCodeResult
    | FailedPythonTransformationCodeResult
    | FailedCadMeasurementTransformationCodeResult
    | FailedTransformationCodeLinesResult
    | FailedTransformationCodeSearchResult
)
TransformationCodeResult: TypeAlias = (
    SucceededSqlTransformationCodeResult
    | FailedSqlTransformationCodeResult
    | SucceededPythonTransformationCodeResult
    | FailedPythonTransformationCodeResult
    | SucceededCadMeasurementTransformationCodeResult
    | FailedCadMeasurementTransformationCodeResult
    | SucceededTransformationCodeLinesResult
    | FailedTransformationCodeLinesResult
    | SucceededTransformationCodeSearchResult
    | FailedTransformationCodeSearchResult
)
