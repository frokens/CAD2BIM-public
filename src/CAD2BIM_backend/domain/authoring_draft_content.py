"""Complete editable content stored by each existing authoring owner in Artifact."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_authoring import GeometryAuthoringInputMetadata, GeometryAuthoringSourceFile, GeometryDraftInputBinding
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_runtime import MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES
from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftDefinition, ProductFamilyAuthoringSourceFile, FamilyDraftGeometryBinding
from CAD2BIM_backend.domain.product_family import MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS
from CAD2BIM_backend.domain.product_realization import MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS, MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES
from CAD2BIM_backend.domain.version_usage import validate_version_usage_instructions


class GeometryPublishedBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    resource_id: str = Field(min_length=1, max_length=80)
    resource_version_id: str = Field(min_length=1, max_length=80)


class GeometryDraftContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    display_name: str = Field(min_length=1, max_length=255)
    usage_instructions: str = Field(min_length=1)
    source_files: tuple[GeometryAuthoringSourceFile, ...] = Field(default=(), max_length=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS)
    input_metadata: GeometryAuthoringInputMetadata
    interfaces: GeometryModelInterfaces = ()
    published_base: GeometryPublishedBase | None = None

    @field_validator("usage_instructions")
    @classmethod
    def validate_usage(cls, value: str) -> str:
        return validate_version_usage_instructions(value, required=True)

    @model_validator(mode="after")
    def require_source(self):
        paths = [item.path for item in self.source_files]
        if len(paths) != len(set(paths)):
            raise ValueError("Geometry source paths must be unique")
        if sum(len(item.source.encode("utf-8")) for item in self.source_files) > MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES:
            raise ValueError("Geometry source exceeds the authoring budget")
        if not self.source_files and self.published_base is None:
            raise ValueError("a Geometry draft needs source or an exact fixed-asset base")
        if not self.source_files and self.input_metadata.resource_kind.value != "mapped_model":
            raise ValueError("a modeling function draft needs its author source")
        if self.interfaces and self.input_metadata.resource_kind.value != "mapped_model":
            raise ValueError("interfaces are only accepted for mapped_model; function interfaces come from build")
        return self

    @property
    def execution_digest(self) -> str:
        return sha256_digest(self.model_dump(mode="json", exclude={"display_name", "usage_instructions"}))


class FamilyPublishedBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    family_id: str = Field(min_length=1, max_length=80)
    family_version_id: str = Field(min_length=1, max_length=80)


class FamilyDraftGeometryRoot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_kind: Literal["draft"]
    resource_id: str = Field(min_length=1, max_length=80)


class FamilyDraftGeometryVersion(GeometryPublishedBase):
    source_kind: Literal["published"]


class SavedFamilyDraftGeometryBinding(FamilyDraftGeometryBinding):
    """Persist root/version identity; entry references remain scope projections."""

    source: Annotated[FamilyDraftGeometryRoot | FamilyDraftGeometryVersion, Field(discriminator="source_kind")]


class SavedFamilyDraftDefinition(FamilyDraftDefinition):
    geometry_bindings: tuple[SavedFamilyDraftGeometryBinding, ...] = Field(
        min_length=1, max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    )


class FamilyDraftContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    usage_instructions: str = Field(min_length=1)
    source_files: tuple[ProductFamilyAuthoringSourceFile, ...] = Field(min_length=1, max_length=MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS)
    definition: SavedFamilyDraftDefinition
    published_base: FamilyPublishedBase | None = None

    @field_validator("usage_instructions")
    @classmethod
    def validate_usage(cls, value: str) -> str:
        return validate_version_usage_instructions(value, required=True)

    @model_validator(mode="after")
    def require_source(self):
        paths = [item.path for item in self.source_files]
        if len(paths) != len(set(paths)):
            raise ValueError("Family source paths must be unique")
        if sum(len(item.source.encode("utf-8")) for item in self.source_files) > MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES:
            raise ValueError("Family source exceeds the authoring budget")
        return self

    @property
    def execution_digest(self) -> str:
        return sha256_digest(self.model_dump(mode="json", exclude={"display_name", "purpose", "usage_instructions"}))


class GeometryDraftInfo(BaseModel):
    """Current editable business definition; source text is read through Code."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_kind: Literal["draft"] = "draft"
    draft_ref: str
    edit_token: int = Field(ge=1)
    operation_ref: str | None = None
    operation_alias: str | None = None
    display_name: str
    usage_instructions: str
    resource_kind: Literal["mapped_model", "modeling_function"]
    parameter_schema_json: str | None = None
    geometry_inputs: tuple[GeometryDraftInputBinding, ...] = ()
    interfaces: GeometryModelInterfaces = ()
    source_files: tuple[str, ...] = ()


class FamilyDraftInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_kind: Literal["draft"] = "draft"
    draft_ref: str
    edit_token: int = Field(ge=1)
    operation_ref: str | None = None
    operation_alias: str | None = None
    display_name: str
    purpose: str
    usage_instructions: str
    contract_json: str
    geometry_bindings: tuple[FamilyDraftGeometryBinding, ...]
    source_files: tuple[str, ...]
