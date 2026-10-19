"""Provider-neutral contracts for bounded immutable JSON Payload reads."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.data_query import PageInfo


_PACKAGE_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_CURSOR_PATTERN = r"^cur\..+$"
_PAYLOAD_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,119}$"
_MAX_POINTER_UTF8_BYTES = 4 * 1024
_MAX_VALUE_UTF8_BYTES = 512 * 1024
_MAX_PREVIEW_UTF8_BYTES = 4 * 1024


class DataJsonModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class JsonKind(StrEnum):
    DOCUMENT = "document"
    RECORDS = "records"


class JsonNodeType(StrEnum):
    OBJECT = "object"
    ARRAY = "array"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"


class JsonErrorCode(StrEnum):
    SCHEMA_INVALID = "JSON_SCHEMA_INVALID"
    INSTANCE_INVALID = "JSON_INSTANCE_INVALID"
    OUTPUT_BUDGET_EXCEEDED = "JSON_OUTPUT_BUDGET_EXCEEDED"
    POINTER_INVALID = "JSON_POINTER_INVALID"
    READ_MODE_MISMATCH = "JSON_READ_MODE_MISMATCH"
    VALUE_TOO_LARGE = "JSON_VALUE_TOO_LARGE"
    CURSOR_INVALID = "JSON_CURSOR_INVALID"


class _JsonPayloadQuery(DataJsonModel):
    package_alias: str = Field(pattern=_PACKAGE_ALIAS_PATTERN, max_length=32)
    payload_name: str = Field(pattern=_PAYLOAD_NAME_PATTERN, max_length=120)


class JsonValueQuery(_JsonPayloadQuery):
    mode: Literal["value"]
    pointer: str = Field(max_length=_MAX_POINTER_UTF8_BYTES)

    @field_validator("pointer")
    @classmethod
    def validate_pointer_budget(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_POINTER_UTF8_BYTES:
            raise ValueError("pointer exceeds 4 KiB UTF-8")
        return value


class JsonChildrenQuery(_JsonPayloadQuery):
    mode: Literal["children"]
    pointer: str = Field(max_length=_MAX_POINTER_UTF8_BYTES)
    page_size: int = Field(default=50, strict=True, ge=1, le=100)

    @field_validator("pointer")
    @classmethod
    def validate_pointer_budget(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_POINTER_UTF8_BYTES:
            raise ValueError("pointer exceeds 4 KiB UTF-8")
        return value


class JsonRecordsQuery(_JsonPayloadQuery):
    mode: Literal["records"]
    page_size: int = Field(default=100, strict=True, ge=1, le=200)


JsonReadQuery: TypeAlias = Annotated[
    JsonValueQuery | JsonChildrenQuery | JsonRecordsQuery,
    Field(discriminator="mode"),
]


class JsonReadRequest(DataJsonModel):
    query: JsonReadQuery | None = None
    cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def require_query_xor_cursor(self) -> JsonReadRequest:
        if (self.query is None) == (self.cursor is None):
            raise ValueError("exactly one of query or cursor is required")
        return self


class JsonValueResult(DataJsonModel):
    package_alias: str = Field(pattern=_PACKAGE_ALIAS_PATTERN, max_length=32)
    payload_name: str = Field(pattern=_PAYLOAD_NAME_PATTERN, max_length=120)
    json_kind: JsonKind
    pointer: str
    node_type: JsonNodeType
    size_bytes: int = Field(strict=True, ge=0)
    value_json: str

    @model_validator(mode="after")
    def validate_exact_value(self) -> JsonValueResult:
        size = len(self.value_json.encode("utf-8"))
        if size > _MAX_VALUE_UTF8_BYTES:
            raise ValueError("value_json exceeds 512 KiB UTF-8")
        if self.size_bytes != size:
            raise ValueError("size_bytes must equal encoded value_json bytes")
        return self


class _JsonChildBase(DataJsonModel):
    pointer: str
    selector: str
    node_type: JsonNodeType
    size_bytes: int = Field(strict=True, ge=0)
    child_count: int = Field(strict=True, ge=0)


class JsonChildWithPreview(_JsonChildBase):
    preview_json: str

    @field_validator("preview_json")
    @classmethod
    def validate_preview_budget(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_PREVIEW_UTF8_BYTES:
            raise ValueError("preview_json exceeds 4 KiB UTF-8")
        return value


class JsonChildWithoutPreview(_JsonChildBase):
    pass


JsonChild: TypeAlias = JsonChildWithPreview | JsonChildWithoutPreview


class JsonChildrenResult(DataJsonModel):
    package_alias: str = Field(pattern=_PACKAGE_ALIAS_PATTERN, max_length=32)
    payload_name: str = Field(pattern=_PAYLOAD_NAME_PATTERN, max_length=120)
    json_kind: JsonKind
    pointer: str
    children: tuple[JsonChild, ...] = Field(max_length=100)
    page: PageInfo

    @model_validator(mode="after")
    def validate_page_count(self) -> JsonChildrenResult:
        if len(self.children) != self.page.returned_count:
            raise ValueError("returned_count must equal the number of child items")
        return self


class JsonRecord(DataJsonModel):
    record_index: int = Field(strict=True, ge=0)
    record_json: str

    @field_validator("record_json")
    @classmethod
    def validate_record_budget(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_VALUE_UTF8_BYTES:
            raise ValueError("record_json exceeds 512 KiB UTF-8")
        return value


class JsonRecordsResult(DataJsonModel):
    package_alias: str = Field(pattern=_PACKAGE_ALIAS_PATTERN, max_length=32)
    payload_name: str = Field(pattern=_PAYLOAD_NAME_PATTERN, max_length=120)
    json_kind: Literal[JsonKind.RECORDS]
    records: tuple[JsonRecord, ...] = Field(max_length=200)
    page: PageInfo

    @model_validator(mode="after")
    def validate_page_count(self) -> JsonRecordsResult:
        if len(self.records) != self.page.returned_count:
            raise ValueError("returned_count must equal the number of records")
        return self


JsonReadResult: TypeAlias = JsonValueResult | JsonChildrenResult | JsonRecordsResult


__all__ = [
    "JsonChild",
    "JsonChildWithPreview",
    "JsonChildWithoutPreview",
    "JsonChildrenQuery",
    "JsonChildrenResult",
    "JsonErrorCode",
    "JsonKind",
    "JsonNodeType",
    "JsonReadQuery",
    "JsonReadRequest",
    "JsonReadResult",
    "JsonRecord",
    "JsonRecordsQuery",
    "JsonRecordsResult",
    "JsonValueQuery",
    "JsonValueResult",
]
