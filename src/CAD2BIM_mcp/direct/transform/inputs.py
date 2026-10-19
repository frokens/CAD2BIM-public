"""Provider-safe Direct inputs for data transformation.

The three root models are complete closed branches. They keep Direct session
selection outside the entry-neutral Domain command while making the engine,
inline cell, and output variants explicit to Provider clients.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_transform import (
    MAX_TRANSFORM_CODE_BYTES,
    DataTransformCommand,
    JsonOutputInput,
    OutputPackageInput,
    TransformOutputsSet,
    TransformOutputInput,
    resolve_data_transform_create,
)
from CAD2BIM_mcp.direct.transform.common_inputs import TransformParameter
from CAD2BIM_mcp.tool_docs import data_transform as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_SAFE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_INPUT_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,119}$"
_PAYLOAD_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,119}$"
_INT64_MAX = 9_223_372_036_854_775_807
_INT64_MIN = -9_223_372_036_854_775_808
_MAX_JSON_SCHEMA_BYTES = 65_536
_MAX_PARAMETER_BYTES = 32 * 1_024
_MAX_INLINE_TABLE_BYTES = 1_048_576


class DirectDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _bounded_nonblank_utf8(value: str, limit: int, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} exceeds {limit} UTF-8 bytes")
    return value


class DuckDbSqlExecution(DirectDataInput):
    engine: Literal["duckdb_sql"] = Field(
        description=_tool_docs.INPUTS_DUCK_DB_SQL_EXECUTION_ENGINE_DESCRIPTION
    )
    code: str = Field(
        min_length=1,
        max_length=MAX_TRANSFORM_CODE_BYTES,
        description=(_tool_docs.INPUTS_DUCK_DB_SQL_EXECUTION_CODE_DESCRIPTION),
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _bounded_nonblank_utf8(value, MAX_TRANSFORM_CODE_BYTES, "SQL code")


class PythonArrowExecution(DirectDataInput):
    engine: Literal["python_arrow"] = Field(
        description=_tool_docs.PYTHON_ARROW_EXECUTION_ENGINE_DESCRIPTION
    )
    code: str = Field(
        min_length=1,
        max_length=MAX_TRANSFORM_CODE_BYTES,
        description=(_tool_docs.PYTHON_ARROW_EXECUTION_CODE_DESCRIPTION),
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        # Keep Provider validation to the closed request shape.  The application
        # owns compilation so a rejected source can be recovered through its
        # failed Operation instead of disappearing before execution history exists.
        return _bounded_nonblank_utf8(value, MAX_TRANSFORM_CODE_BYTES, "Python code")


class TransformInputBinding(DirectDataInput):
    input_name: str = Field(
        pattern=_SAFE_NAME_PATTERN,
        max_length=63,
        description=(_tool_docs.INPUTS_TRANSFORM_INPUT_BINDING_INPUT_NAME_DESCRIPTION),
    )
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=_tool_docs.TRANSFORM_INPUT_BINDING_PACKAGE_ALIAS_DESCRIPTION,
    )
    payload_name: str = Field(
        min_length=1,
        max_length=120,
        description=_tool_docs.INPUTS_TRANSFORM_INPUT_BINDING_PAYLOAD_NAME_DESCRIPTION,
    )

    @field_validator("payload_name")
    @classmethod
    def validate_payload_name(cls, value: str) -> str:
        return _bounded_nonblank_utf8(value, 480, "payload_name")


class InlineStringValue(DirectDataInput):
    kind: Literal["string"] = Field(description=_tool_docs.INLINE_STRING_VALUE_KIND_DESCRIPTION)
    value: str = Field(description=_tool_docs.INLINE_STRING_VALUE_VALUE_DESCRIPTION)


class InlineIntegerValue(DirectDataInput):
    kind: Literal["integer"] = Field(description=_tool_docs.INLINE_INTEGER_VALUE_KIND_DESCRIPTION)
    value: StrictInt = Field(
        ge=_INT64_MIN,
        le=_INT64_MAX,
        description=_tool_docs.INLINE_INTEGER_VALUE_VALUE_DESCRIPTION,
    )


class InlineNumberValue(DirectDataInput):
    kind: Literal["number"] = Field(description=_tool_docs.INLINE_NUMBER_VALUE_KIND_DESCRIPTION)
    value: StrictFloat = Field(description=_tool_docs.NUMBER_PARAMETER_VALUE_DESCRIPTION)


class InlineBooleanValue(DirectDataInput):
    kind: Literal["boolean"] = Field(description=_tool_docs.INLINE_BOOLEAN_VALUE_KIND_DESCRIPTION)
    value: StrictBool = Field(description=_tool_docs.BOOLEAN_PARAMETER_VALUE_DESCRIPTION)


class InlineNullValue(DirectDataInput):
    kind: Literal["null"] = Field(description=_tool_docs.INLINE_NULL_VALUE_KIND_DESCRIPTION)


InlineCell: TypeAlias = (
    InlineStringValue
    | InlineIntegerValue
    | InlineNumberValue
    | InlineBooleanValue
    | InlineNullValue
)


class InlineColumn(DirectDataInput):
    name: str = Field(
        min_length=1,
        max_length=255,
        description=_tool_docs.INLINE_COLUMN_NAME_DESCRIPTION,
    )
    type: Literal["string", "integer", "number", "boolean"] = Field(
        description=_tool_docs.INLINE_COLUMN_TYPE_DESCRIPTION
    )
    nullable: StrictBool = Field(description=_tool_docs.INLINE_COLUMN_NULLABLE_DESCRIPTION)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _bounded_nonblank_utf8(value, 1_020, "inline column name")


class InlineRow(DirectDataInput):
    values: tuple[InlineCell, ...] = Field(
        min_length=1,
        max_length=32,
        description=_tool_docs.INLINE_ROW_VALUES_DESCRIPTION,
    )


class InlineTable(DirectDataInput):
    input_name: str = Field(
        pattern=_INPUT_NAME_PATTERN,
        max_length=120,
        description=(_tool_docs.INLINE_TABLE_INPUT_NAME_DESCRIPTION),
    )
    columns: tuple[InlineColumn, ...] = Field(
        min_length=1,
        max_length=32,
        description=_tool_docs.INLINE_TABLE_COLUMNS_DESCRIPTION,
    )
    rows: tuple[InlineRow, ...] = Field(
        default=(),
        max_length=500,
        description=_tool_docs.INLINE_TABLE_ROWS_DESCRIPTION,
    )

    @model_validator(mode="after")
    def validate_columns_and_rows(self) -> "InlineTable":
        names = [column.name for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("inline column names must be unique")
        for row in self.rows:
            if len(row.values) != len(self.columns):
                raise ValueError("inline row values must have exactly the same length as columns")
            for column, cell in zip(self.columns, row.values, strict=True):
                if cell.kind == "null":
                    if not column.nullable:
                        raise ValueError("inline null cell requires a nullable column")
                elif cell.kind != column.type:
                    raise ValueError("inline cell kind must match its column type")
        return self


class ParquetOutputDeclaration(DirectDataInput):
    payload_name: str = Field(
        pattern=_PAYLOAD_NAME_PATTERN,
        max_length=120,
        description=(_tool_docs.INPUTS_PARQUET_OUTPUT_DECLARATION_PAYLOAD_NAME_DESCRIPTION),
    )
    format: Literal["parquet"] = Field(
        description=_tool_docs.PARQUET_OUTPUT_DECLARATION_FORMAT_DESCRIPTION
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description=_tool_docs.INPUTS_PARQUET_OUTPUT_DECLARATION_DESCRIPTION_DESCRIPTION,
    )
    logical_key_fields: tuple[str, ...] = Field(
        min_length=1,
        max_length=16,
        json_schema_extra={"uniqueItems": True},
        description=(_tool_docs.INPUTS_PARQUET_OUTPUT_DECLARATION_LOGICAL_KEY_FIELDS_DESCRIPTION),
    )

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _bounded_nonblank_utf8(value, 2_000, "output description")

    @field_validator("logical_key_fields")
    @classmethod
    def validate_logical_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("logical_key_fields must be unique")
        for value in values:
            if not 1 <= len(value) <= 255:
                raise ValueError("logical key field names must contain 1..255 characters")
            _bounded_nonblank_utf8(value, 1_020, "logical key field")
        return values


JsonOutputDeclaration = JsonOutputInput
TransformOutputDeclaration = TransformOutputInput
OutputPackageDeclaration = OutputPackageInput


class _DirectTransformRoot(DirectDataInput):
    mode: Literal["create"] = Field(description=_tool_docs.DIRECT_TRANSFORM_ROOT_MODE_DESCRIPTION)
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    inputs: tuple[TransformInputBinding, ...] = Field(
        min_length=1,
        max_length=16,
        description=(_tool_docs.DIRECT_TRANSFORM_ROOT_INPUTS_DESCRIPTION),
    )
    inline_tables: tuple[InlineTable, ...] = Field(
        default=(),
        max_length=8,
        description=(_tool_docs.DIRECT_TRANSFORM_ROOT_INLINE_TABLES_DESCRIPTION),
    )
    parameters: tuple[TransformParameter, ...] = Field(
        default=(),
        max_length=32,
        description=(_tool_docs.DIRECT_TRANSFORM_ROOT_PARAMETERS_DESCRIPTION),
    )
    outputs: TransformOutputsSet = Field(
        description=(_tool_docs.DIRECT_TRANSFORM_ROOT_OUTPUTS_DESCRIPTION),
    )
    output_package: OutputPackageDeclaration = Field(
        description=_tool_docs.DIRECT_TRANSFORM_ROOT_OUTPUT_PACKAGE_DESCRIPTION
    )

    @model_validator(mode="after")
    def validate_request(self) -> "_DirectTransformRoot":
        package_names = [item.input_name for item in self.inputs]
        if len(package_names) != len(set(package_names)):
            raise ValueError("Package input_name values must be unique")
        bindings = [(item.package_alias, item.payload_name) for item in self.inputs]
        if len(bindings) != len(set(bindings)):
            raise ValueError("the same exact Package Payload must not be bound twice")
        inline_names = [item.input_name for item in self.inline_tables]
        if len(inline_names) != len(set(inline_names)):
            raise ValueError("inline table input_name values must be unique")
        if set(package_names).intersection(inline_names):
            raise ValueError("inline table input_name must not collide with Package input_name")
        encoded_inline = canonical_json(
            [item.model_dump(mode="json") for item in self.inline_tables]
        ).encode("utf-8")
        if len(encoded_inline) > _MAX_INLINE_TABLE_BYTES:
            raise ValueError("inline tables exceed 1 MiB canonical JSON")
        parameter_names = [item.name for item in self.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("parameter names must be unique")
        encoded_parameters = canonical_json(
            [item.model_dump(mode="json") for item in self.parameters]
        ).encode("utf-8")
        if len(encoded_parameters) > _MAX_PARAMETER_BYTES:
            raise ValueError("typed parameters exceed 32 KiB canonical JSON")
        output_names = [item.payload_name for item in self.outputs.value]
        if len(output_names) != len(set(output_names)):
            raise ValueError("output payload_name values must be unique")
        if set(output_names).intersection(package_names + inline_names):
            raise ValueError("output payload_name must not collide with any input_name")
        resolve_data_transform_create(self.model_dump(mode="python", exclude={"mode", "direct_session_alias"}))
        return self


class DirectDuckDbTransformInput(_DirectTransformRoot):
    execution: DuckDbSqlExecution = Field(
        description=_tool_docs.DIRECT_DUCK_DB_TRANSFORM_INPUT_EXECUTION_DESCRIPTION
    )


class DirectPythonArrowTransformInput(_DirectTransformRoot):
    execution: PythonArrowExecution = Field(
        description=_tool_docs.DIRECT_PYTHON_ARROW_TRANSFORM_INPUT_EXECUTION_DESCRIPTION
    )


class DirectIfcPythonArrowTransformInput(DirectPythonArrowTransformInput):
    ifc_source: Literal["current"] = Field(description=_tool_docs.IFC_SOURCE_DESCRIPTION)
    inputs: tuple[TransformInputBinding, ...] = Field(
        default=(), max_length=16, description=_tool_docs.IFC_INPUTS_DESCRIPTION,
    )


DirectDataTransformCreateInput: TypeAlias = (
    DirectDuckDbTransformInput | DirectPythonArrowTransformInput | DirectIfcPythonArrowTransformInput
)
_DIRECT_TRANSFORM_ADAPTER = TypeAdapter(DirectDataTransformCreateInput)


def validate_direct_data_transform_create_input(value: object) -> DirectDataTransformCreateInput:
    """Validate one complete Direct DuckDB or Python transform branch."""

    return _DIRECT_TRANSFORM_ADAPTER.validate_python(value)


def to_domain_data_transform_create_command(
    value: DirectDataTransformCreateInput,
) -> DataTransformCommand:
    """Drop Direct scope selection and validate the entry-neutral command."""

    payload = value.model_dump(mode="python", exclude={"direct_session_alias", "mode"})
    return resolve_data_transform_create(payload)


def to_domain_duckdb_transform_command(
    value: DirectDuckDbTransformInput,
) -> DataTransformCommand:
    return to_domain_data_transform_create_command(value)


def to_domain_python_arrow_transform_command(
    value: DirectPythonArrowTransformInput,
) -> DataTransformCommand:
    return to_domain_data_transform_create_command(value)


__all__ = [
    "DirectDuckDbTransformInput",
    "DirectDataInput",
    "DirectDataTransformCreateInput",
    "DirectPythonArrowTransformInput",
    "DuckDbSqlExecution",
    "InlineBooleanValue",
    "InlineCell",
    "InlineColumn",
    "InlineIntegerValue",
    "InlineNullValue",
    "InlineNumberValue",
    "InlineRow",
    "InlineStringValue",
    "InlineTable",
    "JsonOutputDeclaration",
    "ParquetOutputDeclaration",
    "PythonArrowExecution",
    "TransformInputBinding",
    "TransformOutputDeclaration",
    "to_domain_duckdb_transform_command",
    "to_domain_data_transform_create_command",
    "to_domain_python_arrow_transform_command",
    "validate_direct_data_transform_create_input",
]
