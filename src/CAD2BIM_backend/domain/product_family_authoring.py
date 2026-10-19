"""Editable Family commands and exact formal publication contracts."""

from __future__ import annotations

from typing import Annotated, ClassVar, Generic, Literal, Self, TypeVar

from pydantic import Field, JsonValue, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_runtime import CriticalOperationKind, CriticalOperationRecord, CriticalOperationStatus, PublishedCriticalOperation
from CAD2BIM_backend.domain.product_family import MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS, ProductFamilyContract, ProductFamilyGeometryPurpose, ProductFamilyRecord, ProductFamilyVersionAssetRecord, ProductFamilyVersionAssetRole, ProductFamilyVersionBundle, ProductFamilyVersionRecord, _ProductFamilyModel, product_family_content_digest
from CAD2BIM_backend.domain.product_family import ProductFamilyPublicParameterContract, ProductFamilyComponentContract, ProductFamilyGeometryFragmentContract, ProductFamilyInterfaceContract, ProductFamilyConstraintContract, ProductFamilyStandardModelContract, ProductFamilyStructureKind
from CAD2BIM_backend.domain.product_family_authoring_contracts import ProductFamilyAuthoringVersionDraft, ProductFamilyInvocationSemanticError, ResolvedProductFamilyGeometryBindingInput, normalize_product_family_validation_scenario
from CAD2BIM_backend.domain.product_realization import MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES, MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScopeKind
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.text_source import SourceBundleChange, TextChange, TextSet
from CAD2BIM_backend.domain.version_usage import validate_version_usage_instructions


_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_REQUEST_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_FAMILY_DRAFT_DEFINITION_JSON_BYTES = 8 * 1024 * 1024
_BusinessKey = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]


def _validate_usage_change(value):
    if value.action == "set":
        validate_version_usage_instructions(value.value, required=True)
    return value


def _require_python_path(value: str) -> str:
    if (
        not value.endswith(".py")
        or "\\" in value
        or value.startswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("product family source path must be a normalized relative .py path")
    return value


def _require_source(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("product family source must be non-empty UTF-8 text")
    if len(value.encode("utf-8")) > MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES:
        raise ValueError("product family source member exceeds its UTF-8 byte budget")
    return value


class ProductFamilyAuthoringSourceFile(_ProductFamilyModel):
    path: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1)

    _validate_path = field_validator("path")(_require_python_path)
    _validate_source = field_validator("source")(_require_source)


class FamilyDraftPublishedGeometrySource(_ProductFamilyModel):
    source_kind: Literal["published"]
    resource_version_alias: str = Field(min_length=1, max_length=16_384)


class FamilyDraftGeometrySource(_ProductFamilyModel):
    source_kind: Literal["draft"]
    draft_ref: str = Field(min_length=1, max_length=16_384, description="Visible Geometry draft reference from Write or draft Info; Preview freezes this dependency and Publish includes it in the closure.")


FamilyDraftGeometryReference = Annotated[
    FamilyDraftPublishedGeometrySource | FamilyDraftGeometrySource,
    Field(discriminator="source_kind"),
]


class FamilyDraftGeometryBinding(_ProductFamilyModel):
    binding_key: str = Field(pattern=_IDENTIFIER_PATTERN)
    source: FamilyDraftGeometryReference
    expected_resource_kind: GeometryResourceKind
    geometry_purpose: ProductFamilyGeometryPurpose


class FamilyDraftDefinition(_ProductFamilyModel):
    family_contract: ProductFamilyContract
    geometry_bindings: tuple[FamilyDraftGeometryBinding, ...] = Field(
        min_length=1, max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    )

    @model_validator(mode="after")
    def require_unique_bindings(self):
        keys = [binding.binding_key for binding in self.geometry_bindings]
        if len(keys) != len(set(keys)):
            raise ValueError("geometry binding keys must be unique")
        if len(canonical_json(self).encode("utf-8")) > MAX_FAMILY_DRAFT_DEFINITION_JSON_BYTES:
            raise ValueError("Family draft definition exceeds its 8 MiB UTF-8 content budget")
        return self


class FamilyDraftDefinitionSet(_ProductFamilyModel):
    action: Literal["set"]
    value: FamilyDraftDefinition


_Item = TypeVar("_Item")


class _FamilyKeyedEdits(_ProductFamilyModel, Generic[_Item]):
    key_field: ClassVar[str]
    set: tuple[_Item, ...] = ()
    remove: tuple[_BusinessKey, ...] = ()

    @model_validator(mode="after")
    def require_distinct_keys(self):
        keys = [getattr(item, self.key_field) for item in self.set]
        if len(keys) != len(set(keys)) or len(self.remove) != len(set(self.remove)):
            raise ValueError(f"{self.key_field} values must be unique within set and remove")
        if set(keys) & set(self.remove):
            raise ValueError(f"the same {self.key_field} cannot occur in set and remove")
        if not keys and not self.remove:
            raise ValueError("collection edit requires set or remove items")
        return self

    def merge(self, previous, *, field: str):
        existing = {getattr(item, self.key_field) for item in previous}
        for index, key in enumerate(self.remove, 1):
            if key not in existing:
                raise ValueError(f"{field}.remove[{index}] names missing {self.key_field} {key!r}")
        replacements = {getattr(item, self.key_field): item for item in self.set}
        result = [replacements.get(getattr(item, self.key_field), item)
                  for item in previous if getattr(item, self.key_field) not in self.remove]
        result.extend(item for item in self.set if getattr(item, self.key_field) not in existing)
        return tuple(result)


class FamilyDraftParameterEdits(_FamilyKeyedEdits[ProductFamilyPublicParameterContract]):
    key_field = "parameter_key"
    set: tuple[ProductFamilyPublicParameterContract, ...] = Field(default=(), max_length=64)
    remove: tuple[_BusinessKey, ...] = Field(default=(), max_length=64)


class FamilyDraftComponentEdits(_FamilyKeyedEdits[ProductFamilyComponentContract]):
    key_field = "component_key"
    set: tuple[ProductFamilyComponentContract, ...] = Field(default=(), max_length=128)
    remove: tuple[_BusinessKey, ...] = Field(default=(), max_length=128)


class FamilyDraftFragmentEdits(_FamilyKeyedEdits[ProductFamilyGeometryFragmentContract]):
    key_field = "fragment_key"
    set: tuple[ProductFamilyGeometryFragmentContract, ...] = Field(default=(), max_length=256)
    remove: tuple[_BusinessKey, ...] = Field(default=(), max_length=256)


class FamilyDraftInterfaceEdits(_FamilyKeyedEdits[ProductFamilyInterfaceContract]):
    key_field = "interface_key"
    set: tuple[ProductFamilyInterfaceContract, ...] = Field(default=(), max_length=64)
    remove: tuple[_BusinessKey, ...] = Field(default=(), max_length=64)


class FamilyDraftConstraintEdits(_FamilyKeyedEdits[ProductFamilyConstraintContract]):
    key_field = "constraint_key"
    set: tuple[ProductFamilyConstraintContract, ...] = Field(default=(), max_length=128)
    remove: tuple[_BusinessKey, ...] = Field(default=(), max_length=128)


class FamilyDraftBindingEdits(_FamilyKeyedEdits[FamilyDraftGeometryBinding]):
    key_field = "binding_key"
    set: tuple[FamilyDraftGeometryBinding, ...] = Field(default=(), max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS)
    remove: tuple[_BusinessKey, ...] = Field(default=(), max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS)


class FamilyDraftStandardModelSet(_ProductFamilyModel):
    action: Literal["set"]
    value: ProductFamilyStandardModelContract


class FamilyDraftStandardModelClear(_ProductFamilyModel):
    action: Literal["clear"]


class FamilyDraftContractEdit(_ProductFamilyModel):
    product_structure_kind: ProductFamilyStructureKind = Field(default_factory=lambda: None)
    public_parameters: FamilyDraftParameterEdits = Field(default_factory=lambda: None)
    solved_parameters: FamilyDraftParameterEdits = Field(default_factory=lambda: None)
    components: FamilyDraftComponentEdits = Field(default_factory=lambda: None)
    geometry_fragments: FamilyDraftFragmentEdits = Field(default_factory=lambda: None)
    interfaces: FamilyDraftInterfaceEdits = Field(default_factory=lambda: None)
    constraints: FamilyDraftConstraintEdits = Field(default_factory=lambda: None)
    standard_model: Annotated[FamilyDraftStandardModelSet | FamilyDraftStandardModelClear, Field(discriminator="action")] = Field(default_factory=lambda: None)

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("family_contract edit requires a business field")
        return self


class FamilyDraftDefinitionEdit(_ProductFamilyModel):
    action: Literal["edit"]
    family_contract: FamilyDraftContractEdit = Field(default_factory=lambda: None)
    geometry_bindings: FamilyDraftBindingEdits = Field(default_factory=lambda: None)

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set - {"action"}:
            raise ValueError("definition edit requires family_contract or geometry_bindings")
        return self

    def merge(self, previous: FamilyDraftDefinition) -> FamilyDraftDefinition:
        values = previous.model_dump(mode="python")
        if "family_contract" in self.model_fields_set:
            contract = values["family_contract"]
            for field in self.family_contract.model_fields_set:
                change = getattr(self.family_contract, field)
                if field == "standard_model":
                    contract[field] = change.value if change.action == "set" else None
                elif field == "product_structure_kind":
                    contract[field] = change
                else:
                    contract[field] = change.merge(getattr(previous.family_contract, field), field=f"definition.family_contract.{field}")
        if "geometry_bindings" in self.model_fields_set:
            values["geometry_bindings"] = self.geometry_bindings.merge(previous.geometry_bindings, field="definition.geometry_bindings")
        return FamilyDraftDefinition.model_validate(values)


FamilyDraftDefinitionChange = Annotated[FamilyDraftDefinitionSet | FamilyDraftDefinitionEdit, Field(discriminator="action")]


class FamilyDraftInlineSource(_ProductFamilyModel):
    source_kind: Literal["inline"]
    source_files: tuple[ProductFamilyAuthoringSourceFile, ...] = Field(
        min_length=1, max_length=MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
        description="Complete unique Python source members including main.py with realize(ctx) and standard_model(ctx); each at most 64 KiB and combined at most 256 KiB UTF-8.",
    )
    definition: FamilyDraftDefinitionSet = Field(description="Set the complete family_contract and geometry_bindings. Bind every declared binding_key to a visible exact published Geometry version or Geometry draft, with its expected kind and purpose.")


class FamilyDraftPublishedSource(_ProductFamilyModel):
    source_kind: Literal["published"]
    family_version_alias: str = Field(min_length=1, max_length=16_384)


FamilyDraftCreateSource = Annotated[
    FamilyDraftInlineSource | FamilyDraftPublishedSource, Field(discriminator="source_kind")
]


class FamilyDraftInlineCreateCommand(_ProductFamilyModel):
    mode: Literal["create"]
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2_048)
    usage_instructions: TextSet = Field(description="Set version-owned usage, supported scope, units and limitations; keep consistent with contract, bindings and source.")
    source: FamilyDraftInlineSource
    _validate_usage = field_validator("usage_instructions")(_validate_usage_change)


class FamilyDraftPublishedCreateCommand(_ProductFamilyModel):
    mode: Literal["create"]
    display_name: str = Field(default_factory=lambda: None, min_length=1, max_length=255)
    purpose: str = Field(default_factory=lambda: None, min_length=1, max_length=2_048)
    usage_instructions: TextSet = Field(default_factory=lambda: None, description="Set an explicit usage override; omit to inherit the selected exact Family version's usage.")
    source: FamilyDraftPublishedSource
    _validate_usage = field_validator("usage_instructions")(_validate_usage_change)


FamilyDraftCreateCommand = FamilyDraftInlineCreateCommand | FamilyDraftPublishedCreateCommand


class FamilyDraftUpdateCommand(_ProductFamilyModel):
    mode: Literal["update"]
    draft_ref: str = Field(min_length=1, max_length=16_384, description="Current-scope editable Family reference from Write or draft Info; copy unchanged.")
    expected_edit_token: int = Field(ge=1, strict=True, description="Latest edit_token from Write or draft Info; stale tokens reject the entire update. Read current Info before editing again.")
    # None is only an omitted-field sentinel; explicit null remains invalid.
    display_name: str = Field(default_factory=lambda: None, min_length=1, max_length=255)
    purpose: str = Field(default_factory=lambda: None, min_length=1, max_length=2_048)
    usage_instructions: TextChange = Field(default_factory=lambda: None, description="Set complete usage or edit exact unique old_text anchors; omit to inherit. Keep usage consistent with definition and source.")
    source_changes: tuple[SourceBundleChange, ...] = Field(default_factory=lambda: None, max_length=64, description="Apply bounded member additions, replacements, deletions or literal edits. Copy member digests from Code reads; omit to inherit. Result stays within 64 members, 64 KiB each and 256 KiB total UTF-8.")
    definition: FamilyDraftDefinitionChange = Field(default_factory=lambda: None, description="Set the complete definition or edit business-key collections; omit to inherit. Complete merged references and exact Geometry kinds are validated before saving.")
    _validate_usage = field_validator("usage_instructions")(_validate_usage_change)

    @model_validator(mode="after")
    def require_changes(self):
        changes = self.model_fields_set - {"mode", "draft_ref", "expected_edit_token"}
        if not changes or any(getattr(self, name) is None for name in changes):
            raise ValueError("update requires changes; omit inherited fields instead of null")
        return self


FamilyDraftWriteCommand = Annotated[
    FamilyDraftInlineCreateCommand | FamilyDraftPublishedCreateCommand | FamilyDraftUpdateCommand,
    Field(description="Complete inline-create, published-create or update Family draft branch."),
]


class ProductFamilyAuthoringPublication(_ProductFamilyModel):
    """One successful Operation and every exact fact committed atomically with it."""

    operation: CriticalOperationRecord
    family: ProductFamilyRecord
    version: ProductFamilyAuthoringVersionDraft
    geometry_bindings: tuple[ResolvedProductFamilyGeometryBindingInput, ...] = Field(
        min_length=1,
        max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    )
    assets: tuple[ProductFamilyVersionAssetRecord, ...] = Field(min_length=1)
    @model_validator(mode="after")
    def validate_atomic_publication(self) -> Self:
        if any(asset.asset_role is ProductFamilyVersionAssetRole.VALIDATION_REPORT for asset in self.assets):
            raise ValueError("new Family versions cannot retain validation reports")
        operation = self.operation
        is_publish = operation.operation_kind is CriticalOperationKind.AUTHORING_PUBLISH
        is_system_copy = (operation.operation_kind is CriticalOperationKind.PRODUCT_FAMILY_WRITE
                          and getattr(operation.detail, "mode", None) == "system_copy")
        if operation.status is not CriticalOperationStatus.SUCCEEDED or not (is_publish or is_system_copy):
            raise ValueError("Family publication requires successful Publish or curator SYSTEM copy")
        copy_plan = operation.detail.system_copy if is_system_copy else None
        if is_system_copy:
            if copy_plan is None or self.family.owner_scope != copy_plan.target_owner:
                raise ValueError("SYSTEM copy must use its saved target owner")
            if self.assets != copy_plan.family_assets:
                raise ValueError("SYSTEM copy must use its saved immutable assets")
        elif self.family.owner_scope.scope_kind is ResourceOwnerScopeKind.TENANT and (
            self.family.owner_scope.tenant_id != operation.tenant_id
        ):
            raise ValueError("Family tenant scope does not match the write Operation")
        if self.family.owner_scope.scope_kind is ResourceOwnerScopeKind.TASK and (
            self.family.owner_scope.tenant_id != operation.tenant_id
            or self.family.owner_scope.task_id != operation.task_id
        ):
            raise ValueError("product family scope does not match the write Operation")
        if operation.target_product_family_id != self.family.family_id:
            raise ValueError("product family write target does not match the family root")
        if self.version.family_id != self.family.family_id:
            raise ValueError("product family version belongs to another family root")
        if operation.result_product_family_version_id != self.version.family_version_id:
            raise ValueError("product family write result does not match the candidate version")
        if operation.base_product_family_version_id != self.version.parent_family_version_id:
            raise ValueError("product family write base does not match the candidate parent")
        if self.geometry_bindings != self.version.geometry_bindings:
            raise ValueError("publication geometry bindings differ from the Operation draft")
        detail_draft = operation.detail.family_version if is_publish else operation.detail.version_draft
        if detail_draft != self.version:
            raise ValueError("product family write detail does not match the publication draft")
        for binding in self.geometry_bindings:
            if not self.family.owner_scope.permits_dependency(binding.geometry_owner_scope):
                raise ValueError("product family geometry binding crosses its owner scope")
        source_assets = tuple(
            asset
            for asset in self.assets
            if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE
        )
        if len(source_assets) != 1:
            raise ValueError("product family publication requires the exact source asset")
        if is_system_copy and (operation.code_artifact is None or source_assets[0].sha256 != operation.code_artifact.sha256):
            raise ValueError("published source asset differs from the Operation code Artifact")
        self.to_bundle(version_no=1)
        return self

    def to_bundle(self, *, version_no: int) -> ProductFamilyVersionBundle:
        binding_records = tuple(
            sorted(
                (
                    binding.to_record(family_version_id=self.version.family_version_id)
                    for binding in self.geometry_bindings
                ),
                key=lambda item: item.binding_key,
            )
        )
        assets = tuple(
            sorted(
                self.assets,
                key=lambda item: item.asset_role.value,
            )
        )
        content_digest = product_family_content_digest(
            contract_digest=self.version.contract_digest,
            program_entry=self.version.program_entry,
            geometry_bindings=binding_records,
            assets=assets,
            usage_instructions=self.version.usage_instructions,
        )
        return ProductFamilyVersionBundle(
            family=self.family,
            version=ProductFamilyVersionRecord(
                family_version_id=self.version.family_version_id,
                family_id=self.version.family_id,
                version_no=version_no,
                parent_family_version_id=self.version.parent_family_version_id,
                display_name=self.version.display_name,
                purpose=self.version.purpose,
                usage_instructions=self.version.usage_instructions,
                family_contract=self.version.family_contract,
                contract_digest=self.version.contract_digest,
                content_digest=content_digest,
                program_entry=self.version.program_entry,
                created_at=self.version.created_at,
            ),
            geometry_bindings=binding_records,
            assets=assets,
        )


class ProductFamilyAuthoringCommitResult(_ProductFamilyModel):
    operation: PublishedCriticalOperation
    family_version: ProductFamilyVersionBundle

    @model_validator(mode="after")
    def validate_committed_result(self) -> Self:
        record = self.operation.operation
        if (
            record.operation_kind is not CriticalOperationKind.PRODUCT_FAMILY_WRITE
            or record.status is not CriticalOperationStatus.SUCCEEDED
            or record.target_product_family_id != self.family_version.family.family_id
            or record.result_product_family_version_id
            != self.family_version.version.family_version_id
        ):
            raise ValueError("product family commit result does not match its Operation")
        return self








__all__ = [
    "FamilyDraftWriteCommand",
    "ProductFamilyAuthoringCommitResult",
    "ProductFamilyAuthoringPublication",
    "ProductFamilyAuthoringSourceFile",
    "ProductFamilyAuthoringVersionDraft",
    "ProductFamilyInvocationSemanticError",
    "ResolvedProductFamilyGeometryBindingInput",
    "normalize_product_family_validation_scenario",
]
