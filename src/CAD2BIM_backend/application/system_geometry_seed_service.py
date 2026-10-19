"""Application orchestration for trusted, manifest-driven SYSTEM geometry seeds."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.geometry_resource_assets import (
    PreparedMappedGeometry, prepare_mapped_geometry_assets,
)
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceKind,
    GeometryResourceRecord,
    GeometryResourceScope,
    GeometryResourceScopeKind,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    geometry_resource_content_digest,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryNormalizeRequest,
    GeometrySummary,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.system_geometry_seed import (
    SystemGeometrySeedEntry,
    SystemGeometrySeedManifest,
    system_geometry_seed_entry_digest,
    system_geometry_seed_resource_id,
    system_geometry_seed_version_id,
)
from CAD2BIM_backend.ports.geometry_resources import GeometryResourceRepository
from CAD2BIM_backend.ports.geometry_runtime import GeometryRuntimePort
from CAD2BIM_backend.ports.storage import StoragePort


class SystemGeometrySeedError(RuntimeError):
    """A trusted manifest or normalized candidate failed before publication."""


@dataclass(frozen=True, slots=True)
class SystemGeometrySeedResult:
    """Deterministic summary safe for a trusted CLI to report."""

    published: tuple[GeometryResourceVersionBundle, ...]
    replayed: tuple[GeometryResourceVersionBundle, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    entry: SystemGeometrySeedEntry
    bundle: GeometryResourceVersionBundle
    contents: tuple[bytes, ...]


def prepare_mapped_system_geometry_seed(
    entry: SystemGeometrySeedEntry,
    *,
    source: bytes,
    source_bundle: bytes,
    normalized: CanonicalGeometry,
    reopened: GeometrySummary,
) -> PreparedMappedGeometry:
    """Assemble deterministic mapped-model assets from the one shared runtime result."""

    try:
        return prepare_mapped_geometry_assets(
            metadata={
                "resource_kind": entry.resource_kind.value,
                "source_format": entry.source_format.value,
                "geometry_format": entry.geometry_format.value,
                "unit": entry.unit,
                "local_frame": entry.local_frame.model_dump(mode="json"),
            },
            source=source, source_bundle=source_bundle,
            normalized=normalized, reopened=reopened,
        )
    except ValueError as exc:
        raise SystemGeometrySeedError(str(exc)) from exc


class SystemGeometrySeedService:
    """Preflight a complete manifest, then publish via the existing resource service."""

    def __init__(
        self,
        repository: GeometryResourceRepository,
        storage: StoragePort,
        geometry_resources: GeometryResourceService,
        geometry_runtime: GeometryRuntimePort | None,
        source_bundle_packer: Callable[[Mapping[str, bytes]], bytes],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._geometry_resources = geometry_resources
        self._geometry_runtime = geometry_runtime
        self._source_bundle_packer = source_bundle_packer
        self._clock = clock or (lambda: datetime.now(UTC))

    async def seed(
        self,
        context: TenantExecutionContext,
        *,
        manifest: SystemGeometrySeedManifest,
        sources: Mapping[str, bytes],
    ) -> SystemGeometrySeedResult:
        """Publish an all-preflighted manifest; an empty manifest performs zero writes."""

        expected_paths = {entry.source_path for entry in manifest.resources}
        if set(sources) != expected_paths:
            raise SystemGeometrySeedError(
                "system geometry seed sources must exactly match the manifest"
            )
        for entry in manifest.resources:
            source = sources[entry.source_path]
            if not isinstance(source, bytes):
                raise SystemGeometrySeedError("system geometry seed sources must be bytes")
            if hashlib.sha256(source).hexdigest() != entry.source_sha256:
                raise SystemGeometrySeedError(
                    f"system geometry seed source digest mismatch: {entry.seed_key}"
                )

        if not manifest.resources:
            return SystemGeometrySeedResult(published=(), replayed=())
        if self._geometry_runtime is None:
            raise SystemGeometrySeedError(
                "non-empty system geometry seed requires the shared geometry runtime"
            )

        # Runtime normalization, digest validation, and every repository conflict check
        # complete before the first immutable Storage write.
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise SystemGeometrySeedError("system geometry seed clock must be timezone-aware")
        candidates: list[_Candidate] = []
        replays: list[GeometryResourceVersionBundle] = []
        for entry in manifest.resources:
            if entry.resource_kind is not GeometryResourceKind.MAPPED_MODEL:
                raise SystemGeometrySeedError(
                    "modeling_function SYSTEM seed is unavailable until the shared runtime "
                    "has an approved build contract"
                )
            source = sources[entry.source_path]
            normalized = await self._geometry_runtime.normalize(
                GeometryNormalizeRequest(
                    source_format=entry.source_format,
                    format=CanonicalGeometryFormat(entry.geometry_format.value),
                    payload=source,
                    unit=entry.unit,
                )
            )
            reopened = await self._geometry_runtime.reopen(normalized)
            source_bundle = self._source_bundle_packer({entry.source_path: source})
            prepared = prepare_mapped_system_geometry_seed(
                entry,
                source=source,
                source_bundle=source_bundle,
                normalized=normalized,
                reopened=reopened,
            )
            candidate = await self._preflight_entry(context, entry, prepared, timestamp)
            if isinstance(candidate, _Candidate):
                candidates.append(candidate)
            else:
                replays.append(candidate)

        published: list[GeometryResourceVersionBundle] = []
        stored_candidates: list[_Candidate] = []
        for candidate in candidates:
            stored_candidates.append(await self._write_candidate_assets(context, candidate))
        for candidate in stored_candidates:
            published.append(
                await self._geometry_resources.publish_candidate(context, candidate.bundle)
            )
        for replay in replays:
            await self._verify_replay_assets(context, replay)
        return SystemGeometrySeedResult(
            published=tuple(published),
            replayed=tuple(replays),
        )

    async def _preflight_entry(
        self,
        context: TenantExecutionContext,
        entry: SystemGeometrySeedEntry,
        prepared: PreparedMappedGeometry,
        timestamp: datetime,
    ) -> _Candidate | GeometryResourceVersionBundle:
        entry_digest = system_geometry_seed_entry_digest(entry)
        resource_id = system_geometry_seed_resource_id(entry.seed_key)
        resource_version_id = system_geometry_seed_version_id(entry.seed_key, entry_digest)
        manifest_digest = sha256_digest(prepared.manifest)
        scope = GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM)

        provisional_assets = tuple(
            GeometryResourceVersionAssetRecord(
                resource_version_id=resource_version_id,
                asset_role=asset_role,
                asset_format=asset_format,
                filename=filename,
                content_type=content_type,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                created_at=timestamp,
            )
            for asset_role, asset_format, filename, content_type, content in prepared.assets
        )
        target_asset = next(
            (
                asset
                for asset in provisional_assets
                if asset.asset_role is entry.target_asset_role
            ),
            None,
        )
        if target_asset is None or target_asset.asset_format != entry.geometry_format.value:
            raise SystemGeometrySeedError(
                f"system geometry seed target asset is missing or has the wrong format: "
                f"{entry.seed_key}"
            )
        content_digest = geometry_resource_content_digest(
            resource_kind=entry.resource_kind,
            manifest_digest=manifest_digest,
            assets=provisional_assets,
        )
        if content_digest != entry.expected_content_digest:
            raise SystemGeometrySeedError(
                f"system geometry seed content digest mismatch: {entry.seed_key}"
            )

        existing = await self._repository.get_exact_geometry_resource_version(
            context,
            resource_id=resource_id,
            resource_version_id=resource_version_id,
        )
        if existing is not None:
            self._require_existing_matches(
                existing,
                entry=entry,
                entry_digest=entry_digest,
                content_digest=content_digest,
                prepared=prepared,
            )
            return existing

        latest = await self._repository.get_latest_geometry_resource_version(
            context,
            resource_id=resource_id,
        )
        if latest is None:
            resource = GeometryResourceRecord(
                resource_id=resource_id,
                resource_kind=entry.resource_kind,
                owner_scope=scope,
                display_name=entry.display_name,
                created_at=timestamp,
            )
            version_no = 1
        else:
            self._require_same_root(latest.resource, entry)
            resource = latest.resource
            version_no = latest.version.version_no + 1

        version = GeometryResourceVersionRecord(
            resource_version_id=resource_version_id,
            resource_id=resource_id,
            version_no=version_no,
            origin_kind=entry.origin_kind,
            content_digest=content_digest,
            manifest_digest=manifest_digest,
            manifest=prepared.manifest,
            lineage={
                "seed_key": entry.seed_key,
                "seed_manifest_digest": entry_digest,
                "source_path": entry.source_path,
                "source_sha256": entry.source_sha256,
                "source_format": entry.source_format.value,
                "geometry_format": entry.geometry_format.value,
                "unit": entry.unit,
                "local_frame": entry.local_frame.model_dump(mode="json"),
            },
            created_at=timestamp,
        )
        bundle = GeometryResourceVersionBundle(
            resource=resource,
            version=version,
            assets=provisional_assets,
        )
        return _Candidate(
            entry=entry,
            bundle=bundle,
            contents=tuple(asset[-1] for asset in prepared.assets),
        )

    @staticmethod
    def _require_same_root(
        resource: GeometryResourceRecord,
        entry: SystemGeometrySeedEntry,
    ) -> None:
        if (
            resource.resource_kind is not entry.resource_kind
            or resource.owner_scope.scope_kind is not GeometryResourceScopeKind.SYSTEM
            or resource.owner_scope.tenant_id is not None
            or resource.owner_scope.task_id is not None
            or resource.display_name != entry.display_name.strip()
        ):
            raise SystemGeometrySeedError(
                f"system geometry seed root metadata changed: {entry.seed_key}"
            )

    @classmethod
    def _require_existing_matches(
        cls,
        existing: GeometryResourceVersionBundle,
        *,
        entry: SystemGeometrySeedEntry,
        entry_digest: str,
        content_digest: str,
        prepared: PreparedMappedGeometry,
    ) -> None:
        cls._require_same_root(existing.resource, entry)
        if (
            existing.version.origin_kind != entry.origin_kind
            or existing.version.content_digest != content_digest
            or existing.version.manifest != prepared.manifest
            or existing.version.lineage.get("seed_manifest_digest") != entry_digest
        ):
            raise SystemGeometrySeedError(
                f"system geometry seed deterministic version identity conflicts: {entry.seed_key}"
            )

    async def _write_candidate_assets(
        self, context: TenantExecutionContext, candidate: _Candidate,
    ) -> _Candidate:
        bundle = await self._geometry_resources.store_candidate_assets(
            context, candidate.bundle, candidate.contents,
        )
        return _Candidate(entry=candidate.entry, bundle=bundle, contents=candidate.contents)

    async def _verify_replay_assets(
        self,
        context: TenantExecutionContext,
        replay: GeometryResourceVersionBundle,
    ) -> None:
        for asset in replay.assets:
            exact = await self._geometry_resources.read_exact_asset(
                context,
                resource_id=replay.resource.resource_id,
                resource_version_id=replay.version.resource_version_id,
                asset_role=asset.asset_role,
            )
            if exact is None or exact[0] != asset:
                raise SystemGeometrySeedError(
                    "replayed SYSTEM geometry seed asset failed exact readback"
                )

__all__ = [
    "SystemGeometrySeedError",
    "SystemGeometrySeedResult",
    "SystemGeometrySeedService",
]
