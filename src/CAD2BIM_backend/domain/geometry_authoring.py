"""Atomic persistence contracts for Task-scoped geometry authoring writes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceKind,
    GeometryResourceInputs,
    GeometryResourceScopeKind,
    GeometryResourceRecord,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    require_formal_geometry_publication,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
    MAX_GEOMETRY_BUILD_INPUTS,
    _PARAMETER_NAME_PATTERN,
    validate_geometry_function_parameter_schema,
)
from CAD2BIM_backend.domain.geometry_catalog import GeometrySourceMember
from CAD2BIM_backend.domain.text_source import SourceBundleChange, TextChange, TextSet, TextSourceViewResult


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class GeometryAuthoringContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GeometryAuthoringInputMetadata(GeometryAuthoringContract):
    """Closed ArtifactVersion metadata for exact authoring build inputs."""

    resource_kind: GeometryResourceKind
    geometry_inputs: GeometryResourceInputs = ()
    parameter_schema: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_kind_inputs(self) -> Self:
        if self.resource_kind is GeometryResourceKind.MAPPED_MODEL:
            if self.parameter_schema is not None:
                raise ValueError("mapped_model authoring cannot contain function inputs")
            return self
        if self.parameter_schema is None:
            raise ValueError("modeling_function authoring requires a parameter schema")
        validate_geometry_function_parameter_schema(self.parameter_schema)
        return self

    def version_metadata(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


class GeometryAuthoringSourceFile(GeometryAuthoringContract):
    path: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES)

    @field_validator("path")
    @classmethod
    def require_python_path(cls, value: str) -> str:
        if (
            not value.endswith(".py")
            or "\\" in value
            or value.startswith("/")
            or "//" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("geometry authoring source path must be a normalized .py path")
        return value

    @field_validator("source")
    @classmethod
    def require_bounded_utf8(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES:
            raise ValueError("geometry authoring source exceeds the UTF-8 budget")
        return value


class GeometryDraftInputBinding(GeometryAuthoringContract):
    input_key: str = Field(pattern=_IDENTIFIER_PATTERN, description="Unique ctx.geometry(input_key) name for this exact fixed-BREP input.")
    resource_version_alias: str = Field(min_length=1, max_length=16_384, description="Exact visible published mapped_model BREP version; root aliases, modeling functions and meshes are invalid.")


class GeometryDraftMappedSource(GeometryAuthoringContract):
    source_kind: Literal["mapped_model"]
    source_files: tuple[GeometryAuthoringSourceFile, ...] = Field(min_length=1, max_length=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, description="Complete unique Python members including main.py with build(ctx); each at most 64 KiB and combined at most 256 KiB UTF-8.")
    geometry_inputs: tuple[GeometryDraftInputBinding, ...] = Field(default=(), max_length=MAX_GEOMETRY_BUILD_INPUTS, description="Named exact fixed-BREP dependencies available through ctx.geometry(key), at most 8 and 32 MiB combined; omitted means no inputs at create.")
    interfaces: GeometryModelInterfaces = ()


class GeometryParameterSchemaSet(GeometryAuthoringContract):
    action: Literal["set"]
    value: dict[str, JsonValue]

    @field_validator("value")
    @classmethod
    def require_parameter_schema(cls, value):
        validate_geometry_function_parameter_schema(value)
        return value


class GeometryParameterUpdate(GeometryAuthoringContract):
    parameter_schema: dict[str, JsonValue] = Field(alias="schema")
    required: bool = Field(strict=True)


class GeometryParameterSchemaEdit(GeometryAuthoringContract):
    action: Literal["edit"]
    set: dict[Annotated[str, Field(pattern=_PARAMETER_NAME_PATTERN)], GeometryParameterUpdate] = Field(default_factory=dict, max_length=64)
    remove: tuple[Annotated[str, Field(pattern=_PARAMETER_NAME_PATTERN)], ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def require_disjoint_changes(self):
        if not self.set and not self.remove:
            raise ValueError("parameter edit requires set or remove entries")
        if len(self.remove) != len(set(self.remove)):
            raise ValueError("parameter remove entries must be unique")
        if self.set.keys() & set(self.remove):
            raise ValueError("a parameter cannot be set and removed in the same edit")
        return self


GeometryParameterSchemaChange = Annotated[
    GeometryParameterSchemaSet | GeometryParameterSchemaEdit, Field(discriminator="action")
]


class GeometryParameterSchemaEditError(ValueError):
    def __init__(self, message: str, *, parameter_key: str | None = None):
        super().__init__(message)
        self.parameter_key = parameter_key


def resolve_geometry_parameter_schema(
    current: dict[str, JsonValue], change: GeometryParameterSchemaChange,
) -> dict[str, JsonValue]:
    """Resolve one top-level edit, preserving untouched root and parameter content."""
    from copy import deepcopy

    if change.action == "set":
        return deepcopy(change.value)
    result = deepcopy(current)
    properties, required = result["properties"], result["required"]
    for key in change.remove:
        if key not in properties:
            raise GeometryParameterSchemaEditError(
                f"parameter {key!r} does not exist and cannot be removed", parameter_key=key)
        del properties[key]
        required[:] = [item for item in required if item != key]
    for key, item in change.set.items():
        properties[key] = deepcopy(item.parameter_schema)
        if item.required and key not in required:
            required.append(key)
        elif not item.required:
            required[:] = [name for name in required if name != key]
    validate_geometry_function_parameter_schema(result)
    return result


class GeometryDraftFunctionSource(GeometryAuthoringContract):
    source_kind: Literal["modeling_function"]
    source_files: tuple[GeometryAuthoringSourceFile, ...] = Field(min_length=1, max_length=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, description="Complete unique Python members including main.py with build(ctx); each at most 64 KiB and combined at most 256 KiB UTF-8.")
    geometry_inputs: tuple[GeometryDraftInputBinding, ...] = Field(default=(), max_length=MAX_GEOMETRY_BUILD_INPUTS, description="Named exact fixed-BREP dependencies available through ctx.geometry(key), at most 8 and 32 MiB combined; omitted means no inputs at create.")
    parameter_schema: GeometryParameterSchemaSet = Field(description="action=set with the complete closed modeling_function parameter schema; declares supported inputs, units, bounds and defaults used by build(ctx).")


class GeometryDraftPublishedSource(GeometryAuthoringContract):
    source_kind: Literal["published"]
    resource_version_alias: str = Field(min_length=1, max_length=16_384)


GeometryDraftCreateSource = Annotated[
    GeometryDraftMappedSource | GeometryDraftFunctionSource,
    Field(discriminator="source_kind"),
]


class GeometryDraftCreateCommand(GeometryAuthoringContract):
    mode: Literal["create"]
    display_name: str = Field(min_length=1, max_length=255)
    usage_instructions: TextSet = Field(description="action=set with complete resource-specific usage; final text must match source, parameters and interfaces.")
    source: GeometryDraftCreateSource


class GeometryDraftPublishedCreateCommand(GeometryAuthoringContract):
    mode: Literal["create"]
    source: GeometryDraftPublishedSource
    display_name: str = Field(default_factory=lambda: None, min_length=1, max_length=255)
    usage_instructions: TextSet = Field(default_factory=lambda: None, description="Omit to inherit exact-version usage; action=set overrides it. Missing or invalid inherited usage requires set.")


class GeometryDraftUpdateCommand(GeometryAuthoringContract):
    mode: Literal["update"]
    draft_ref: str = Field(min_length=1, max_length=16_384, description="Current-scope editable Geometry reference from Write or draft Info; copy unchanged.")
    expected_edit_token: int = Field(ge=1, strict=True, description="Latest edit_token from Write or draft Info; stale tokens reject the entire update. Read current Info before editing again.")
    # None is only an omitted-field sentinel; explicit null remains invalid.
    display_name: str = Field(default_factory=lambda: None, min_length=1, max_length=255)
    usage_instructions: TextChange = Field(default_factory=lambda: None, description="action=set replaces usage; action=edit applies exact text replacements. Omit to inherit the complete resource-specific text.")
    source_changes: tuple[SourceBundleChange, ...] = Field(default_factory=lambda: None, max_length=64, description="Apply bounded member additions, replacements, deletions or literal edits to current source. Copy member digests from Code reads; omit to inherit. Result stays within 16 members, 64 KiB each and 256 KiB total UTF-8.")
    geometry_inputs: tuple[GeometryDraftInputBinding, ...] = Field(default_factory=lambda: None, max_length=MAX_GEOMETRY_BUILD_INPUTS, description="Replace all named exact fixed-BREP inputs; omit to inherit, empty list clears. At most 8 inputs and 32 MiB combined.")
    parameter_schema: GeometryParameterSchemaChange = Field(default_factory=lambda: None, description="action=set replaces the closed modeling_function schema; action=edit sets/removes named top-level parameters and their required membership. Omit to inherit; invalid for mapped_model.")
    interfaces: GeometryModelInterfaces = Field(default_factory=lambda: None, description="Replace mapped_model local-metre interfaces; omit to inherit, empty list clears. Function interfaces come from build output.")

    @model_validator(mode="after")
    def require_changes(self):
        changes = self.model_fields_set - {"mode", "draft_ref", "expected_edit_token"}
        if not changes or any(getattr(self, name) is None for name in changes):
            raise ValueError("update requires changes; omit inherited fields instead of null")
        return self


GeometryDraftWriteCommand = GeometryDraftCreateCommand | GeometryDraftPublishedCreateCommand | GeometryDraftUpdateCommand


class GeometryAuthoringVersionDraft(GeometryAuthoringContract):
    """Validated version fields whose monotonic number is assigned by PostgreSQL."""

    resource_version_id: str = Field(min_length=1, max_length=80)
    resource_id: str = Field(min_length=1, max_length=80)
    origin_kind: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    content_digest: str = Field(pattern=_SHA256_PATTERN)
    manifest_digest: str = Field(pattern=_SHA256_PATTERN)
    manifest: dict[str, JsonValue]
    lineage: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("manifest", "lineage")
    @classmethod
    def require_canonical_json(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        canonical_json(value)
        return value

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("geometry authoring timestamps must be timezone-aware")
        return value

    def to_record(self, *, version_no: int) -> GeometryResourceVersionRecord:
        return GeometryResourceVersionRecord(
            **self.model_dump(mode="python"),
            version_no=version_no,
        )


class GeometryAuthoringSuccessPublication(GeometryAuthoringContract):
    """One successful Operation and its candidate root/version/assets."""

    operation: CriticalOperationRecord
    resource: GeometryResourceRecord
    version: GeometryAuthoringVersionDraft
    assets: tuple[GeometryResourceVersionAssetRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_atomic_binding(self) -> Self:
        require_formal_geometry_publication(self.version, self.assets)
        operation = self.operation
        if (
            operation.operation_kind is not CriticalOperationKind.AUTHORING_PUBLISH
            or operation.status is not CriticalOperationStatus.SUCCEEDED
        ):
            raise ValueError("geometry publication requires a successful Publish Operation")
        if not any(item.resource_id == self.resource.resource_id
                   and item.resource_version_id == self.version.resource_version_id
                   for item in operation.detail.geometry_versions):
            raise ValueError("geometry publication is outside the admitted exact closure")
        if self.version.resource_id != self.resource.resource_id:
            raise ValueError("geometry authoring candidate belongs to another resource root")
        if self.resource.owner_scope.scope_kind is GeometryResourceScopeKind.TENANT and (
            self.resource.owner_scope.tenant_id != operation.tenant_id
        ):
            raise ValueError("geometry tenant scope does not match the write Operation")
        if self.resource.owner_scope.scope_kind is GeometryResourceScopeKind.TASK and (
            self.resource.owner_scope.tenant_id != operation.tenant_id
            or self.resource.owner_scope.task_id != operation.task_id
        ):
            raise ValueError("geometry authoring resource scope does not match the Operation")
        if any(
            asset.resource_version_id != self.version.resource_version_id for asset in self.assets
        ):
            raise ValueError("geometry authoring asset belongs to another exact version")
        GeometryResourceVersionBundle(
            resource=self.resource,
            version=self.version.to_record(version_no=1),
            assets=self.assets,
        )
        return self

    def to_bundle(self, *, version_no: int) -> GeometryResourceVersionBundle:
        return GeometryResourceVersionBundle(
            resource=self.resource,
            version=self.version.to_record(version_no=version_no),
            assets=self.assets,
        )


class GeometryAuthoringCommitResult(GeometryAuthoringContract):
    operation: PublishedCriticalOperation
    resource_version: GeometryResourceVersionBundle


class GeometryAuthoringSourceReadResult(GeometryAuthoringContract):
    """Exact source projection for one Operation-bound code ArtifactVersion."""

    operation_id: str = Field(min_length=1, max_length=80)
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    members: tuple[GeometrySourceMember, ...] = Field(min_length=1, max_length=16)
    selected_path: str | None = Field(default=None, min_length=1, max_length=255)
    view: TextSourceViewResult | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if (self.selected_path is None) != (self.view is None):
            raise ValueError("selected source path and source view must be returned together")
        if self.selected_path is not None and self.selected_path not in {
            member.source_path for member in self.members
        }:
            raise ValueError("selected source path is absent from bundle members")
        return self


__all__ = [
    "GeometryDraftWriteCommand",
    "GeometryAuthoringCommitResult",
    "GeometryAuthoringContract",
    "GeometryAuthoringInputMetadata",
    "GeometryAuthoringSuccessPublication",
    "GeometryAuthoringSourceReadResult",
    "GeometryAuthoringSourceFile",
    "GeometryAuthoringVersionDraft",
]
