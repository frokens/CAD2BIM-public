"""Closed, snapshot-local facts for selected IFC product reads.

Local STEP identifiers only locate objects within the one snapshot being read.
They are neither reusable resource references nor mutation authorizations.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator


MAX_READ_PRODUCTS = 32
MAX_READ_COLLECTION = 128
MAX_READ_RESPONSE_BYTES = 256 * 1024
IfcReadGlobalId = Annotated[str, Field(pattern=r"^[0-9A-Za-z_$]{22}$")]
IfcReadExactName = Annotated[str, Field(min_length=1)]
IfcReadProductKey = Annotated[str, Field(min_length=1)]
IfcReadStatus: TypeAlias = Literal["found", "empty", "unsupported", "ambiguous", "limit_exceeded"]
IfcReadSource: TypeAlias = Literal["instance", "type"]
IfcReadVector: TypeAlias = tuple[float, float, float]
IfcReadRotation: TypeAlias = tuple[IfcReadVector, IfcReadVector, IfcReadVector]


class IfcProductsReadError(RuntimeError):
    """Recognizable business failure that survives Pydantic after validators."""

    def __init__(self, code: str, message: str, diagnostic: str | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic


class IfcProductsReadContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class IfcReadText(IfcProductsReadContract):
    value: str = Field(max_length=2048)
    truncated: bool = False
    original_length: int = Field(ge=0)

    @model_validator(mode="after")
    def check_length(self):
        if self.original_length < len(self.value) or self.truncated != (self.original_length > len(self.value)):
            raise ValueError("text truncation must describe the original character length")
        return self


class IfcReadEntity(IfcProductsReadContract):
    global_id: IfcReadGlobalId
    ifc_class: str = Field(min_length=1, max_length=128)
    name: IfcReadText | None = None


class IfcReadLocalObject(IfcProductsReadContract):
    """Snapshot-local diagnostic location; semantic names live on their facts."""

    step_id: int = Field(gt=0)
    ifc_class: str = Field(min_length=1, max_length=128)


class IfcReadGroupResult(IfcProductsReadContract):
    status: IfcReadStatus
    diagnostic: IfcReadText | None = None
    total_count: int | None = Field(default=None, ge=0)


class IfcReadSelection(IfcProductsReadContract):
    @model_validator(mode="after")
    def unique_choices(self):
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, tuple) and len(set(value)) != len(value):
                raise ValueError(f"{name} must be unique")
        return self


class IfcStructureReadSelection(IfcReadSelection):
    mode: Literal["direct"] = "direct"


class IfcPropertyNamesSelection(IfcReadSelection):
    mode: Literal["names"] = "names"
    sources: tuple[IfcReadSource, ...] = Field(default=("instance", "type"), min_length=1, max_length=2)
    set_kinds: tuple[Literal["pset", "qto"], ...] = Field(default=("pset", "qto"), min_length=1, max_length=2)


class IfcPropertySelector(IfcReadSelection):
    source: IfcReadSource
    set_kind: Literal["pset", "qto"]
    set_name: IfcReadExactName
    property_names: tuple[IfcReadExactName, ...] = Field(min_length=1, max_length=128)


class IfcPropertyValuesSelection(IfcReadSelection):
    mode: Literal["values"] = "values"
    selectors: tuple[IfcPropertySelector, ...] = Field(min_length=1, max_length=128)


IfcPropertiesReadSelection = Annotated[IfcPropertyNamesSelection | IfcPropertyValuesSelection, Field(discriminator="mode")]


class IfcMaterialNamesSelection(IfcReadSelection):
    mode: Literal["names"] = "names"
    sources: tuple[IfcReadSource, ...] = Field(default=("instance", "type"), min_length=1, max_length=2)


class IfcMaterialSelector(IfcReadSelection):
    """Names are revalidated within this product and source at each read."""

    source: IfcReadSource
    set_name: IfcReadExactName
    member_names: tuple[IfcReadExactName, ...] = Field(min_length=1, max_length=128)


class IfcMaterialValuesSelection(IfcReadSelection):
    mode: Literal["values"] = "values"
    selectors: tuple[IfcMaterialSelector, ...] = Field(min_length=1, max_length=128)


IfcMaterialsReadSelection = Annotated[IfcMaterialNamesSelection | IfcMaterialValuesSelection, Field(discriminator="mode")]


class IfcSpatialReadSelection(IfcReadSelection):
    mode: Literal["direct"] = "direct"


class IfcSystemsReadSelection(IfcReadSelection):
    mode: Literal["direct"] = "direct"


class IfcPortNamesSelection(IfcReadSelection):
    mode: Literal["names"] = "names"


class IfcPortValuesSelection(IfcReadSelection):
    mode: Literal["values"] = "values"
    global_ids: tuple[IfcReadGlobalId, ...] = Field(min_length=1, max_length=128)


IfcPortsReadSelection = Annotated[IfcPortNamesSelection | IfcPortValuesSelection, Field(discriminator="mode")]


class IfcRepresentationsReadSelection(IfcReadSelection):
    mode: Literal["summary"] = "summary"


class IfcProductsReadGroups(IfcProductsReadContract):
    structure: IfcStructureReadSelection | None = None
    properties: IfcPropertiesReadSelection | None = None
    materials: IfcMaterialsReadSelection | None = None
    spatial: IfcSpatialReadSelection | None = None
    systems: IfcSystemsReadSelection | None = None
    ports: IfcPortsReadSelection | None = None
    representations: IfcRepresentationsReadSelection | None = None


class IfcProductsReadRuntimeRequest(IfcProductsReadContract):
    global_ids: tuple[IfcReadGlobalId, ...] = Field(min_length=1, max_length=MAX_READ_PRODUCTS)
    groups: IfcProductsReadGroups = Field(default_factory=IfcProductsReadGroups)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.global_ids)) != len(self.global_ids):
            raise ValueError("global_ids must be unique")
        return self


class IfcProductsReadAppliedQuery(IfcProductsReadContract):
    mode: Literal["from_apply"] = "from_apply"
    view: Literal["applied"] = "applied"
    product_keys: tuple[IfcReadProductKey, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_keys(self):
        if len(set(self.product_keys)) != len(self.product_keys):
            raise ValueError("product_keys must be unique")
        return self


class IfcProductsReadCurrentQuery(IfcProductsReadContract):
    mode: Literal["from_apply"] = "from_apply"
    view: Literal["current"]
    product_keys: tuple[IfcReadProductKey, ...] = Field(min_length=1, max_length=32)
    groups: IfcProductsReadGroups = Field(default_factory=IfcProductsReadGroups)

    @model_validator(mode="after")
    def unique_keys(self):
        if len(set(self.product_keys)) != len(self.product_keys):
            raise ValueError("product_keys must be unique")
        return self


class IfcProductsReadIfcQuery(IfcProductsReadRuntimeRequest):
    mode: Literal["from_ifc"] = "from_ifc"


IfcProductsReadQuery: TypeAlias = IfcProductsReadAppliedQuery | IfcProductsReadCurrentQuery | IfcProductsReadIfcQuery


class IfcReadPlacement(IfcProductsReadContract):
    status: Literal["found", "empty", "unsupported", "ambiguous"]
    translation_m: IfcReadVector | None = None
    rotation: IfcReadRotation | None = None
    diagnostic: IfcReadText | None = None

    @model_validator(mode="after")
    def require_complete_frame(self):
        if self.status == "found":
            if self.translation_m is None or self.rotation is None:
                raise ValueError("found placement requires a complete project-world metre frame")
        elif self.translation_m is not None or self.rotation is not None:
            raise ValueError("unresolved placement cannot carry a frame")
        return self


class IfcReadAggregateParent(IfcReadGroupResult):
    parents: tuple[IfcReadEntity, ...] = Field(default=(), max_length=128)
    relationship_guids: tuple[IfcReadGlobalId, ...] = Field(default=(), max_length=128)


class IfcProductBasicFacts(IfcProductsReadContract):
    ifc_class: str = Field(min_length=1, max_length=128)
    name: IfcReadText | None = None
    placement: IfcReadPlacement
    aggregate_parent: IfcReadAggregateParent
    direct_child_count: int = Field(ge=0)


class IfcReadRelation(IfcProductsReadContract):
    relationship: IfcReadEntity
    related: IfcReadEntity
    direction: Literal["parent", "child", "reference"]


class IfcReadPlacementDependency(IfcProductsReadContract):
    placement: IfcReadLocalObject
    relative_to: IfcReadLocalObject | None = None
    dependents: tuple[IfcReadLocalObject, ...] = Field(default=(), max_length=128)
    owners: tuple[IfcReadEntity, ...] = Field(default=(), max_length=128)
    owner_count: int = Field(default=0, ge=0)


class IfcReadReferenceSummary(IfcProductsReadContract):
    ifc_class: str = Field(min_length=1, max_length=128)
    count: int = Field(ge=0)


class IfcStructureReadFacts(IfcReadGroupResult):
    aggregation: tuple[IfcReadRelation, ...] = Field(default=(), max_length=128)
    nesting: tuple[IfcReadRelation, ...] = Field(default=(), max_length=128)
    placement_dependencies: tuple[IfcReadPlacementDependency, ...] = Field(default=(), max_length=128)
    references: tuple[IfcReadReferenceSummary, ...] = Field(default=(), max_length=128)


class IfcReadUnit(IfcProductsReadContract):
    ifc_class: str = Field(min_length=1, max_length=128)
    name: IfcReadText | None = Field(default=None,
        description="IFC unit name; for IfcSIUnit this is the base Name, not a prefix-expanded name.")
    symbol: IfcReadText | None = Field(default=None,
        description="Unit symbol; for IfcSIUnit it includes the SI Prefix. Interpret numeric values using it together with name.")


IfcReadScalar: TypeAlias = StrictBool | StrictInt | StrictFloat | Annotated[StrictStr, Field(max_length=2048)]


class IfcReadPropertyValue(IfcProductsReadContract):
    status: Literal["found", "empty", "unsupported", "limit_exceeded"]
    value_type: str | None = Field(default=None, max_length=128)
    scalar: IfcReadScalar | None = Field(default=None,
        description="Saved scalar; numeric values remain in unit and are not converted to metres. This differs from metre-valued placement.")
    values: tuple[IfcReadScalar, ...] | None = Field(default=None, max_length=128,
        description="Saved list or enumeration values; numeric members remain in unit, without conversion to metres.")
    unit: IfcReadUnit | None = None
    truncated: bool = False
    original_length: int | None = Field(default=None, ge=0)
    diagnostic: IfcReadText | None = None

    @model_validator(mode="after")
    def check_value(self):
        if self.scalar is not None and self.values is not None:
            raise ValueError("property value must be scalar or list")
        if self.status != "found" and (self.scalar is not None or self.values is not None):
            raise ValueError("unresolved property value cannot carry values")
        texts = [value for value in ((self.scalar,) if self.values is None else self.values) if isinstance(value, str)]
        if any(len(value) > 2048 for value in texts):
            raise ValueError("property text exceeds the bounded text length")
        if self.truncated and self.original_length is None:
            raise ValueError("truncated property text requires original_length")
        return self


class IfcReadProperty(IfcProductsReadContract):
    object: IfcReadLocalObject
    name: IfcReadText | None = None
    selector_name: IfcReadExactName | None = None
    value: IfcReadPropertyValue | None = None


class IfcReadPropertySet(IfcProductsReadContract):
    object: IfcReadLocalObject
    global_id: IfcReadGlobalId | None = None
    source: IfcReadSource
    source_type: IfcReadEntity | None = None
    set_kind: Literal["pset", "qto"]
    name: IfcReadText | None = None
    selector_name: IfcReadExactName | None = None
    relationship_guids: tuple[IfcReadGlobalId, ...] = Field(default=(), max_length=128)
    properties: tuple[IfcReadProperty, ...] = Field(default=(), max_length=128)


class IfcPropertiesReadFacts(IfcReadGroupResult):
    description: IfcReadText | None = None
    tag: IfcReadText | None = None
    object_type: IfcReadText | None = None
    predefined_type: IfcReadText | None = None
    types: tuple[IfcReadEntity, ...] = Field(default=(), max_length=128)
    type_relationship_guids: tuple[IfcReadGlobalId, ...] = Field(default=(), max_length=128)
    sets: tuple[IfcReadPropertySet, ...] = Field(default=(), max_length=128)


class IfcReadMaterialMember(IfcProductsReadContract):
    object: IfcReadLocalObject
    name: IfcReadText | None = None
    selector_name: IfcReadExactName | None = None
    material: IfcReadLocalObject | None = None
    material_name: IfcReadText | None = None
    category: IfcReadText | None = None
    thickness_m: float | None = None
    fraction: float | None = None
    profile: IfcReadLocalObject | None = None
    unit: IfcReadUnit | None = None


class IfcReadMaterialAssignment(IfcProductsReadContract):
    relationship: IfcReadEntity
    source: IfcReadSource
    source_type: IfcReadEntity | None = None
    material: IfcReadLocalObject
    name: IfcReadText | None = None
    selector_name: IfcReadExactName | None = None
    kind: Literal["single", "layer_set", "layer_set_usage", "profile_set", "profile_set_usage", "constituent_set", "list", "unsupported"]
    members: tuple[IfcReadMaterialMember, ...] = Field(default=(), max_length=128)
    offset_m: float | None = None
    direction_sense: IfcReadText | None = None


class IfcMaterialsReadFacts(IfcReadGroupResult):
    assignments: tuple[IfcReadMaterialAssignment, ...] = Field(default=(), max_length=128)


class IfcReadSpatialAssignment(IfcProductsReadContract):
    relationship: IfcReadEntity
    kind: Literal["containment", "reference"]
    container: IfcReadEntity
    hierarchy: tuple[IfcReadEntity, ...] = Field(default=(), max_length=128)


class IfcSpatialReadFacts(IfcReadGroupResult):
    assignments: tuple[IfcReadSpatialAssignment, ...] = Field(default=(), max_length=128)


class IfcReadSystemAssignment(IfcProductsReadContract):
    relationship: IfcReadEntity
    group: IfcReadEntity
    service_relationships: tuple[IfcReadRelation, ...] = Field(default=(), max_length=128)


class IfcSystemsReadFacts(IfcReadGroupResult):
    assignments: tuple[IfcReadSystemAssignment, ...] = Field(default=(), max_length=128)


class IfcReadPortConnection(IfcProductsReadContract):
    relationship: IfcReadEntity
    peer_port: IfcReadEntity
    peer_owners: tuple[IfcReadEntity, ...] = Field(default=(), max_length=128)
    realizing_element: IfcReadEntity | None = None


class IfcReadPort(IfcProductsReadContract):
    port: IfcReadEntity
    owners: tuple[IfcReadEntity, ...] = Field(default=(), max_length=128)
    ownership_relationship_guids: tuple[IfcReadGlobalId, ...] = Field(default=(), max_length=128)
    placement: IfcReadPlacement | None = None
    flow_direction: IfcReadText | None = None
    predefined_type: IfcReadText | None = None
    system_type: IfcReadText | None = None
    connections: tuple[IfcReadPortConnection, ...] = Field(default=(), max_length=128)


class IfcPortsReadFacts(IfcReadGroupResult):
    ports: tuple[IfcReadPort, ...] = Field(default=(), max_length=128)


class IfcReadRepresentation(IfcProductsReadContract):
    object: IfcReadLocalObject
    identifier: IfcReadText | None = None
    representation_type: IfcReadText | None = None
    context: IfcReadLocalObject | None = None
    context_identifier: IfcReadText | None = None
    context_type: IfcReadText | None = None
    target_view: IfcReadText | None = None
    item_count: int = Field(ge=0)


class IfcRepresentationsReadFacts(IfcReadGroupResult):
    has_representation: bool
    body_status: Literal["found", "empty", "ambiguous", "unsupported", "limit_exceeded"]
    representations: tuple[IfcReadRepresentation, ...] = Field(default=(), max_length=128)


class IfcProductReadFacts(IfcProductsReadContract):
    global_id: IfcReadGlobalId
    status: Literal["found", "not_found", "ambiguous", "not_product"]
    match_count: int = Field(ge=0)
    basic: IfcProductBasicFacts | None = None
    diagnostic: IfcReadText | None = None
    structure: IfcStructureReadFacts | None = None
    properties: IfcPropertiesReadFacts | None = None
    materials: IfcMaterialsReadFacts | None = None
    spatial: IfcSpatialReadFacts | None = None
    systems: IfcSystemsReadFacts | None = None
    ports: IfcPortsReadFacts | None = None
    representations: IfcRepresentationsReadFacts | None = None

    @model_validator(mode="after")
    def check_location(self):
        if self.status == "found" and (self.match_count != 1 or self.basic is None):
            raise ValueError("found product requires one match and basic facts")
        if self.status == "not_found" and self.match_count != 0:
            raise ValueError("not_found product requires zero matches")
        if self.status == "ambiguous" and self.match_count < 2:
            raise ValueError("ambiguous product requires multiple matches")
        if self.status != "found" and (self.basic is not None or any(getattr(self, name) is not None for name in IfcProductsReadGroups.model_fields)):
            raise ValueError("unresolved product cannot carry product facts")
        return self


class IfcProductsReadRuntimeResult(IfcProductsReadContract):
    products: tuple[IfcProductReadFacts, ...] = Field(max_length=MAX_READ_PRODUCTS)

    @model_validator(mode="after")
    def check_response_bytes(self):
        if len(self.model_dump_json().encode("utf-8")) > MAX_READ_RESPONSE_BYTES:
            raise IfcProductsReadError("IFC_PRODUCTS_READ_LIMIT_EXCEEDED",
                "Product read response exceeds 256 KiB; narrow the selected products or semantic groups, "
                "or use ifc_query_python for a bounded custom projection")
        return self


class IfcApplyProductMatch(IfcProductsReadContract):
    status: Literal["not_checked", "matched", "not_found", "ambiguous", "not_committed", "unknown", "report_unavailable"]
    report_match_count: int | None = Field(default=None, ge=0)
    input_row_index: int | None = Field(default=None, ge=0)
    product_key: str | None = None
    root_global_id: IfcReadGlobalId | None = None
    component_key: str | None = None
    parent_component_key: str | None = None
    roles: tuple[Literal["root", "component"], ...] = Field(default=(), max_length=2)
    solve_status: str | None = Field(default=None, max_length=128)
    build_status: str | None = Field(default=None, max_length=128)
    commit_status: str | None = Field(default=None, max_length=128)


class IfcProductsReadItem(IfcProductsReadContract):
    product_key: str | None = None
    global_id: IfcReadGlobalId | None = None
    apply_match: IfcApplyProductMatch
    current: IfcProductReadFacts | None = None


class IfcProductsReadFacts(IfcProductsReadContract):
    view: Literal["applied", "current"]
    items: tuple[IfcProductsReadItem, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def check_response_bytes(self):
        if self.view == "applied" and any(item.current is not None for item in self.items):
            raise ValueError("applied view cannot contain current IFC facts")
        if len(self.model_dump_json().encode("utf-8")) > MAX_READ_RESPONSE_BYTES:
            raise IfcProductsReadError("IFC_PRODUCTS_READ_LIMIT_EXCEEDED",
                "Product read response exceeds 256 KiB; narrow the selected products or semantic groups, "
                "or use ifc_query_python for a bounded custom projection")
        return self
