"""Safe application projections for the ready Geometry Resource Catalog."""

from __future__ import annotations

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D

import hashlib
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_serializer,
    model_validator,
)

from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceKind,
    GeometryResourceScopeKind,
    GeometryResourceVersionBundle,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    GeometryPreviewProfile,
    GeometryPreviewRenderMetadata,
    GeometryRenderedPreviewMetadata,
    GeometryRenderHostOverlay,
    GeometryRenderInterfaceOverlay,
    GeometryRenderScene,
    GeometryRenderReference,
    CanonicalGeometryFormat,
    GeometryRenderStraightProfileOverlay,
    GeometryStoredPreviewMetadata,
)
from CAD2BIM_backend.domain.text_source import TextSourceViewResult


class GeometryCatalogError(Exception):
    code: ClassVar[str] = "GEOMETRY_CATALOG_ERROR"


class GeometryCatalogReferenceError(GeometryCatalogError, ValueError):
    code = "GEOMETRY_RESOURCE_REFERENCE_INVALID"


class GeometryCatalogNotFoundError(GeometryCatalogError, LookupError):
    code = "GEOMETRY_RESOURCE_NOT_FOUND"


class GeometryCatalogIntegrityError(GeometryCatalogError, RuntimeError):
    code = "GEOMETRY_RESOURCE_INTEGRITY_ERROR"


class GeometrySourceUnavailableError(GeometryCatalogError, LookupError):
    code = "GEOMETRY_SOURCE_UNAVAILABLE"


class GeometrySourceBundleError(GeometryCatalogError, ValueError):
    code = "GEOMETRY_SOURCE_BUNDLE_INVALID"


class GeometrySourceReadError(GeometryCatalogError, ValueError):
    code = "GEOMETRY_SOURCE_READ_INVALID"


class GeometrySourceNotTextError(GeometrySourceReadError):
    code = "GEOMETRY_SOURCE_NOT_TEXT"


class GeometryPreviewUnavailableError(GeometryCatalogError, RuntimeError):
    code = "GEOMETRY_PREVIEW_UNAVAILABLE"


class GeometryCatalogContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GeometryCatalogListQuery(GeometryCatalogContract):
    resource_kinds: tuple[GeometryResourceKind, ...] = ()
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_filters(self) -> "GeometryCatalogListQuery":
        if len(self.resource_kinds) != len(set(self.resource_kinds)):
            raise ValueError("geometry resource kind filters must be unique")
        return self


class GeometryResourceVersionListQuery(GeometryCatalogContract):
    """Repository-only exact-root version boundary, ordered newest first."""

    resource_id: str = Field(min_length=1, max_length=80)
    before_version_no: int | None = Field(default=None, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class GeometryResourceVersionPage(GeometryCatalogContract):
    versions: tuple[GeometryResourceVersionBundle, ...] = Field(max_length=100)
    complete: bool
    next_before_version_no: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_page(self) -> "GeometryResourceVersionPage":
        if self.complete == (self.next_before_version_no is not None):
            raise ValueError("geometry resource version continuation disagrees with completion")
        if self.next_before_version_no is not None:
            if not self.versions:
                raise ValueError("incomplete version page must contain versions")
            if self.next_before_version_no != self.versions[-1].version.version_no:
                raise ValueError("version continuation must match the last version")
        numbers = tuple(item.version.version_no for item in self.versions)
        if any(left <= right for left, right in zip(numbers, numbers[1:])):
            raise ValueError("geometry resource versions must be strictly descending")
        return self


class GeometryCatalogAssetSummary(GeometryCatalogContract):
    asset_role: str = Field(min_length=1, max_length=64)
    asset_format: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GeometryCatalogVersionSummary(GeometryCatalogContract):
    resource_version_ref: str = Field(min_length=1, max_length=16_384)
    version_no: int = Field(ge=1)
    origin_kind: str = Field(min_length=1, max_length=64)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_available: bool
    preview_available: bool
    created_at: datetime


class GeometryCatalogResourceSummary(GeometryCatalogContract):
    resource_root_ref: str = Field(min_length=1, max_length=16_384)
    resource_kind: GeometryResourceKind
    owner_scope: GeometryResourceScopeKind
    display_name: str = Field(min_length=1, max_length=255)
    latest_version: GeometryCatalogVersionSummary
    created_at: datetime


class GeometryCatalogListResult(GeometryCatalogContract):
    resources: tuple[GeometryCatalogResourceSummary, ...] = Field(max_length=100)
    returned_count: int = Field(ge=0, le=100)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def validate_count(self) -> "GeometryCatalogListResult":
        if self.returned_count != len(self.resources):
            raise ValueError("returned_count must equal resources length")
        return self


class GeometryCatalogRootInfo(GeometryCatalogContract):
    target_kind: Literal["resource_root"] = "resource_root"
    resource: GeometryCatalogResourceSummary
    recent_versions: tuple[GeometryCatalogVersionSummary, ...] = Field(max_length=20)
    recent_versions_complete: bool

    @model_validator(mode="after")
    def validate_versions(self) -> "GeometryCatalogRootInfo":
        if not self.recent_versions:
            raise ValueError("ready geometry resource root must contain a version")
        if self.recent_versions[0] != self.resource.latest_version:
            raise ValueError("root latest version must head recent_versions")
        return self


class GeometryCatalogVersionInfo(GeometryCatalogContract):
    target_kind: Literal["resource_version"] = "resource_version"
    resource_root_ref: str = Field(min_length=1, max_length=16_384)
    resource_kind: GeometryResourceKind
    owner_scope: GeometryResourceScopeKind
    display_name: str = Field(min_length=1, max_length=255)
    version: GeometryCatalogVersionSummary
    manifest: dict[str, JsonValue]
    lineage: dict[str, JsonValue]
    assets: tuple[GeometryCatalogAssetSummary, ...] = Field(min_length=1, max_length=16)


GeometryCatalogInfoResult = GeometryCatalogRootInfo | GeometryCatalogVersionInfo


class GeometrySourceMember(GeometryCatalogContract):
    source_path: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_available: bool

    @field_validator("source_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if "\\" in value or value.startswith("/") or "//" in value:
            raise ValueError("source path must be a normalized relative POSIX path")
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("source path must not contain empty or traversal segments")
        return value


class GeometrySourceReadResult(GeometryCatalogContract):
    resource_version_ref: str = Field(min_length=1, max_length=16_384)
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    members: tuple[GeometrySourceMember, ...] = Field(min_length=1, max_length=64)
    selected_path: str | None = Field(default=None, min_length=1, max_length=255)
    view: TextSourceViewResult | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> "GeometrySourceReadResult":
        if (self.selected_path is None) != (self.view is None):
            raise ValueError("selected source path and source view must be returned together")
        if self.selected_path is not None and self.selected_path not in {
            member.source_path for member in self.members
        }:
            raise ValueError("selected source path is absent from bundle members")
        return self




def _validate_authorized_overlay_projection(
    render_metadata: GeometryPreviewRenderMetadata | None,
    interface_overlays: tuple[GeometryRenderInterfaceOverlay, ...],
    host_overlays: tuple[GeometryRenderHostOverlay, ...],
    straight_profile_overlays: tuple[GeometryRenderStraightProfileOverlay, ...],
    *,
    label: str,
) -> None:
    overlay_ids = tuple(overlay.overlay_id for overlay in interface_overlays)
    if len(overlay_ids) != len(set(overlay_ids)):
        raise ValueError(f"{label} interface overlay ids must be unique")
    if render_metadata is None or isinstance(render_metadata, GeometryStoredPreviewMetadata):
        if interface_overlays or host_overlays or straight_profile_overlays:
            raise ValueError(f"{label} display overlays require rendered metadata")
        return
    available = set(overlay_ids)
    if any(
        not set(panel.visible_overlay_ids + panel.hidden_overlay_ids).issubset(available)
        for panel in render_metadata.resolved_panels
    ):
        raise ValueError(f"{label} panel discloses an absent interface overlay")
    GeometryRenderScene(
        coordinate_space=render_metadata.coordinate_space,
        geometry_sha256="0" * 64,
        display_context_sha256=(
            "0" * 64
            if any(item.display_kind != "original" for item in straight_profile_overlays)
            else None
        ),
        interface_overlays=interface_overlays,
        host_overlays=host_overlays,
        straight_profile_overlays=straight_profile_overlays,
    )


class GeometryPreviewReference(GeometryCatalogContract):
    reference_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    resource_version_ref: str = Field(min_length=1, max_length=16_384)
    datum_transform: RigidTransform3D


class GeometryPreviewReferenceSnapshot(GeometryPreviewReference):
    resource_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry_format: CanonicalGeometryFormat

    @property
    def render_reference(self) -> GeometryRenderReference:
        return GeometryRenderReference(reference_key=self.reference_key, geometry_sha256=self.geometry_sha256,
            geometry_format=self.geometry_format, datum_transform=self.datum_transform)


class GeometryPreviewAuthorization(GeometryCatalogContract):
    reference_geometries: tuple[GeometryPreviewReferenceSnapshot, ...] = Field(default=(), max_length=8)
    sample_parameters_json: str | None = Field(default=None, min_length=2, max_length=16 * 1024)
    datum_transform: RigidTransform3D | None = None
    resource_version_ref: str = Field(min_length=1, max_length=16_384)
    title: str = Field(min_length=1, max_length=255)
    mime_type: Literal["image/png"] = "image/png"
    size_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: bytes = Field(min_length=1, max_length=16 * 1024 * 1024)
    render_metadata: GeometryPreviewRenderMetadata | None = None
    interface_overlays: tuple[GeometryRenderInterfaceOverlay, ...] = Field(
        default=(),
        max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    )
    host_overlays: tuple[GeometryRenderHostOverlay, ...] = Field(
        default=(), max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS
    )
    straight_profile_overlays: tuple[GeometryRenderStraightProfileOverlay, ...] = Field(
        default=(), max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS
    )
    derived_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_exact_content(self) -> "GeometryPreviewAuthorization":
        if self.datum_transform is not None and (
            not isinstance(self.render_metadata, GeometryRenderedPreviewMetadata)
            or self.render_metadata.coordinate_space.value != "project_world"
        ):
            raise ValueError("placed preview requires project-world render coordinates")
        if len(self.content) != self.size_bytes:
            raise ValueError("geometry preview content size changed")
        if hashlib.sha256(self.content).hexdigest() != self.content_sha256:
            raise ValueError("geometry preview content digest changed")
        _validate_authorized_overlay_projection(
            self.render_metadata,
            self.interface_overlays,
            self.host_overlays,
            self.straight_profile_overlays,
            label="preview",
        )
        return self

    @model_serializer(mode="wrap")
    def omit_absent_authorization_fields(self, handler):
        result = handler(self)
        if self.sample_parameters_json is None:
            result.pop("sample_parameters_json", None)
        if self.datum_transform is None:
            result.pop("datum_transform", None)
        if self.render_metadata is None:
            result.pop("render_metadata", None)
        if not self.interface_overlays:
            result.pop("interface_overlays", None)
        if not self.host_overlays:
            result.pop("host_overlays", None)
        if not self.straight_profile_overlays:
            result.pop("straight_profile_overlays", None)
        return result


class ProductRealizationPreviewAuthorization(GeometryCatalogContract):
    reference_geometries: tuple[GeometryPreviewReferenceSnapshot, ...] = Field(default=(), max_length=8)
    family_version_ref: str = Field(min_length=1, max_length=16_384)
    title: str = Field(min_length=1, max_length=255)
    mime_type: Literal["image/png"] = "image/png"
    size_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: bytes = Field(min_length=1, max_length=16 * 1024 * 1024)
    render_metadata: GeometryPreviewRenderMetadata | None = None
    interface_overlays: tuple[GeometryRenderInterfaceOverlay, ...] = Field(
        default=(),
        max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    )
    host_overlays: tuple[GeometryRenderHostOverlay, ...] = Field(
        default=(), max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS
    )
    straight_profile_overlays: tuple[GeometryRenderStraightProfileOverlay, ...] = Field(
        default=(), max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS
    )
    frame_check_status: Literal["PASS", "FAIL"] | None = Field(default=None, description=(
        "The saved Family's numerical result for declared internal interface relations and its "
        "invocation external conditions, not a current cross-product installation result."))
    derived_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_exact_content(self) -> "ProductRealizationPreviewAuthorization":
        if len(self.content) != self.size_bytes:
            raise ValueError("product realization preview content size changed")
        if hashlib.sha256(self.content).hexdigest() != self.content_sha256:
            raise ValueError("product realization preview content digest changed")
        _validate_authorized_overlay_projection(
            self.render_metadata,
            self.interface_overlays,
            self.host_overlays,
            self.straight_profile_overlays,
            label="product realization preview",
        )
        return self

    @model_serializer(mode="wrap")
    def omit_absent_authorization_fields(self, handler):
        result = handler(self)
        if self.render_metadata is None:
            result.pop("render_metadata", None)
        if not self.interface_overlays:
            result.pop("interface_overlays", None)
        if not self.host_overlays:
            result.pop("host_overlays", None)
        if not self.straight_profile_overlays:
            result.pop("straight_profile_overlays", None)
        if self.frame_check_status is None:
            result.pop("frame_check_status", None)
        return result


__all__ = [
    "GeometryCatalogAssetSummary",
    "GeometryCatalogError",
    "GeometryCatalogInfoResult",
    "GeometryCatalogIntegrityError",
    "GeometryCatalogListQuery",
    "GeometryCatalogListResult",
    "GeometryCatalogNotFoundError",
    "GeometryCatalogReferenceError",
    "GeometryCatalogResourceSummary",
    "GeometryCatalogRootInfo",
    "GeometryCatalogVersionInfo",
    "GeometryCatalogVersionSummary",
    "GeometryResourceVersionListQuery",
    "GeometryResourceVersionPage",
    "GeometryPreviewAuthorization",
    "GeometryPreviewProfile",
    "GeometryPreviewUnavailableError",
    "ProductRealizationPreviewAuthorization",
    "GeometrySourceBundleError",
    "GeometrySourceMember",
    "GeometrySourceNotTextError",
    "GeometrySourceReadError",
    "GeometrySourceReadResult",
    "GeometrySourceUnavailableError",
]
