"""Task-owned display assets of exact saved Catalog or inspection geometry."""

from __future__ import annotations

import json
from typing import Literal

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.application.geometry_resource_service import (
    GeometryResourceBuildError, read_geometry_function_parameter_schema,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole, GeometryResourceKind
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogIntegrityError
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry, CanonicalGeometryFormat, GeometryRenderCoordinateSpace,
    GeometryRenderRequest, GeometryRenderScene,
    normalize_geometry_function_parameters, validate_geometry_function_parameter_schema,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.ports.geometry_runtime import GeometryRuntimePort


DisplaySourceKind = Literal["geometry_resource_version", "family_standard_model", "geometry_inspection"]


class SavedGeometryDisplayService:
    """Reuse the Artifact owner; never build, realize or execute author code."""

    def __init__(self, *, artifacts: ArtifactService, geometry_runtime: GeometryRuntimePort,
                 geometry_catalog, geometry_resources, family_catalog, family_assets, inspections):
        self._artifacts = artifacts
        self._runtime = geometry_runtime
        self._geometry_catalog = geometry_catalog
        self._geometry_resources = geometry_resources
        self._family_catalog = family_catalog
        self._family_assets = family_assets
        self._inspections = inspections

    async def definition_source(self, scope: InvocationScope, source_ref: str):
        """Select one saved default-parameter entity of this exact definition."""
        bundle = await self._geometry_catalog.resolve_exact_version(scope.identity, source_ref)
        if bundle.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL:
            return {"source_kind": "geometry_resource_version", "source_ref": source_ref}
        try:
            schema = await read_geometry_function_parameter_schema(self._geometry_resources, scope, bundle)
            validate_geometry_function_parameter_schema(schema)
        except (GeometryResourceBuildError, ValueError) as exc:
            raise GeometryCatalogIntegrityError(f"The exact Geometry parameter contract is invalid: {exc}") from exc
        try:
            parameters = normalize_geometry_function_parameters(schema, {})
        except ValueError:
            # A function with mandatory inputs and no defaults has no declared
            # default entity to select. No sample is invented or executed.
            return None
        return await self._inspections.saved_version_display_source(scope, bundle, parameters)

    async def read(self, scope: InvocationScope, source_kind: DisplaySourceKind, source_ref: str):
        # Authorize the exact source on every read, including a cached display.
        geometry, source, _title = await self._load(scope, source_kind, source_ref)
        return await self._read_saved(scope, source, geometry.sha256)

    async def project(self, scope: InvocationScope, source_kind: DisplaySourceKind, source_ref: str):
        geometry, source, title = await self._load(scope, source_kind, source_ref)
        saved = await self._read_saved(scope, source, geometry.sha256)
        if saved is not None:
            return saved
        key = self._key(scope, source)
        common = {"source": source, "geometry_sha256": geometry.sha256,
                  "coordinate_space": "saved_geometry_coordinates", "unit": "METRE"}
        warnings = []
        assets = {}
        try:
            for extension in ("png", "stl"):
                asset = await self._artifacts.find_by_idempotency_key(
                    scope.owner_id, scope.identity.task_id, f"{key}:{extension}")
                if asset is None:
                    if extension == "png":
                        display = await self._runtime.render(GeometryRenderRequest(
                            geometry=geometry, width_px=720, height_px=480,
                            scene=GeometryRenderScene(geometry_sha256=geometry.sha256,
                                coordinate_space=GeometryRenderCoordinateSpace.GEOMETRY_COORDINATES)))
                    else:
                        display = await self._runtime.export_stl(geometry)
                    asset = await self._artifacts.upload_bytes(owner_id=scope.owner_id,
                        task_id=scope.identity.task_id, run_id=scope.run_id,
                        filename=f"saved_geometry.{extension}", content_type=display.content_type,
                        content=display.payload, idempotency_key=f"{key}:{extension}",
                        kind="geometry_display", version_metadata={**common,
                            "display_quality_warnings": [item.model_dump(mode="json")
                                for item in display.display_quality_warnings]})
                self._require_asset(scope, asset, geometry.sha256, "geometry_display")
                # Reused partial uploads are the same immutable pair on a retry.
                warnings.extend(asset.version_metadata.get("display_quality_warnings", ()))
                assets[extension] = self._asset_view(asset)
        finally:
            self._runtime.release_execution()
        document = {**common, "title": title, **assets,
                    "display_quality_warnings": [json.loads(item) for item in sorted(
                        {canonical_json(item) for item in warnings})]}
        # The manifest is published last; a failed projection exposes no partial pair.
        await self._artifacts.upload_bytes(owner_id=scope.owner_id, task_id=scope.identity.task_id,
            run_id=scope.run_id, filename="saved_geometry_display.json", content_type="application/json",
            content=canonical_json(document).encode(), idempotency_key=f"{key}:manifest",
            kind="geometry_display_manifest", version_metadata=common)
        return document

    async def _load(self, scope, source_kind, source_ref):
        if not isinstance(source_ref, str) or not 1 <= len(source_ref) <= 16_384:
            raise ValueError("an exact saved source reference is required")
        source = {"source_kind": source_kind, "source_ref": source_ref}
        if source_kind == "geometry_resource_version":
            bundle = await self._geometry_catalog.resolve_exact_version(scope.identity, source_ref)
            if bundle.resource.resource_kind is not GeometryResourceKind.MAPPED_MODEL:
                raise ValueError("This Geometry definition has no saved canonical entity; select its saved inspection")
            asset, payload = await self._geometry_resources.read_exact_asset(scope.identity,
                resource_id=bundle.resource.resource_id,
                resource_version_id=bundle.version.resource_version_id,
                asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY)
            geometry = CanonicalGeometry(format=CanonicalGeometryFormat(asset.asset_format),
                content_type=asset.content_type, payload=payload, sha256=asset.sha256)
            source.update(resource_id=bundle.resource.resource_id,
                resource_version_id=bundle.version.resource_version_id)
            title = bundle.resource.display_name
        elif source_kind == "family_standard_model":
            bundle = await self._family_catalog.resolve_exact_version(scope.identity, source_ref, verify_assets=False)
            if bundle.version.family_contract.standard_model is None:
                raise ValueError("This Family has no saved standard model; select its saved inspection")
            manifest, payload = await self._family_assets.standard_model_assets(bundle)
            geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
                content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
                payload=payload, sha256=manifest.geometry_sha256, summary=manifest.geometry_summary)
            source.update(family_id=bundle.family.family_id, family_version_id=bundle.version.family_version_id)
            title = bundle.version.display_name
        elif source_kind == "geometry_inspection":
            loaded = await self._inspections.read(scope, source_ref)
            geometry, title = loaded.geometry, loaded.manifest.title
            source.update(inspection_artifact=loaded.artifact.model_dump(mode="json"),
                case_key=loaded.manifest.case_key,
                source_coordinate_space=loaded.manifest.scene.coordinate_space.value)
        else:
            raise ValueError("unsupported saved display source")
        return geometry, source, title

    async def _read_saved(self, scope, source, geometry_sha256):
        asset = await self._artifacts.find_by_idempotency_key(scope.owner_id,
            scope.identity.task_id, f"{self._key(scope, source)}:manifest")
        if asset is None:
            return None
        self._require_asset(scope, asset, geometry_sha256, "geometry_display_manifest")
        downloaded = await self._artifacts.download_version(scope.owner_id, asset.artifact_id, asset.version_id)
        document = json.loads(downloaded.content)
        if document.get("source") != source or document.get("geometry_sha256") != geometry_sha256:
            raise ConflictError("saved display manifest differs from its exact source")
        for extension in ("png", "stl"):
            item = document[extension]
            record = await self._artifacts.get_version_metadata(scope.owner_id, item["artifact_id"], item["version_id"])
            self._require_asset(scope, record, geometry_sha256, "geometry_display")
            if self._asset_view(record) != item:
                raise ConflictError("saved display asset differs from its manifest")
        return document

    @staticmethod
    def _key(scope, source):
        # An actual Artifact idempotency/cache identity, not an execution qualification.
        return f"geometry-display:{scope.run_id}:{sha256_digest(source)}"

    @staticmethod
    def _require_asset(scope, asset, geometry_sha256, kind):
        if (asset.owner_id != scope.owner_id or asset.task_id != scope.identity.task_id
                or asset.run_id != scope.run_id or asset.kind != kind):
            raise NotFoundError("saved display is not available in this Task and Run")
        if asset.version_metadata.get("geometry_sha256") != geometry_sha256:
            raise ConflictError("saved display does not match its canonical source")

    @staticmethod
    def _asset_view(asset):
        return {"artifact_id": asset.artifact_id, "version_id": asset.version_id,
                "sha256": asset.sha256, "filename": asset.filename, "content_type": asset.content_type,
                "content_url": f"/tasks/{asset.task_id}/artifacts/{asset.artifact_id}/versions/{asset.version_id}/content"}
