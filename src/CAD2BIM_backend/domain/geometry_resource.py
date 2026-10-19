"""Durable identities for reusable geometry resources.

These server-side contracts describe one stable resource root, its immutable
versions, and the exact assets that make a version usable.  Provider aliases,
authoring attempts, Operations, and physical storage paths intentionally live
outside this module.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.version_usage import validate_version_usage_instructions
from CAD2BIM_backend.domain.geometry_runtime import MAX_GEOMETRY_BUILD_INPUTS, GEOMETRY_INPUT_KEY_PATTERN
from CAD2BIM_backend.domain.resource_scope import (
    ResourceOwnerScope,
    ResourceOwnerScopeKind,
)


_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("geometry resource timestamps must be timezone-aware")
    return value


class GeometryResourceContract(BaseModel):
    """Closed, immutable base for internal geometry-resource contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GeometryResourceKind(StrEnum):
    MAPPED_MODEL = "mapped_model"
    MODELING_FUNCTION = "modeling_function"


GeometryResourceScopeKind = ResourceOwnerScopeKind


class GeometryResourceAssetRole(StrEnum):
    """Closed logical roles; one version may bind each role at most once."""

    CANONICAL_GEOMETRY = "canonical_geometry"
    MANIFEST = "manifest"
    VALIDATION_REPORT = "validation_report"
    SOURCE_BUNDLE = "source_bundle"
    PARAMETER_SCHEMA = "parameter_schema"
    PREVIEW = "preview"
    INSPECTION = "inspection"
    SAMPLE_OUTPUT = "sample_output"


MAPPED_MODEL_REQUIRED_ASSET_ROLES = frozenset(
    {
        GeometryResourceAssetRole.CANONICAL_GEOMETRY,
        GeometryResourceAssetRole.MANIFEST,
    }
)
MAPPED_MODEL_OPTIONAL_ASSET_ROLES = frozenset(
    {
        GeometryResourceAssetRole.VALIDATION_REPORT,  # Decode historical rows only; current reuse rejects it.
        GeometryResourceAssetRole.SOURCE_BUNDLE,
        GeometryResourceAssetRole.PREVIEW,
        GeometryResourceAssetRole.INSPECTION,
    }
)
MODELING_FUNCTION_REQUIRED_ASSET_ROLES = frozenset(
    {
        GeometryResourceAssetRole.SOURCE_BUNDLE,
        GeometryResourceAssetRole.PARAMETER_SCHEMA,
    }
)
MODELING_FUNCTION_OPTIONAL_ASSET_ROLES = frozenset(
    {
        GeometryResourceAssetRole.VALIDATION_REPORT,  # Decode historical rows only; current reuse rejects it.
        GeometryResourceAssetRole.PREVIEW,
        GeometryResourceAssetRole.INSPECTION,
        GeometryResourceAssetRole.SAMPLE_OUTPUT,
    }
)


GeometryResourceScope = ResourceOwnerScope


class GeometryResourceInput(GeometryResourceContract):
    """Frozen exact fixed-BREP dependency declared by the consuming version."""

    input_key: str = Field(pattern=GEOMETRY_INPUT_KEY_PATTERN)
    resource_id: str = Field(min_length=1, max_length=80)
    resource_version_id: str = Field(min_length=1, max_length=80)
    content_digest: str = Field(pattern=_SHA256_PATTERN)
    canonical_geometry_sha256: str = Field(pattern=_SHA256_PATTERN)


def _validate_resource_inputs(values: tuple[GeometryResourceInput, ...]) -> tuple[GeometryResourceInput, ...]:
    if len({item.input_key for item in values}) != len(values):
        raise ValueError("geometry resource input keys must be unique")
    return tuple(sorted(values, key=lambda item: item.input_key))


GeometryResourceInputs = Annotated[
    tuple[GeometryResourceInput, ...], Field(max_length=MAX_GEOMETRY_BUILD_INPUTS),
    AfterValidator(_validate_resource_inputs),
]
_INPUTS_ADAPTER = TypeAdapter(GeometryResourceInputs)


def geometry_resource_inputs(manifest: dict[str, JsonValue]) -> tuple[GeometryResourceInput, ...]:
    """Read the one declaration, including current versions with no dependencies."""
    return _INPUTS_ADAPTER.validate_python(manifest.get("geometry_inputs", ()))


def require_formal_geometry_publication(version, assets) -> None:
    """Require the same formal facts at publication and before current reuse."""
    if any(asset.asset_role is GeometryResourceAssetRole.VALIDATION_REPORT for asset in assets):
        raise ValueError("new Geometry versions cannot retain validation reports")
    if set(version.manifest) & {"validation_profile", "validation_samples", "sample_executions", "samples"}:
        raise ValueError("new Geometry manifests cannot retain validation cases")


class GeometryResourceRecord(GeometryResourceContract):
    """Stable identity for one reusable model or modeling function."""

    resource_id: str = Field(min_length=1, max_length=80)
    resource_kind: GeometryResourceKind
    owner_scope: GeometryResourceScope
    display_name: str = Field(min_length=1, max_length=255)
    created_at: datetime

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("geometry resource display name contains control characters")
        return value.strip()

    _validate_created_at = field_validator("created_at")(_require_aware)


class GeometryResourceVersionRecord(GeometryResourceContract):
    """One immutable version of a stable geometry resource root."""

    resource_version_id: str = Field(min_length=1, max_length=80)
    resource_id: str = Field(min_length=1, max_length=80)
    version_no: int = Field(ge=1)
    origin_kind: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    content_digest: str = Field(pattern=_SHA256_PATTERN)
    manifest_digest: str = Field(pattern=_SHA256_PATTERN)
    manifest: dict[str, JsonValue]
    lineage: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("manifest", "lineage")
    @classmethod
    def require_canonical_json_domain(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        canonical_json(value)
        return value

    _validate_created_at = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def validate_manifest_digest(self) -> Self:
        geometry_resource_inputs(self.manifest)
        if "usage_instructions" in self.manifest:
            value = self.manifest["usage_instructions"]
            validate_version_usage_instructions(
                value if isinstance(value, str) else None,
                required=True,
            )
        if self.manifest_digest != sha256_digest(self.manifest):
            raise ValueError("manifest_digest does not match the canonical manifest")
        return self


class GeometryResourceVersionAssetRecord(GeometryResourceContract):
    """Exact immutable bytes bound to one resource version and logical role."""

    resource_version_id: str = Field(min_length=1, max_length=80)
    asset_role: GeometryResourceAssetRole
    asset_format: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    created_at: datetime

    @field_validator("filename")
    @classmethod
    def require_filename(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError("geometry resource asset filename must not contain a path")
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("geometry resource asset filename contains control characters")
        return value

    _validate_created_at = field_validator("created_at")(_require_aware)


def geometry_resource_content_digest(
    *,
    resource_kind: GeometryResourceKind,
    manifest_digest: str,
    assets: Sequence[GeometryResourceVersionAssetRecord],
) -> str:
    """Digest the exact content identity while excluding origin and projections."""

    ordered_assets = sorted(assets, key=lambda asset: asset.asset_role.value)
    return sha256_digest(
        {
            "domain": "geometry_resource_version_content",
            "resource_kind": resource_kind.value,
            "manifest_digest": manifest_digest,
            "assets": [
                {
                    "asset_role": asset.asset_role.value,
                    "asset_format": asset.asset_format,
                    "filename": asset.filename,
                    "content_type": asset.content_type,
                    "size_bytes": asset.size_bytes,
                    "sha256": asset.sha256,
                }
                for asset in ordered_assets
            ],
        }
    )


class GeometryResourceVersionBundle(GeometryResourceContract):
    """Atomic repository publication/readback unit for one exact version."""

    resource: GeometryResourceRecord
    version: GeometryResourceVersionRecord
    assets: tuple[GeometryResourceVersionAssetRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_exact_binding(self) -> Self:
        if self.version.resource_id != self.resource.resource_id:
            raise ValueError("geometry resource version belongs to a different resource root")
        if any(
            asset.resource_version_id != self.version.resource_version_id for asset in self.assets
        ):
            raise ValueError("geometry resource asset belongs to a different exact version")

        roles = tuple(asset.asset_role for asset in self.assets)
        role_set = frozenset(roles)
        if len(roles) != len(role_set):
            raise ValueError("geometry resource version asset roles must be unique")

        if self.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL:
            required = MAPPED_MODEL_REQUIRED_ASSET_ROLES
            allowed = required | MAPPED_MODEL_OPTIONAL_ASSET_ROLES
        else:
            required = MODELING_FUNCTION_REQUIRED_ASSET_ROLES
            allowed = required | MODELING_FUNCTION_OPTIONAL_ASSET_ROLES

        missing = required - role_set
        unsupported = role_set - allowed
        if missing:
            names = ", ".join(sorted(role.value for role in missing))
            raise ValueError(f"geometry resource version is missing required assets: {names}")
        if unsupported:
            names = ", ".join(sorted(role.value for role in unsupported))
            raise ValueError(f"asset roles are incompatible with resource kind: {names}")
        manifest_asset = next(
            (
                asset
                for asset in self.assets
                if asset.asset_role is GeometryResourceAssetRole.MANIFEST
            ),
            None,
        )
        if manifest_asset is not None:
            canonical_manifest = canonical_json(self.version.manifest).encode("utf-8")
            if (
                manifest_asset.sha256 != self.version.manifest_digest
                or manifest_asset.size_bytes != len(canonical_manifest)
            ):
                raise ValueError("manifest asset does not match the canonical version manifest")
        expected_content_digest = geometry_resource_content_digest(
            resource_kind=self.resource.resource_kind,
            manifest_digest=self.version.manifest_digest,
            assets=self.assets,
        )
        if self.version.content_digest != expected_content_digest:
            raise ValueError(
                "content_digest does not match the exact geometry resource version assets"
            )
        return self


class GeometryResourceListQuery(GeometryResourceContract):
    """Bounded repository query; Provider cursor encoding is an outer concern."""

    resource_kinds: tuple[GeometryResourceKind, ...] = ()
    before_created_at: datetime | None = None
    before_resource_id: str | None = Field(default=None, min_length=1, max_length=80)
    page_size: int = Field(default=50, ge=1, le=100)

    @field_validator("before_created_at")
    @classmethod
    def require_aware_boundary(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if len(self.resource_kinds) != len(set(self.resource_kinds)):
            raise ValueError("geometry resource kind filters must be unique")
        if (self.before_created_at is None) != (self.before_resource_id is None):
            raise ValueError("geometry resource list boundary requires timestamp and resource ID")
        return self


class GeometryResourcePage(GeometryResourceContract):
    """Internal deterministic page ordered by ``created_at, resource_id`` descending."""

    resources: tuple[GeometryResourceRecord, ...] = Field(max_length=100)
    complete: bool
    next_before_created_at: datetime | None = None
    next_before_resource_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("next_before_created_at")
    @classmethod
    def require_aware_boundary(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        has_boundary = (
            self.next_before_created_at is not None and self.next_before_resource_id is not None
        )
        if (self.next_before_created_at is None) != (self.next_before_resource_id is None):
            raise ValueError("geometry resource page continuation is incomplete")
        if self.complete == has_boundary:
            raise ValueError("geometry resource page completion and continuation disagree")
        if has_boundary:
            if not self.resources:
                raise ValueError("incomplete geometry resource page must contain resources")
            last = self.resources[-1]
            if (
                self.next_before_created_at != last.created_at
                or self.next_before_resource_id != last.resource_id
            ):
                raise ValueError("geometry resource page continuation must match its last row")
        keys = tuple((item.created_at, item.resource_id) for item in self.resources)
        if any(left <= right for left, right in zip(keys, keys[1:])):
            raise ValueError("geometry resource page must be strictly descending")
        return self


__all__ = [
    "GeometryResourceAssetRole",
    "GeometryResourceContract",
    "GeometryResourceKind",
    "GeometryResourceListQuery",
    "GeometryResourcePage",
    "GeometryResourceRecord",
    "GeometryResourceScope",
    "GeometryResourceScopeKind",
    "GeometryResourceVersionAssetRecord",
    "GeometryResourceVersionBundle",
    "GeometryResourceVersionRecord",
    "MAPPED_MODEL_OPTIONAL_ASSET_ROLES",
    "MAPPED_MODEL_REQUIRED_ASSET_ROLES",
    "MODELING_FUNCTION_OPTIONAL_ASSET_ROLES",
    "MODELING_FUNCTION_REQUIRED_ASSET_ROLES",
    "geometry_resource_content_digest",
]
