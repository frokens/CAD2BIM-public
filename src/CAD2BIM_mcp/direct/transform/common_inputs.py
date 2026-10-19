"""Provider-visible inputs shared by Direct catalog, operation, query, and transform tools.

These models are boundary adapters.  They express complete, closed JSON
objects for the Provider and keep Direct session selection out of the shared
Domain/Application command.
"""

from __future__ import annotations

from CAD2BIM_backend.domain.data_transform import (
    TransformMetadataSelection, TransformInlineSchemaSelection, TransformInlineRowsSelection,
    TransformParameterSelection, TransformInlineTextSelection,
)

from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    field_validator,
)

from CAD2BIM_backend.domain.text_source import TextSourceView
from CAD2BIM_backend.domain.data_runtime import CriticalOperationKind
from CAD2BIM_backend.domain.data_transform import (
    TransformationCodeReference as DomainTransformationCodeReference,
)
from CAD2BIM_mcp.tool_docs import data_transform as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_OPERATION_ALIAS_PATTERN = r"^op_[0-9]{3,}$"
_SAFE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_PAYLOAD_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,119}$"
_INT64_MAX = 9_223_372_036_854_775_807
_INT64_MIN = -9_223_372_036_854_775_808
_MAX_SOURCE_BYTES = 65_536
_MAX_PARAMETER_STRING_BYTES = 4_096
_MAX_PARAMETERS_BYTES = 32 * 1_024


class DirectM2aInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _validate_short_text(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be empty or whitespace")
    if "\x00" in value:
        raise ValueError("value must not contain NUL")
    return value


def _validate_utf8(value: str, maximum: int) -> str:
    if _utf8_size(value) > maximum:
        raise ValueError(f"value exceeds {maximum} UTF-8 bytes")
    return value


PositiveInt64 = Annotated[StrictInt, Field(ge=1, le=_INT64_MAX)]
SignedInt64 = Annotated[StrictInt, Field(ge=_INT64_MIN, le=_INT64_MAX)]
DirectoryPageSize = Annotated[StrictInt, Field(ge=1, le=50)]
ParameterString = Annotated[str, Field(max_length=_MAX_PARAMETER_STRING_BYTES)]
LogicalKeyField = Annotated[str, Field(min_length=1, max_length=255)]


class DirectPackageListInput(DirectM2aInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    before_sequence: PositiveInt64 | None = Field(
        default=None,
        description=(_tool_docs.DIRECT_PACKAGE_LIST_INPUT_BEFORE_SEQUENCE_DESCRIPTION),
    )
    page_size: DirectoryPageSize = Field(
        default=20,
        description=_tool_docs.DIRECT_PACKAGE_LIST_INPUT_PAGE_SIZE_DESCRIPTION,
    )


class OperationListFields(DirectM2aInput):
    tool_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,95}$")
    outcome: Literal["succeeded", "failed"] | None = None
    before_sequence: PositiveInt64 | None = Field(
        default=None,
        description=(_tool_docs.DIRECT_OPERATION_LIST_INPUT_BEFORE_SEQUENCE_DESCRIPTION),
    )
    page_size: DirectoryPageSize = Field(
        default=20,
        description=_tool_docs.DIRECT_OPERATION_LIST_INPUT_PAGE_SIZE_DESCRIPTION,
    )
    operation_kind: CriticalOperationKind | None = Field(
        default=None,
        description=(_tool_docs.DIRECT_OPERATION_LIST_INPUT_OPERATION_KIND_DESCRIPTION),
    )


class DirectOperationListInput(OperationListFields):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )


from CAD2BIM_backend.domain.preview_report_read import PreviewCaseTextSelection, PreviewReportTextSelection


class _OperationGetInput(DirectM2aInput):
    direct_session_alias: str = Field(pattern=_DIRECT_ALIAS_PATTERN, max_length=32)


class OperationCaseFields(DirectM2aInput):
    case_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    after_case_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    page_size: int = Field(default=1, ge=1, le=4)


class OperationAliasFields(OperationCaseFields):
    mode: Literal["operation"] = "operation"
    operation_alias: str = Field(pattern=_OPERATION_ALIAS_PATTERN, max_length=32)


class OperationReferenceFields(OperationCaseFields):
    mode: Literal["operation_reference"] = "operation_reference"
    operation_ref: str = Field(min_length=1, max_length=16_384)


class OperationInspectionFields(DirectM2aInput):
    mode: Literal["inspection"] = "inspection"
    inspection_ref: str = Field(min_length=1, max_length=16_384)


class OperationTransformDescriptionFields(DirectM2aInput):
    mode: Literal["transform_input"]
    operation_alias: str = Field(pattern=_OPERATION_ALIAS_PATTERN, max_length=32)
    input_kind: Literal["package_description"]
    view: TextSourceView


class OperationTransformSchemaFields(DirectM2aInput):
    mode: Literal["transform_input"]
    operation_alias: str = Field(pattern=_OPERATION_ALIAS_PATTERN, max_length=32)
    input_kind: Literal["output_schema"]
    payload_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$", max_length=120)
    view: TextSourceView


class OperationAliasSelection(OperationAliasFields, _OperationGetInput):
    pass


class OperationReferenceSelection(OperationReferenceFields, _OperationGetInput):
    pass


class OperationInspectionSelection(OperationInspectionFields, _OperationGetInput):
    pass


class OperationTransformDescriptionSelection(OperationTransformDescriptionFields, _OperationGetInput):
    pass


class OperationTransformSchemaSelection(OperationTransformSchemaFields, _OperationGetInput):
    pass


class OperationTransformMetadataSelection(TransformMetadataSelection, _OperationGetInput):
    pass


class OperationTransformInlineSchemaSelection(TransformInlineSchemaSelection, _OperationGetInput):
    pass


class OperationTransformInlineRowsSelection(TransformInlineRowsSelection, _OperationGetInput):
    pass


class OperationTransformParameterSelection(TransformParameterSelection, _OperationGetInput):
    pass


class OperationTransformInlineTextSelection(TransformInlineTextSelection, _OperationGetInput):
    pass


class DirectPreviewCaseTextSelection(PreviewCaseTextSelection, _OperationGetInput):
    pass


class DirectPreviewReportTextSelection(PreviewReportTextSelection, _OperationGetInput):
    pass


DirectOperationGetInput: TypeAlias = (DirectPreviewCaseTextSelection | DirectPreviewReportTextSelection | OperationAliasSelection | OperationReferenceSelection
    | OperationInspectionSelection | OperationTransformDescriptionSelection | OperationTransformSchemaSelection
    | OperationTransformMetadataSelection | OperationTransformInlineSchemaSelection
    | OperationTransformInlineRowsSelection | OperationTransformParameterSelection | OperationTransformInlineTextSelection)


class TransformInputBinding(DirectM2aInput):
    input_name: str = Field(
        pattern=_SAFE_NAME_PATTERN,
        max_length=63,
        description=_tool_docs.TRANSFORM_INPUT_BINDING_INPUT_NAME_DESCRIPTION,
    )
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=_tool_docs.TRANSFORM_INPUT_BINDING_PACKAGE_ALIAS_DESCRIPTION,
    )
    payload_name: str = Field(
        min_length=1,
        max_length=120,
        description=_tool_docs.TRANSFORM_INPUT_BINDING_PAYLOAD_NAME_DESCRIPTION,
    )

    _payload_name = field_validator("payload_name")(_validate_short_text)


class ResolvedTransformInputBinding(TransformInputBinding):
    __doc__ = _tool_docs.RESOLVED_TRANSFORM_INPUT_BINDING_DESCRIPTION
    logical_key_fields: tuple[LogicalKeyField, ...] = Field(max_length=16)

    @field_validator("logical_key_fields")
    @classmethod
    def unique_keys(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("resolved logical_key_fields must be unique")
        return value


class _ParameterBase(DirectM2aInput):
    name: str = Field(
        pattern=_SAFE_NAME_PATTERN,
        max_length=63,
        description=_tool_docs.PARAMETER_BASE_NAME_DESCRIPTION,
    )


class StringParameter(_ParameterBase):
    kind: Literal["string"] = Field(description=_tool_docs.STRING_PARAMETER_KIND_DESCRIPTION)
    value: str = Field(
        max_length=_MAX_PARAMETER_STRING_BYTES,
        description=_tool_docs.STRING_PARAMETER_VALUE_DESCRIPTION,
    )

    @field_validator("value")
    @classmethod
    def validate_bytes(cls, value: str) -> str:
        return _validate_utf8(value, _MAX_PARAMETER_STRING_BYTES)


class IntegerParameter(_ParameterBase):
    kind: Literal["integer"] = Field(description=_tool_docs.INTEGER_PARAMETER_KIND_DESCRIPTION)
    value: SignedInt64 = Field(description=_tool_docs.INTEGER_PARAMETER_VALUE_DESCRIPTION)


class NumberParameter(_ParameterBase):
    kind: Literal["number"] = Field(description=_tool_docs.NUMBER_PARAMETER_KIND_DESCRIPTION)
    value: StrictFloat = Field(description=_tool_docs.NUMBER_PARAMETER_VALUE_DESCRIPTION)


class BooleanParameter(_ParameterBase):
    kind: Literal["boolean"] = Field(description=_tool_docs.BOOLEAN_PARAMETER_KIND_DESCRIPTION)
    value: StrictBool = Field(description=_tool_docs.BOOLEAN_PARAMETER_VALUE_DESCRIPTION)


StringParameterValues = Annotated[tuple[ParameterString, ...], Field(min_length=1, max_length=100)]
IntegerParameterValues = Annotated[tuple[SignedInt64, ...], Field(min_length=1, max_length=100)]
NumberParameterValues = Annotated[tuple[StrictFloat, ...], Field(min_length=1, max_length=100)]
BooleanParameterValues = Annotated[tuple[StrictBool, ...], Field(min_length=1, max_length=100)]


class StringListParameter(_ParameterBase):
    kind: Literal["string_list"] = Field(
        description=_tool_docs.STRING_LIST_PARAMETER_KIND_DESCRIPTION
    )
    value: StringParameterValues = Field(
        description=_tool_docs.STRING_LIST_PARAMETER_VALUE_DESCRIPTION
    )

    @field_validator("value")
    @classmethod
    def validate_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _validate_utf8(item, _MAX_PARAMETER_STRING_BYTES)
        return value


class IntegerListParameter(_ParameterBase):
    kind: Literal["integer_list"] = Field(
        description=_tool_docs.INTEGER_LIST_PARAMETER_KIND_DESCRIPTION
    )
    value: IntegerParameterValues = Field(
        description=_tool_docs.INTEGER_LIST_PARAMETER_VALUE_DESCRIPTION
    )


class NumberListParameter(_ParameterBase):
    kind: Literal["number_list"] = Field(
        description=_tool_docs.NUMBER_LIST_PARAMETER_KIND_DESCRIPTION
    )
    value: NumberParameterValues = Field(
        description=_tool_docs.NUMBER_LIST_PARAMETER_VALUE_DESCRIPTION
    )


class BooleanListParameter(_ParameterBase):
    kind: Literal["boolean_list"] = Field(
        description=_tool_docs.BOOLEAN_LIST_PARAMETER_KIND_DESCRIPTION
    )
    value: BooleanParameterValues = Field(
        description=_tool_docs.BOOLEAN_LIST_PARAMETER_VALUE_DESCRIPTION
    )


TransformParameter: TypeAlias = (
    StringParameter
    | IntegerParameter
    | NumberParameter
    | BooleanParameter
    | StringListParameter
    | IntegerListParameter
    | NumberListParameter
    | BooleanListParameter
)


class DuckDbSqlExecution(DirectM2aInput):
    engine: Literal["duckdb_sql"] = Field(
        description=_tool_docs.DUCK_DB_SQL_EXECUTION_ENGINE_DESCRIPTION
    )
    code: str = Field(
        min_length=1,
        max_length=_MAX_SOURCE_BYTES,
        description=(_tool_docs.DUCK_DB_SQL_EXECUTION_CODE_DESCRIPTION),
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SQL code must not be whitespace")
        if "\x00" in value:
            raise ValueError("SQL code must not contain NUL")
        return _validate_utf8(value, _MAX_SOURCE_BYTES)


class ParquetOutputDeclaration(DirectM2aInput):
    payload_name: str = Field(
        pattern=_PAYLOAD_NAME_PATTERN,
        max_length=120,
        description=(_tool_docs.PARQUET_OUTPUT_DECLARATION_PAYLOAD_NAME_DESCRIPTION),
    )
    format: Literal["parquet"] = Field(
        description=_tool_docs.PARQUET_OUTPUT_DECLARATION_FORMAT_DESCRIPTION
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description=_tool_docs.PARQUET_OUTPUT_DECLARATION_DESCRIPTION_DESCRIPTION,
    )
    logical_key_fields: tuple[LogicalKeyField, ...] = Field(
        min_length=1,
        max_length=16,
        json_schema_extra={"uniqueItems": True},
        description=(_tool_docs.PARQUET_OUTPUT_DECLARATION_LOGICAL_KEY_FIELDS_DESCRIPTION),
    )

    _description = field_validator("description")(_validate_short_text)

    @field_validator("logical_key_fields")
    @classmethod
    def validate_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("logical_key_fields must be unique")
        for item in value:
            if not 1 <= len(item) <= 255:
                raise ValueError("logical key field names must contain 1..255 characters")
            _validate_short_text(item)
        return value


class OutputPackageDeclaration(DirectM2aInput):
    name: str = Field(
        min_length=1,
        max_length=255,
        description=_tool_docs.OUTPUT_PACKAGE_DECLARATION_NAME_DESCRIPTION,
    )
    description_markdown: str = Field(
        min_length=1,
        max_length=_MAX_SOURCE_BYTES,
        description=(_tool_docs.OUTPUT_PACKAGE_DECLARATION_DESCRIPTION_MARKDOWN_DESCRIPTION),
    )

    _name = field_validator("name")(_validate_short_text)

    @field_validator("description_markdown")
    @classmethod
    def validate_markdown(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description_markdown must not be whitespace")
        if "\x00" in value:
            raise ValueError("description_markdown must not contain NUL")
        return _validate_utf8(value, _MAX_SOURCE_BYTES)


class PackageTransformationCodeReference(DirectM2aInput):
    reference_kind: Literal["package"] = Field(
        description=_tool_docs.PACKAGE_TRANSFORMATION_CODE_REFERENCE_REFERENCE_KIND_DESCRIPTION
    )
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=_tool_docs.PACKAGE_TRANSFORMATION_CODE_REFERENCE_PACKAGE_ALIAS_DESCRIPTION,
    )


class OperationTransformationCodeReference(DirectM2aInput):
    reference_kind: Literal["operation"] = Field(
        description=_tool_docs.OPERATION_TRANSFORMATION_CODE_REFERENCE_REFERENCE_KIND_DESCRIPTION
    )
    operation_alias: str = Field(
        pattern=_OPERATION_ALIAS_PATTERN,
        max_length=32,
        description=_tool_docs.OPERATION_TRANSFORMATION_CODE_REFERENCE_OPERATION_ALIAS_DESCRIPTION,
    )


TransformationCodeReference: TypeAlias = (
    PackageTransformationCodeReference | OperationTransformationCodeReference
)


class DirectTransformationCodeInput(DirectM2aInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    reference: TransformationCodeReference = Field(
        description=_tool_docs.DIRECT_TRANSFORMATION_CODE_INPUT_REFERENCE_DESCRIPTION
    )


_DOMAIN_CODE_REFERENCE = TypeAdapter(DomainTransformationCodeReference)


def to_domain_transformation_code_reference(
    value: TransformationCodeReference,
) -> DomainTransformationCodeReference:
    """Convert one complete Provider reference to its entry-neutral Domain union."""

    return _DOMAIN_CODE_REFERENCE.validate_python(value.model_dump(mode="python"))


__all__ = [
    "BooleanListParameter",
    "BooleanParameter",
    "DirectPackageListInput",
    "DirectM2aInput",
    "DirectOperationGetInput",
    "DirectOperationListInput",
    "DirectTransformationCodeInput",
    "DirectoryPageSize",
    "DuckDbSqlExecution",
    "IntegerListParameter",
    "IntegerParameter",
    "NumberListParameter",
    "NumberParameter",
    "OperationTransformationCodeReference",
    "OutputPackageDeclaration",
    "PackageTransformationCodeReference",
    "ParquetOutputDeclaration",
    "PositiveInt64",
    "StringListParameter",
    "StringParameter",
    "TransformInputBinding",
    "TransformParameter",
    "TransformationCodeReference",
    "to_domain_transformation_code_reference",
]
