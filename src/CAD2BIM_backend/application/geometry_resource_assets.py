"""Pure mapped-asset assembly shared by trusted intake and manifest seeding."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry, CanonicalGeometryFormat, GeometrySummary,
)


@dataclass(frozen=True, slots=True)
class PreparedMappedGeometry:
    manifest: dict
    assets: tuple[tuple[GeometryResourceAssetRole, str, str, str, bytes], ...]


def prepare_mapped_geometry_assets(
    *, metadata: dict, source: bytes, source_bundle: bytes,
    normalized: CanonicalGeometry, reopened: GeometrySummary,
) -> PreparedMappedGeometry:
    """Validate exact runtime bytes and assemble formal mapped assets."""
    if normalized.format.value != metadata["geometry_format"]:
        raise ValueError("shared geometry runtime returned the wrong format")
    if hashlib.sha256(normalized.payload).hexdigest() != normalized.sha256:
        raise ValueError("shared geometry runtime returned a bad payload digest")
    asset_content_type = normalized.content_type.split(";", 1)[0].strip().lower()
    manifest = {
        **metadata,
        "asset_content_type": asset_content_type,
        "runtime_content_type": normalized.content_type,
        "canonical_geometry_sha256": normalized.sha256,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_bundle_sha256": hashlib.sha256(source_bundle).hexdigest(),
        "geometry_summary": reopened.model_dump(mode="json"),
    }
    if normalized.source_provenance is not None:
        if normalized.source_provenance.source_sha256 != manifest["source_sha256"]:
            raise ValueError("native geometry provenance source digest changed")
        manifest["source_provenance"] = normalized.source_provenance.model_dump(mode="json")
    geometry_filename = (
        "geometry.brep" if normalized.format is CanonicalGeometryFormat.OCCT_BREP_V1
        else "geometry.mesh.json"
    )
    return PreparedMappedGeometry(
        manifest=manifest,
        assets=(
            (GeometryResourceAssetRole.CANONICAL_GEOMETRY, normalized.format.value,
             geometry_filename, asset_content_type, normalized.payload),
            (GeometryResourceAssetRole.MANIFEST, "json", "manifest.json",
             "application/json", canonical_json(manifest).encode("utf-8")),
            (GeometryResourceAssetRole.SOURCE_BUNDLE, "source_bundle_v1", "source.zip",
             "application/zip", source_bundle),
        ),
    )
