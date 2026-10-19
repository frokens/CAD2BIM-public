"""Durable ProductFamily roots, immutable versions, exact bindings, and assets."""

from __future__ import annotations

import unicodedata
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_interface import InterfaceFrameRelation
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.domain.version_usage import validate_version_usage_instructions


_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
PRODUCT_FAMILY_PROGRAM_ENTRY = "main.py:realize"
MAX_PRODUCT_FAMILY_CONTRACT_JSON_BYTES = 65_536
MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS = 320
MAX_PRODUCT_FAMILY_VERSION_ASSETS = 4


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("product family timestamps must be timezone-aware")
    return value


def _clean_text(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(f"{label} contains control characters")
    return normalized


class _ProductFamilyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductFamilyStructureKind(StrEnum):
    SINGLE_PRODUCT = "single_product"
    SEMANTIC_ASSEMBLY = "semantic_assembly"


class ProductFamilyIfcClass(StrEnum):
    """Closed IFC Product classes approved for the first product-family slice."""

    ELEMENT_ASSEMBLY = "IfcElementAssembly"
    PIPE_SEGMENT = "IfcPipeSegment"
    PIPE_FITTING = "IfcPipeFitting"
    VALVE = "IfcValve"
    FLOW_METER = "IfcFlowMeter"
    DISCRETE_ACCESSORY = "IfcDiscreteAccessory"
    DISTRIBUTION_CHAMBER_ELEMENT = "IfcDistributionChamberElement"


class ProductFamilyVersionAssetRole(StrEnum):
    REALIZATION_SOURCE_BUNDLE = "realization_source_bundle"
    VALIDATION_REPORT = "validation_report"
    STANDARD_MODEL_GEOMETRY = "standard_model_geometry"
    STANDARD_MODEL_MANIFEST = "standard_model_manifest"


class ProductFamilyGeometryPurpose(StrEnum):
    PRODUCT_COMPONENT = "product_component"
    SHAPE_FRAGMENT = "shape_fragment"


class ProductFamilyInterfaceKind(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class ProductFamilyNumericUnit(StrEnum):
    DIMENSIONLESS = "dimensionless"
    METRE = "metre"
    RADIAN = "radian"


class ProductFamilyNumberValueContract(_ProductFamilyModel):
    """One numeric value; names and defaults belong to object members."""

    parameter_kind: Literal["number"] = "number"
    unit: ProductFamilyNumericUnit = ProductFamilyNumericUnit.DIMENSIONLESS
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def validate_numeric_domain(self) -> Self:
        values = tuple(
            value for value in (self.minimum, self.maximum) if value is not None
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("product family number parameter values must be finite")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("product family number parameter range is inverted")
        return self


class ProductFamilyIntegerValueContract(_ProductFamilyModel):
    parameter_kind: Literal["integer"] = "integer"
    minimum: int | None = None
    maximum: int | None = None

    @model_validator(mode="after")
    def validate_integer_domain(self) -> Self:
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("product family integer parameter range is inverted")
        return self


class ProductFamilyBooleanValueContract(_ProductFamilyModel):
    parameter_kind: Literal["boolean"] = "boolean"

class ProductFamilyChoiceValueContract(_ProductFamilyModel):
    parameter_kind: Literal["choice"] = "choice"
    choices: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("choices")
    @classmethod
    def validate_choices(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _clean_text(item, label="product family parameter choice") for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("product family parameter choices must be unique")
        return normalized

class ProductFamilyArrayValueContract(_ProductFamilyModel):
    """A homogeneous array whose element is an anonymous value contract."""

    parameter_kind: Literal["array"] = "array"
    min_items: int = Field(ge=0, le=128)
    max_items: int = Field(ge=0, le=128)
    items: ProductFamilyValueContract

    @model_validator(mode="after")
    def validate_array_domain(self) -> Self:
        if self.min_items > self.max_items:
            raise ValueError("product family array parameter range is inverted")
        _validate_structured_parameter(self)
        return self


class ProductFamilyObjectValueContract(_ProductFamilyModel):
    """A closed object whose named fields retain their Family types and units."""

    parameter_kind: Literal["object"] = "object"
    properties: tuple[ProductFamilyPublicParameterContract, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_object_domain(self) -> Self:
        keys = [item.parameter_key for item in self.properties]
        if len(keys) != len(set(keys)):
            raise ValueError("product family object property keys must be unique")
        _validate_structured_parameter(self)
        return self


class ProductFamilyTaggedObjectValueContract(_ProductFamilyModel):
    """Exactly one complete closed object selected by an explicit string tag."""

    parameter_kind: Literal["tagged_object"] = "tagged_object"
    discriminator_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    branches: tuple[ProductFamilyObjectValueContract, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_tagged_object_domain(self) -> Self:
        tags = []
        for branch in self.branches:
            tag = next((item for item in branch.properties
                        if item.parameter_key == self.discriminator_key), None)
            if (not isinstance(tag, ProductFamilyChoiceParameterContract)
                    or len(tag.choices) != 1 or not tag.required or tag.default_value is not None):
                raise ValueError("tagged object branches require an explicit single-choice discriminator")
            tags.append(tag.choices[0])
        if len(tags) != len(set(tags)):
            raise ValueError("tagged object discriminator choices must be unique")
        _validate_structured_parameter(self)
        return self


class ProductFamilyNumberArrayValueContract(ProductFamilyNumberValueContract):
    """Short definition with the same validation semantics as array + number."""

    parameter_kind: Literal["number_array"] = "number_array"
    length: int = Field(ge=0, le=128)

    def validation_view(self) -> ProductFamilyArrayValueContract:
        return ProductFamilyArrayValueContract.model_construct(
            min_items=self.length, max_items=self.length,
            items=ProductFamilyNumberValueContract.model_construct(
                unit=self.unit, minimum=self.minimum, maximum=self.maximum),
        )


class _NamedFamilyParameter(_ProductFamilyModel):
    parameter_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    required: bool = True

    @model_validator(mode="after")
    def validate_member_default(self) -> Self:
        if self.default_value is not None:
            _normalize_family_parameter(self, self.default_value, path=self.parameter_key)
        return self


class ProductFamilyNumberParameterContract(ProductFamilyNumberValueContract, _NamedFamilyParameter):
    default_value: float | None = None


class ProductFamilyIntegerParameterContract(ProductFamilyIntegerValueContract, _NamedFamilyParameter):
    default_value: int | None = None


class ProductFamilyBooleanParameterContract(ProductFamilyBooleanValueContract, _NamedFamilyParameter):
    default_value: bool | None = None


class ProductFamilyChoiceParameterContract(ProductFamilyChoiceValueContract, _NamedFamilyParameter):
    default_value: str | None = Field(default=None, min_length=1, max_length=64)


class ProductFamilyArrayParameterContract(ProductFamilyArrayValueContract, _NamedFamilyParameter):
    default_value: list[JsonValue] | None = None


class ProductFamilyObjectParameterContract(ProductFamilyObjectValueContract, _NamedFamilyParameter):
    default_value: dict[str, JsonValue] | None = None


class ProductFamilyTaggedObjectParameterContract(ProductFamilyTaggedObjectValueContract, _NamedFamilyParameter):
    default_value: dict[str, JsonValue] | None = None


class ProductFamilyNumberArrayParameterContract(ProductFamilyNumberArrayValueContract, _NamedFamilyParameter):
    default_value: list[JsonValue] | None = None


ProductFamilyValueContract = Annotated[
    ProductFamilyNumberValueContract | ProductFamilyIntegerValueContract
    | ProductFamilyBooleanValueContract | ProductFamilyChoiceValueContract
    | ProductFamilyArrayValueContract | ProductFamilyObjectValueContract
    | ProductFamilyTaggedObjectValueContract | ProductFamilyNumberArrayValueContract,
    Field(discriminator="parameter_kind"),
]


ProductFamilyPublicParameterContract = Annotated[
    ProductFamilyNumberParameterContract
    | ProductFamilyIntegerParameterContract
    | ProductFamilyBooleanParameterContract
    | ProductFamilyChoiceParameterContract
    | ProductFamilyArrayParameterContract
    | ProductFamilyObjectParameterContract
    | ProductFamilyTaggedObjectParameterContract
    | ProductFamilyNumberArrayParameterContract,
    Field(discriminator="parameter_kind"),
]


ProductFamilyArrayParameterContract.model_rebuild()
ProductFamilyObjectParameterContract.model_rebuild()
ProductFamilyTaggedObjectParameterContract.model_rebuild()
ProductFamilyArrayValueContract.model_rebuild()
ProductFamilyObjectValueContract.model_rebuild()
ProductFamilyTaggedObjectValueContract.model_rebuild()


def _validate_parameter_tree(parameters: tuple[ProductFamilyPublicParameterContract, ...]) -> None:
    count = 0

    def visit(parameter: ProductFamilyPublicParameterContract, depth: int) -> None:
        nonlocal count
        count += 1
        if depth > 4 or count > 256:
            raise ValueError("product family parameter tree exceeds depth 4 or 256 nodes")
        if isinstance(parameter, ProductFamilyNumberArrayValueContract):
            visit(parameter.validation_view().items, depth + 1)
        elif isinstance(parameter, ProductFamilyArrayValueContract):
            visit(parameter.items, depth + 1)
        elif isinstance(parameter, ProductFamilyObjectValueContract):
            for child in parameter.properties:
                visit(child, depth + 1)
        elif isinstance(parameter, ProductFamilyTaggedObjectValueContract):
            for branch in parameter.branches:
                visit(branch, depth)

    for parameter in parameters:
        visit(parameter, 1)


def _validate_structured_parameter(parameter: ProductFamilyPublicParameterContract) -> None:
    _validate_parameter_tree((parameter,))
    if getattr(parameter, "default_value", None) is not None:
        _normalize_family_parameter(parameter, parameter.default_value, path=parameter.parameter_key)


class ProductFamilyCircularInterfaceProfileContract(_ProductFamilyModel):
    """Circular interface size fixed by a value or one public length parameter."""

    profile_kind: Literal["circular"] = "circular"
    outer_diameter_m: float | None = Field(default=None, gt=0.0, le=1000.0)
    outer_diameter_parameter_key: str | None = Field(
        default=None,
        pattern=_IDENTIFIER_PATTERN,
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_diameter_source(self) -> Self:
        if (self.outer_diameter_m is None) == (self.outer_diameter_parameter_key is None):
            raise ValueError("circular interface profile requires one exact diameter source")
        if self.outer_diameter_m is not None and not math.isfinite(self.outer_diameter_m):
            raise ValueError("circular interface diameter must be finite")
        return self


class ProductFamilyComponentContract(_ProductFamilyModel):
    """One IFC product occurrence planned by a family realization."""

    component_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    ifc_class: ProductFamilyIfcClass
    parent_component_key: str | None = Field(
        default=None,
        pattern=_IDENTIFIER_PATTERN,
        max_length=64,
    )
    local_transform_mode: Literal["fixed", "program"] = "fixed"
    fixed_local_transform: RigidTransform3D | None = None

    @model_validator(mode="after")
    def validate_transform_mode(self) -> Self:
        if self.local_transform_mode == "program" and self.fixed_local_transform is not None:
            raise ValueError("program component cannot also declare a fixed transform")
        return self

    @model_serializer(mode="wrap")
    def serialize_optional_transform(self, handler):
        data = handler(self)
        if self.local_transform_mode == "fixed":
            data.pop("local_transform_mode", None)
        if self.fixed_local_transform is None:
            data.pop("fixed_local_transform", None)
        return data


class ProductFamilyFixedParameterSource(_ProductFamilyModel):
    source_kind: Literal["fixed"] = "fixed"
    value: JsonValue

    @field_validator("value")
    @classmethod
    def require_bounded_value(cls, value):
        if len(canonical_json(value).encode("utf-8")) > 16 * 1024:
            raise ValueError("fixed geometry parameter exceeds 16 KiB")
        return value


class ProductFamilyPublicParameterSource(_ProductFamilyModel):
    source_kind: Literal["public_parameter"] = "public_parameter"
    parameter_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)


class ProductFamilySolvedParameterSource(_ProductFamilyModel):
    source_kind: Literal["solved_parameter"] = "solved_parameter"
    parameter_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)


ProductFamilyParameterSource = Annotated[
    ProductFamilyFixedParameterSource | ProductFamilyPublicParameterSource | ProductFamilySolvedParameterSource,
    Field(discriminator="source_kind"),
]


class ProductFamilyParameterSources(_ProductFamilyModel):
    """Input shorthand frozen to complete overrides before saving a definition."""

    default_source: Literal["public_parameter", "solved_parameter"] | None = None
    overrides: dict[str, ProductFamilyParameterSource] = Field(default_factory=dict, max_length=64)

    @model_serializer(mode="wrap")
    def serialize_sources(self, handler):
        data = handler(self)
        if self.default_source is None:
            data.pop("default_source", None)
        return data


class ProductFamilyFragmentBindingOption(_ProductFamilyModel):
    """One exact Geometry choice for a Family-owned fragment."""

    binding_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    parameter_sources: ProductFamilyParameterSources
    uniform_scale_source: ProductFamilyParameterSource = Field(
        default_factory=lambda: ProductFamilyFixedParameterSource(value=1.0),
    )


class ProductFamilyGeometryFragmentContract(_ProductFamilyModel):
    """One placed fragment with a finite set of exact Geometry choices."""

    fragment_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    owner_component_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    binding_options: tuple[ProductFamilyFragmentBindingOption, ...] = Field(min_length=1, max_length=16)
    local_transform_mode: Literal["fixed", "program"] = "fixed"
    fixed_local_transform: RigidTransform3D | None = None

    @model_validator(mode="after")
    def validate_transform_mode(self) -> Self:
        if self.local_transform_mode == "program" and self.fixed_local_transform is not None:
            raise ValueError("program fragment cannot also declare a fixed transform")
        keys = tuple(option.binding_key for option in self.binding_options)
        if len(keys) != len(set(keys)):
            raise ValueError("fragment binding option keys must be unique")
        return self

    @model_serializer(mode="wrap")
    def serialize_optional_transform_mode(self, handler):
        data = handler(self)
        if self.local_transform_mode == "fixed":
            data.pop("local_transform_mode", None)
        if self.fixed_local_transform is None:
            data.pop("fixed_local_transform", None)
        return data


class ProductFamilyModelInterfaceSource(_ProductFamilyModel):
    source_kind: Literal["model_interface"] = "model_interface"
    fragment_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    model_interface_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)


class ProductFamilyExternalConstraintContract(_ProductFamilyModel):
    """The immutable lower bound on caller-supplied external requirements."""

    required: bool = Field(
        default=True,
        description=(
            "When true, including the default when omitted, every self_geometry invocation must "
            "supply a condition for this interface. False permits omission; any condition that is "
            "supplied is still independently checked by the Backend."
        ),
    )
    allowed_kinds: tuple[Literal["finite_segment", "fixed_point", "infinite_axis"], ...] = Field(min_length=1, max_length=3)
    allowed_directions: tuple[Literal["none", "same", "opposed"], ...] = Field(min_length=1, max_length=3)
    roll_required: bool = False
    profile_required: bool = Field(default=False,
        description="True requires a circular OD target. False permits omission; any supplied OD is still compared to the bound output interface profile.")
    maximum_position_tolerance_m: float = Field(gt=0, le=1, allow_inf_nan=False)
    maximum_angular_tolerance_rad: float = Field(gt=0, le=math.pi, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_consistent_choices(self) -> Self:
        if len(set(self.allowed_kinds)) != len(self.allowed_kinds) or len(set(self.allowed_directions)) != len(self.allowed_directions):
            raise ValueError("external constraint modes must be unique")
        if self.roll_required and "none" in self.allowed_directions:
            raise ValueError("a required roll cannot permit unconstrained direction")
        return self


class ProductFamilyInterfaceContract(_ProductFamilyModel):
    """A Family interface backed by the same-build Geometry interface receipt."""

    interface_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    owner_component_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    interface_kind: ProductFamilyInterfaceKind
    source: ProductFamilyModelInterfaceSource
    external_constraint: ProductFamilyExternalConstraintContract | None = None
    connection_profile: ProductFamilyCircularInterfaceProfileContract | None = None


class ProductFamilyInterfaceCoincidenceConstraintContract(_ProductFamilyModel):
    constraint_kind: Literal["interface_coincidence"] = "interface_coincidence"
    constraint_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    interface_a_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    interface_b_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    position_tolerance_m: float = Field(gt=0.0, le=1.0)
    angular_tolerance_rad: float = Field(gt=0.0, le=math.pi)
    frame_relation: InterfaceFrameRelation

    @field_validator("position_tolerance_m", "angular_tolerance_rad")
    @classmethod
    def validate_finite_tolerance(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("product family constraint tolerance must be finite")
        return value


ProductFamilyConstraintContract = ProductFamilyInterfaceCoincidenceConstraintContract


class ProductFamilyStandardModelContract(_ProductFamilyModel):
    """Formal standard construction capability, with no validation selector."""

    public_parameters: dict[str, JsonValue] = Field(default_factory=dict,
        description="Standard product parameter values. Omitted values use this Family's declared defaults; other required values must be supplied.")

    @model_serializer(mode="wrap")
    def serialize_parameters(self, handler):
        data = handler(self)
        if not data.get("public_parameters"):
            data.pop("public_parameters", None)
        return data


class ProductFamilyContract(_ProductFamilyModel):
    """Closed structural contract persisted by one immutable family version."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    product_structure_kind: ProductFamilyStructureKind
    standard_model: ProductFamilyStandardModelContract | None = None
    components: tuple[ProductFamilyComponentContract, ...] = Field(min_length=1, max_length=128)
    geometry_fragments: tuple[ProductFamilyGeometryFragmentContract, ...] = Field(
        min_length=1,
        max_length=256,
    )
    interfaces: tuple[ProductFamilyInterfaceContract, ...] = Field(default=(), max_length=64)
    public_parameters: tuple[ProductFamilyPublicParameterContract, ...] = Field(
        default=(),
        max_length=64,
    )
    solved_parameters: tuple[ProductFamilyPublicParameterContract, ...] = Field(default=(), max_length=64)
    constraints: tuple[ProductFamilyConstraintContract, ...] = Field(
        default=(),
        max_length=128,
    )

    @model_serializer(mode="wrap")
    def serialize_optional_fields(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, object]:
        data = handler(self)
        assert isinstance(data, dict)
        for key in ("public_parameters", "constraints", "solved_parameters"):
            if not data.get(key):
                data.pop(key, None)
        if self.standard_model is None:
            data.pop("standard_model", None)
        return data

    @model_validator(mode="after")
    def validate_reference_closure(self) -> Self:
        _validate_self_geometry_contract(self)
        component_keys = tuple(component.component_key for component in self.components)
        if len(component_keys) != len(set(component_keys)):
            raise ValueError("product family component keys must be unique")
        interface_keys = tuple(interface.interface_key for interface in self.interfaces)
        if len(interface_keys) != len(set(interface_keys)):
            raise ValueError("product family interface keys must be unique")
        fragment_keys = tuple(fragment.fragment_key for fragment in self.geometry_fragments)
        if len(fragment_keys) != len(set(fragment_keys)):
            raise ValueError("product family geometry fragment keys must be unique")
        fragments = {fragment.fragment_key: fragment for fragment in self.geometry_fragments}
        parameter_keys = tuple(parameter.parameter_key for parameter in self.public_parameters)
        if len(parameter_keys) != len(set(parameter_keys)):
            raise ValueError("product family public parameter keys must be unique")
        constraint_keys = tuple(constraint.constraint_key for constraint in self.constraints)
        if len(constraint_keys) != len(set(constraint_keys)):
            raise ValueError("product family constraint keys must be unique")

        components = {component.component_key: component for component in self.components}
        roots = tuple(
            component.component_key
            for component in self.components
            if component.parent_component_key is None
        )
        if len(roots) != 1:
            raise ValueError("product family component graph requires exactly one root")
        if self.product_structure_kind is ProductFamilyStructureKind.SINGLE_PRODUCT:
            if len(self.components) != 1:
                raise ValueError("single_product family requires exactly one component")
            if components[roots[0]].ifc_class is ProductFamilyIfcClass.ELEMENT_ASSEMBLY:
                raise ValueError("single_product family cannot use IfcElementAssembly")
        else:
            if len(self.components) < 2:
                raise ValueError("semantic_assembly family requires at least two components")

        root = roots[0]
        if components[root].local_transform_mode != "fixed":
            raise ValueError("product family root component requires a fixed local transform")
        if components[root].fixed_local_transform not in (None, RigidTransform3D()):
            raise ValueError("product family root component local transform must be identity")
        for component in self.components:
            parent = component.parent_component_key
            if parent is not None and parent not in components:
                raise ValueError("product family component parent reference is missing")
            if parent == component.component_key:
                raise ValueError("product family component cannot parent itself")
            visited = {component.component_key}
            current = component
            while current.parent_component_key is not None:
                parent_key = current.parent_component_key
                if parent_key in visited:
                    raise ValueError("product family component graph contains a cycle")
                visited.add(parent_key)
                current = components[parent_key]
            if current.component_key != root:
                raise ValueError("product family component is not reachable from the root")

        for interface in self.interfaces:
            if interface.owner_component_key not in components:
                raise ValueError("product family interface owner reference is missing")
            if isinstance(interface.source, ProductFamilyModelInterfaceSource):
                fragment = fragments.get(interface.source.fragment_key)
                if fragment is None:
                    raise ValueError("model interface source fragment reference is missing")
                if fragment.owner_component_key != interface.owner_component_key:
                    raise ValueError("model interface source owner disagrees with its fragment")
            profile = interface.connection_profile
            if profile is not None and profile.outer_diameter_parameter_key is not None:
                parameter = next(
                    (
                        item
                        for item in self.public_parameters
                        if item.parameter_key == profile.outer_diameter_parameter_key
                    ),
                    None,
                )
                if not isinstance(parameter, ProductFamilyNumberParameterContract):
                    raise ValueError("interface diameter must reference a public number parameter")
                if parameter.unit is not ProductFamilyNumericUnit.METRE:
                    raise ValueError("interface diameter parameter must use metre units")

        interfaces = {interface.interface_key: interface for interface in self.interfaces}
        for constraint in self.constraints:
            if constraint.interface_a_key == constraint.interface_b_key:
                raise ValueError("interface coincidence constraint requires distinct interfaces")
            selected = (interfaces.get(constraint.interface_a_key), interfaces.get(constraint.interface_b_key))
            if any(interface is None for interface in selected):
                raise ValueError("product family constraint interface reference is missing")
            if any(interface.interface_kind is not ProductFamilyInterfaceKind.INTERNAL for interface in selected):
                raise ValueError("interface coincidence constraints require internal interfaces")
        fragments_by_owner: dict[str, int] = {}
        for fragment in self.geometry_fragments:
            if fragment.owner_component_key not in components:
                raise ValueError("product family geometry fragment owner reference is missing")
            fragments_by_owner[fragment.owner_component_key] = (
                fragments_by_owner.get(fragment.owner_component_key, 0) + 1
            )
        for component in self.components:
            if (
                component.component_key != root
                and component.component_key not in fragments_by_owner
            ):
                raise ValueError("a non-root product family component requires geometry")
        if (
            components[root].ifc_class is not ProductFamilyIfcClass.ELEMENT_ASSEMBLY
            and root not in fragments_by_owner
        ):
            raise ValueError("a physical product family root requires geometry")
        if len(canonical_json(self).encode("utf-8")) > MAX_PRODUCT_FAMILY_CONTRACT_JSON_BYTES:
            raise ValueError("product family contract exceeds its canonical JSON byte budget")
        return self


def _validate_self_geometry_contract(contract: ProductFamilyContract) -> None:
    if any(item.external_constraint is not None and item.interface_kind != ProductFamilyInterfaceKind.EXTERNAL
           for item in contract.interfaces):
        raise ValueError("only exposed external interfaces may declare external requirements")
    public = {item.parameter_key: item for item in contract.public_parameters}
    solved = {item.parameter_key: item for item in contract.solved_parameters}
    _validate_parameter_tree(contract.public_parameters + contract.solved_parameters)
    if len(solved) != len(contract.solved_parameters):
        raise ValueError("solved parameter keys must be unique")

    unbounded_paths: list[str] = []

    def require_bounded(parameter, path):
        if isinstance(parameter, (ProductFamilyNumberValueContract, ProductFamilyIntegerValueContract)):
            if parameter.minimum is None or parameter.maximum is None:
                unbounded_paths.append(path)
        elif isinstance(parameter, ProductFamilyArrayValueContract):
            require_bounded(parameter.items, f"{path}.items")
        elif isinstance(parameter, ProductFamilyObjectValueContract):
            for child in parameter.properties:
                require_bounded(child, f"{path}.{child.parameter_key}")
        elif isinstance(parameter, ProductFamilyTaggedObjectValueContract):
            for index, branch in enumerate(parameter.branches):
                require_bounded(branch, f"{path}.branches[{index}]")

    def require_optional_defaults(parameter, *, allow_absent: bool = False):
        if (not allow_absent and isinstance(parameter, _NamedFamilyParameter)
                and not parameter.required and parameter.default_value is None):
            raise ValueError("optional public or nested Family parameters require a default")
        if isinstance(parameter, ProductFamilyArrayValueContract):
            require_optional_defaults(parameter.items)
        elif isinstance(parameter, ProductFamilyObjectValueContract):
            for child in parameter.properties:
                require_optional_defaults(child)
        elif isinstance(parameter, ProductFamilyTaggedObjectValueContract):
            for branch in parameter.branches:
                require_optional_defaults(branch)

    for parameter in contract.public_parameters:
        require_optional_defaults(parameter)
    for parameter in contract.solved_parameters:
        require_optional_defaults(parameter, allow_absent=True)
        require_bounded(parameter, f"solved_parameters.{parameter.parameter_key}")
    if unbounded_paths:
        raise ValueError(
            "solved numeric parameters require explicit minimum and maximum; "
            f"affected_count={len(unbounded_paths)}; paths={', '.join(unbounded_paths)}"
        )
    binding_keys = {
        option.binding_key
        for fragment in contract.geometry_fragments
        for option in fragment.binding_options
    }
    if len(binding_keys) > MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS:
        raise ValueError("product family geometry binding choices exceed the exact binding budget")
    for fragment in contract.geometry_fragments:
        for option in fragment.binding_options:
            sources = [(name, source, False) for name, source in option.parameter_sources.overrides.items()]
            sources.append(("uniform_scale", option.uniform_scale_source, True))
            for name, source, is_scale in sources:
                if not name or len(name) > 64 or not name[0].isalpha() or not all(c.isascii() and (c.isalnum() or c == "_") for c in name):
                    raise ValueError("fragment parameter source names must be safe identifiers")
                if isinstance(source, ProductFamilyPublicParameterSource):
                    parameter = public.get(source.parameter_key)
                elif isinstance(source, ProductFamilySolvedParameterSource):
                    parameter = solved.get(source.parameter_key)
                else:
                    parameter = None
                if not isinstance(source, ProductFamilyFixedParameterSource) and parameter is None:
                    raise ValueError(
                        f"fragment {fragment.fragment_key!r} binding {option.binding_key!r} "
                        f"parameter source {name!r} references undeclared {source.source_kind} key {source.parameter_key!r}"
                    )
                if is_scale:
                    if isinstance(source, ProductFamilyFixedParameterSource):
                        value = source.value
                        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                            raise ValueError("fixed uniform scale must be a finite positive number")
                    elif (not isinstance(parameter, ProductFamilyNumberParameterContract)
                          or parameter.unit != ProductFamilyNumericUnit.DIMENSIONLESS
                          or parameter.minimum is None or parameter.minimum <= 0 or parameter.maximum is None):
                        raise ValueError("variable uniform scale requires a bounded positive dimensionless number")


def standard_product_family_structure(contract: ProductFamilyContract) -> ProductFamilyContract:
    """Use the complete immutable Family for standard construction."""
    if contract.standard_model is None:
        raise ValueError("this exact Family version has no validated standard model contract")
    return contract


def normalize_product_family_public_parameters(
    contract: ProductFamilyContract,
    values: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Validate one parameter set against the sole typed schema and apply defaults."""

    return normalize_product_family_parameter_values(contract.public_parameters, values)


def normalize_product_family_parameter_values(
    parameters: tuple[ProductFamilyPublicParameterContract, ...],
    values: Mapping[str, JsonValue],
    *,
    _path: str = "",
) -> dict[str, JsonValue]:
    """Normalize a closed set of Family-owned public, solved or object fields."""

    _validate_parameter_tree(parameters)
    parameter_by_key = {parameter.parameter_key: parameter for parameter in parameters}
    unknown_keys = values.keys() - parameter_by_key.keys()
    if unknown_keys:
        paths = ", ".join(f"{_path}.{key}" if _path else key for key in sorted(unknown_keys))
        raise ValueError(f"product family parameters contain an unknown key: {paths}")
    missing_keys = [
        f"{_path}.{key}" if _path else key
        for key, parameter in parameter_by_key.items()
        if key not in values and parameter.required
    ]
    if missing_keys:
        raise ValueError(
            "product family parameters are missing required keys; "
            f"affected_count={len(missing_keys)}; paths={', '.join(missing_keys)}"
        )
    normalized: dict[str, JsonValue] = {}
    for key, parameter in parameter_by_key.items():
        if key not in values:
            if parameter.default_value is None:
                continue
            value = parameter.default_value
        else:
            value = values[key]
        normalized[key] = _normalize_family_parameter(
            parameter, value, path=f"{_path}.{key}" if _path else key)
    if len(canonical_json(normalized).encode("utf-8")) > 65_536:
        raise ValueError("product family parameters exceed their 64 KiB byte budget")
    return normalized


def _normalize_family_parameter(
    parameter: ProductFamilyPublicParameterContract,
    value: JsonValue,
    *,
    path: str,
) -> JsonValue:
    try:
        if isinstance(parameter, ProductFamilyNumberArrayValueContract):
            return _normalize_family_parameter(parameter.validation_view(), value, path=path)
        if isinstance(parameter, ProductFamilyNumberValueContract):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("product family number parameter has the wrong type")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("product family number parameter must be finite")
            if parameter.minimum is not None and number < parameter.minimum:
                raise ValueError("product family number parameter is below minimum")
            if parameter.maximum is not None and number > parameter.maximum:
                raise ValueError("product family number parameter is above maximum")
            return number
        if isinstance(parameter, ProductFamilyIntegerValueContract):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("product family integer parameter has the wrong type")
            if parameter.minimum is not None and value < parameter.minimum:
                raise ValueError("product family integer parameter is below minimum")
            if parameter.maximum is not None and value > parameter.maximum:
                raise ValueError("product family integer parameter is above maximum")
            return value
        if isinstance(parameter, ProductFamilyBooleanValueContract):
            if not isinstance(value, bool):
                raise ValueError("product family boolean parameter has the wrong type")
            return value
        if isinstance(parameter, ProductFamilyChoiceValueContract):
            if not isinstance(value, str) or value not in parameter.choices:
                raise ValueError("product family choice parameter is not allowed")
            return value
        if isinstance(parameter, ProductFamilyArrayValueContract):
            if not isinstance(value, list):
                raise ValueError("product family array parameter has the wrong type")
            if not parameter.min_items <= len(value) <= parameter.max_items:
                raise ValueError("product family array parameter length is outside its range")
            return [_normalize_family_parameter(parameter.items, item, path=f"{path}[{index}]")
                    for index, item in enumerate(value)]
        if not isinstance(value, dict):
            raise ValueError("product family object parameter has the wrong type")
        if isinstance(parameter, ProductFamilyTaggedObjectValueContract):
            tag = value.get(parameter.discriminator_key)
            for branch in parameter.branches:
                discriminator = next(item for item in branch.properties
                                     if item.parameter_key == parameter.discriminator_key)
                if tag == discriminator.choices[0]:
                    return normalize_product_family_parameter_values(branch.properties, value, _path=path)
            raise ValueError("product family tagged object requires a declared discriminator choice")
        return normalize_product_family_parameter_values(parameter.properties, value, _path=path)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


class ProductFamilyRecord(_ProductFamilyModel):
    """Stable identity and owner only; the root has no CURRENT or mutable metadata."""

    family_id: str = Field(min_length=1, max_length=80)
    owner_scope: ResourceOwnerScope
    created_at: datetime

    _validate_created_at = field_validator("created_at")(_require_aware)


class ProductFamilyVersionRecord(_ProductFamilyModel):
    """One immutable published ProductFamily contract snapshot."""

    family_version_id: str = Field(min_length=1, max_length=80)
    family_id: str = Field(min_length=1, max_length=80)
    version_no: int = Field(ge=1)
    parent_family_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    usage_instructions: str | None = None
    family_contract: ProductFamilyContract
    contract_digest: str = Field(pattern=_SHA256_PATTERN)
    content_digest: str = Field(pattern=_SHA256_PATTERN)
    program_entry: str = Field(default=PRODUCT_FAMILY_PROGRAM_ENTRY)
    created_at: datetime

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return _clean_text(value, label="product family display_name")

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, value: str) -> str:
        return _clean_text(value, label="product family purpose")

    @field_validator("usage_instructions")
    @classmethod
    def validate_usage_instructions(cls, value: str | None) -> str | None:
        return validate_version_usage_instructions(value, required=False)

    _validate_created_at = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def validate_version_identity(self) -> Self:
        if self.parent_family_version_id == self.family_version_id:
            raise ValueError("a product family version cannot parent itself")
        if self.program_entry != PRODUCT_FAMILY_PROGRAM_ENTRY:
            raise ValueError("product family program entry must be main.py:realize")
        if self.contract_digest != sha256_digest(self.family_contract):
            raise ValueError("contract_digest does not match the canonical family contract")
        return self

    @property
    def product_structure_kind(self) -> ProductFamilyStructureKind:
        return self.family_contract.product_structure_kind


class ProductFamilyGeometryBindingRecord(_ProductFamilyModel):
    """One family-local key bound to an exact immutable geometry version pair."""

    family_version_id: str = Field(min_length=1, max_length=80)
    binding_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    geometry_resource_id: str = Field(min_length=1, max_length=80)
    geometry_resource_version_id: str = Field(min_length=1, max_length=80)
    geometry_content_digest: str = Field(pattern=_SHA256_PATTERN)
    expected_resource_kind: GeometryResourceKind
    geometry_purpose: ProductFamilyGeometryPurpose


class ProductFamilyVersionAssetRecord(_ProductFamilyModel):
    """Exact immutable byte metadata owned by one ProductFamilyVersion."""

    family_version_id: str = Field(min_length=1, max_length=80)
    asset_role: ProductFamilyVersionAssetRole
    asset_format: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    created_at: datetime

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError("product family version asset filename must not contain a path")
        return _clean_text(value, label="product family version asset filename")

    _validate_created_at = field_validator("created_at")(_require_aware)


def product_family_content_digest(
    *,
    contract_digest: str,
    program_entry: str,
    geometry_bindings: Sequence[ProductFamilyGeometryBindingRecord],
    assets: Sequence[ProductFamilyVersionAssetRecord],
    usage_instructions: str | None = None,
) -> str:
    """Digest executable family content, excluding root identity and latest projections."""

    content = {
            "domain": "product_family_version_content",
            "contract_digest": contract_digest,
            "program_entry": program_entry,
            "geometry_bindings": [
                {
                    "binding_key": item.binding_key,
                    "geometry_resource_id": item.geometry_resource_id,
                    "geometry_resource_version_id": item.geometry_resource_version_id,
                    "geometry_content_digest": item.geometry_content_digest,
                    "expected_resource_kind": item.expected_resource_kind.value,
                    "geometry_purpose": item.geometry_purpose,
                }
                for item in sorted(geometry_bindings, key=lambda value: value.binding_key)
            ],
            "assets": [
                {
                    "asset_role": item.asset_role.value,
                    "asset_format": item.asset_format,
                    "filename": item.filename,
                    "content_type": item.content_type,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in sorted(assets, key=lambda value: value.asset_role.value)
            ],
        }
    if usage_instructions is not None:
        content["usage_instructions"] = usage_instructions
    return sha256_digest(content)


class ProductFamilyVersionBundle(_ProductFamilyModel):
    """Complete ready-only repository read unit for one exact family version."""

    family: ProductFamilyRecord
    version: ProductFamilyVersionRecord
    geometry_bindings: tuple[ProductFamilyGeometryBindingRecord, ...] = Field(
        min_length=1,
        max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    )
    assets: tuple[ProductFamilyVersionAssetRecord, ...] = Field(
        min_length=1,
        max_length=MAX_PRODUCT_FAMILY_VERSION_ASSETS,
    )

    @model_validator(mode="after")
    def validate_complete_version(self) -> Self:
        if self.version.family_id != self.family.family_id:
            raise ValueError("product family version belongs to a different root")
        if any(
            binding.family_version_id != self.version.family_version_id
            for binding in self.geometry_bindings
        ):
            raise ValueError("product family geometry binding belongs to another version")
        if any(asset.family_version_id != self.version.family_version_id for asset in self.assets):
            raise ValueError("product family asset belongs to another version")
        keys = tuple(binding.binding_key for binding in self.geometry_bindings)
        if len(keys) != len(set(keys)):
            raise ValueError("product family geometry binding keys must be unique")
        roles = tuple(asset.asset_role for asset in self.assets)
        if len(roles) != len(set(roles)):
            raise ValueError("product family version asset roles must be unique")
        if roles.count(ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE) != 1:
            raise ValueError(
                "product family version requires exactly one realization source bundle"
            )
        source = next(
            asset
            for asset in self.assets
            if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE
        )
        if source.asset_format != "source_bundle_v1" or source.content_type != "application/zip":
            raise ValueError("realization source bundle must use canonical source_bundle_v1 ZIP")
        standard_roles = {ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY, ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST}
        if self.version.family_contract.standard_model is not None:
            if not standard_roles.issubset(roles):
                raise ValueError("standard Family version requires complete geometry and manifest assets")
            geometry_asset = next(asset for asset in self.assets if asset.asset_role is ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY)
            if geometry_asset.asset_format != "occt_brep_v1" or geometry_asset.content_type != "application/vnd.opencascade.brep":
                raise ValueError("standard model geometry must be canonical OCCT BREP")
            manifest_asset = next(asset for asset in self.assets if asset.asset_role is ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST)
            if manifest_asset.asset_format != "json" or manifest_asset.content_type != "application/json":
                raise ValueError("standard model manifest must be canonical JSON")
        elif standard_roles.intersection(roles):
            raise ValueError("Family cannot contain standard assets without their contract")

        binding_by_key = {binding.binding_key: binding for binding in self.geometry_bindings}
        component_binding_keys = {
            option.binding_key
            for fragment in self.version.family_contract.geometry_fragments
            for option in fragment.binding_options
        }
        referenced_binding_keys = component_binding_keys
        missing_binding_keys = referenced_binding_keys - binding_by_key.keys()
        if missing_binding_keys:
            raise ValueError("product family contract references a missing geometry binding")
        unused_binding_keys = binding_by_key.keys() - referenced_binding_keys
        if unused_binding_keys:
            raise ValueError("product family version contains an unused geometry binding")
        if any(
            binding_by_key[key].geometry_purpose
            not in {
                ProductFamilyGeometryPurpose.PRODUCT_COMPONENT,
                ProductFamilyGeometryPurpose.SHAPE_FRAGMENT,
            }
            for key in component_binding_keys
        ):
            raise ValueError("component geometry binding has an incompatible purpose")
        expected_digest = product_family_content_digest(
            contract_digest=self.version.contract_digest,
            program_entry=self.version.program_entry,
            geometry_bindings=self.geometry_bindings,
            assets=self.assets,
            usage_instructions=self.version.usage_instructions,
        )
        if self.version.content_digest != expected_digest:
            raise ValueError("content_digest does not match the exact product family version")
        return self


class ProductFamilyListEntry(_ProductFamilyModel):
    """One root and the exact version selected as latest by the same catalog query."""

    family: ProductFamilyRecord
    latest_version: ProductFamilyVersionBundle

    @model_validator(mode="after")
    def validate_latest_owner(self) -> Self:
        if self.latest_version.family != self.family:
            raise ValueError("product family list entry latest version belongs to another root")
        return self


class ProductFamilyListQuery(_ProductFamilyModel):
    owner_scopes: tuple[ResourceOwnerScopeKind, ...] = ()
    product_structure_kinds: tuple[ProductFamilyStructureKind, ...] = ()
    purpose_contains: str | None = Field(default=None, min_length=1, max_length=128)
    before_created_at: datetime | None = None
    before_family_id: str | None = Field(default=None, min_length=1, max_length=80)
    page_size: int = Field(default=50, ge=1, le=100)

    @field_validator("purpose_contains")
    @classmethod
    def normalize_purpose_filter(cls, value: str | None) -> str | None:
        return None if value is None else _clean_text(value, label="purpose filter")

    @field_validator("before_created_at")
    @classmethod
    def require_aware_boundary(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if len(self.owner_scopes) != len(set(self.owner_scopes)):
            raise ValueError("product family owner scope filters must be unique")
        if len(self.product_structure_kinds) != len(set(self.product_structure_kinds)):
            raise ValueError("product family structure filters must be unique")
        if (self.before_created_at is None) != (self.before_family_id is None):
            raise ValueError("product family list boundary is incomplete")
        return self


class ProductFamilyVersionListQuery(_ProductFamilyModel):
    family_id: str = Field(min_length=1, max_length=80)
    before_version_no: int | None = Field(default=None, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


__all__ = [
    "MAX_PRODUCT_FAMILY_CONTRACT_JSON_BYTES",
    "MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS",
    "MAX_PRODUCT_FAMILY_VERSION_ASSETS",
    "PRODUCT_FAMILY_PROGRAM_ENTRY",
    "ProductFamilyBooleanParameterContract",
    "ProductFamilyChoiceParameterContract",
    "ProductFamilyCircularInterfaceProfileContract",
    "ProductFamilyComponentContract",
    "ProductFamilyContract",
    "ProductFamilyConstraintContract",
    "ProductFamilyGeometryBindingRecord",
    "ProductFamilyFragmentBindingOption",
    "ProductFamilyGeometryFragmentContract",
    "ProductFamilyGeometryPurpose",
    "ProductFamilyIfcClass",
    "ProductFamilyInterfaceContract",
    "ProductFamilyInterfaceCoincidenceConstraintContract",
    "ProductFamilyInterfaceKind",
    "ProductFamilyIntegerParameterContract",
    "ProductFamilyListEntry",
    "ProductFamilyListQuery",
    "ProductFamilyNumberParameterContract",
    "ProductFamilyNumberArrayParameterContract",
    "ProductFamilyNumberValueContract",
    "ProductFamilyIntegerValueContract",
    "ProductFamilyBooleanValueContract",
    "ProductFamilyChoiceValueContract",
    "ProductFamilyArrayValueContract",
    "ProductFamilyObjectValueContract",
    "ProductFamilyTaggedObjectValueContract",
    "ProductFamilyNumberArrayValueContract",
    "ProductFamilyValueContract",
    "ProductFamilyParameterSources",
    "ProductFamilyNumericUnit",
    "ProductFamilyModelInterfaceSource",
    "ProductFamilyPublicParameterContract",
    "ProductFamilyRecord",
    "ProductFamilyStructureKind",
    "ProductFamilyVersionAssetRecord",
    "ProductFamilyVersionAssetRole",
    "ProductFamilyVersionBundle",
    "ProductFamilyVersionListQuery",
    "ProductFamilyVersionRecord",
    "normalize_product_family_public_parameters",
    "product_family_content_digest",
]
