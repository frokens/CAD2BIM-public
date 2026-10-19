"""Trusted manifest contracts for version-controlled SYSTEM geometry seeds."""

from __future__ import annotations

import math
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryNormalizeRequest,
    GeometrySourceFormat,
)
from CAD2BIM_backend.domain.identity import stable_internal_identity_id


_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class SystemGeometrySeedContract(BaseModel):
    """Closed immutable base for trusted seed input."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SystemGeometryLocalFrame(SystemGeometrySeedContract):
    """Explicit metre-based right-handed local frame for normalized geometry."""

    handedness: Literal["right"]
    origin: tuple[float, float, float]
    x_axis: tuple[float, float, float]
    y_axis: tuple[float, float, float]
    z_axis: tuple[float, float, float]

    @field_validator("origin", "x_axis", "y_axis", "z_axis")
    @classmethod
    def require_finite_vector(
        cls,
        value: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        if not all(math.isfinite(component) for component in value):
            raise ValueError("system geometry seed frame vectors must be finite")
        return value

    @model_validator(mode="after")
    def require_orthonormal_right_handed_basis(self) -> Self:
        axes = (self.x_axis, self.y_axis, self.z_axis)
        for axis in axes:
            if not math.isclose(sum(value * value for value in axis), 1.0, abs_tol=1e-9):
                raise ValueError("system geometry seed frame axes must be unit vectors")
        if any(
            not math.isclose(
                sum(left[index] * right[index] for index in range(3)),
                0.0,
                abs_tol=1e-9,
            )
            for left, right in ((axes[0], axes[1]), (axes[0], axes[2]), (axes[1], axes[2]))
        ):
            raise ValueError("system geometry seed frame axes must be orthogonal")
        cross = (
            self.x_axis[1] * self.y_axis[2] - self.x_axis[2] * self.y_axis[1],
            self.x_axis[2] * self.y_axis[0] - self.x_axis[0] * self.y_axis[2],
            self.x_axis[0] * self.y_axis[1] - self.x_axis[1] * self.y_axis[0],
        )
        if any(
            not math.isclose(cross[index], self.z_axis[index], abs_tol=1e-9)
            for index in range(3)
        ):
            raise ValueError("system geometry seed frame must be right-handed")
        return self


class SystemGeometrySeedEntry(SystemGeometrySeedContract):
    """One reviewed source and its expected normalized geometry identity."""

    seed_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    resource_kind: GeometryResourceKind
    display_name: str = Field(min_length=1, max_length=255)
    origin_kind: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    source_path: str = Field(min_length=1, max_length=255)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_format: GeometrySourceFormat
    target_asset_role: GeometryResourceAssetRole
    geometry_format: CanonicalGeometryFormat
    unit: Literal["METRE"]
    local_frame: SystemGeometryLocalFrame
    expected_content_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("source_path")
    @classmethod
    def require_safe_relative_source_path(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("system geometry seed source_path must be portable POSIX syntax")
        path = PurePosixPath(value)
        if path.is_absolute() or value != path.as_posix():
            raise ValueError("system geometry seed source_path must be relative and normalized")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("system geometry seed source_path must not traverse directories")
        return value

    @model_validator(mode="after")
    def require_source_target_for_kind(self) -> Self:
        expected_role = (
            GeometryResourceAssetRole.CANONICAL_GEOMETRY
            if self.resource_kind is GeometryResourceKind.MAPPED_MODEL
            else GeometryResourceAssetRole.SOURCE_BUNDLE
        )
        if self.target_asset_role is not expected_role:
            raise ValueError(
                "system geometry seed target_asset_role is incompatible with resource_kind"
            )
        GeometryNormalizeRequest(
            source_format=self.source_format,
            format=self.geometry_format,
            payload=b"manifest-shape-check",
            unit=self.unit,
        )
        return self


class SystemGeometrySeedManifest(SystemGeometrySeedContract):
    """Version-controlled production or neutral-test seed manifest."""

    resources: tuple[SystemGeometrySeedEntry, ...] = ()

    @model_validator(mode="after")
    def require_unique_seed_keys(self) -> Self:
        seed_keys = tuple(entry.seed_key for entry in self.resources)
        if len(seed_keys) != len(set(seed_keys)):
            raise ValueError("system geometry seed manifest seed_key values must be unique")
        return self


def system_geometry_seed_entry_digest(entry: SystemGeometrySeedEntry) -> str:
    """Return the canonical reviewed-entry identity used for idempotent replay."""

    return sha256_digest(entry.model_dump(mode="json"))


def system_geometry_seed_manifest_digest(manifest: SystemGeometrySeedManifest) -> str:
    """Return the canonical digest of the external manifest document."""

    return sha256_digest(manifest.model_dump(mode="json", by_alias=True))


def system_geometry_seed_resource_id(seed_key: str) -> str:
    """Return a deterministic opaque stable root ID without exposing a DB sequence."""

    return stable_internal_identity_id(
        "grs_",
        "cad2bim.system_geometry_seed.resource.v1",
        seed_key,
    )


def system_geometry_seed_version_id(
    seed_key: str,
    entry_digest: str,
) -> str:
    """Return the deterministic exact version ID for one reviewed entry digest."""

    return stable_internal_identity_id(
        "grsv_",
        "cad2bim.system_geometry_seed.version.v1",
        seed_key,
        entry_digest,
    )


__all__ = [
    "SystemGeometryLocalFrame",
    "SystemGeometrySeedContract",
    "SystemGeometrySeedEntry",
    "SystemGeometrySeedManifest",
    "system_geometry_seed_entry_digest",
    "system_geometry_seed_manifest_digest",
    "system_geometry_seed_resource_id",
    "system_geometry_seed_version_id",
]
