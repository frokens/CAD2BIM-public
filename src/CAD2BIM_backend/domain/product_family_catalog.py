"""Safe read projections for the ready ProductFamily Catalog."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyContract,
    ProductFamilyListEntry,
    ProductFamilyRecord,
    ProductFamilyVersionAssetRecord,
    ProductFamilyVersionAssetRole,
    ProductFamilyGeometryPurpose,
    ProductFamilyStructureKind,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScopeKind


class ProductFamilyCatalogError(Exception):
    code: ClassVar[str] = "PRODUCT_FAMILY_CATALOG_ERROR"


class ProductFamilyCatalogReferenceError(ProductFamilyCatalogError, ValueError):
    code = "PRODUCT_FAMILY_REFERENCE_INVALID"


class ProductFamilyCatalogNotFoundError(ProductFamilyCatalogError, LookupError):
    code = "PRODUCT_FAMILY_NOT_FOUND"


class ProductFamilyCatalogIntegrityError(ProductFamilyCatalogError, RuntimeError):
    code = "PRODUCT_FAMILY_INTEGRITY_ERROR"


class ProductFamilyCatalogContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductFamilyVersionMetadata(ProductFamilyCatalogContract):
    """Scoped identity and stored summary; no parsed executable Family contract."""

    family: ProductFamilyRecord
    family_version_id: str = Field(min_length=1, max_length=80)
    version_no: int = Field(ge=1)
    parent_family_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    product_structure_kind: ProductFamilyStructureKind
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class ProductFamilyValidationIssue(ProductFamilyCatalogContract):
    field_path: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ProductFamilyStoredContractError(ProductFamilyCatalogIntegrityError):
    """Only current contract DTO validation; never a database or asset failure."""

    code = "PRODUCT_FAMILY_CONTRACT_INVALID"

    def __init__(self, issues: tuple[ProductFamilyValidationIssue, ...]) -> None:
        super().__init__("stored ProductFamily contract does not satisfy the current structure")
        self.issues = issues


class ProductFamilyStoredRejection(ProductFamilyCatalogContract):
    family: ProductFamilyRecord
    family_version_id: str = Field(min_length=1, max_length=80)
    version_no: int = Field(ge=1)
    created_at: datetime
    issues: tuple[ProductFamilyValidationIssue, ...] = Field(min_length=1)


class ProductFamilyCatalogRejection(ProductFamilyCatalogContract):
    family_version_ref: str = Field(min_length=1, max_length=16_384)
    version_no: int = Field(ge=1)
    code: Literal["PRODUCT_FAMILY_CONTRACT_INVALID"] = "PRODUCT_FAMILY_CONTRACT_INVALID"
    field_path: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ProductFamilyDiscoveryPage(ProductFamilyCatalogContract):
    """One bounded scan, including rejected rows in its continuation boundary."""

    entries: tuple[ProductFamilyListEntry, ...] = Field(max_length=100)
    rejections: tuple[ProductFamilyStoredRejection, ...] = Field(default=(), max_length=100)
    complete: bool
    next_before_created_at: datetime | None = None
    next_before_family_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("next_before_created_at")
    @classmethod
    def validate_boundary_time(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Family scan boundary must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_scan(self) -> "ProductFamilyDiscoveryPage":
        keys = [(entry.family.created_at, entry.family.family_id) for entry in self.entries]
        if any(left <= right for left, right in zip(keys, keys[1:])):
            raise ValueError("Family scan entries must be strictly descending")
        keys.extend((item.family.created_at, item.family.family_id) for item in self.rejections)
        if len(keys) > 100 or len(keys) != len(set(keys)):
            raise ValueError("Family scan must contain at most 100 distinct roots")
        has_boundary = self.next_before_created_at is not None
        if has_boundary != (self.next_before_family_id is not None) or self.complete == has_boundary:
            raise ValueError("Family scan continuation disagrees with completion")
        if has_boundary and (not keys or min(keys) != (self.next_before_created_at, self.next_before_family_id)):
            raise ValueError("Family scan continuation must match its last scanned root")
        return self


class ProductFamilyMetadataPage(ProductFamilyCatalogContract):
    versions: tuple[ProductFamilyVersionMetadata, ...] = Field(max_length=100)
    complete: bool
    next_before_version_no: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_page(self) -> "ProductFamilyMetadataPage":
        if self.complete == (self.next_before_version_no is not None):
            raise ValueError("Family metadata continuation disagrees with completion")
        numbers = tuple(item.version_no for item in self.versions)
        if any(left <= right for left, right in zip(numbers, numbers[1:])):
            raise ValueError("Family metadata versions must be strictly descending")
        if self.next_before_version_no is not None and (not numbers or numbers[-1] != self.next_before_version_no):
            raise ValueError("Family metadata continuation must match its last row")
        return self


class ProductFamilyProgramSource(ProductFamilyCatalogContract):
    """Authorized source ownership only; never a decoded or executable contract."""

    family: ProductFamilyRecord
    family_version_id: str = Field(min_length=1, max_length=80)
    version_no: int = Field(ge=1)
    created_at: datetime
    source_asset: ProductFamilyVersionAssetRecord

    @model_validator(mode="after")
    def validate_source_owner(self) -> "ProductFamilyProgramSource":
        if (self.source_asset.family_version_id != self.family_version_id
                or self.source_asset.asset_role is not ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE):
            raise ValueError("Family source must belong to the exact version")
        return self


class ProductFamilyProgramSourcePage(ProductFamilyCatalogContract):
    sources: tuple[ProductFamilyProgramSource, ...] = Field(max_length=50)
    rejections: tuple[ProductFamilyStoredRejection, ...] = Field(default=(), max_length=50)
    complete: bool
    next_before_created_at: datetime | None = None
    next_before_family_version_id: str | None = Field(default=None, min_length=1, max_length=80)

    _validate_boundary_time = field_validator("next_before_created_at")(
        ProductFamilyDiscoveryPage.validate_boundary_time.__func__
    )

    @model_validator(mode="after")
    def validate_scan(self) -> "ProductFamilyProgramSourcePage":
        keys = [(item.created_at, item.family_version_id) for item in self.sources]
        keys.extend((item.created_at, item.family_version_id) for item in self.rejections)
        if len(keys) > 50 or len(keys) != len(set(keys)):
            raise ValueError("Family source scan must contain at most 50 distinct versions")
        has_boundary = self.next_before_created_at is not None
        if has_boundary != (self.next_before_family_version_id is not None) or self.complete == has_boundary:
            raise ValueError("Family source continuation disagrees with completion")
        if has_boundary and (not keys or min(keys) != (self.next_before_created_at, self.next_before_family_version_id)):
            raise ValueError("Family source continuation must match its last scanned version")
        return self


class ProductFamilyCatalogProgramSourcePage(ProductFamilyCatalogContract):
    sources: tuple[ProductFamilyProgramSource, ...] = Field(max_length=50)
    rejections: tuple[ProductFamilyCatalogRejection, ...] = ()
    complete: bool
    next_before_created_at: datetime | None = None
    next_before_family_version_id: str | None = None

class ProductFamilyCatalogListQuery(ProductFamilyCatalogContract):
    owner_scopes: tuple[ResourceOwnerScopeKind, ...] = ()
    product_structure_kinds: tuple[ProductFamilyStructureKind, ...] = ()
    purpose_contains: str | None = Field(default=None, min_length=1, max_length=128)
    page_size: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_filters(self) -> "ProductFamilyCatalogListQuery":
        if len(self.owner_scopes) != len(set(self.owner_scopes)):
            raise ValueError("product family owner scope filters must be unique")
        if len(self.product_structure_kinds) != len(set(self.product_structure_kinds)):
            raise ValueError("product family structure filters must be unique")
        return self


class ProductFamilyCatalogVersionSummary(ProductFamilyCatalogContract):
    family_version_ref: str = Field(min_length=1, max_length=16_384)
    version_no: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    product_structure_kind: ProductFamilyStructureKind
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class ProductFamilyCatalogFamilySummary(ProductFamilyCatalogContract):
    family_root_ref: str = Field(min_length=1, max_length=16_384)
    owner_scope: ResourceOwnerScopeKind
    latest_version: ProductFamilyCatalogVersionSummary
    created_at: datetime


class ProductFamilyCatalogListResult(ProductFamilyCatalogContract):
    families: tuple[ProductFamilyCatalogFamilySummary, ...] = Field(max_length=100)
    returned_count: int = Field(ge=0, le=100)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=16_384)
    rejections: tuple[ProductFamilyCatalogRejection, ...] = ()

    @model_validator(mode="after")
    def validate_count(self) -> "ProductFamilyCatalogListResult":
        if self.returned_count != len(self.families):
            raise ValueError("returned_count must equal product families length")
        return self


class ProductFamilyCatalogRootInfo(ProductFamilyCatalogContract):
    target_kind: Literal["family_root"] = "family_root"
    family: ProductFamilyCatalogFamilySummary
    versions: tuple[ProductFamilyCatalogVersionSummary, ...] = Field(max_length=100)
    versions_complete: bool
    next_before_version_no: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_versions(self) -> "ProductFamilyCatalogRootInfo":
        if not self.versions:
            raise ValueError("ready product family root must contain a version")
        if self.versions[0].version_no > self.family.latest_version.version_no:
            raise ValueError("product family version page exceeds the latest version")
        if self.versions_complete == (self.next_before_version_no is not None):
            raise ValueError("product family version continuation disagrees with completion")
        return self


class ProductFamilyCatalogGeometryBindingSummary(ProductFamilyCatalogContract):
    binding_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", max_length=64)
    geometry_resource_version_ref: str = Field(min_length=1, max_length=16_384)
    geometry_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_resource_kind: GeometryResourceKind
    geometry_purpose: ProductFamilyGeometryPurpose


class ProductFamilyCatalogAssetSummary(ProductFamilyCatalogContract):
    asset_role: str = Field(min_length=1, max_length=64)
    asset_format: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProductFamilyCatalogVersionInfo(ProductFamilyCatalogContract):
    target_kind: Literal["family_version"] = "family_version"
    family_root_ref: str = Field(min_length=1, max_length=16_384)
    family_version_ref: str = Field(min_length=1, max_length=16_384)
    parent_family_version_ref: str | None = Field(default=None, min_length=1, max_length=16_384)
    owner_scope: ResourceOwnerScopeKind
    version_no: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    usage_instructions: str | None = None
    product_structure_kind: ProductFamilyStructureKind
    family_contract: ProductFamilyContract
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    program_entry: Literal["main.py:realize"]
    geometry_bindings: tuple[ProductFamilyCatalogGeometryBindingSummary, ...] = Field(min_length=1)
    assets: tuple[ProductFamilyCatalogAssetSummary, ...] = Field(min_length=1)
    created_at: datetime


ProductFamilyCatalogInfoResult = ProductFamilyCatalogRootInfo | ProductFamilyCatalogVersionInfo


__all__ = [name for name in globals() if name.startswith("ProductFamilyCatalog")]
