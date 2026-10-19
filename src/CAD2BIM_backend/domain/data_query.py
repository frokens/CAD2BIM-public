"""Provider-neutral contracts for bounded Data and CAD L0 queries.

Direct-session authority belongs to the entry adapter.  These models start at
the shared application boundary, so they deliberately contain no tenant,
authentication, run, or ``direct_session_alias`` fields.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import TypeAliasType

from CAD2BIM_backend.domain.data_runtime import ArrowTypeString


_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_CURSOR_PATTERN = r"^cur\..+$"
_DECIMAL_PATTERN = r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_TYPED_KINDS = frozenset({"null", "string", "integer", "number", "binary"})
_CANONICAL_BASE64_PATTERN = (
    r"^(?:[A-Za-z0-9+/]{4})*"
    r"(?:[A-Za-z0-9+/][AQgw]==|[A-Za-z0-9+/]{2}[AEIMQUYcgkosw048]=)?$"
)
StrictInt32: TypeAlias = Annotated[
    int,
    Field(strict=True, ge=_INT32_MIN, le=_INT32_MAX),
]
StrictInt64: TypeAlias = Annotated[
    int,
    Field(strict=True, ge=_INT64_MIN, le=_INT64_MAX),
]
StrictPageSize: TypeAlias = Annotated[int, Field(strict=True, ge=1, le=200)]
StrictBool: TypeAlias = Annotated[bool, Field(strict=True)]
StrictNumber: TypeAlias = Annotated[float, Field(strict=True)]
NonEmptyString: TypeAlias = Annotated[str, Field(min_length=1)]


class DataQueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _unique_strings(value: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if any(not item for item in value):
        raise ValueError("array items must be non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError("array items must be unique")
    return value


def _unique_integers(value: tuple[int, ...] | None) -> tuple[int, ...] | None:
    if value is not None and len(set(value)) != len(value):
        raise ValueError("array items must be unique")
    return value


def _typed_value_identity(value: TypedValue) -> str:
    return value.model_dump_json(exclude_none=False)


def _canonical_base64(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ValueError("base64 must be a canonical base64 string") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("base64 must be a canonical base64 string")
    return value


class NullTypedValue(DataQueryModel):
    kind: Literal["null"]


class StringTypedValue(DataQueryModel):
    kind: Literal["string"]
    value: str


class IntegerTypedValue(DataQueryModel):
    kind: Literal["integer"]
    value: int = Field(strict=True, ge=_INT64_MIN, le=_INT64_MAX)


class NumberTypedValue(DataQueryModel):
    kind: Literal["number"]
    value: float = Field(strict=True)


class BinaryTypedValue(DataQueryModel):
    kind: Literal["binary"]
    # Keep the public pattern within the Provider regex subset. Exact canonical
    # Base64, including newline rejection, remains enforced by the validator.
    base64: str = Field(json_schema_extra={"pattern": _CANONICAL_BASE64_PATTERN})

    @field_validator("base64")
    @classmethod
    def validate_base64(cls, value: str) -> str:
        return _canonical_base64(value)


TypedValue: TypeAlias = Annotated[
    NullTypedValue | StringTypedValue | IntegerTypedValue | NumberTypedValue | BinaryTypedValue,
    Field(discriminator="kind"),
]


class BinaryCellValue(DataQueryModel):
    kind: Literal["binary_base64"]
    base64: str = Field(json_schema_extra={"pattern": _CANONICAL_BASE64_PATTERN})

    @field_validator("base64")
    @classmethod
    def validate_base64(cls, value: str) -> str:
        return _canonical_base64(value)


GenericCellValue: TypeAlias = None | StrictBool | StrictInt64 | StrictNumber | str | BinaryCellValue
GenericSelectedCellValue = TypeAliasType(
    "GenericSelectedCellValue",
    GenericCellValue | list["GenericSelectedCellValue"] | dict[str, "GenericSelectedCellValue"],
)
FilterScalar: TypeAlias = StrictBool | StrictInt64 | StrictNumber | str | BinaryCellValue


def _filter_scalar_json_kind(value: FilterScalar) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) or isinstance(value, float) and value.is_integer():
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "binary"


class PageInfo(DataQueryModel):
    total_matched_count: int = Field(ge=0)
    returned_count: int = Field(ge=0, le=200)
    remaining_count: int = Field(ge=0)
    complete: bool
    next_cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def validate_page_state(self) -> PageInfo:
        if self.total_matched_count < self.returned_count + self.remaining_count:
            raise ValueError("page counts exceed total_matched_count")
        if self.complete != (self.remaining_count == 0 and self.next_cursor is None):
            raise ValueError("complete requires zero remaining rows and no cursor")
        if not self.complete and (self.remaining_count == 0 or self.next_cursor is None):
            raise ValueError("incomplete page requires remaining rows and next_cursor")
        return self


class FilterOperator(StrEnum):
    EQ = "eq"
    IN = "in"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class FilterCondition(DataQueryModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "op": {"enum": ["is_null", "is_not_null"]},
                        },
                        "required": ["op"],
                    },
                    "then": {"not": {"required": ["value"]}},
                },
                {
                    "if": {
                        "properties": {
                            "op": {"enum": ["eq", "lt", "lte", "gt", "gte"]},
                        },
                        "required": ["op"],
                    },
                    "then": {
                        "required": ["value"],
                        "properties": {"value": {"not": {"type": ["array", "null"]}}},
                    },
                },
                {
                    "if": {
                        "properties": {"op": {"const": "in"}},
                        "required": ["op"],
                    },
                    "then": {
                        "required": ["value"],
                        "properties": {
                            "value": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 100,
                                "anyOf": [
                                    {"items": {"type": "boolean"}},
                                    {"items": {"type": "integer"}},
                                    {
                                        "items": {
                                            "type": "number",
                                            "not": {"type": "integer"},
                                        }
                                    },
                                    {"items": {"type": "string"}},
                                    {"items": {"type": "object"}},
                                ],
                            }
                        },
                    },
                },
            ]
        },
    )

    field: str = Field(min_length=1, max_length=255)
    op: FilterOperator
    value: FilterScalar | tuple[FilterScalar, ...] | None = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> FilterCondition:
        value_was_supplied = "value" in self.model_fields_set
        if self.op in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            if value_was_supplied:
                raise ValueError(f"{self.op.value} must omit value")
            return self
        if not value_was_supplied or self.value is None:
            raise ValueError(f"{self.op.value} requires value")
        if self.op is FilterOperator.IN:
            if not isinstance(self.value, tuple) or not 1 <= len(self.value) <= 100:
                raise ValueError("in requires an array of 1..100 values")
            kinds = {_filter_scalar_json_kind(item) for item in self.value}
            if len(kinds) != 1:
                raise ValueError("in values must have one JSON type")
        elif isinstance(self.value, tuple):
            raise ValueError(f"{self.op.value} requires one scalar value")
        return self


class PackageSummary(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    package_kind: Literal["source_l0", "derived"]
    source_filename: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_source_filename_scope(self) -> PackageSummary:
        if self.package_kind == "source_l0" and self.source_filename is None:
            raise ValueError("source_l0 package summary requires source_filename")
        if self.package_kind == "derived" and self.source_filename is not None:
            raise ValueError("derived package summary cannot contain source_filename")
        return self


class ParquetPayloadSummary(DataQueryModel):
    payload_name: str = Field(min_length=1, max_length=120)
    format: Literal["parquet"] = "parquet"
    row_count: int = Field(ge=0)
    logical_key_fields: tuple[str, ...] = Field(min_length=1)

    _validate_keys = field_validator("logical_key_fields")(_unique_strings)


class JsonPayloadSummary(DataQueryModel):
    payload_name: str = Field(min_length=1, max_length=120)
    format: Literal["json"] = "json"
    size_bytes: int = Field(ge=1)
    json_kind: Literal["document", "records"]
    schema_available: Literal[True] = True


PayloadSummary: TypeAlias = Annotated[
    ParquetPayloadSummary | JsonPayloadSummary,
    Field(discriminator="format"),
]


class ColumnSchema(DataQueryModel):
    name: str = Field(min_length=1, max_length=255)
    type: ArrowTypeString
    nullable: bool


class ParquetPayloadSchema(DataQueryModel):
    payload_name: str = Field(min_length=1, max_length=120)
    logical_key_fields: tuple[str, ...] = Field(min_length=1)
    columns: tuple[ColumnSchema, ...] = Field(min_length=1)

    _validate_keys = field_validator("logical_key_fields")(_unique_strings)

    @field_validator("columns")
    @classmethod
    def validate_unique_columns(cls, value: tuple[ColumnSchema, ...]) -> tuple[ColumnSchema, ...]:
        names = [column.name for column in value]
        if len(set(names)) != len(names):
            raise ValueError("column names must be unique")
        return value


class JsonPayloadSchema(DataQueryModel):
    payload_name: str = Field(min_length=1, max_length=120)
    format: Literal["json"] = "json"
    json_kind: Literal["document", "records"]
    schema_json: str = Field(min_length=1)

    @field_validator("schema_json")
    @classmethod
    def validate_schema_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 65_536:
            raise ValueError("schema_json must be at most 65536 UTF-8 bytes")
        return value


PayloadSchema: TypeAlias = ParquetPayloadSchema | JsonPayloadSchema


class DataDescribePackageQuery(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    payload_name: str | None = Field(default=None, min_length=1, max_length=120)


class DataDescribePackageResult(DataQueryModel):
    package: PackageSummary
    producing_operation_alias: str | None = Field(default=None, pattern=r"^op_[0-9]{3,}$", max_length=32)
    payloads: tuple[PayloadSummary, ...]
    payload_schema: PayloadSchema | None = None

    @model_validator(mode="after")
    def validate_payloads(self) -> DataDescribePackageResult:
        names = [payload.payload_name for payload in self.payloads]
        if len(set(names)) != len(names):
            raise ValueError("payload summaries must be unique")
        if self.payload_schema is not None and self.payload_schema.payload_name not in names:
            raise ValueError("payload_schema must describe a listed payload")
        if self.payload_schema is not None:
            summary = next(
                payload
                for payload in self.payloads
                if payload.payload_name == self.payload_schema.payload_name
            )
            if isinstance(summary, JsonPayloadSummary) != isinstance(
                self.payload_schema, JsonPayloadSchema
            ):
                raise ValueError("payload_schema format must match its payload summary")
        return self


class GenericQueryRow(DataQueryModel):
    key_values: dict[str, GenericCellValue] = Field(min_length=1)
    values: dict[str, GenericSelectedCellValue]

    @field_validator("key_values", "values")
    @classmethod
    def validate_field_names(
        cls, value: dict[str, GenericSelectedCellValue]
    ) -> dict[str, GenericSelectedCellValue]:
        if any(not name for name in value):
            raise ValueError("row field names must be non-empty")
        return value


class DataRowsQuery(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    payload_name: str = Field(min_length=1, max_length=120)
    select: tuple[NonEmptyString, ...] = Field(
        max_length=64,
        json_schema_extra={"uniqueItems": True},
    )
    filters: tuple[FilterCondition, ...] = Field(default=(), max_length=32)
    page_size: StrictPageSize = 100

    _validate_select = field_validator("select")(_unique_strings)


class DataQueryRowsRequest(DataQueryModel):
    query: DataRowsQuery | None = None
    cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def require_query_xor_cursor(self) -> DataQueryRowsRequest:
        if (self.query is None) == (self.cursor is None):
            raise ValueError("exactly one of query or cursor is required")
        return self


class DataQueryRowsResult(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    payload_name: str = Field(min_length=1, max_length=120)
    logical_key_fields: tuple[str, ...] = Field(min_length=1)
    rows: tuple[GenericQueryRow, ...] = Field(max_length=200)
    page: PageInfo

    _validate_keys = field_validator("logical_key_fields")(_unique_strings)


class EntityScope(StrEnum):
    TOP_LEVEL_MODELSPACE = "top_level_modelspace"
    ALL_ENTITIES = "all_entities"


class EntityFilters(DataQueryModel):
    layers: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description="CAD layer names discovered by count_by=layer; values inside the array are OR.",
    )
    entity_types: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description=(
            "DXF entity types discovered by count_by=entity_type; values inside the array are OR."
        ),
    )
    entity_keys: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=200,
        json_schema_extra={"uniqueItems": True},
        description="Stable package-internal entity keys returned by rows mode.",
    )
    handles: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description="DXF handles previously observed in entity rows.",
    )
    typed_geometry_supported: StrictBool | None = Field(
        default=None,
        description="Optional exact match on whether Phase-1 emitted typed geometry.",
    )

    _validate_arrays = field_validator("layers", "entity_types", "entity_keys", "handles")(
        _unique_strings
    )


class EntitySummary(DataQueryModel):
    entity_key: str = Field(min_length=1)
    record_key: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    layer: str | None
    handle: str | None
    entity_scope: str = Field(min_length=1)
    typed_geometry_supported: bool
    parse_disposition: str = Field(min_length=1)


class EntityDetail(DataQueryModel):
    document_key: str = Field(min_length=1)
    record_key: str = Field(min_length=1)
    entity_key: str = Field(min_length=1)
    entity_scope: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    section: str = Field(min_length=1)
    handle: str | None
    owner_handle: str | None
    layer: str | None
    layout: str | None
    linetype: str | None
    color_index: int | None = Field(default=None, ge=_INT32_MIN, le=_INT32_MAX)
    true_color: int | None = Field(default=None, ge=_INT64_MIN, le=_INT64_MAX)
    lineweight: int | None = Field(default=None, ge=_INT32_MIN, le=_INT32_MAX)
    transparency: int | None = Field(default=None, ge=_INT64_MIN, le=_INT64_MAX)
    visible: bool
    thickness: float | None
    extrusion_x: float | None
    extrusion_y: float | None
    extrusion_z: float | None
    typed_geometry_supported: bool
    unsupported_reason: str | None
    parse_disposition: str = Field(min_length=1)


class EntityQueryMode(StrEnum):
    ROWS = "rows"
    COUNT_BY = "count_by"


class EntityCountBy(StrEnum):
    LAYER = "layer"
    ENTITY_TYPE = "entity_type"
    LAYER_AND_ENTITY_TYPE = "layer_and_entity_type"


class CadEntitiesQuery(DataQueryModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"mode": {"const": "rows"}},
                        "required": ["mode"],
                    },
                    "then": {
                        "properties": {
                            "count_by": {"type": "null"},
                            "include_declared_empty_layers": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"mode": {"const": "count_by"}},
                        "required": ["mode"],
                    },
                    "then": {
                        "required": ["count_by"],
                        "properties": {"count_by": {"type": "string"}},
                    },
                },
                {
                    "if": {
                        "properties": {"count_by": {"const": "layer"}},
                        "required": ["count_by"],
                    },
                    "then": {"properties": {"include_declared_empty_layers": {"type": "boolean"}}},
                },
                {
                    "if": {
                        "properties": {
                            "count_by": {"enum": ["entity_type", "layer_and_entity_type"]}
                        },
                        "required": ["count_by"],
                    },
                    "then": {"properties": {"include_declared_empty_layers": {"type": "null"}}},
                },
            ]
        },
    )

    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    mode: EntityQueryMode
    entity_scope: EntityScope = EntityScope.TOP_LEVEL_MODELSPACE
    filters: EntityFilters = Field(default_factory=EntityFilters)
    count_by: EntityCountBy | None = None
    include_declared_empty_layers: StrictBool | None = None
    page_size: StrictPageSize = 100

    @model_validator(mode="before")
    @classmethod
    def apply_layer_default(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("mode") == EntityQueryMode.COUNT_BY.value:
            if value.get("count_by") == EntityCountBy.LAYER.value:
                value = dict(value)
                value.setdefault("include_declared_empty_layers", True)
        return value

    @model_validator(mode="after")
    def validate_mode_fields(self) -> CadEntitiesQuery:
        if self.mode is EntityQueryMode.ROWS:
            if self.count_by is not None or self.include_declared_empty_layers is not None:
                raise ValueError("rows mode does not accept count fields")
        elif self.count_by is None:
            raise ValueError("count_by mode requires count_by")
        elif self.count_by is EntityCountBy.LAYER:
            if self.include_declared_empty_layers is None:
                raise ValueError("layer count requires include_declared_empty_layers")
        elif self.include_declared_empty_layers is not None:
            raise ValueError("include_declared_empty_layers is only valid for layer count")
        return self


class CadQueryEntitiesRequest(DataQueryModel):
    query: CadEntitiesQuery | None = None
    cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def require_query_xor_cursor(self) -> CadQueryEntitiesRequest:
        if (self.query is None) == (self.cursor is None):
            raise ValueError("exactly one of query or cursor is required")
        return self


class EntityCountScope(DataQueryModel):
    entity_scope: EntityScope
    matched_entity_count: int = Field(ge=0)
    declared_layer_count: int | None = Field(default=None, ge=0)
    used_layer_count: int | None = Field(default=None, ge=0)
    undeclared_used_layer_count: int | None = Field(default=None, ge=0)


class LayerCountBucket(DataQueryModel):
    layer: str
    declared_in_layer_table: bool
    declaration_count: int = Field(ge=0)
    entity_count: int = Field(ge=0)


class EntityTypeCountBucket(DataQueryModel):
    entity_type: str = Field(min_length=1)
    entity_count: int = Field(ge=0)


class LayerEntityTypeCountBucket(DataQueryModel):
    layer: str
    entity_type: str = Field(min_length=1)
    declared_in_layer_table: bool
    entity_count: int = Field(ge=0)


EntityCountBucket: TypeAlias = LayerCountBucket | EntityTypeCountBucket | LayerEntityTypeCountBucket


class CadEntityRowsResult(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    mode: Literal["rows"] = "rows"
    rows: tuple[EntitySummary, ...] = Field(max_length=200)
    page: PageInfo


class CadEntityCountResult(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    mode: Literal["count_by"] = "count_by"
    count_by: EntityCountBy
    scope: EntityCountScope
    counts: tuple[EntityCountBucket, ...] = Field(max_length=200)
    page: PageInfo

    @model_validator(mode="after")
    def validate_bucket_shapes(self) -> CadEntityCountResult:
        expected = {
            EntityCountBy.LAYER: LayerCountBucket,
            EntityCountBy.ENTITY_TYPE: EntityTypeCountBucket,
            EntityCountBy.LAYER_AND_ENTITY_TYPE: LayerEntityTypeCountBucket,
        }[self.count_by]
        if any(type(bucket) is not expected for bucket in self.counts):
            raise ValueError("count bucket does not match count_by")
        layer_counts = self.count_by in {
            EntityCountBy.LAYER,
            EntityCountBy.LAYER_AND_ENTITY_TYPE,
        }
        layer_scope_values = (
            self.scope.declared_layer_count,
            self.scope.used_layer_count,
            self.scope.undeclared_used_layer_count,
        )
        if layer_counts != all(value is not None for value in layer_scope_values):
            raise ValueError("layer scope counts must exist exactly for layer dimensions")
        return self


CadQueryEntitiesResult: TypeAlias = Annotated[
    CadEntityRowsResult | CadEntityCountResult, Field(discriminator="mode")
]


class EntitySection(StrEnum):
    GEOMETRY_ELEMENTS = "geometry_elements"
    XDATA_TAGS = "xdata_tags"
    RELATIONSHIPS = "relationships"
    RAW_TAGS = "raw_tags"


class CadEntityQuery(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    entity_key: str = Field(min_length=1, max_length=255)
    section: EntitySection
    page_size: StrictPageSize = 100


class CadGetEntityRequest(DataQueryModel):
    query: CadEntityQuery | None = None
    cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def require_query_xor_cursor(self) -> CadGetEntityRequest:
        if (self.query is None) == (self.cursor is None):
            raise ValueError("exactly one of query or cursor is required")
        return self


class GeometryElementRow(DataQueryModel):
    document_key: str = Field(min_length=1)
    geometry_element_key: str = Field(min_length=1)
    entity_key: str = Field(min_length=1)
    geometry_component_kind: str = Field(min_length=1)
    component_path: str = Field(min_length=1)
    ordinal: int = Field(ge=_INT32_MIN, le=_INT32_MAX)
    x: float | None
    y: float | None
    z: float | None
    bulge: float | None
    start_width: float | None
    end_width: float | None
    constant_width: float | None
    elevation: float | None
    angle_degrees: float | None
    closed: bool | None
    flags: int | None = Field(default=None, ge=_INT32_MIN, le=_INT32_MAX)


class XdataQueryRow(DataQueryModel):
    xdata_tag_key: str = Field(min_length=1)
    entity_key: str = Field(min_length=1)
    layer: str | None
    entity_type: str = Field(min_length=1)
    appid: str = Field(min_length=1)
    appid_group_ordinal: int = Field(ge=_INT32_MIN, le=_INT32_MAX)
    tag_ordinal: int = Field(ge=_INT32_MIN, le=_INT32_MAX)
    is_appid_marker: bool
    group_code: int = Field(ge=_INT32_MIN, le=_INT32_MAX)
    raw_lexeme: str | None
    typed_value: TypedValue
    assignment_key: str | None
    assignment_value: TypedValue | None
    source_raw_tag_key: str = Field(min_length=1)


class RelationshipRow(DataQueryModel):
    document_key: str = Field(min_length=1)
    relationship_key: str = Field(min_length=1)
    relation_kind: str = Field(min_length=1)
    subject_key: str = Field(min_length=1)
    target_handle_or_raw_target: str | None
    resolved_target_key: str | None
    resolution_status: str = Field(min_length=1)


class RawTagRow(DataQueryModel):
    document_key: str = Field(min_length=1)
    record_key: str = Field(min_length=1)
    raw_tag_key: str = Field(min_length=1)
    global_tag_ordinal: int = Field(ge=0, le=_INT64_MAX)
    record_tag_ordinal: int = Field(ge=0, le=_INT32_MAX)
    section: str = Field(min_length=1)
    group_code: int = Field(ge=_INT32_MIN, le=_INT32_MAX)
    raw_code_lexeme: str
    raw_value_bytes: BinaryCellValue
    value_kind: str = Field(min_length=1)
    string_value: str | None
    int_value: int | None = Field(default=None, ge=_INT64_MIN, le=_INT64_MAX)
    float_value: float | None
    binary_value: BinaryCellValue | None
    byte_start: int = Field(ge=0, le=_INT64_MAX)
    byte_end: int = Field(ge=0, le=_INT64_MAX)

    @model_validator(mode="after")
    def validate_byte_range(self) -> RawTagRow:
        if self.byte_end < self.byte_start:
            raise ValueError("byte_end precedes byte_start")
        return self


EntitySectionRow: TypeAlias = GeometryElementRow | XdataQueryRow | RelationshipRow | RawTagRow


class CadGetEntityResult(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    entity: EntityDetail
    section: EntitySection
    items: tuple[EntitySectionRow, ...] = Field(max_length=200)
    page: PageInfo

    @model_validator(mode="after")
    def validate_item_section(self) -> CadGetEntityResult:
        expected = {
            EntitySection.GEOMETRY_ELEMENTS: GeometryElementRow,
            EntitySection.XDATA_TAGS: XdataQueryRow,
            EntitySection.RELATIONSHIPS: RelationshipRow,
            EntitySection.RAW_TAGS: RawTagRow,
        }[self.section]
        if any(type(item) is not expected for item in self.items):
            raise ValueError("section item does not match requested section")
        return self


class XdataEntityFilters(DataQueryModel):
    entity_scope: EntityScope = Field(
        default=EntityScope.TOP_LEVEL_MODELSPACE,
        description="Owner-entity population; top_level_modelspace is the default.",
    )
    layers: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description="CAD layers discovered by cad_query_entities; values inside the array are OR.",
    )
    entity_types: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description=(
            "DXF entity types discovered by cad_query_entities; values inside the array are OR."
        ),
    )
    entity_keys: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=200,
        json_schema_extra={"uniqueItems": True},
        description="Owner entity keys returned by cad_query_entities rows mode.",
    )

    _validate_arrays = field_validator("layers", "entity_types", "entity_keys")(_unique_strings)


class XdataFilters(DataQueryModel):
    appids: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description="AppIDs first discovered with profile_dimension=appid.",
    )
    group_codes: tuple[StrictInt32, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description="DXF XDATA group codes first discovered with profile_dimension=group_code.",
    )
    assignment_keys: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description=(
            "Projected assignment keys first discovered with profile_dimension=assignment_key."
        ),
    )
    assignment_values: tuple[TypedValue, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description=(
            "Typed assignment values. All supplied values must have one kind; runtime "
            "validation enforces homogeneity without conditional Provider Schema keywords."
        ),
    )

    _validate_strings = field_validator("appids", "assignment_keys")(_unique_strings)
    _validate_codes = field_validator("group_codes")(_unique_integers)

    @field_validator("assignment_values")
    @classmethod
    def validate_unique_values(
        cls, value: tuple[TypedValue, ...] | None
    ) -> tuple[TypedValue, ...] | None:
        if value is not None:
            if len({item.kind for item in value}) > 1:
                raise ValueError("assignment values must have one kind")
            identities = [_typed_value_identity(item) for item in value]
            if len(set(identities)) != len(identities):
                raise ValueError("assignment values must be unique")
        return value


class XdataQueryMode(StrEnum):
    ROWS = "rows"
    PROFILE = "profile"


class XdataProfileDimension(StrEnum):
    APPID = "appid"
    GROUP_CODE = "group_code"
    ASSIGNMENT_KEY = "assignment_key"


class CadXdataQuery(DataQueryModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"mode": {"const": "rows"}},
                        "required": ["mode"],
                    },
                    "then": {
                        "properties": {
                            "xdata_filters": {"not": {"type": "null"}},
                            "profile_dimension": {"type": "null"},
                            "assignment_keys": {"type": "null"},
                            "include_declared_unused_appids": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"mode": {"const": "profile"}},
                        "required": ["mode"],
                    },
                    "then": {
                        "required": ["profile_dimension"],
                        "properties": {
                            "xdata_filters": {"type": "null"},
                            "profile_dimension": {"type": "string"},
                        },
                    },
                },
                {
                    "if": {
                        "properties": {"profile_dimension": {"const": "appid"}},
                        "required": ["profile_dimension"],
                    },
                    "then": {
                        "properties": {
                            "assignment_keys": {"type": "null"},
                            "include_declared_unused_appids": {"type": "boolean"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"profile_dimension": {"const": "group_code"}},
                        "required": ["profile_dimension"],
                    },
                    "then": {
                        "properties": {
                            "assignment_keys": {"type": "null"},
                            "include_declared_unused_appids": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"profile_dimension": {"const": "assignment_key"}},
                        "required": ["profile_dimension"],
                    },
                    "then": {"properties": {"include_declared_unused_appids": {"type": "null"}}},
                },
            ]
        },
    )

    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    mode: XdataQueryMode
    entity_filters: XdataEntityFilters = Field(default_factory=XdataEntityFilters)
    xdata_filters: XdataFilters | None = None
    profile_dimension: XdataProfileDimension | None = None
    assignment_keys: tuple[NonEmptyString, ...] | None = Field(
        default=None, max_length=100, json_schema_extra={"uniqueItems": True}
    )
    include_declared_unused_appids: StrictBool | None = None
    page_size: StrictPageSize = 100

    _validate_assignment_keys = field_validator("assignment_keys")(_unique_strings)

    @model_validator(mode="before")
    @classmethod
    def apply_profile_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if value.get("mode") == XdataQueryMode.ROWS.value:
            value = dict(value)
            value.setdefault("xdata_filters", {})
        elif (
            value.get("mode") == XdataQueryMode.PROFILE.value
            and value.get("profile_dimension") == XdataProfileDimension.APPID.value
        ):
            value = dict(value)
            value.setdefault("include_declared_unused_appids", True)
        return value

    @model_validator(mode="after")
    def validate_mode_fields(self) -> CadXdataQuery:
        if self.mode is XdataQueryMode.ROWS:
            if self.profile_dimension is not None:
                raise ValueError("rows mode does not accept profile_dimension")
            if self.assignment_keys is not None or self.include_declared_unused_appids is not None:
                raise ValueError("rows mode does not accept profile fields")
            if self.xdata_filters is None:
                raise ValueError("rows mode requires xdata_filters")
            return self
        if self.xdata_filters is not None:
            raise ValueError("profile mode does not accept xdata_filters")
        if self.profile_dimension is None:
            raise ValueError("profile mode requires profile_dimension")
        if self.profile_dimension is XdataProfileDimension.APPID:
            if self.assignment_keys is not None:
                raise ValueError("appid profile does not accept assignment_keys")
            if self.include_declared_unused_appids is None:
                raise ValueError("appid profile requires include_declared_unused_appids")
        elif self.profile_dimension is XdataProfileDimension.GROUP_CODE:
            if self.assignment_keys is not None or self.include_declared_unused_appids is not None:
                raise ValueError("group_code profile does not accept other profile fields")
        elif self.include_declared_unused_appids is not None:
            raise ValueError("include_declared_unused_appids is only valid for appid profile")
        return self


class CadQueryXdataRequest(DataQueryModel):
    query: CadXdataQuery | None = None
    cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def require_query_xor_cursor(self) -> CadQueryXdataRequest:
        if (self.query is None) == (self.cursor is None):
            raise ValueError("exactly one of query or cursor is required")
        return self


class ValueFrequency(DataQueryModel):
    value: TypedValue
    occurrence_count: int = Field(gt=0)


class ValueEnumeration(DataQueryModel):
    value_enumeration_complete: bool
    returned_value_count: int = Field(ge=0, le=100)
    other_distinct_value_count: int = Field(ge=0)
    other_occurrence_count: int = Field(ge=0)
    items: tuple[ValueFrequency, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_counts(self) -> ValueEnumeration:
        if self.returned_value_count != len(self.items):
            raise ValueError("returned_value_count must equal item count")
        if self.value_enumeration_complete:
            if self.other_distinct_value_count or self.other_occurrence_count:
                raise ValueError("complete enumeration cannot omit values")
        elif (
            len(self.items) != 20
            or self.other_distinct_value_count == 0
            or self.other_occurrence_count == 0
        ):
            raise ValueError("high-cardinality enumeration requires Top 20 and omitted values")
        identities = [_typed_value_identity(item.value) for item in self.items]
        if len(set(identities)) != len(identities):
            raise ValueError("enumerated values must be unique")
        return self


class NumericInterpretation(StrEnum):
    NATIVE_NUMERIC = "native_numeric"
    STRICT_NUMERIC_LEXEME = "strict_numeric_lexeme"
    NOT_APPLICABLE = "not_applicable"


class NumericProfile(DataQueryModel):
    applicable: bool
    interpretation: NumericInterpretation
    numeric_value_count: int = Field(ge=0)
    minimum: str | None = Field(default=None, pattern=_DECIMAL_PATTERN)
    maximum: str | None = Field(default=None, pattern=_DECIMAL_PATTERN)
    non_numeric_non_empty_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_applicability(self) -> NumericProfile:
        if self.applicable:
            if self.interpretation is NumericInterpretation.NOT_APPLICABLE:
                raise ValueError("applicable numeric profile needs a numeric interpretation")
            if self.numeric_value_count == 0 or self.minimum is None or self.maximum is None:
                raise ValueError("applicable numeric profile requires count and range")
            if self.non_numeric_non_empty_count:
                raise ValueError("non-numeric values make a numeric profile not applicable")
        else:
            if self.interpretation is not NumericInterpretation.NOT_APPLICABLE:
                raise ValueError("non-applicable profile requires not_applicable interpretation")
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("non-applicable profile cannot expose a range")
        return self


class XdataProfileScope(DataQueryModel):
    matched_entity_count: int = Field(ge=0)
    entities_with_xdata_count: int = Field(ge=0)
    raw_non_marker_tag_count: int = Field(ge=0)
    projected_assignment_count: int = Field(ge=0)
    unprojected_non_marker_tag_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> XdataProfileScope:
        if self.entities_with_xdata_count > self.matched_entity_count:
            raise ValueError("entities_with_xdata_count exceeds matched entities")
        if (
            self.projected_assignment_count + self.unprojected_non_marker_tag_count
            != self.raw_non_marker_tag_count
        ):
            raise ValueError("projected and unprojected counts must cover non-marker tags")
        return self


class AppidProfileBucket(DataQueryModel):
    appid: str = Field(min_length=1)
    declared_in_appid_table: bool
    declaration_count: int = Field(ge=0)
    entity_count: int = Field(ge=0)
    appid_group_count: int = Field(ge=0)
    marker_count: int = Field(ge=0)
    payload_tag_count: int = Field(ge=0)


class GroupCodeProfileBucket(DataQueryModel):
    group_code: int = Field(ge=_INT32_MIN, le=_INT32_MAX)
    is_appid_marker_code: bool
    tag_count: int = Field(ge=0)
    entity_count: int = Field(ge=0)
    typed_value_kind_counts: dict[str, int]

    @field_validator("typed_value_kind_counts")
    @classmethod
    def validate_kind_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if not set(value).issubset(_TYPED_KINDS) or any(count < 0 for count in value.values()):
            raise ValueError("typed value kind counts are invalid")
        return value


class AssignmentKeyProfileBucket(DataQueryModel):
    assignment_key: str = Field(min_length=1)
    source_appids: tuple[str, ...]
    occurrence_count: int = Field(ge=0)
    entity_count: int = Field(ge=0)
    missing_entity_count: int = Field(ge=0)
    duplicate_entity_count: int = Field(ge=0)
    empty_value_count: int = Field(ge=0)
    distinct_value_count: int = Field(ge=0)
    value_kind_counts: dict[str, int]
    values: ValueEnumeration
    numeric_profile: NumericProfile

    _validate_appids = field_validator("source_appids")(_unique_strings)

    @field_validator("value_kind_counts")
    @classmethod
    def validate_kind_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if not set(value).issubset(_TYPED_KINDS) or any(count < 0 for count in value.values()):
            raise ValueError("typed value kind counts are invalid")
        return value

    @model_validator(mode="after")
    def validate_profile_counts(self) -> AssignmentKeyProfileBucket:
        if self.entity_count > self.occurrence_count:
            raise ValueError("entity_count cannot exceed occurrence_count")
        if self.duplicate_entity_count > self.entity_count:
            raise ValueError("duplicate_entity_count cannot exceed entity_count")
        if self.empty_value_count > self.occurrence_count:
            raise ValueError("empty_value_count cannot exceed occurrence_count")
        if self.distinct_value_count != (
            self.values.returned_value_count + self.values.other_distinct_value_count
        ):
            raise ValueError("distinct value counts are inconsistent")
        if self.values.value_enumeration_complete != (self.distinct_value_count <= 100):
            raise ValueError("value enumeration mode does not match cardinality threshold")
        if sum(self.value_kind_counts.values()) != self.occurrence_count:
            raise ValueError("value kind counts must cover all occurrences")
        return self


XdataProfileBucket: TypeAlias = (
    AppidProfileBucket | GroupCodeProfileBucket | AssignmentKeyProfileBucket
)


class CadXdataRowsResult(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    mode: Literal["rows"] = "rows"
    rows: tuple[XdataQueryRow, ...] = Field(max_length=200)
    page: PageInfo


class CadXdataProfileResult(DataQueryModel):
    package_alias: str = Field(pattern=_DATA_ALIAS_PATTERN, max_length=32)
    mode: Literal["profile"] = "profile"
    profile_dimension: XdataProfileDimension
    scope: XdataProfileScope
    profiles: tuple[XdataProfileBucket, ...] = Field(max_length=200)
    page: PageInfo

    @model_validator(mode="after")
    def validate_profile_shapes(self) -> CadXdataProfileResult:
        expected = {
            XdataProfileDimension.APPID: AppidProfileBucket,
            XdataProfileDimension.GROUP_CODE: GroupCodeProfileBucket,
            XdataProfileDimension.ASSIGNMENT_KEY: AssignmentKeyProfileBucket,
        }[self.profile_dimension]
        if any(type(profile) is not expected for profile in self.profiles):
            raise ValueError("profile bucket does not match profile_dimension")
        return self


CadQueryXdataResult: TypeAlias = Annotated[
    CadXdataRowsResult | CadXdataProfileResult, Field(discriminator="mode")
]


__all__ = [
    "AppidProfileBucket",
    "AssignmentKeyProfileBucket",
    "BinaryCellValue",
    "BinaryTypedValue",
    "CadEntitiesQuery",
    "CadEntityCountResult",
    "CadEntityQuery",
    "CadEntityRowsResult",
    "CadGetEntityRequest",
    "CadGetEntityResult",
    "CadQueryEntitiesRequest",
    "CadQueryEntitiesResult",
    "CadQueryXdataRequest",
    "CadQueryXdataResult",
    "CadXdataProfileResult",
    "CadXdataQuery",
    "CadXdataRowsResult",
    "ColumnSchema",
    "DataDescribePackageQuery",
    "DataDescribePackageResult",
    "DataQueryModel",
    "DataQueryRowsRequest",
    "DataQueryRowsResult",
    "DataRowsQuery",
    "EntityCountBucket",
    "EntityCountBy",
    "EntityCountScope",
    "EntityDetail",
    "EntityFilters",
    "EntityQueryMode",
    "EntityScope",
    "EntitySection",
    "EntitySectionRow",
    "EntitySummary",
    "FilterCondition",
    "FilterOperator",
    "GenericCellValue",
    "GenericSelectedCellValue",
    "GenericQueryRow",
    "GeometryElementRow",
    "GroupCodeProfileBucket",
    "IntegerTypedValue",
    "LayerCountBucket",
    "LayerEntityTypeCountBucket",
    "NullTypedValue",
    "NumberTypedValue",
    "NumericInterpretation",
    "NumericProfile",
    "PackageSummary",
    "PageInfo",
    "JsonPayloadSchema",
    "JsonPayloadSummary",
    "ParquetPayloadSchema",
    "ParquetPayloadSummary",
    "PayloadSchema",
    "PayloadSummary",
    "RawTagRow",
    "RelationshipRow",
    "StringTypedValue",
    "TypedValue",
    "ValueEnumeration",
    "ValueFrequency",
    "XdataEntityFilters",
    "XdataFilters",
    "XdataProfileBucket",
    "XdataProfileDimension",
    "XdataProfileScope",
    "XdataQueryMode",
    "XdataQueryRow",
]
