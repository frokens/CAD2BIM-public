"""Application boundary for exact geometry-resource asset publication and reads."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
import hashlib
import json
from pydantic import JsonValue, ValidationError
from CAD2BIM_backend.application.geometry_resource_assets import prepare_mapped_geometry_assets
from CAD2BIM_backend.domain.control_plane import IdempotencyConflictError
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogIntegrityError
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_runtime import (CanonicalGeometry, CanonicalGeometryFormat,
    GeometryNormalizeRequest, GeometryValidateFunctionRequest, GeometryRuntimeError,
    GeometryRuntimeResourceLimitError, GeometryRuntimeValidationError,
    GeometryBuildInput, GeometryBuildInputs, MAX_GEOMETRY_BUILD_INPUT_BYTES)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.ports.geometry_runtime import GeometryRuntimePort

from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceScope,
    GeometryResourceScopeKind,
    GeometryResourceListQuery,
    GeometryResourcePage,
    GeometryResourceRecord,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    GeometryResourceInputs,
    GeometryResourceInput,
    geometry_resource_inputs,
    geometry_resource_content_digest,
    require_formal_geometry_publication,
)
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryResourceVersionListQuery,
    GeometryResourceVersionPage,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext, stable_internal_identity_id
from CAD2BIM_backend.ports.geometry_resources import GeometryResourceRepository
from CAD2BIM_backend.ports.storage import (
    ResourceStorageKey,
    StorageConflictError,
    StorageObjectNotFoundError,
    StoragePort,
    StorageValidationError,
)


class GeometryResourceIntegrityError(RuntimeError):
    """An exact database asset binding and immutable stored bytes disagree."""


class GeometryResourceBuildError(RuntimeError):
    """An exact resource cannot produce the requested bounded geometry sample."""


class GeometryResourceService:
    """Make a complete stored candidate visible and read it back exactly."""

    def __init__(
        self,
        repository: GeometryResourceRepository,
        storage: StoragePort,
        *,
        geometry_runtime: GeometryRuntimePort | None = None,
        source_bundle_packer: Callable[[Mapping[str, bytes]], bytes] | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._geometry_runtime = geometry_runtime
        self._source_bundle_packer = source_bundle_packer

    async def delete_resource(self, scope: TenantExecutionContext, *, resource_id: str) -> bool:
        return await self._repository.delete_geometry_resource(scope, resource_id=resource_id)

    @staticmethod
    def prepare_system_copy(
        *,
        source: GeometryResourceVersionBundle,
        source_assets: tuple[bytes, ...],
        target_resource_id: str,
        target_resource_version_id: str,
        geometry_inputs: Mapping[str, GeometryResourceVersionBundle],
        created_at: datetime,
    ) -> tuple[GeometryResourceVersionBundle, tuple[bytes, ...]]:
        """Prepare an exact SYSTEM copy without executing or rebuilding the source."""
        try:
            source = GeometryResourceVersionBundle.model_validate(source.model_dump(mode="python"))
            require_formal_geometry_publication(source.version, source.assets)
        except ValueError as exc:
            raise GeometryResourceIntegrityError(
                "SYSTEM copy requires a complete current successful source version"
            ) from exc
        if len(source_assets) != len(source.assets) or any(
            not isinstance(content, bytes)
            or len(content) != asset.size_bytes
            or hashlib.sha256(content).hexdigest() != asset.sha256
            for asset, content in zip(source.assets, source_assets, strict=True)
        ):
            raise GeometryResourceIntegrityError("SYSTEM copy source asset bytes are incomplete or changed")
        if (target_resource_id == source.resource.resource_id
            or target_resource_version_id == source.version.resource_version_id):
            raise GeometryResourceIntegrityError("SYSTEM copy requires new resource and version identities")

        inputs = geometry_resource_inputs(source.version.manifest)
        if set(geometry_inputs) != {item.input_key for item in inputs}:
            raise GeometryResourceIntegrityError("SYSTEM copy requires the complete exact geometry input mapping")
        mapped_inputs = []
        for item in inputs:
            try:
                target = GeometryResourceVersionBundle.model_validate(
                    geometry_inputs[item.input_key].model_dump(mode="python")
                )
                require_formal_geometry_publication(target.version, target.assets)
            except ValueError as exc:
                raise GeometryResourceIntegrityError("SYSTEM copy input is not a current successful version") from exc
            canonical_asset = next((asset for asset in target.assets
                if asset.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY), None)
            if (target.resource.owner_scope.scope_kind is not GeometryResourceScopeKind.SYSTEM
                or target.resource.resource_kind is not GeometryResourceKind.MAPPED_MODEL
                or target.version.resource_version_id == target_resource_version_id
                or canonical_asset is None
                or canonical_asset.asset_format != CanonicalGeometryFormat.OCCT_BREP_V1.value
                or canonical_asset.sha256 != item.canonical_geometry_sha256
                or target.version.manifest.get("canonical_geometry_sha256") != item.canonical_geometry_sha256):
                raise GeometryResourceIntegrityError("SYSTEM copy input must retain its exact fixed BREP in SYSTEM")
            mapped_inputs.append(GeometryResourceInput(
                input_key=item.input_key,
                resource_id=target.resource.resource_id,
                resource_version_id=target.version.resource_version_id,
                content_digest=target.version.content_digest,
                canonical_geometry_sha256=item.canonical_geometry_sha256,
            ))

        manifest = dict(source.version.manifest)
        if inputs:
            manifest["geometry_inputs"] = [item.model_dump(mode="json") for item in mapped_inputs]
        manifest_bytes = canonical_json(manifest).encode("utf-8")
        contents = tuple(manifest_bytes if asset.asset_role is GeometryResourceAssetRole.MANIFEST else content
            for asset, content in zip(source.assets, source_assets, strict=True))
        assets = tuple(asset.model_copy(update={
            "resource_version_id": target_resource_version_id,
            "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "created_at": created_at,
        }) for asset, content in zip(source.assets, contents, strict=True))
        manifest_digest = sha256_digest(manifest)
        resource = GeometryResourceRecord(
            resource_id=target_resource_id, resource_kind=source.resource.resource_kind,
            owner_scope=GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM),
            display_name=source.resource.display_name, created_at=created_at,
        )
        version = GeometryResourceVersionRecord(
            resource_version_id=target_resource_version_id, resource_id=target_resource_id,
            version_no=1, origin_kind="system_copy", manifest=manifest, manifest_digest=manifest_digest,
            content_digest=geometry_resource_content_digest(
                resource_kind=resource.resource_kind, manifest_digest=manifest_digest, assets=assets,
            ),
            lineage={"source_resource_id": source.resource.resource_id,
                "source_resource_version_id": source.version.resource_version_id,
                "source_content_digest": source.version.content_digest},
            created_at=created_at,
        )
        return GeometryResourceVersionBundle(resource=resource, version=version, assets=assets), contents

    async def import_mapped_asset(
        self,
        context: TenantExecutionContext,
        *,
        scope: GeometryResourceScope,
        request_key: str,
        display_name: str,
        source_name: str,
        source_sha256: str,
        normalization: GeometryNormalizeRequest,
        documents: Mapping[str, bytes],
    ) -> GeometryResourceVersionBundle:
        """Trusted backend intake of exact bytes; no host paths or model upload API.

        A request key owns one new immutable root. Exact replay verifies all stored
        assets, including after a lost commit response. A changed source or document
        requires a new request key. Unpublished immutable writes remain invisible
        and reusable by retry; they are never deleted after an uncertain commit.
        """
        await self._repository.require_geometry_resource_publication_allowed(context, scope=scope)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("asset intake request_key requires 1..128 characters")
        if hashlib.sha256(normalization.payload).hexdigest() != source_sha256:
            raise GeometryResourceIntegrityError("asset intake source digest mismatch")
        if self._geometry_runtime is None or self._source_bundle_packer is None:
            raise GeometryResourceBuildError("asset intake requires the shared runtime and source packer")
        if source_name in documents:
            raise ValueError("asset intake source name collides with a document")
        # The shared packer owns normalized paths, member count and aggregate limits.
        source_bundle = self._source_bundle_packer({source_name: normalization.payload, **documents})
        request_digest = sha256_digest({
            "scope": scope.model_dump(mode="json"), "display_name": display_name,
            "source_name": source_name, "source_sha256": source_sha256,
            "source_bundle_sha256": hashlib.sha256(source_bundle).hexdigest(),
            "normalization": normalization.model_dump(mode="json", exclude={"payload"}),
        })
        resource_id = stable_internal_identity_id(
            "grs_", "cad2bim.geometry_asset_intake.resource.v1",
            canonical_json(scope.model_dump(mode="json")), request_key,
        )
        version_id = stable_internal_identity_id(
            "grsv_", "cad2bim.geometry_asset_intake.version.v1", resource_id,
        )
        existing = await self.get_exact_version(context, resource_id=resource_id, resource_version_id=version_id)
        if existing is not None:
            if existing.version.lineage.get("intake_request_digest") != request_digest:
                raise IdempotencyConflictError("asset intake request key was reused with different input")
            await self._verify_bundle_assets(existing)
            return existing
        timestamp = datetime.now(UTC)
        resource = GeometryResourceRecord(
            resource_id=resource_id, resource_kind=GeometryResourceKind.MAPPED_MODEL,
            owner_scope=scope, display_name=display_name, created_at=timestamp,
        )
        normalized = await self._geometry_runtime.normalize(normalization)
        reopened = await self._geometry_runtime.reopen(normalized)
        prepared = prepare_mapped_geometry_assets(
            metadata={
                "resource_kind": resource.resource_kind.value,
                "source_format": normalization.effective_source_format.value,
                "geometry_format": normalization.format.value, "unit": "METRE",
                "local_frame": {"handedness": "right", "origin": [0.0, 0.0, 0.0],
                    "x_axis": [1.0, 0.0, 0.0], "y_axis": [0.0, 1.0, 0.0], "z_axis": [0.0, 0.0, 1.0]},
                "source_name": source_name,
                "source_documents": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content)} for name, content in sorted(documents.items())],
                "normalization": normalization.model_dump(mode="json", exclude={"payload"}),
            }, source=normalization.payload, source_bundle=source_bundle,
            normalized=normalized, reopened=reopened,
        )
        assets = tuple(GeometryResourceVersionAssetRecord(
            resource_version_id=version_id, asset_role=role, asset_format=fmt,
            filename=filename, content_type=mime, size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(), created_at=timestamp,
        ) for role, fmt, filename, mime, content in prepared.assets)
        manifest_digest = sha256_digest(prepared.manifest)
        version = GeometryResourceVersionRecord(
            resource_version_id=version_id, resource_id=resource_id, version_no=1,
            origin_kind="backend_import", manifest=prepared.manifest, manifest_digest=manifest_digest,
            content_digest=geometry_resource_content_digest(resource_kind=resource.resource_kind,
                manifest_digest=manifest_digest, assets=assets),
            lineage={"intake_request_digest": request_digest, "source_name": source_name,
                "source_sha256": source_sha256},
            created_at=timestamp,
        )
        bundle = GeometryResourceVersionBundle(resource=resource, version=version, assets=assets)
        stored = await self.store_candidate_assets(context, bundle, tuple(asset[-1] for asset in prepared.assets))
        return await self.publish_candidate(context, stored)

    async def store_candidate_assets(
        self, context: TenantExecutionContext, bundle: GeometryResourceVersionBundle,
        contents: tuple[bytes, ...],
    ) -> GeometryResourceVersionBundle:
        """Store exact candidate bytes for seed/intake under the existing owner."""
        await self._repository.require_geometry_resource_publication_allowed(context, scope=bundle.resource.owner_scope)
        if len(contents) != len(bundle.assets) or any(
            len(content) != asset.size_bytes or hashlib.sha256(content).hexdigest() != asset.sha256
            for asset, content in zip(bundle.assets, contents, strict=True)
        ):
            raise GeometryResourceIntegrityError("candidate asset bytes differ from the immutable manifest")
        stored_assets = []
        for asset, content in zip(bundle.assets, contents, strict=True):
            key = self.resource_storage_key(bundle, asset)
            stored = await self._storage.write(key, filename=asset.filename,
                content_type=asset.content_type, chunks=self._one_chunk(content))
            if (stored.key != key or stored.filename != asset.filename or stored.content_type != asset.content_type
                or stored.size_bytes != asset.size_bytes or stored.sha256 != asset.sha256):
                raise GeometryResourceIntegrityError("stored geometry candidate asset metadata changed")
            stored_assets.append(asset.model_copy(update={"created_at": stored.created_at}))
        return GeometryResourceVersionBundle(resource=bundle.resource, version=bundle.version, assets=tuple(stored_assets))

    @staticmethod
    async def _one_chunk(content: bytes) -> AsyncIterator[bytes]:
        yield content

    async def verify_candidate_assets(
        self, context: TenantExecutionContext, bundle: GeometryResourceVersionBundle,
    ) -> None:
        """Authorize and verify every saved candidate asset before publication recovery."""
        await self._repository.require_geometry_resource_publication_allowed(context, scope=bundle.resource.owner_scope)
        await self._verify_bundle_assets(bundle)

    async def publish_candidate(
        self,
        context: TenantExecutionContext,
        publication: GeometryResourceVersionBundle,
    ) -> GeometryResourceVersionBundle:
        """Publish only after every required immutable asset is independently readable."""

        await self._repository.require_geometry_resource_publication_allowed(
            context,
            scope=publication.resource.owner_scope,
        )
        await self._verify_bundle_assets(publication)
        committed = await self._repository.publish_geometry_resource_version(
            context,
            publication,
        )
        reread = await self._repository.get_exact_geometry_resource_version(
            context,
            resource_id=committed.resource.resource_id,
            resource_version_id=committed.version.resource_version_id,
        )
        if reread != committed:
            raise GeometryResourceIntegrityError(
                "committed geometry resource version failed exact database readback"
            )
        await self._verify_bundle_assets(reread)
        return reread

    async def read_exact_asset(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
        resource_version_id: str,
        asset_role: GeometryResourceAssetRole,
    ) -> tuple[GeometryResourceVersionAssetRecord, bytes] | None:
        """Resolve one visible exact version and return one verified asset's bytes."""

        bundle = await self._repository.get_exact_geometry_resource_version(
            context,
            resource_id=resource_id,
            resource_version_id=resource_version_id,
        )
        if bundle is None:
            return None
        asset = next(
            (item for item in bundle.assets if item.asset_role is asset_role),
            None,
        )
        if asset is None:
            return None
        content = await self._verify_asset(bundle, asset)
        return asset, content


    async def load_build_inputs(
        self, context: TenantExecutionContext, inputs: GeometryResourceInputs,
    ) -> GeometryBuildInputs:
        """Reauthorize frozen dependencies and bound bytes before any Storage read."""
        resolved = []
        total_bytes = 0
        for item in inputs:
            bundle = await self.get_exact_version(
                context, resource_id=item.resource_id,
                resource_version_id=item.resource_version_id,
            )
            if bundle is None:
                raise GeometryResourceBuildError(
                    f"geometry input {item.input_key!r} is not visible in the current scope"
                )
            asset = next((asset for asset in bundle.assets
                          if asset.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY), None)
            if (bundle.resource.resource_kind is not GeometryResourceKind.MAPPED_MODEL
                or bundle.version.content_digest != item.content_digest
                or bundle.version.manifest.get("canonical_geometry_sha256") != item.canonical_geometry_sha256
                or asset is None or asset.asset_format != CanonicalGeometryFormat.OCCT_BREP_V1.value
                or asset.sha256 != item.canonical_geometry_sha256):
                raise GeometryResourceBuildError(
                    f"geometry input {item.input_key!r} must identify the frozen fixed BREP version"
                )
            total_bytes += asset.size_bytes
            if total_bytes > MAX_GEOMETRY_BUILD_INPUT_BYTES:
                raise GeometryResourceBuildError("geometry inputs exceed the combined 32 MiB limit")
            resolved.append((item, bundle, asset))
        return tuple([GeometryBuildInput(
            input_key=item.input_key, payload=await self._verify_asset(bundle, asset),
            sha256=item.canonical_geometry_sha256,
        ) for item, bundle, asset in resolved])

    async def get_resource(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
    ) -> GeometryResourceRecord | None:
        return await self._repository.get_geometry_resource(
            context,
            resource_id=resource_id,
        )

    async def get_exact_version(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
        resource_version_id: str,
    ) -> GeometryResourceVersionBundle | None:
        return await self._repository.get_exact_geometry_resource_version(
            context,
            resource_id=resource_id,
            resource_version_id=resource_version_id,
        )

    async def get_version_by_id(
        self,
        context: TenantExecutionContext,
        *,
        resource_version_id: str,
    ) -> GeometryResourceVersionBundle | None:
        return await self._repository.get_geometry_resource_version_by_id(
            context,
            resource_version_id=resource_version_id,
        )

    async def get_latest_version(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
    ) -> GeometryResourceVersionBundle | None:
        return await self._repository.get_latest_geometry_resource_version(
            context,
            resource_id=resource_id,
        )

    async def list_ready_resources(
        self,
        context: TenantExecutionContext,
        *,
        query: GeometryResourceListQuery,
    ) -> GeometryResourcePage:
        return await self._repository.list_geometry_resources(context, query=query)

    async def list_versions(
        self,
        context: TenantExecutionContext,
        *,
        query: GeometryResourceVersionListQuery,
    ) -> GeometryResourceVersionPage:
        return await self._repository.list_geometry_resource_versions(context, query=query)

    async def list_executable_program_versions(
        self,
        context: TenantExecutionContext,
        *,
        before_created_at: datetime | None,
        before_resource_version_id: str | None,
        limit: int,
    ) -> tuple[GeometryResourceVersionBundle, ...]:
        """List visible canonical Python program versions across resource roots."""

        if (before_created_at is None) != (before_resource_version_id is None):
            raise ValueError("geometry program boundary requires timestamp and version ID")
        if before_created_at is not None and (
            before_created_at.tzinfo is None or before_created_at.utcoffset() is None
        ):
            raise ValueError("geometry program boundary timestamp must be timezone-aware")
        if before_resource_version_id is not None and not (
            1 <= len(before_resource_version_id) <= 80
        ):
            raise ValueError("geometry program boundary version ID is invalid")
        if not 1 <= limit <= 51:
            raise ValueError("geometry program list limit must be between 1 and 51")
        return await self._repository.list_executable_program_versions(
            context,
            before_created_at=before_created_at,
            before_resource_version_id=before_resource_version_id,
            limit=limit,
        )

    async def _verify_bundle_assets(
        self,
        bundle: GeometryResourceVersionBundle,
    ) -> None:
        for asset in bundle.assets:
            await self._verify_asset(bundle, asset)

    async def _verify_asset(
        self,
        bundle: GeometryResourceVersionBundle,
        asset: GeometryResourceVersionAssetRecord,
    ) -> bytes:
        key = self.resource_storage_key(bundle, asset)
        try:
            verified = await self._storage.read_verified(key)
        except (
            FileNotFoundError,
            StorageConflictError,
            StorageObjectNotFoundError,
            StorageValidationError,
        ) as exc:
            raise GeometryResourceIntegrityError(
                "geometry resource candidate asset is missing"
            ) from exc
        stored = verified.stored
        content = verified.content
        if (
            stored.key != key
            or stored.filename != asset.filename
            or stored.content_type != asset.content_type
            or stored.size_bytes != asset.size_bytes
            or stored.sha256 != asset.sha256
            or len(content) != asset.size_bytes
            or verified.verified_sha256 != asset.sha256
        ):
            raise GeometryResourceIntegrityError(
                "geometry resource candidate asset metadata or digest does not match"
            )
        return content

    @staticmethod
    def resource_storage_key(
        bundle: GeometryResourceVersionBundle,
        asset: GeometryResourceVersionAssetRecord,
    ) -> ResourceStorageKey:
        """Derive the only logical storage address from database-owned identities."""

        return ResourceStorageKey(
            scope_kind=bundle.resource.owner_scope.scope_kind.name,
            scope_id=bundle.resource.owner_scope.scope_id,
            resource_version_id=bundle.version.resource_version_id,
            asset_role=asset.asset_role.value,
        )


async def build_geometry_function_sample(
    resources: GeometryResourceService,
    runtime: GeometryRuntimePort,
    scope: InvocationScope,
    bundle: GeometryResourceVersionBundle,
    parameters: dict[str, JsonValue],
) -> tuple[CanonicalGeometry, GeometryModelInterfaces]:
    source_resolved = await resources.read_exact_asset(
        scope.identity,
        resource_id=bundle.resource.resource_id,
        resource_version_id=bundle.version.resource_version_id,
        asset_role=GeometryResourceAssetRole.SOURCE_BUNDLE,
    )
    schema_resolved = await resources.read_exact_asset(
        scope.identity,
        resource_id=bundle.resource.resource_id,
        resource_version_id=bundle.version.resource_version_id,
        asset_role=GeometryResourceAssetRole.PARAMETER_SCHEMA,
    )
    if source_resolved is None or schema_resolved is None:
        raise GeometryResourceBuildError(
            "the modeling-function version lacks exact source or parameter schema"
        )
    source_asset, source_bundle = source_resolved
    schema_asset, schema_bytes = schema_resolved
    if (
        source_asset.asset_format != "source_bundle_v1"
        or source_asset.content_type != "application/zip"
        or schema_asset.asset_format != "json_schema"
        or schema_asset.content_type != "application/schema+json"
    ):
        raise GeometryCatalogIntegrityError(
            "the modeling-function source or parameter schema asset contract changed"
        )
    try:
        parameter_schema = json.loads(schema_bytes)
        if not isinstance(parameter_schema, dict):
            raise ValueError("parameter schema must be a JSON object")
        request = GeometryValidateFunctionRequest(
            source_bundle=source_bundle,
            parameter_schema=parameter_schema,
            sample_parameters=(parameters,),
        )
        evaluation = await runtime.evaluate_function_interfaces(request)
        inputs = geometry_resource_inputs(bundle.version.manifest)
        actual = request.model_copy(update={"geometry_inputs": await resources.load_build_inputs(scope.identity, inputs) if inputs else ()})
        result = await runtime.instantiate_function(actual, evaluation)
    except GeometryRuntimeResourceLimitError:
        raise
    except GeometryRuntimeValidationError as exc:
        # Runtime validation already projects an authored reason without a traceback.
        # Keep it actionable for Preview while bounding arbitrary user-code text.
        message = " ".join(str(exc).split()) or "modeling-function source failed validation"
        encoded = message.encode("utf-8", errors="replace")
        if len(encoded) > 2_000:
            message = encoded[:1_985].decode("utf-8", errors="ignore") + "... [truncated]"
        raise GeometryResourceBuildError(message) from exc
    except (
        json.JSONDecodeError,
        ValueError,
        ValidationError,
    ) as exc:
        raise GeometryResourceBuildError(
            "sample parameters or modeling-function source failed validation"
        ) from exc
    except GeometryRuntimeError as exc:
        raise GeometryResourceBuildError(
            "the modeling-function runtime is unavailable"
        ) from exc
    sample = result.sample_geometry
    if (
        len(result.sample_executions) != 1
        or result.sample_executions[0].output_sha256 != sample.sha256
    ):
        raise GeometryCatalogIntegrityError(
            "the modeling-function runtime changed its exact inputs or sample output"
        )
    return sample, result.sample_executions[0].interfaces


async def normalize_geometry_function_sample_parameters(
    resources: GeometryResourceService,
    scope: InvocationScope,
    bundle: GeometryResourceVersionBundle,
    parameters: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Authorize the exact schema before a Preview lookup hashes build inputs."""

    from CAD2BIM_backend.domain.geometry_runtime import normalize_geometry_function_parameters

    schema = await read_geometry_function_parameter_schema(resources, scope, bundle)
    try:
        return normalize_geometry_function_parameters(schema, parameters)
    except ValueError as exc:
        raise GeometryResourceBuildError(f"modeling-function parameters are invalid: {exc}") from exc


async def read_geometry_function_parameter_schema(
    resources: GeometryResourceService, scope: InvocationScope, bundle: GeometryResourceVersionBundle,
) -> dict[str, JsonValue]:
    """Read the exact authorized parameter asset without executing the function."""
    resolved = await resources.read_exact_asset(
        scope.identity,
        resource_id=bundle.resource.resource_id,
        resource_version_id=bundle.version.resource_version_id,
        asset_role=GeometryResourceAssetRole.PARAMETER_SCHEMA,
    )
    if resolved is None:
        raise GeometryResourceBuildError("the modeling-function version lacks its exact parameter schema")
    asset, content = resolved
    if asset.asset_format != "json_schema" or asset.content_type != "application/schema+json":
        raise GeometryCatalogIntegrityError("the modeling-function parameter schema asset contract changed")
    try:
        schema = json.loads(content)
        if not isinstance(schema, dict):
            raise ValueError("parameter schema must be a JSON object")
        return schema
    except ValueError as exc:
        raise GeometryResourceBuildError(f"modeling-function parameters are invalid: {exc}") from exc

async def reopen_exact_canonical_geometry(
    runtime: GeometryRuntimePort,
    asset_format: str,
    payload: bytes,
) -> CanonicalGeometry:
    try:
        geometry_format = CanonicalGeometryFormat(asset_format)
    except ValueError as exc:
        raise GeometryCatalogIntegrityError(
            "geometry asset uses an unsupported canonical format"
        ) from exc
    canonical = await runtime.normalize(
        GeometryNormalizeRequest(format=geometry_format, payload=payload, unit="METRE")
    )
    if canonical.payload != payload or hashlib.sha256(payload).hexdigest() != canonical.sha256:
        raise GeometryCatalogIntegrityError(
            "canonical geometry changed while reopening for preview"
        )
    return canonical

__all__ = ["GeometryResourceIntegrityError", "GeometryResourceService", "GeometryResourceBuildError",
           "build_geometry_function_sample", "reopen_exact_canonical_geometry"]
