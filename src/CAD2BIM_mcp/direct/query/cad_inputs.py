"""Provider-visible inputs for the Direct CAD/Data query tools.

The domain query models deliberately keep their runtime validators.  These
boundary models express the same public JSON as complete object variants so a
tool provider never has to translate conditional JSON Schema keywords such as
``allOf``/``if``/``then`` into an LLM-facing declaration.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from CAD2BIM_backend.domain.data_query import (
    BinaryTypedValue,
    CadEntitiesQuery,
    CadEntityQuery,
    CadXdataQuery,
    DataRowsQuery,
    EntityFilters,
    EntitySection,
    EntityScope,
    FilterScalar,
    IntegerTypedValue,
    NonEmptyString,
    NullTypedValue,
    NumberTypedValue,
    StrictBool,
    StrictInt32,
    StrictPageSize,
    StringTypedValue,
    XdataEntityFilters,
)
from CAD2BIM_mcp.tool_docs import cad as _tool_docs


_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"


class _ProviderQueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EqFilter(_ProviderQueryModel):
    field: str = Field(
        min_length=1,
        max_length=255,
        description=_tool_docs.EQ_FILTER_FIELD_DESCRIPTION,
    )
    op: Literal["eq"] = Field(description=_tool_docs.EQ_FILTER_OP_DESCRIPTION)
    value: FilterScalar = Field(description=_tool_docs.EQ_FILTER_VALUE_DESCRIPTION)


class InFilter(_ProviderQueryModel):
    field: str = Field(
        min_length=1,
        max_length=255,
        description=_tool_docs.EQ_FILTER_FIELD_DESCRIPTION,
    )
    op: Literal["in"] = Field(description=_tool_docs.IN_FILTER_OP_DESCRIPTION)
    value: tuple[FilterScalar, ...] = Field(
        min_length=1,
        max_length=100,
        description=(_tool_docs.IN_FILTER_VALUE_DESCRIPTION),
    )


class OrderedFilter(_ProviderQueryModel):
    field: str = Field(
        min_length=1,
        max_length=255,
        description=_tool_docs.EQ_FILTER_FIELD_DESCRIPTION,
    )
    op: Literal["lt", "lte", "gt", "gte"] = Field(
        description=_tool_docs.ORDERED_FILTER_OP_DESCRIPTION
    )
    value: FilterScalar = Field(description=_tool_docs.EQ_FILTER_VALUE_DESCRIPTION)


class NullFilter(_ProviderQueryModel):
    field: str = Field(
        min_length=1,
        max_length=255,
        description=_tool_docs.EQ_FILTER_FIELD_DESCRIPTION,
    )
    op: Literal["is_null", "is_not_null"] = Field(description=_tool_docs.NULL_FILTER_OP_DESCRIPTION)


DataFilterInput: TypeAlias = EqFilter | InFilter | OrderedFilter | NullFilter


# Put the item bound on every concrete array branch. Applying ``max_length``
# to the outer union makes Pydantic emit ``maxLength`` beside ``anyOf``, which
# providers interpret as a string constraint instead of the intended array
# ``maxItems`` constraint.
NullAssignmentValues: TypeAlias = Annotated[tuple[NullTypedValue, ...], Field(max_length=100)]
StringAssignmentValues: TypeAlias = Annotated[tuple[StringTypedValue, ...], Field(max_length=100)]
IntegerAssignmentValues: TypeAlias = Annotated[tuple[IntegerTypedValue, ...], Field(max_length=100)]
NumberAssignmentValues: TypeAlias = Annotated[tuple[NumberTypedValue, ...], Field(max_length=100)]
BinaryAssignmentValues: TypeAlias = Annotated[tuple[BinaryTypedValue, ...], Field(max_length=100)]


class DataRowsToolQuery(_ProviderQueryModel):
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=(_tool_docs.DATA_ROWS_TOOL_QUERY_PACKAGE_ALIAS_DESCRIPTION),
    )
    payload_name: str = Field(
        min_length=1,
        max_length=120,
        description=_tool_docs.DATA_ROWS_TOOL_QUERY_PAYLOAD_NAME_DESCRIPTION,
    )
    select: tuple[NonEmptyString, ...] = Field(
        max_length=64,
        json_schema_extra={"uniqueItems": True},
        description=(_tool_docs.DATA_ROWS_TOOL_QUERY_SELECT_DESCRIPTION),
    )
    filters: tuple[DataFilterInput, ...] = Field(
        default=(),
        max_length=32,
        description=_tool_docs.DATA_ROWS_TOOL_QUERY_FILTERS_DESCRIPTION,
    )
    page_size: StrictPageSize = Field(
        default=100,
        description=_tool_docs.DATA_ROWS_TOOL_QUERY_PAGE_SIZE_DESCRIPTION,
    )


class _EntityToolQueryBase(_ProviderQueryModel):
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=(_tool_docs.ENTITY_TOOL_QUERY_BASE_PACKAGE_ALIAS_DESCRIPTION),
    )
    entity_scope: EntityScope = Field(
        default=EntityScope.TOP_LEVEL_MODELSPACE,
        description=(_tool_docs.ENTITY_TOOL_QUERY_BASE_ENTITY_SCOPE_DESCRIPTION),
    )
    filters: EntityFilters = Field(
        default_factory=EntityFilters,
        description=(_tool_docs.ENTITY_TOOL_QUERY_BASE_FILTERS_DESCRIPTION),
    )
    page_size: StrictPageSize = Field(
        default=100,
        description=_tool_docs.ENTITY_TOOL_QUERY_BASE_PAGE_SIZE_DESCRIPTION,
    )


class EntityRowsToolQuery(_EntityToolQueryBase):
    mode: Literal["rows"] = Field(description=_tool_docs.ENTITY_ROWS_TOOL_QUERY_MODE_DESCRIPTION)


class CountByLayerToolQuery(_EntityToolQueryBase):
    mode: Literal["count_by"] = Field(
        description=_tool_docs.COUNT_BY_LAYER_TOOL_QUERY_MODE_DESCRIPTION
    )
    count_by: Literal["layer"] = Field(
        description=_tool_docs.COUNT_BY_LAYER_TOOL_QUERY_COUNT_BY_DESCRIPTION
    )
    include_declared_empty_layers: StrictBool = Field(
        default=True,
        description=_tool_docs.COUNT_BY_LAYER_TOOL_QUERY_INCLUDE_DECLARED_EMPTY_LAYERS_DESCRIPTION,
    )


class CountByEntityTypeToolQuery(_EntityToolQueryBase):
    mode: Literal["count_by"] = Field(
        description=_tool_docs.COUNT_BY_LAYER_TOOL_QUERY_MODE_DESCRIPTION
    )
    count_by: Literal["entity_type"] = Field(
        description=_tool_docs.COUNT_BY_ENTITY_TYPE_TOOL_QUERY_COUNT_BY_DESCRIPTION
    )


class CountByLayerAndTypeToolQuery(_EntityToolQueryBase):
    mode: Literal["count_by"] = Field(
        description=_tool_docs.COUNT_BY_LAYER_TOOL_QUERY_MODE_DESCRIPTION
    )
    count_by: Literal["layer_and_entity_type"] = Field(
        description=_tool_docs.COUNT_BY_LAYER_AND_TYPE_TOOL_QUERY_COUNT_BY_DESCRIPTION
    )


CadEntitiesToolQuery: TypeAlias = (
    EntityRowsToolQuery
    | CountByLayerToolQuery
    | CountByEntityTypeToolQuery
    | CountByLayerAndTypeToolQuery
)


class CadEntityToolQuery(_ProviderQueryModel):
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=(_tool_docs.ENTITY_TOOL_QUERY_BASE_PACKAGE_ALIAS_DESCRIPTION),
    )
    entity_key: str = Field(
        min_length=1,
        max_length=255,
        description=_tool_docs.CAD_ENTITY_TOOL_QUERY_ENTITY_KEY_DESCRIPTION,
    )
    section: EntitySection = Field(
        description=(_tool_docs.CAD_ENTITY_TOOL_QUERY_SECTION_DESCRIPTION)
    )
    page_size: StrictPageSize = Field(
        default=100,
        description=_tool_docs.CAD_ENTITY_TOOL_QUERY_PAGE_SIZE_DESCRIPTION,
    )


class ProviderXdataFilters(_ProviderQueryModel):
    appids: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description=_tool_docs.PROVIDER_XDATA_FILTERS_APPIDS_DESCRIPTION,
    )
    group_codes: tuple[StrictInt32, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description=_tool_docs.PROVIDER_XDATA_FILTERS_GROUP_CODES_DESCRIPTION,
    )
    assignment_keys: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description=(_tool_docs.PROVIDER_XDATA_FILTERS_ASSIGNMENT_KEYS_DESCRIPTION),
    )
    assignment_values: (
        NullAssignmentValues
        | StringAssignmentValues
        | IntegerAssignmentValues
        | NumberAssignmentValues
        | BinaryAssignmentValues
        | None
    ) = Field(
        default=None,
        json_schema_extra={"uniqueItems": True},
        description=(_tool_docs.PROVIDER_XDATA_FILTERS_ASSIGNMENT_VALUES_DESCRIPTION),
    )


class _XdataToolQueryBase(_ProviderQueryModel):
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=(_tool_docs.ENTITY_TOOL_QUERY_BASE_PACKAGE_ALIAS_DESCRIPTION),
    )
    entity_filters: XdataEntityFilters = Field(
        default_factory=XdataEntityFilters,
        description=(_tool_docs.XDATA_TOOL_QUERY_BASE_ENTITY_FILTERS_DESCRIPTION),
    )
    page_size: StrictPageSize = Field(
        default=100,
        description=_tool_docs.XDATA_TOOL_QUERY_BASE_PAGE_SIZE_DESCRIPTION,
    )


class XdataRowsToolQuery(_XdataToolQueryBase):
    mode: Literal["rows"] = Field(description=_tool_docs.XDATA_ROWS_TOOL_QUERY_MODE_DESCRIPTION)
    xdata_filters: ProviderXdataFilters = Field(
        default_factory=ProviderXdataFilters,
        description=(_tool_docs.XDATA_ROWS_TOOL_QUERY_XDATA_FILTERS_DESCRIPTION),
    )


class AppidProfileToolQuery(_XdataToolQueryBase):
    mode: Literal["profile"] = Field(
        description=_tool_docs.APPID_PROFILE_TOOL_QUERY_MODE_DESCRIPTION
    )
    profile_dimension: Literal["appid"] = Field(
        description=_tool_docs.APPID_PROFILE_TOOL_QUERY_PROFILE_DIMENSION_DESCRIPTION
    )
    include_declared_unused_appids: StrictBool = Field(
        default=True,
        description=_tool_docs.APPID_PROFILE_TOOL_QUERY_INCLUDE_DECLARED_UNUSED_APPIDS_DESCRIPTION,
    )


class GroupCodeProfileToolQuery(_XdataToolQueryBase):
    mode: Literal["profile"] = Field(
        description=_tool_docs.APPID_PROFILE_TOOL_QUERY_MODE_DESCRIPTION
    )
    profile_dimension: Literal["group_code"] = Field(
        description=_tool_docs.GROUP_CODE_PROFILE_TOOL_QUERY_PROFILE_DIMENSION_DESCRIPTION
    )


class AssignmentKeyProfileToolQuery(_XdataToolQueryBase):
    mode: Literal["profile"] = Field(
        description=_tool_docs.APPID_PROFILE_TOOL_QUERY_MODE_DESCRIPTION
    )
    profile_dimension: Literal["assignment_key"] = Field(
        description=_tool_docs.ASSIGNMENT_KEY_PROFILE_TOOL_QUERY_PROFILE_DIMENSION_DESCRIPTION
    )
    assignment_keys: tuple[NonEmptyString, ...] | None = Field(
        default=None,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
        description=(_tool_docs.ASSIGNMENT_KEY_PROFILE_TOOL_QUERY_ASSIGNMENT_KEYS_DESCRIPTION),
    )


CadXdataToolQuery: TypeAlias = (
    XdataRowsToolQuery
    | AppidProfileToolQuery
    | GroupCodeProfileToolQuery
    | AssignmentKeyProfileToolQuery
)


_DATA_ROWS_ADAPTER = TypeAdapter(DataRowsToolQuery)
_CAD_ENTITIES_ADAPTER = TypeAdapter(CadEntitiesToolQuery)
_CAD_ENTITY_ADAPTER = TypeAdapter(CadEntityToolQuery)
_CAD_XDATA_ADAPTER = TypeAdapter(CadXdataToolQuery)


def _provider_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        # Internal callers may pass the wider Domain DTO directly. Null fields
        # that are absent from a narrower Provider branch carry no semantics.
        return value.model_dump(mode="python", exclude_none=True)
    return value


def to_domain_data_rows_query(value: Any) -> DataRowsQuery:
    parsed = _DATA_ROWS_ADAPTER.validate_python(_provider_value(value))
    return DataRowsQuery.model_validate(parsed.model_dump(mode="python", exclude_none=False))


def to_domain_cad_entities_query(value: Any) -> CadEntitiesQuery:
    parsed = _CAD_ENTITIES_ADAPTER.validate_python(_provider_value(value))
    return CadEntitiesQuery.model_validate(parsed.model_dump(mode="python", exclude_none=False))


def to_domain_cad_entity_query(value: Any) -> CadEntityQuery:
    parsed = _CAD_ENTITY_ADAPTER.validate_python(_provider_value(value))
    return CadEntityQuery.model_validate(parsed.model_dump(mode="python", exclude_none=False))


def to_domain_cad_xdata_query(value: Any) -> CadXdataQuery:
    parsed = _CAD_XDATA_ADAPTER.validate_python(_provider_value(value))
    return CadXdataQuery.model_validate(parsed.model_dump(mode="python", exclude_none=False))


__all__ = [
    "CadEntityToolQuery",
    "CadEntitiesToolQuery",
    "CadXdataToolQuery",
    "DataRowsToolQuery",
    "to_domain_cad_entity_query",
    "to_domain_cad_entities_query",
    "to_domain_cad_xdata_query",
    "to_domain_data_rows_query",
]
