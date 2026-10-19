"""Entry-neutral contracts for M2 DuckDB data transformation workers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields
from typing import Annotated, Literal, Mapping, TypeAlias

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
from CAD2BIM_backend.domain.runtime_execution import DATA_PYTHON_EXECUTION, DATA_SQL_EXECUTION
from CAD2BIM_backend.domain.text_source import SourceEdit, TextChange, TextEdit, TextSet, TextSourceView, TextSourceViewResult


_SAFE_NAME = r"^[a-z][a-z0-9_]{0,62}$"
_PAYLOAD_NAME = r"^[a-z][a-z0-9_]{0,119}$"
_PACKAGE_ALIAS = r"^data_[0-9]{3,}$"
_OPERATION_ALIAS = r"^op_[0-9]{3,}$"
MAX_TRANSFORM_CODE_BYTES = 128 * 1024
_MAX_MARKDOWN_BYTES = 65_536
_MAX_PARAMETER_BYTES = 32 * 1024
_MAX_STRING_PARAMETER_BYTES = 4_096
_MAX_ERROR_MESSAGE_BYTES = 2_000
_MAX_INLINE_TABLE_BYTES = 1_048_576
_MAX_JSON_SCHEMA_BYTES = 65_536
_MAX_REPAIR_EDIT_BYTES = 16_384
_MAX_REPAIR_EDITS_BYTES = 32_768


class DataTransformModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _reject_blank_or_nul(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    return value


def _bounded_utf8(value: str, limit: int, field_name: str) -> str:
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} exceeds {limit} UTF-8 bytes")
    return value


def truncate_transform_error_message(
    message: str,
    *,
    fallback: str = "Data transformation failed",
) -> str:
    """Return one non-empty transform error bounded by UTF-8 bytes."""

    normalized = message.strip() or fallback
    encoded = normalized.encode("utf-8")
    if len(encoded) <= _MAX_ERROR_MESSAGE_BYTES:
        return normalized
    return encoded[:_MAX_ERROR_MESSAGE_BYTES].decode("utf-8", errors="ignore")


class TransformInputBinding(DataTransformModel):
    input_name: str = Field(
        pattern=_SAFE_NAME,
        max_length=63,
        description="Local SQL relation or Python ctx input name for this exact Payload binding.",
    )
    package_alias: str = Field(
        pattern=_PACKAGE_ALIAS,
        max_length=32,
        description="Visible immutable Data Package alias selected from the Package catalog.",
    )
    payload_name: str = Field(
        min_length=1,
        max_length=120,
        description="Exact Parquet Payload name selected from the Package description.",
    )

    @field_validator("payload_name")
    @classmethod
    def validate_payload_name(cls, value: str) -> str:
        return _reject_blank_or_nul(value, "payload_name")


class _NamedParameter(DataTransformModel):
    name: str = Field(
        pattern=_SAFE_NAME,
        max_length=63,
        description="Local DuckDB named parameter or Python ctx.param name.",
    )


class StringParameter(_NamedParameter):
    kind: Literal["string"] = Field(description="Selects the string parameter branch.")
    value: str = Field(description="UTF-8 string value, limited to 4,096 encoded bytes.")

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        return _bounded_utf8(value, _MAX_STRING_PARAMETER_BYTES, "string parameter")


class IntegerParameter(_NamedParameter):
    kind: Literal["integer"] = Field(description="Selects the signed integer branch.")
    value: StrictInt = Field(
        ge=-(2**63),
        le=2**63 - 1,
        description="Strict signed int64 value; JSON booleans are rejected.",
    )


class NumberParameter(_NamedParameter):
    kind: Literal["number"] = Field(description="Selects the finite float64 branch.")
    value: StrictFloat = Field(description="Finite float64 value; JSON booleans are rejected.")


class BooleanParameter(_NamedParameter):
    kind: Literal["boolean"] = Field(description="Selects the boolean parameter branch.")
    value: StrictBool = Field(description="Strict JSON boolean value.")


class StringListParameter(_NamedParameter):
    kind: Literal["string_list"] = Field(description="Selects the string-list branch.")
    value: tuple[str, ...] = Field(
        min_length=1,
        max_length=100,
        description="Ordered 1..100 string values; duplicates are preserved.",
    )

    @field_validator("value")
    @classmethod
    def validate_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _bounded_utf8(value, _MAX_STRING_PARAMETER_BYTES, "string list element")
        return values


class IntegerListParameter(_NamedParameter):
    kind: Literal["integer_list"] = Field(description="Selects the int64-list branch.")
    value: tuple[StrictInt, ...] = Field(
        min_length=1,
        max_length=100,
        description="Ordered 1..100 strict signed int64 values.",
    )

    @field_validator("value")
    @classmethod
    def validate_values(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value < -(2**63) or value > 2**63 - 1 for value in values):
            raise ValueError("integer list element is outside signed int64")
        return values


class NumberListParameter(_NamedParameter):
    kind: Literal["number_list"] = Field(description="Selects the float64-list branch.")
    value: tuple[StrictFloat, ...] = Field(
        min_length=1,
        max_length=100,
        description="Ordered 1..100 finite float64 values.",
    )


class BooleanListParameter(_NamedParameter):
    kind: Literal["boolean_list"] = Field(description="Selects the boolean-list branch.")
    value: tuple[StrictBool, ...] = Field(
        min_length=1,
        max_length=100,
        description="Ordered 1..100 strict JSON boolean values.",
    )


TransformParameter: TypeAlias = Annotated[
    StringParameter
    | IntegerParameter
    | NumberParameter
    | BooleanParameter
    | StringListParameter
    | IntegerListParameter
    | NumberListParameter
    | BooleanListParameter,
    Field(discriminator="kind"),
]


class DuckDbSqlExecution(DataTransformModel):
    engine: Literal["duckdb_sql"] = Field(description="Selects the M2b DuckDB SQL engine.")
    code: str = Field(
        min_length=1,
        max_length=MAX_TRANSFORM_CODE_BYTES,
        description=(
            f"Private multi-statement DuckDB SQL, limited to {MAX_TRANSFORM_CODE_BYTES:,} UTF-8 bytes. "
            "Declared outputs are same-named final TEMP relations."
        ),
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        _reject_blank_or_nul(value, "code")
        return _bounded_utf8(value, MAX_TRANSFORM_CODE_BYTES, "code")


class PythonArrowExecution(DataTransformModel):
    engine: Literal["python_arrow"] = Field(
        description="Selects the Data Python/Arrow Worker engine."
    )
    code: str = Field(
        min_length=1,
        max_length=MAX_TRANSFORM_CODE_BYTES,
        description=(
            "Python source defining the fixed transform(ctx) entry point, limited to "
            f"{MAX_TRANSFORM_CODE_BYTES:,} UTF-8 bytes."
        ),
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        _reject_blank_or_nul(value, "code")
        # Compilation and the fixed transform(ctx) contract are accepted-source
        # validation, not request-shape validation.  The application persists the
        # exact source and description before reporting those failures as an
        # inspectable Data Transform Operation.
        return _bounded_utf8(value, MAX_TRANSFORM_CODE_BYTES, "code")


class _InlineCell(DataTransformModel):
    pass


class InlineStringValue(_InlineCell):
    kind: Literal["string"] = Field(description="Selects a UTF-8 string cell.")
    value: str = Field(description="Exact UTF-8 string cell value.")


class InlineIntegerValue(_InlineCell):
    kind: Literal["integer"] = Field(description="Selects a signed int64 cell.")
    value: StrictInt = Field(ge=-(2**63), le=2**63 - 1, description="Strict signed int64 value.")


class InlineNumberValue(_InlineCell):
    kind: Literal["number"] = Field(description="Selects a finite float64 cell.")
    value: StrictFloat = Field(description="Finite float64 value.")


class InlineBooleanValue(_InlineCell):
    kind: Literal["boolean"] = Field(description="Selects a strict JSON boolean cell.")
    value: StrictBool = Field(description="Strict JSON boolean value.")


class InlineNullValue(_InlineCell):
    kind: Literal["null"] = Field(description="Selects a null cell for a nullable column.")


InlineCell: TypeAlias = Annotated[
    InlineStringValue
    | InlineIntegerValue
    | InlineNumberValue
    | InlineBooleanValue
    | InlineNullValue,
    Field(discriminator="kind"),
]


class InlineColumn(DataTransformModel):
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Unique case-sensitive column name in this inline table.",
    )
    type: Literal["string", "integer", "number", "boolean"] = Field(
        description="Exact scalar type required for every non-null cell in this column."
    )
    nullable: StrictBool = Field(description="Whether this column accepts the null cell branch.")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _reject_blank_or_nul(value, "inline column name")


class InlineRow(DataTransformModel):
    values: tuple[InlineCell, ...] = Field(
        min_length=1,
        max_length=32,
        description="Ordered typed cells matching the table's columns exactly.",
    )


class InlineTable(DataTransformModel):
    input_name: str = Field(
        pattern=_PAYLOAD_NAME,
        max_length=120,
        description="Local read-only relation name for this request-scoped inline table.",
    )
    columns: tuple[InlineColumn, ...] = Field(
        min_length=1,
        max_length=32,
        description="One to 32 closed scalar column declarations.",
    )
    rows: tuple[InlineRow, ...] = Field(
        default=(),
        max_length=500,
        description="Zero to 500 rows whose cells match the declared columns.",
    )

    @model_validator(mode="after")
    def validate_columns_and_rows(self) -> "InlineTable":
        column_names = [column.name for column in self.columns]
        if len(column_names) != len(set(column_names)):
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


class ParquetOutputDeclaration(DataTransformModel):
    payload_name: str = Field(
        pattern=_PAYLOAD_NAME,
        max_length=120,
        description="Name of one derived Parquet Payload submitted by SQL or Python.",
    )
    format: Literal["parquet"] = Field(description="Publishes this declared output as Parquet.")
    description: str = Field(
        min_length=1,
        max_length=500,
        description="Short purpose of this output Payload.",
    )
    logical_key_fields: tuple[str, ...] = Field(
        min_length=1,
        max_length=16,
        description="Case-sensitive output columns forming a non-null unique logical key.",
    )

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _reject_blank_or_nul(value, "description")

    @field_validator("logical_key_fields")
    @classmethod
    def validate_logical_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("logical_key_fields must be unique")
        for value in values:
            if len(value) > 255:
                raise ValueError("logical key field exceeds 255 characters")
            _reject_blank_or_nul(value, "logical key field")
        return values


class JsonOutputDeclaration(DataTransformModel):
    payload_name: str = Field(
        pattern=_PAYLOAD_NAME,
        max_length=120,
        description="Name of one derived JSON Payload.",
    )
    format: Literal["json"] = Field(description="Publishes this declared output as JSON.")
    description: str = Field(
        min_length=1,
        max_length=500,
        description="Short purpose of this JSON Payload.",
    )
    json_kind: Literal["document", "records"] = Field(
        description="One logical JSON document or a top-level homogeneous records array."
    )
    schema_json: str = Field(
        min_length=1,
        max_length=_MAX_JSON_SCHEMA_BYTES,
        description=(
            "Complete JSON Schema Draft 2020-12 text for this Payload, limited to 65,536 "
            "UTF-8 bytes; the Host parses and validates it before publication."
        ),
    )

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _reject_blank_or_nul(value, "description")

    @field_validator("schema_json")
    @classmethod
    def validate_schema_json(cls, value: str) -> str:
        _reject_blank_or_nul(value, "schema_json")
        return _bounded_utf8(value, _MAX_JSON_SCHEMA_BYTES, "schema_json")


TransformOutputDeclaration: TypeAlias = Annotated[
    ParquetOutputDeclaration | JsonOutputDeclaration,
    Field(discriminator="format"),
]


class OutputPackageDeclaration(DataTransformModel):
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Stable business name for the immutable derived Package.",
    )
    description_markdown: str = Field(
        min_length=1,
        max_length=_MAX_MARKDOWN_BYTES,
        description=(
            "Package description Markdown, at most 65,536 UTF-8 bytes (about 65,536 "
            "ASCII characters or 21,845 common Chinese characters; encoded bytes govern). "
            "It must contain exactly one H1, followed by these non-empty H2 sections in "
            "this exact order: Summary, Intended use, Contents, Method, Scope and "
            "completeness, Known limitations. Summary must be the first H2."
        ),
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _reject_blank_or_nul(value, "output package name")

    @field_validator("description_markdown")
    @classmethod
    def validate_markdown(cls, value: str) -> str:
        _reject_blank_or_nul(value, "description_markdown")
        return _bounded_utf8(value, _MAX_MARKDOWN_BYTES, "description_markdown")


class DataTransformCommand(DataTransformModel):
    inputs: tuple[TransformInputBinding, ...] = Field(
        min_length=0,
        max_length=16,
        description="Exact read-only Package Payload bindings used as SQL base relations.",
    )
    ifc_source: Literal["current"] | None = Field(
        default=None,
        description="Explicitly read one fixed CURRENT IFC snapshot in Python execution.",
    )
    parameters: tuple[TransformParameter, ...] = Field(
        default=(),
        max_length=32,
        description="Small typed runtime values bound as DuckDB named parameters.",
    )
    inline_tables: tuple[InlineTable, ...] = Field(
        default=(),
        max_length=8,
        description="Zero to eight small request-scoped typed input relations.",
    )
    execution: Annotated[
        DuckDbSqlExecution | PythonArrowExecution,
        Field(discriminator="engine"),
    ] = Field(description="Closed DuckDB SQL or Python/Arrow execution branch.")
    outputs: tuple[TransformOutputDeclaration, ...] = Field(
        min_length=1,
        max_length=16,
        description="One to 16 declared Parquet or JSON outputs.",
    )
    output_package: OutputPackageDeclaration = Field(
        description="Name and required human-readable description of the derived Package."
    )

    @model_validator(mode="after")
    def validate_unique_bindings_and_parameters(self) -> "DataTransformCommand":
        if self.ifc_source is not None and not isinstance(self.execution, PythonArrowExecution):
            raise ValueError("IFC input requires python_arrow execution")
        if not self.inputs and self.ifc_source is None:
            raise ValueError("at least one Package input or explicit IFC source is required")
        input_names = [item.input_name for item in self.inputs]
        if len(input_names) != len(set(input_names)):
            raise ValueError("input_name must be unique")
        exact_inputs = [(item.package_alias, item.payload_name) for item in self.inputs]
        if len(exact_inputs) != len(set(exact_inputs)):
            raise ValueError("the same exact package payload cannot be bound twice")
        parameter_names = [item.name for item in self.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("parameter name must be unique")
        output_names = [item.payload_name for item in self.outputs]
        if len(output_names) != len(set(output_names)):
            raise ValueError("output payload_name must be unique")
        if set(output_names).intersection(input_names):
            raise ValueError("output payload_name must not collide with input_name")
        if any(name.startswith("__cad2bim_") for name in output_names):
            raise ValueError("output payload_name uses the server-reserved prefix")
        encoded = canonical_json([item.model_dump(mode="json") for item in self.parameters]).encode(
            "utf-8"
        )
        if len(encoded) > _MAX_PARAMETER_BYTES:
            raise ValueError("typed parameters exceed 32 KiB canonical JSON")
        inline_names = [item.input_name for item in self.inline_tables]
        if len(inline_names) != len(set(inline_names)):
            raise ValueError("inline table input_name must be unique")
        if any(name.startswith("__cad2bim_") for name in inline_names):
            raise ValueError("inline table input_name uses the server-reserved prefix")
        if set(inline_names).intersection(input_names):
            raise ValueError("inline table input_name must not collide with Package input_name")
        inline_encoded = canonical_json(
            [item.model_dump(mode="json") for item in self.inline_tables]
        ).encode("utf-8")
        if len(inline_encoded) > _MAX_INLINE_TABLE_BYTES:
            raise ValueError("inline tables exceed 1 MiB canonical JSON")
        if set(output_names).intersection(inline_names):
            raise ValueError("output payload_name must not collide with inline table input_name")
        return self


class TransformSourceEdit(DataTransformModel):
    """One exact literal replacement against the same immutable parent source."""

    old_text: str = Field(
        min_length=1,
        max_length=_MAX_REPAIR_EDIT_BYTES,
        description=(
            "Exact non-empty source literal that must occur once; whitespace-only literals are "
            "valid and matching is case-sensitive."
        ),
    )
    new_text: str = Field(
        default="",
        max_length=_MAX_REPAIR_EDIT_BYTES,
        description="Exact replacement text; an empty value deletes the matched literal.",
    )

    @field_validator("old_text", "new_text")
    @classmethod
    def validate_text(cls, value: str, info) -> str:
        if "\x00" in value:
            raise ValueError(f"{info.field_name} must not contain NUL")
        return _bounded_utf8(value, _MAX_REPAIR_EDIT_BYTES, info.field_name)


class JsonSchemaSet(DataTransformModel):
    """The full Data JSON Schema dialect, including boolean schemas."""

    action: Literal["set"]
    value: dict[str, object] | StrictBool


JsonSchemaChange: TypeAlias = Annotated[JsonSchemaSet | TextEdit, Field(discriminator="action")]
_SCHEMA_CHANGE = TypeAdapter(JsonSchemaChange)


def parse_data_schema_change(value: str) -> JsonSchemaChange:
    return _SCHEMA_CHANGE.validate_json(value)


class JsonOutputInput(JsonOutputDeclaration):
    """Pending output input; schema_json carries action JSON, never persisted."""

    schema_json: str = Field(
        min_length=1,
        # JSON escaping can expand a legal original field by up to six times.
        max_length=6 * _MAX_JSON_SCHEMA_BYTES + 128,
        description=(
            'JSON action string: {"action":"set","value":<object or boolean JSON Schema>} '
            'or {"action":"edit","edits":[{"old_text":"...","new_text":"..."}]}. '
            "Edit uses the same payload_name's saved Schema; a new output requires set. "
            "The resolved Schema remains limited to 65,536 UTF-8 bytes."
        ),
    )

    @field_validator("schema_json")
    @classmethod
    def validate_schema_json(cls, value: str) -> str:
        change = parse_data_schema_change(value)
        if isinstance(change, JsonSchemaSet):
            _bounded_utf8(canonical_json(change.value), _MAX_JSON_SCHEMA_BYTES, "schema_json")
        return value


TransformOutputInput: TypeAlias = Annotated[
    ParquetOutputDeclaration | JsonOutputInput, Field(discriminator="format")
]


class TransformOutputsSet(DataTransformModel):
    action: Literal["set"]
    value: tuple[TransformOutputInput, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_keys(self) -> "TransformOutputsSet":
        names = [item.payload_name for item in self.value]
        if len(names) != len(set(names)):
            raise ValueError("output payload_name values must be unique")
        for item in self.value:
            if isinstance(item, JsonOutputInput) and not isinstance(parse_data_schema_change(item.schema_json), JsonSchemaSet):
                raise ValueError(f"outputs.{item.payload_name}.schema_json requires set when outputs action=set")
        return self


class TransformOutputsEdit(DataTransformModel):
    action: Literal["edit"]
    set: tuple[TransformOutputInput, ...] = Field(default=(), max_length=16)
    remove: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_keys(self) -> "TransformOutputsEdit":
        names = [item.payload_name for item in self.set]
        if not names and not self.remove:
            raise ValueError("outputs edit requires set or remove")
        if len(names) != len(set(names)) or len(self.remove) != len(set(self.remove)):
            raise ValueError("outputs edit payload_name keys must be unique")
        if set(names).intersection(self.remove):
            raise ValueError("outputs edit cannot set and remove the same payload_name")
        for name in self.remove:
            if not re.fullmatch(_PAYLOAD_NAME, name):
                raise ValueError("outputs remove requires a valid payload_name")
        return self


TransformOutputsChange: TypeAlias = Annotated[
    TransformOutputsSet | TransformOutputsEdit, Field(discriminator="action")
]


class OutputPackageInput(DataTransformModel):
    name: str = Field(min_length=1, max_length=255)
    description_markdown: TextSet = Field(description=OutputPackageDeclaration.model_fields["description_markdown"].description)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _reject_blank_or_nul(value, "output package name")


class OutputPackageUpdate(DataTransformModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description_markdown: TextChange | None = Field(default=None, description=OutputPackageDeclaration.model_fields["description_markdown"].description)

    @model_validator(mode="after")
    def validate_update(self) -> "OutputPackageUpdate":
        if self.name is None and self.description_markdown is None:
            raise ValueError("output_package requires name or description_markdown")
        if self.name is not None:
            _reject_blank_or_nul(self.name, "output package name")
        return self


def resolve_data_transform_create(payload: dict[str, object]) -> DataTransformCommand:
    """Project action inputs to the existing complete executable contract."""

    outputs = TransformOutputsSet.model_validate(payload["outputs"])
    resolved_outputs = []
    for item in outputs.value:
        resolved = item.model_dump(mode="python")
        if isinstance(item, JsonOutputInput):
            change = parse_data_schema_change(item.schema_json)
            if not isinstance(change, JsonSchemaSet):
                raise ValueError(f"outputs.{item.payload_name}.schema_json requires set for create")
            resolved["schema_json"] = canonical_json(change.value)
        resolved_outputs.append(resolved)
    package = OutputPackageInput.model_validate(payload["output_package"])
    return DataTransformCommand.model_validate({
        **payload,
        "outputs": resolved_outputs,
        "output_package": {"name": package.name, "description_markdown": package.description_markdown.value},
    })


class DataRepairTransformCommand(DataTransformModel):
    """Changes to one exact terminal transform, resolved before execution."""

    parent_operation_id: str = Field(min_length=1, max_length=80)
    edits: tuple[SourceEdit, ...] | None = Field(default=None, min_length=1, max_length=16)
    outputs: TransformOutputsChange | None = None
    output_package: OutputPackageUpdate | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "DataRepairTransformCommand":
        if self.edits is None and self.outputs is None and self.output_package is None:
            raise ValueError("repair requires edits, outputs or output_package")
        if self.edits is not None:
            TextEdit(action="edit", edits=self.edits)
        return self


class DataTransformTextInputRead(DataTransformModel):
    """One original saved input projected through the shared bounded text view."""

    mode: Literal["transform_input"] = "transform_input"
    operation_alias: str = Field(pattern=_OPERATION_ALIAS, max_length=32)
    input_kind: Literal["package_description", "output_schema"]
    payload_name: str | None = Field(default=None, pattern=_PAYLOAD_NAME, max_length=120)
    source: TextSourceViewResult


class TransformInputMetadata(DataTransformModel):
    metadata_kind: Literal["input"] = "input"
    input_name: str
    package_alias: str
    payload_name: str
    logical_key_fields: tuple[str, ...]


class TransformOutputMetadata(DataTransformModel):
    metadata_kind: Literal["output"] = "output"
    payload_name: str
    format: Literal["parquet", "json"]
    description: str
    logical_key_fields: tuple[str, ...] = ()
    json_kind: Literal["document", "records"] | None = None


class TransformParameterMetadata(DataTransformModel):
    metadata_kind: Literal["parameter"] = "parameter"
    name: str
    parameter_kind: Literal["string", "integer", "number", "boolean", "string_list", "integer_list", "number_list", "boolean_list"]
    value_count: int = Field(ge=0)


class TransformInlineTableMetadata(DataTransformModel):
    metadata_kind: Literal["inline_table"] = "inline_table"
    input_name: str
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)


TransformMetadataSection: TypeAlias = Literal["inputs", "outputs", "parameters", "inline_tables"]
TransformMetadataItem: TypeAlias = Annotated[
    TransformInputMetadata | TransformOutputMetadata | TransformParameterMetadata | TransformInlineTableMetadata,
    Field(discriminator="metadata_kind"),
]


class TransformMetadataPage(DataTransformModel):
    section: TransformMetadataSection
    total_count: int = Field(ge=0)
    start_index: int = Field(ge=0)
    items: tuple[TransformMetadataItem, ...]
    returned_count: int = Field(ge=0)
    complete: bool
    next_index: int | None = Field(default=None, ge=0)


class _TransformInputSelection(DataTransformModel):
    mode: Literal["transform_input"] = "transform_input"
    operation_alias: str = Field(pattern=_OPERATION_ALIAS, max_length=32)


class TransformMetadataSelection(_TransformInputSelection):
    input_kind: Literal["metadata"]
    section: TransformMetadataSection
    start_index: int = Field(default=0, ge=0)
    item_count: int = Field(default=100, ge=1, le=100)


class TransformInlineSchemaSelection(_TransformInputSelection):
    input_kind: Literal["inline_schema"]
    input_name: str = Field(pattern=_PAYLOAD_NAME, max_length=120)
    start_column: int = Field(default=0, ge=0)
    column_count: int = Field(default=32, ge=1, le=32)


class TransformInlineRowsSelection(_TransformInputSelection):
    input_kind: Literal["inline_rows"]
    input_name: str = Field(pattern=_PAYLOAD_NAME, max_length=120)
    start_row: int = Field(default=0, ge=0)
    row_count: int = Field(default=100, ge=1, le=200)
    select: tuple[str, ...] = Field(default=(), max_length=32)


class TransformParameterSelection(_TransformInputSelection):
    input_kind: Literal["parameter"]
    parameter_name: str = Field(pattern=_SAFE_NAME, max_length=63)


class TransformInlineTextSelection(_TransformInputSelection):
    input_kind: Literal["inline_text"]
    input_name: str = Field(pattern=_PAYLOAD_NAME, max_length=120)
    row_index: int = Field(ge=0)
    column_name: str = Field(min_length=1, max_length=255)
    start_character: int = Field(default=0, ge=0)
    character_count: int = Field(default=4096, ge=1, le=16_384)


TransformSavedInputSelection: TypeAlias = Annotated[
    TransformMetadataSelection | TransformInlineSchemaSelection | TransformInlineRowsSelection
    | TransformParameterSelection | TransformInlineTextSelection,
    Field(discriminator="input_kind"),
]


class DataTransformMetadataRead(_TransformInputSelection):
    input_kind: Literal["metadata"] = "metadata"
    metadata: TransformMetadataPage


class DataTransformInlineSchemaRead(_TransformInputSelection):
    input_kind: Literal["inline_schema"] = "inline_schema"
    input_name: str
    total_columns: int = Field(ge=0)
    start_column: int = Field(ge=0)
    columns: tuple[InlineColumn, ...]
    complete: bool
    next_column: int | None = Field(default=None, ge=0)


class DataTransformInlineRowsRead(_TransformInputSelection):
    input_kind: Literal["inline_rows"] = "inline_rows"
    input_name: str
    total_rows: int = Field(ge=0)
    start_row: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    columns: tuple[InlineColumn, ...]
    rows: tuple[InlineRow, ...]
    complete: bool
    next_row: int | None = Field(default=None, ge=0)


class DataTransformParameterRead(_TransformInputSelection):
    input_kind: Literal["parameter"] = "parameter"
    parameter: TransformParameter


class DataTransformInlineTextRead(_TransformInputSelection):
    input_kind: Literal["inline_text"] = "inline_text"
    input_name: str
    row_index: int = Field(ge=0)
    column_name: str
    total_characters: int = Field(ge=0)
    start_character: int = Field(ge=0)
    returned_characters: int = Field(ge=0)
    text: str
    complete: bool
    next_character: int | None = Field(default=None, ge=0)


DataTransformInputRead: TypeAlias = (
    DataTransformTextInputRead | DataTransformMetadataRead | DataTransformInlineSchemaRead
    | DataTransformInlineRowsRead | DataTransformParameterRead | DataTransformInlineTextRead
)


class PackageCodeReference(DataTransformModel):
    reference_kind: Literal["package"] = Field(description="Selects lookup by derived Package.")
    package_alias: str = Field(
        pattern=_PACKAGE_ALIAS,
        max_length=32,
        description="Visible derived Package alias whose producing SQL should be read.",
    )


class OperationCodeReference(DataTransformModel):
    reference_kind: Literal["operation"] = Field(description="Selects lookup by Operation.")
    operation_alias: str = Field(
        pattern=_OPERATION_ALIAS,
        max_length=32,
        description="Visible Data Transform Operation alias whose exact SQL should be read.",
    )


TransformationCodeReference: TypeAlias = Annotated[
    PackageCodeReference | OperationCodeReference,
    Field(discriminator="reference_kind"),
]


WorkerParameterValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | tuple[str, ...]
    | tuple[int, ...]
    | tuple[float, ...]
    | tuple[bool, ...]
)

WorkerResultMode: TypeAlias = Literal[
    "legacy_final_result",
    "named_relations",
    "query_final_result",
]


@dataclass(frozen=True, slots=True)
class DataTransformWorkerParameter:
    name: str
    value: WorkerParameterValue

    def __post_init__(self) -> None:
        if re.fullmatch(_SAFE_NAME, self.name) is None:
            raise ValueError("worker parameter name is invalid")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("worker number parameter must be finite")
        if isinstance(self.value, tuple):
            if not self.value or len(self.value) > 100:
                raise ValueError("worker list parameter must contain 1..100 items")
            if any(isinstance(item, float) and not math.isfinite(item) for item in self.value):
                raise ValueError("worker number list parameter must be finite")


@dataclass(frozen=True, slots=True)
class DataTransformWorkerRequest:
    code: str
    parameters: tuple[DataTransformWorkerParameter, ...] = ()
    result_mode: WorkerResultMode = "legacy_final_result"
    output_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _reject_blank_or_nul(self.code, "code")
        _bounded_utf8(self.code, MAX_TRANSFORM_CODE_BYTES, "code")
        names = [item.name for item in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("worker parameter names must be unique")
        if self.result_mode == "named_relations":
            if not 1 <= len(self.output_names) <= 16:
                raise ValueError("named relation mode requires 1..16 outputs")
        elif self.output_names:
            raise ValueError("only named relation mode accepts output_names")
        if len(self.output_names) != len(set(self.output_names)):
            raise ValueError("worker output names must be unique")
        if any(re.fullmatch(_PAYLOAD_NAME, name) is None for name in self.output_names):
            raise ValueError("worker output name is invalid")
        if any(name.startswith("__cad2bim_") for name in self.output_names):
            raise ValueError("worker output name uses the server-reserved prefix")

    @classmethod
    def from_command(cls, command: DataTransformCommand) -> "DataTransformWorkerRequest":
        return cls(
            code=command.execution.code,
            parameters=tuple(
                DataTransformWorkerParameter(name=item.name, value=item.value)
                for item in command.parameters
            ),
            result_mode="named_relations",
            output_names=tuple(item.payload_name for item in command.outputs),
        )


@dataclass(frozen=True, slots=True)
class PythonArrowWorkerOutputDeclaration:
    payload_name: str
    format: Literal["parquet", "json"]
    json_kind: Literal["document", "records"] | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(_PAYLOAD_NAME, self.payload_name) is None:
            raise ValueError("Python worker output name is invalid")
        if self.payload_name.startswith("__cad2bim_"):
            raise ValueError("Python worker output name uses the server-reserved prefix")
        if self.format == "parquet" and self.json_kind is not None:
            raise ValueError("Parquet Python worker output must not define json_kind")
        if self.format == "json" and self.json_kind not in {"document", "records"}:
            raise ValueError("JSON Python worker output requires json_kind")


@dataclass(frozen=True, slots=True)
class PythonArrowWorkerRequest:
    code: str
    parameters: tuple[DataTransformWorkerParameter, ...] = ()
    outputs: tuple[PythonArrowWorkerOutputDeclaration, ...] = ()

    def __post_init__(self) -> None:
        PythonArrowExecution(engine="python_arrow", code=self.code)
        parameter_names = [item.name for item in self.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("Python worker parameter names must be unique")
        if not 1 <= len(self.outputs) <= 16:
            raise ValueError("Python worker requires 1..16 declared outputs")
        output_names = [item.payload_name for item in self.outputs]
        if len(output_names) != len(set(output_names)):
            raise ValueError("Python worker output names must be unique")

    @property
    def output_names(self) -> tuple[str, ...]:
        return tuple(item.payload_name for item in self.outputs)

    @classmethod
    def from_command(cls, command: DataTransformCommand) -> "PythonArrowWorkerRequest":
        if not isinstance(command.execution, PythonArrowExecution):
            raise ValueError("Python worker request requires python_arrow execution")
        return cls(
            code=command.execution.code,
            parameters=tuple(
                DataTransformWorkerParameter(name=item.name, value=item.value)
                for item in command.parameters
            ),
            outputs=tuple(
                PythonArrowWorkerOutputDeclaration(
                    payload_name=item.payload_name,
                    format=item.format,
                    json_kind=item.json_kind if isinstance(item, JsonOutputDeclaration) else None,
                )
                for item in command.outputs
            ),
        )


@dataclass(frozen=True, slots=True)
class PythonArrowWorkerPolicy:
    input_row_limit: int = 4_000_000
    input_stored_parquet_bytes: int = 256 * 1024 * 1024
    input_arrow_bytes: int = 512 * 1024 * 1024
    output_row_limit: int = 2_000_000
    output_arrow_bytes: int = 512 * 1024 * 1024
    output_parquet_bytes: int = 256 * 1024 * 1024
    per_payload_parquet_bytes: int = 100 * 1024 * 1024
    rss_bytes: int = 4 * 1024 * 1024 * 1024
    scratch_bytes: int = 4 * 1024 * 1024 * 1024
    threads: int = 2
    open_file_descriptors: int = 64
    cpu_seconds: int = 180
    wall_seconds: float = 90.0
    total_call_seconds: float = DATA_PYTHON_EXECUTION.total_seconds
    capacity_wait_seconds: float = DATA_PYTHON_EXECUTION.queue_seconds
    execution_seconds: float = DATA_PYTHON_EXECUTION.execution_seconds
    terminal_seconds: float = DATA_PYTHON_EXECUTION.terminal_seconds
    cleanup_seconds: float = DATA_PYTHON_EXECUTION.cleanup_seconds
    max_concurrent_workers: int = 1
    capture_bytes: int = 16 * 1024
    max_output_count: int = 16
    max_output_columns: int = 512

    def __post_init__(self) -> None:
        for item in fields(self):
            name = item.name
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.threads > 2:
            raise ValueError("Data Python Worker threads cannot exceed 2")
        if self.max_concurrent_workers > 1:
            raise ValueError("Data Python Worker capacity cannot exceed 1")
        if self.max_output_count > 16:
            raise ValueError("Data Python Worker output count cannot exceed 16")
        if self.max_output_columns > 512:
            raise ValueError("Data Python Worker output columns cannot exceed 512")


@dataclass(frozen=True, slots=True)
class DataTransformWorkerPolicy:
    input_row_limit: int = 4_000_000
    input_arrow_bytes: int = 512 * 1024 * 1024
    output_row_limit: int = 2_000_000
    output_arrow_bytes: int = 512 * 1024 * 1024
    duckdb_memory_bytes: int = 512 * 1024 * 1024
    rss_bytes: int = 2 * 1024 * 1024 * 1024
    scratch_bytes: int = 2 * 1024 * 1024 * 1024
    threads: int = 2
    open_file_descriptors: int = 64
    cpu_seconds: int = 90
    wall_seconds: float = 45.0
    cleanup_seconds: float = DATA_SQL_EXECUTION.cleanup_seconds
    max_concurrent_workers: int = 2
    capacity_wait_seconds: float = DATA_SQL_EXECUTION.queue_seconds
    execution_seconds: float = DATA_SQL_EXECUTION.execution_seconds
    terminal_seconds: float = DATA_SQL_EXECUTION.terminal_seconds
    capture_bytes: int = 16 * 1024
    output_batch_rows: int = 65_536
    max_output_count: int = 16
    max_output_columns: int = 512

    def __post_init__(self) -> None:
        for item in fields(self):
            name = item.name
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.threads > 2:
            raise ValueError("M2a worker threads cannot exceed 2")
        if self.max_concurrent_workers > 2:
            raise ValueError("M2a worker capacity cannot exceed 2")
        if self.max_output_count > 16:
            raise ValueError("M2b worker output count cannot exceed 16")
        if self.max_output_columns > 512:
            raise ValueError("M2b worker output columns cannot exceed 512")


@dataclass(frozen=True, slots=True)
class DataTransformWorkerReceipt:
    row_count: int
    arrow_nbytes: int
    execution_ms: int


class DataTransformWorkerError(RuntimeError):
    """Stable internal failure carrying the public M2a error projection facts."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal["preflight", "code_validation", "execution", "output_validation"],
        execution_started: bool,
        retryable: bool | None = None,
        diagnostic: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            truncate_transform_error_message(
                message,
                fallback="Data transform worker failed",
            )
        )
        self.code = code
        self.phase = phase
        self.execution_started = execution_started
        self.retryable = retryable
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


__all__ = [
    "MAX_TRANSFORM_CODE_BYTES",
    "BooleanListParameter",
    "BooleanParameter",
    "DataRepairTransformCommand",
    "DataTransformCommand",
    "DataTransformWorkerError",
    "DataTransformWorkerParameter",
    "DataTransformWorkerPolicy",
    "DataTransformWorkerReceipt",
    "DataTransformWorkerRequest",
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
    "IntegerListParameter",
    "IntegerParameter",
    "NumberListParameter",
    "NumberParameter",
    "OperationCodeReference",
    "OutputPackageDeclaration",
    "PackageCodeReference",
    "ParquetOutputDeclaration",
    "JsonOutputDeclaration",
    "PythonArrowExecution",
    "PythonArrowWorkerOutputDeclaration",
    "PythonArrowWorkerPolicy",
    "PythonArrowWorkerRequest",
    "StringListParameter",
    "StringParameter",
    "TransformInputBinding",
    "TransformParameter",
    "TransformSourceEdit",
    "TransformOutputDeclaration",
    "TransformationCodeReference",
    "WorkerResultMode",
    "truncate_transform_error_message",
]
