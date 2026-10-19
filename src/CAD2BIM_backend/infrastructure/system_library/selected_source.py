"""Read an explicitly selected immutable Family/Geometry export for SYSTEM publication."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceVersionBundle,
    geometry_resource_inputs,
    require_formal_geometry_publication,
)
from CAD2BIM_backend.domain.product_family import ProductFamilyVersionAssetRole, ProductFamilyVersionBundle
from CAD2BIM_backend.domain.system_family_publication import SystemFamilyPublicationCommand
from CAD2BIM_backend.infrastructure.postgres.models import (
    GeometryResourceModel,
    GeometryResourceVersionAssetModel,
    GeometryResourceVersionModel,
    ProductFamilyGeometryBindingModel,
    ProductFamilyModel,
    ProductFamilyVersionAssetModel,
    ProductFamilyVersionModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import PostgresGeometryResourceRepositoryMixin
from CAD2BIM_backend.infrastructure.postgres.repository_product_family import PostgresProductFamilyRepositoryMixin


def _index(rows, key):
    indexed = {row[key]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError(f"selected library has duplicate {key} rows")
    return indexed


def _group(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    return groups


def _model(model_type, row):
    fields = dict(row)
    created_at = datetime.fromisoformat(fields["created_at"])
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("selected library timestamps must be timezone-aware")
    fields["created_at"] = created_at
    return model_type(**fields)


class SelectedLibrarySystemFamilySource:
    """One offline source; selection is explicit and grants no publication authority."""

    source_kind = "selected_library"

    def __init__(self, *, selector_path: Path, library_path: Path) -> None:
        selector_path, library_path = selector_path.resolve(strict=True), library_path.resolve(strict=True)
        self.source_reference = str(selector_path)
        self._asset_root = library_path.parent
        selector = json.loads(selector_path.read_text(encoding="utf-8"))
        library = json.loads(library_path.read_text(encoding="utf-8"))
        # Source environment, historical paths and auxiliary summaries are provenance.
        # The explicit exact selection and current Domain content remain authoritative.
        tables = library["tables"]
        expected_tables = {
            "geometry_resources", "geometry_resource_versions", "geometry_resource_version_assets",
            "geometry_resource_version_inputs", "product_families", "product_family_versions",
            "product_family_geometry_bindings", "product_family_version_assets",
        }
        if set(tables) != expected_tables:
            raise ValueError("selected library must contain exactly the current eight resource tables")
        self._family_selection = selector["families"]
        geometry_selection = selector["geometries"]
        family_versions = _index(tables["product_family_versions"], "family_version_id")
        family_roots = _index(tables["product_families"], "family_id")
        geometry_versions = _index(tables["geometry_resource_versions"], "resource_version_id")
        geometry_roots = _index(tables["geometry_resources"], "resource_id")
        if (set(family_versions) != {item["family_version_id"] for item in self._family_selection.values()}
            or set(family_roots) != {item["family_id"] for item in self._family_selection.values()}
            or len(family_versions) != len(self._family_selection)
            or set(geometry_versions) != set(geometry_selection)
            or set(geometry_roots) != {item["resource_id"] for item in geometry_selection.values()}):
            raise ValueError("selected library roots and exact versions do not match the explicit selector")

        geometry_assets = _group(tables["geometry_resource_version_assets"], "resource_version_id")
        family_assets = _group(tables["product_family_version_assets"], "family_version_id")
        family_bindings = _group(tables["product_family_geometry_bindings"], "family_version_id")
        if (set(geometry_assets) != set(geometry_versions) or set(family_assets) != set(family_versions)
            or set(family_bindings) != set(family_versions)):
            raise ValueError("selected library asset or binding rows are outside the selected exact closure")
        geometry_repository = PostgresGeometryResourceRepositoryMixin
        family_repository = PostgresProductFamilyRepositoryMixin
        self._geometries = {}
        for version_id, row in geometry_versions.items():
            bundle = GeometryResourceVersionBundle(
                resource=geometry_repository._geometry_resource_record(
                    _model(GeometryResourceModel, geometry_roots[row["resource_id"]])),
                version=geometry_repository._geometry_version_record(_model(GeometryResourceVersionModel, row)),
                assets=tuple(geometry_repository._geometry_asset_record(_model(GeometryResourceVersionAssetModel, asset))
                    for asset in sorted(geometry_assets[version_id], key=lambda item: item["asset_role"])),
            )
            require_formal_geometry_publication(bundle.version, bundle.assets)
            selected = geometry_selection[version_id]
            if (selected["resource_id"] != bundle.resource.resource_id
                or selected["content_digest"] != bundle.version.content_digest):
                raise ValueError(f"selected Geometry metadata disagrees with exact version {version_id}")
            self._geometries[(bundle.resource.resource_id, version_id)] = bundle
        self._families = {}
        for version_id, row in family_versions.items():
            bundle = ProductFamilyVersionBundle(
                family=family_repository._product_family_record(_model(ProductFamilyModel, family_roots[row["family_id"]])),
                version=family_repository._product_family_version_record(_model(ProductFamilyVersionModel, row)),
                geometry_bindings=tuple(family_repository._product_family_binding_record(
                    _model(ProductFamilyGeometryBindingModel, binding))
                    for binding in sorted(family_bindings[version_id], key=lambda item: item["binding_key"])),
                assets=tuple(family_repository._product_family_asset_record(_model(ProductFamilyVersionAssetModel, asset))
                    for asset in sorted(family_assets[version_id], key=lambda item: item["asset_role"])),
            )
            if any(asset.asset_role is ProductFamilyVersionAssetRole.VALIDATION_REPORT for asset in bundle.assets):
                raise ValueError("selected Family cannot retain a retired validation report")
            self._families[(bundle.family.family_id, version_id)] = bundle
        self._check_dependency_closure(tables["geometry_resource_version_inputs"])
        self._files = {}
        for entry in library["asset_files"]:
            key = (entry["kind"], entry["version_id"], entry["asset_role"])
            if key in self._files:
                raise ValueError("selected library has duplicate asset_files entries")
            self._asset_path(entry)
            self._files[key] = entry
        expected_files = set()
        for kind, bundles in (
            ("geometry", self._geometries.values()),
            ("family", self._families.values()),
        ):
            for bundle in bundles:
                version_id = bundle.version.resource_version_id if kind == "geometry" else bundle.version.family_version_id
                for asset in bundle.assets:
                    key = (kind, version_id, asset.asset_role.value)
                    expected_files.add(key)
        if set(self._files) != expected_files:
            raise ValueError("asset_files contains assets outside the selected exact closure")
        for selected in self._family_selection.values():
            bundle = self._families[(selected["family_id"], selected["family_version_id"])]
            for field in ("family_id", "family_version_id", "content_digest"):
                if selected[field] != getattr(bundle.version, field):
                    raise ValueError(f"selector {field} disagrees with Family exact {bundle.version.family_version_id}")

    def _check_dependency_closure(self, projected_inputs) -> None:
        expected_inputs = set()
        dependencies = {}
        for bundle in self._geometries.values():
            version_id = bundle.version.resource_version_id
            dependencies[version_id] = []
            for item in geometry_resource_inputs(bundle.version.manifest):
                target = self._geometries.get((item.resource_id, item.resource_version_id))
                canonical = next((asset for asset in target.assets if asset.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY), None) if target else None
                if (target is None or target.resource.resource_kind is not GeometryResourceKind.MAPPED_MODEL
                    or target.version.content_digest != item.content_digest or canonical is None
                    or canonical.asset_format != "occt_brep_v1" or canonical.sha256 != item.canonical_geometry_sha256
                    or target.version.manifest.get("canonical_geometry_sha256") != item.canonical_geometry_sha256):
                    raise ValueError("selected Geometry input is outside its exact fixed BREP closure")
                expected_inputs.add((version_id, item.input_key, item.resource_version_id))
                dependencies[version_id].append(item.resource_version_id)
        actual_inputs = {(row["resource_version_id"], row["input_key"], row["target_resource_version_id"]) for row in projected_inputs}
        if actual_inputs != expected_inputs or len(actual_inputs) != len(projected_inputs):
            raise ValueError("selected Geometry input rows disagree with their manifests")
        reachable = set()
        pending = []
        for bundle in self._families.values():
            for binding in bundle.geometry_bindings:
                target = self._geometries.get((binding.geometry_resource_id, binding.geometry_resource_version_id))
                if (target is None or target.resource.resource_kind is not binding.expected_resource_kind
                    or target.version.content_digest != binding.geometry_content_digest):
                    raise ValueError("selected Family binding is outside its exact Geometry closure")
                pending.append(binding.geometry_resource_version_id)
        while pending:
            version_id = pending.pop()
            if version_id not in reachable:
                reachable.add(version_id)
                pending.extend(dependencies[version_id])
        if reachable != set(dependencies):
            raise ValueError("selected library includes Geometry outside the Family dependency closure")

    def _asset_path(self, entry) -> Path:
        relative = Path(entry["backup_relative_path"])
        if relative.is_absolute():
            raise ValueError("selected asset paths must be relative to the library directory")
        path = (self._asset_root / relative).resolve(strict=True)
        if not path.is_relative_to(self._asset_root) or not path.is_file():
            raise ValueError("selected asset path escapes the library directory or is not a file")
        return path

    def _read_assets(self, kind, version_id, assets) -> tuple[bytes, ...]:
        contents = []
        for asset in assets:
            entry = self._files[(kind, version_id, asset.asset_role.value)]
            content = self._asset_path(entry).read_bytes()
            if len(content) != asset.size_bytes or hashlib.sha256(content).hexdigest() != asset.sha256:
                raise ValueError(f"selected {kind} asset bytes changed for {version_id}/{asset.asset_role.value}")
            contents.append(content)
        return tuple(contents)

    async def read_family(self, *, family_id: str, family_version_id: str) -> tuple[ProductFamilyVersionBundle, tuple[bytes, ...]]:
        bundle = self._families.get((family_id, family_version_id))
        if bundle is None:
            raise ValueError("Family exact is not in the explicit selected library")
        contents = self._read_assets("family", family_version_id, bundle.assets)
        manifest = next(content for asset, content in zip(bundle.assets, contents, strict=True)
            if asset.asset_role is ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST)
        ProductFamilyAssetService._parse_standard_model_manifest(bundle, manifest)
        return bundle, contents

    async def read_geometry(self, *, resource_id: str, resource_version_id: str) -> tuple[GeometryResourceVersionBundle, tuple[bytes, ...]]:
        bundle = self._geometries.get((resource_id, resource_version_id))
        if bundle is None:
            raise ValueError("Geometry exact is not in the explicit selected library")
        contents = self._read_assets("geometry", resource_version_id, bundle.assets)
        for asset, content in zip(bundle.assets, contents, strict=True):
            if asset.asset_role is GeometryResourceAssetRole.MANIFEST and content != canonical_json(bundle.version.manifest).encode("utf-8"):
                raise ValueError("selected Geometry manifest bytes disagree with the current Domain manifest")
        return bundle, contents

    def commands(self, request_prefix: str) -> tuple[SystemFamilyPublicationCommand, ...]:
        if not request_prefix or request_prefix != request_prefix.strip():
            raise ValueError("selected library publication requires a nonempty stable request prefix")
        return tuple(SystemFamilyPublicationCommand(request_id=f"{request_prefix}:{name}",
            source_family_id=selected["family_id"], source_family_version_id=selected["family_version_id"])
            for name, selected in sorted(self._family_selection.items()))
