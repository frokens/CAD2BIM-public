from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.system_geometry_seed_service import (
    SystemGeometrySeedError,
    SystemGeometrySeedService,
    prepare_mapped_system_geometry_seed,
)
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    geometry_resource_content_digest,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometryNormalizeRequest,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.system_geometry_seed import (
    SystemGeometrySeedEntry,
    SystemGeometrySeedManifest,
    system_geometry_seed_entry_digest,
    system_geometry_seed_resource_id,
    system_geometry_seed_version_id,
)
from CAD2BIM_backend.infrastructure.geometry_seed.loader import (
    SystemGeometrySeedLoadError,
    load_production_system_geometry_seed,
    load_system_geometry_seed_directory,
)
from CAD2BIM_backend.infrastructure.geometry_runtime import OcpCadQueryGeometryRuntime
from CAD2BIM_backend.infrastructure.geometry_seed.cli import (
    _require_trusted_system_curator,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.infrastructure.source_text import (
    inspect_source_bundle,
    pack_source_bundle,
)


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
SOURCE = b"neutral normalized geometry source"
SEED_CONTENT_TYPES = {
    "application/json",
    "application/octet-stream",
    "application/zip",
    CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
    CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1.content_type.split(";", 1)[0],
}


def _context() -> TenantExecutionContext:
    return TenantExecutionContext(
        tenant_id="system",
        principal_id="system-geometry-seed",
        actor_kind=TenantActorKind.SYSTEM_CURATOR,
        command_id="seed-test",
    )


class _Runtime:
    def normalized(self, entry: SystemGeometrySeedEntry, source: bytes) -> CanonicalGeometry:
        summary = GeometrySummary(
            format=entry.geometry_format.value,
            bounds=GeometryBounds(minimum_m=(0.0, 0.0, 0.0), maximum_m=(1.0, 1.0, 1.0)),
            topology=GeometryTopologySummary(
                vertex_count=8,
                edge_count=12,
                wire_count=6,
                face_count=6,
                shell_count=1,
                solid_count=1,
                triangle_count=12,
                boundary_edge_count=0,
            ),
            surface_area_m2=6.0,
            volume_m3=1.0,
        )
        return CanonicalGeometry(
            format=entry.geometry_format.value,
            content_type=CanonicalGeometryFormat(entry.geometry_format.value).content_type,
            payload=source,
            sha256=hashlib.sha256(source).hexdigest(),
            summary=summary,
        )

    async def normalize(self, request):
        entry = _entry(source=request.payload)
        return self.normalized(entry, request.payload)

    async def reopen(self, geometry):
        return geometry.summary


def _entry(
    *,
    source: bytes = SOURCE,
    source_path: str = "sources/neutral.brep",
) -> SystemGeometrySeedEntry:
    base = SystemGeometrySeedEntry(
        seed_key="neutral_block",
        resource_kind="mapped_model",
        display_name="Neutral block",
        origin_kind="reviewed_system_seed",
        source_path=source_path,
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_format="occt_brep",
        target_asset_role="canonical_geometry",
        geometry_format="occt_brep_v1",
        unit="METRE",
        local_frame={
            "handedness": "right",
            "origin": [0.0, 0.0, 0.0],
            "x_axis": [1.0, 0.0, 0.0],
            "y_axis": [0.0, 1.0, 0.0],
            "z_axis": [0.0, 0.0, 1.0],
        },
        expected_content_digest="0" * 64,
    )
    return _with_expected(base, source=source, normalized=_Runtime().normalized(base, source))


def _with_expected(
    base: SystemGeometrySeedEntry,
    *,
    source: bytes,
    normalized: CanonicalGeometry,
) -> SystemGeometrySeedEntry:
    prepared = prepare_mapped_system_geometry_seed(
        base,
        source=source,
        source_bundle=pack_source_bundle({base.source_path: source}),
        normalized=normalized,
        reopened=normalized.summary,
    )
    version_id = system_geometry_seed_version_id(
        base.seed_key,
        system_geometry_seed_entry_digest(base),
    )
    assets = tuple(
        GeometryResourceVersionAssetRecord(
            resource_version_id=version_id,
            asset_role=asset_role,
            asset_format=asset_format,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=NOW,
        )
        for asset_role, asset_format, filename, content_type, content in prepared.assets
    )
    digest = geometry_resource_content_digest(
        resource_kind=base.resource_kind,
        manifest_digest=sha256_digest(prepared.manifest),
        assets=assets,
    )
    return base.model_copy(update={"expected_content_digest": digest})


class _Repository:
    def __init__(self) -> None:
        self.bundles: dict[tuple[str, str], GeometryResourceVersionBundle] = {}
        self.latest: dict[str, GeometryResourceVersionBundle] = {}
        self.publish_calls = 0

    async def require_geometry_resource_publication_allowed(self, context, *, scope) -> None:
        return None

    async def get_exact_geometry_resource_version(
        self,
        context,
        *,
        resource_id: str,
        resource_version_id: str,
    ) -> GeometryResourceVersionBundle | None:
        return self.bundles.get((resource_id, resource_version_id))

    async def get_latest_geometry_resource_version(
        self,
        context,
        *,
        resource_id: str,
    ) -> GeometryResourceVersionBundle | None:
        return self.latest.get(resource_id)

    async def publish_geometry_resource_version(
        self,
        context,
        publication: GeometryResourceVersionBundle,
    ) -> GeometryResourceVersionBundle:
        self.publish_calls += 1
        key = (
            publication.resource.resource_id,
            publication.version.resource_version_id,
        )
        existing = self.bundles.get(key)
        if existing is not None:
            assert existing == publication
            return existing
        latest = self.latest.get(publication.resource.resource_id)
        assert publication.version.version_no == (
            1 if latest is None else latest.version.version_no + 1
        )
        self.bundles[key] = publication
        self.latest[publication.resource.resource_id] = publication
        return publication


def _service(tmp_path: Path):
    repository = _Repository()
    storage = LocalStorage(
        tmp_path / "storage",
        allowed_content_types=SEED_CONTENT_TYPES,
    )
    storage.write = AsyncMock(wraps=storage.write)
    resources = GeometryResourceService(repository, storage)
    service = SystemGeometrySeedService(
        repository,
        storage,
        resources,
        _Runtime(),
        pack_source_bundle,
        clock=lambda: NOW,
    )
    return service, repository, storage


def test_seed_contract_has_deterministic_ids_and_rejects_unsafe_paths() -> None:
    entry = _entry()
    digest = system_geometry_seed_entry_digest(entry)
    assert system_geometry_seed_resource_id(entry.seed_key) == system_geometry_seed_resource_id(
        entry.seed_key
    )
    assert system_geometry_seed_version_id(
        entry.seed_key, digest
    ) == system_geometry_seed_version_id(entry.seed_key, digest)

    with pytest.raises(ValidationError, match="source_path"):
        _entry(source_path="../outside.brep")
    native_ifc = SystemGeometrySeedEntry.model_validate(
        {**entry.model_dump(mode="json"), "source_format": "ifc"}
    )
    assert native_ifc.geometry_format is CanonicalGeometryFormat.OCCT_BREP_V1


@pytest.mark.asyncio
async def test_backend_asset_intake_recovers_lost_response_and_keeps_exact_sources(tmp_path: Path) -> None:
    from CAD2BIM_backend.domain.control_plane import IdempotencyConflictError
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceScope

    repository = _Repository()
    storage = LocalStorage(tmp_path / "intake", allowed_content_types=SEED_CONTENT_TYPES)
    runtime = _Runtime()
    runtime.normalize = AsyncMock(wraps=runtime.normalize)
    resources = GeometryResourceService(repository, storage, geometry_runtime=runtime,
        source_bundle_packer=pack_source_bundle)
    args = dict(scope=GeometryResourceScope(scope_kind="system"), request_key="reviewed-source",
        display_name="Reviewed asset", source_name="source.brep", source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        normalization=GeometryNormalizeRequest(format="occt_brep_v1", payload=SOURCE),
        documents={"README.md": "来源和已知缺陷\n".encode(), "repair.json": b'{"modified":false}'})
    original_publish = repository.publish_geometry_resource_version

    async def lose_response(context, publication):
        await original_publish(context, publication)
        raise ConnectionError("commit response lost")

    repository.publish_geometry_resource_version = lose_response
    with pytest.raises(ConnectionError, match="response lost"):
        await resources.import_mapped_asset(_context(), **args)
    assert repository.publish_calls == 1
    restarted = GeometryResourceService(repository, storage, geometry_runtime=runtime,
        source_bundle_packer=pack_source_bundle)
    recovered = await restarted.import_mapped_asset(_context(), **args)
    assert runtime.normalize.await_count == 1
    assert repository.publish_calls == 1
    exact = await restarted.read_exact_asset(_context(), resource_id=recovered.resource.resource_id,
        resource_version_id=recovered.version.resource_version_id, asset_role=GeometryResourceAssetRole.SOURCE_BUNDLE)
    source = inspect_source_bundle(exact[1])
    assert source.read("source.brep") == SOURCE
    assert source.read_text("README.md") == "来源和已知缺陷\n"
    assert recovered.version.manifest["source_documents"][0]["path"] == "README.md"
    with pytest.raises(IdempotencyConflictError, match="different input"):
        await restarted.import_mapped_asset(_context(), **{**args, "documents": {"README.md": b"changed"}})
    assert repository.publish_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["scope", "source_digest", "path", "runtime", "reopen", "storage"])
async def test_backend_asset_intake_failures_never_publish(tmp_path: Path, failure: str) -> None:
    from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceIntegrityError
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceScope

    repository = _Repository()
    storage = LocalStorage(tmp_path / "intake-failure", allowed_content_types=SEED_CONTENT_TYPES)
    storage.write = AsyncMock(wraps=storage.write)
    runtime = _Runtime()
    runtime.normalize = AsyncMock(wraps=runtime.normalize)
    packer = Mock(wraps=pack_source_bundle)
    resources = GeometryResourceService(repository, storage, geometry_runtime=runtime, source_bundle_packer=packer)
    args = dict(scope=GeometryResourceScope(scope_kind="system"), request_key="reviewed-source",
        display_name="Reviewed asset", source_name="source.brep", source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        normalization=GeometryNormalizeRequest(format="occt_brep_v1", payload=SOURCE), documents={"README.md": b"source notes"})
    expected = ValueError
    if failure == "scope":
        repository.require_geometry_resource_publication_allowed = AsyncMock(side_effect=PermissionError("scope denied"))
        expected = PermissionError
    elif failure == "source_digest":
        args["source_sha256"] = "0" * 64
        expected = GeometryResourceIntegrityError
    elif failure == "path":
        args["source_name"] = "../source.brep"
    elif failure == "runtime":
        runtime.normalize.side_effect = ValueError("invalid native source")
    elif failure == "reopen":
        runtime.reopen = AsyncMock(side_effect=ValueError("invalid reopened geometry"))
    elif failure == "storage":
        storage.write.side_effect = OSError("storage failed")
        expected = OSError
    with pytest.raises(expected):
        await resources.import_mapped_asset(_context(), **args)
    assert repository.publish_calls == 0
    assert repository.bundles == {}
    if failure != "storage":
        storage.write.assert_not_awaited()
    if failure in {"scope", "source_digest"}:
        packer.assert_not_called()
        runtime.normalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_production_manifest_is_legal_and_performs_zero_writes(
    tmp_path: Path,
) -> None:
    loaded = load_production_system_geometry_seed()
    assert loaded.manifest.resources == ()
    assert loaded.sources == {}

    repository = AsyncMock()
    storage = AsyncMock()
    resources = GeometryResourceService(repository, storage)
    service = SystemGeometrySeedService(
        repository,
        storage,
        resources,
        None,
        pack_source_bundle,
    )
    result = await service.seed(_context(), manifest=loaded.manifest, sources=loaded.sources)

    assert result.published == ()
    assert result.replayed == ()
    storage.write.assert_not_awaited()
    repository.publish_geometry_resource_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_manifest_replays_exact_version_without_another_write(tmp_path: Path) -> None:
    service, repository, storage = _service(tmp_path)
    entry = _entry()
    manifest = SystemGeometrySeedManifest(
        resources=(entry,),
    )

    first = await service.seed(_context(), manifest=manifest, sources={entry.source_path: SOURCE})
    second = await service.seed(_context(), manifest=manifest, sources={entry.source_path: SOURCE})

    assert len(first.published) == 1
    assert first.published[0].version.version_no == 1
    assert second.published == ()
    assert second.replayed == first.published
    assert repository.publish_calls == 1
    assert storage.write.await_count == 3
    retired_profiles = {"validation_profile", "serializer_profile", "runtime_profile"}
    assert not retired_profiles & set(first.published[0].version.manifest)
    assert not retired_profiles & set(first.published[0].version.lineage)
    exact_source = await GeometryResourceService(repository, storage).read_exact_asset(
        _context(),
        resource_id=first.published[0].resource.resource_id,
        resource_version_id=first.published[0].version.resource_version_id,
        asset_role=GeometryResourceAssetRole.SOURCE_BUNDLE,
    )
    assert exact_source is not None
    source_bundle = inspect_source_bundle(exact_source[1])
    assert source_bundle.read(entry.source_path) == SOURCE
    assert exact_source[0].asset_format == "source_bundle_v1"
    assert exact_source[0].content_type == "application/zip"


@pytest.mark.asyncio
async def test_changed_manifest_creates_new_immutable_version(tmp_path: Path) -> None:
    service, repository, _storage = _service(tmp_path)
    first_entry = _entry()
    changed_source = SOURCE + b" changed geometry"
    changed_entry = _entry(source=changed_source)

    first = await service.seed(
        _context(),
        manifest=SystemGeometrySeedManifest(
            resources=(first_entry,),
        ),
        sources={first_entry.source_path: SOURCE},
    )
    changed = await service.seed(
        _context(),
        manifest=SystemGeometrySeedManifest(
            resources=(changed_entry,),
        ),
        sources={changed_entry.source_path: changed_source},
    )

    assert first.published[0].version.version_no == 1
    assert changed.published[0].version.version_no == 2
    assert (
        changed.published[0].version.resource_version_id
        != first.published[0].version.resource_version_id
    )
    assert len(repository.bundles) == 2


@pytest.mark.asyncio
async def test_ifc_source_format_reaches_runtime_and_only_canonical_mesh_is_published(
    tmp_path: Path,
) -> None:
    source = b"IFC source with SECRET_PSET and SECRET_MATERIAL"
    clean_mesh = b"canonical mesh without business semantics"
    base = SystemGeometrySeedEntry.model_validate(
        {
            **_entry().model_dump(mode="json"),
            "seed_key": "neutral_ifc_mesh",
            "source_path": "sources/neutral.ifc",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "source_format": "ifc",
            "geometry_format": "indexed_triangle_mesh_v1",
            "expected_content_digest": "0" * 64,
        }
    )
    normalized = _Runtime().normalized(base, clean_mesh)
    entry = _with_expected(base, source=source, normalized=normalized)
    runtime = AsyncMock()
    runtime.normalize.return_value = normalized
    runtime.reopen.return_value = normalized.summary
    repository = _Repository()
    storage = LocalStorage(
        tmp_path / "storage",
        allowed_content_types=SEED_CONTENT_TYPES,
    )
    resources = GeometryResourceService(repository, storage)
    service = SystemGeometrySeedService(
        repository,
        storage,
        resources,
        runtime,
        pack_source_bundle,
        clock=lambda: NOW,
    )

    result = await service.seed(
        _context(),
        manifest=SystemGeometrySeedManifest(
            resources=(entry,),
        ),
        sources={entry.source_path: source},
    )

    request = runtime.normalize.await_args.args[0]
    assert request.source_format.value == "ifc"
    assert request.format.value == "indexed_triangle_mesh_v1"
    published = result.published[0]
    canonical = await resources.read_exact_asset(
        _context(),
        resource_id=published.resource.resource_id,
        resource_version_id=published.version.resource_version_id,
        asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
    )
    source_asset = await resources.read_exact_asset(
        _context(),
        resource_id=published.resource.resource_id,
        resource_version_id=published.version.resource_version_id,
        asset_role=GeometryResourceAssetRole.SOURCE_BUNDLE,
    )
    manifest_asset = await resources.read_exact_asset(
        _context(),
        resource_id=published.resource.resource_id,
        resource_version_id=published.version.resource_version_id,
        asset_role=GeometryResourceAssetRole.MANIFEST,
    )
    assert canonical is not None and canonical[1] == clean_mesh
    assert source_asset is not None
    assert inspect_source_bundle(source_asset[1]).read(entry.source_path) == source
    assert manifest_asset is not None
    assert b"SECRET_PSET" not in canonical[1]
    assert b"SECRET_PSET" not in manifest_asset[1]


@pytest.mark.asyncio
async def test_neutral_mesh_seed_uses_shared_runtime_and_storage_round_trip(
    tmp_path: Path,
) -> None:
    source = (
        b'{"schema":"indexed_triangle_mesh_v1","triangles":[[0,2,1]],'
        b'"unit":"METRE","vertices":[[0,0,0],[0,1,0],[1,0,0]]}'
    )
    base = SystemGeometrySeedEntry.model_validate(
        {
            **_entry().model_dump(mode="json"),
            "seed_key": "neutral_mesh",
            "source_path": "sources/neutral.mesh.json",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "source_format": "indexed_triangle_mesh",
            "geometry_format": "indexed_triangle_mesh_v1",
            "expected_content_digest": "0" * 64,
        }
    )
    runtime = OcpCadQueryGeometryRuntime()
    try:
        normalized = await runtime.normalize(
            GeometryNormalizeRequest(
                source_format=base.source_format,
                format=base.geometry_format,
                payload=source,
                unit=base.unit,
            )
        )
        entry = _with_expected(base, source=source, normalized=normalized)
        repository = _Repository()
        storage = LocalStorage(tmp_path / "storage")
        resources = GeometryResourceService(repository, storage)
        result = await SystemGeometrySeedService(
            repository,
            storage,
            resources,
            runtime,
            pack_source_bundle,
            clock=lambda: NOW,
        ).seed(
            _context(),
            manifest=SystemGeometrySeedManifest(
                resources=(entry,),
            ),
            sources={entry.source_path: source},
        )
        canonical = await resources.read_exact_asset(
            _context(),
            resource_id=result.published[0].resource.resource_id,
            resource_version_id=result.published[0].version.resource_version_id,
            asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
        )
        assert canonical is not None
        assert canonical[0].content_type == ("application/vnd.cad2bim.indexed-triangle-mesh+json")
        assert canonical[1] == normalized.payload
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_mapped_brep_seed_preserves_legacy_exact_geometry_and_replays(
    tmp_path: Path,
) -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "geometry_runtime"
        / "canonical_reopen"
        / "mapped_body_canonical.brep"
    )
    source = fixture.read_bytes()
    expected_sha256 = "50c1e2f497b0fce8e26cc24cbf6b810b5120d6adf7cf7f3e3ec4395abc6d40f9"
    assert hashlib.sha256(source).hexdigest() == expected_sha256
    base = _entry(source=source, source_path="sources/mapped-body-canonical.brep")
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        normalized = await runtime.normalize(
            GeometryNormalizeRequest(
                source_format=base.source_format,
                format=base.geometry_format,
                payload=source,
                unit=base.unit,
            )
        )
        assert normalized.payload == source
        assert normalized.sha256 == expected_sha256
        entry = _with_expected(base, source=source, normalized=normalized)
        repository = _Repository()
        storage = LocalStorage(tmp_path / "storage", allowed_content_types=SEED_CONTENT_TYPES)
        resources = GeometryResourceService(repository, storage)
        service = SystemGeometrySeedService(
            repository,
            storage,
            resources,
            runtime,
            pack_source_bundle,
            clock=lambda: NOW,
        )
        manifest = SystemGeometrySeedManifest(
            resources=(entry,),
        )

        first = await service.seed(
            _context(),
            manifest=manifest,
            sources={entry.source_path: source},
        )
        replay = await service.seed(
            _context(),
            manifest=manifest,
            sources={entry.source_path: source},
        )
    finally:
        await runtime.close()

    assert len(first.published) == 1
    published = first.published[0]
    assert replay.published == ()
    assert replay.replayed == first.published
    assert repository.publish_calls == 1
    assert {asset.asset_role for asset in published.assets} == {
        GeometryResourceAssetRole.CANONICAL_GEOMETRY,
        GeometryResourceAssetRole.MANIFEST,
        GeometryResourceAssetRole.SOURCE_BUNDLE,
    }
    exact_assets = {
        role: await resources.read_exact_asset(
            _context(),
            resource_id=published.resource.resource_id,
            resource_version_id=published.version.resource_version_id,
            asset_role=role,
        )
        for role in (
            GeometryResourceAssetRole.CANONICAL_GEOMETRY,
            GeometryResourceAssetRole.MANIFEST,
            GeometryResourceAssetRole.SOURCE_BUNDLE,
        )
    }
    assert all(asset is not None for asset in exact_assets.values())
    canonical = exact_assets[GeometryResourceAssetRole.CANONICAL_GEOMETRY]
    assert canonical is not None
    assert canonical[1] == source
    assert canonical[0].sha256 == expected_sha256
    source_asset = exact_assets[GeometryResourceAssetRole.SOURCE_BUNDLE]
    assert source_asset is not None
    assert inspect_source_bundle(source_asset[1]).read(entry.source_path) == source
    manifest_asset = exact_assets[GeometryResourceAssetRole.MANIFEST]
    assert manifest_asset is not None
    exact_manifest = json.loads(manifest_asset[1])
    assert exact_manifest == published.version.manifest
    assert exact_manifest["source_sha256"] == expected_sha256
    assert exact_manifest["canonical_geometry_sha256"] == expected_sha256
    assert exact_manifest["local_frame"] == entry.local_frame.model_dump(mode="json")
    assert exact_manifest["geometry_summary"]["topology"] == normalized.summary.topology.model_dump(mode="json")


@pytest.mark.asyncio
async def test_complete_manifest_is_preflighted_before_any_storage_or_database_write(
    tmp_path: Path,
) -> None:
    service, repository, storage = _service(tmp_path)
    valid = _entry()
    invalid = _entry(source_path="sources/other.brep").model_copy(
        update={"seed_key": "neutral_other", "expected_content_digest": "f" * 64}
    )
    manifest = SystemGeometrySeedManifest(
        resources=(valid, invalid),
    )

    with pytest.raises(SystemGeometrySeedError, match="content digest mismatch"):
        await service.seed(
            _context(),
            manifest=manifest,
            sources={valid.source_path: SOURCE, invalid.source_path: SOURCE},
        )

    storage.write.assert_not_awaited()
    assert repository.publish_calls == 0


@pytest.mark.asyncio
async def test_source_digest_mismatch_fails_before_runtime_or_writes(tmp_path: Path) -> None:
    repository = _Repository()
    storage = LocalStorage(
        tmp_path / "storage",
        allowed_content_types=SEED_CONTENT_TYPES,
    )
    storage.write = AsyncMock(wraps=storage.write)
    runtime = AsyncMock()
    service = SystemGeometrySeedService(
        repository,
        storage,
        GeometryResourceService(repository, storage),
        runtime,
        pack_source_bundle,
    )
    entry = _entry()

    with pytest.raises(SystemGeometrySeedError, match="source digest mismatch"):
        await service.seed(
            _context(),
            manifest=SystemGeometrySeedManifest(
                resources=(entry,),
            ),
            sources={entry.source_path: b"tampered"},
        )

    runtime.prepare_system_seed.assert_not_awaited()
    storage.write.assert_not_awaited()
    assert repository.publish_calls == 0


def test_fixture_loader_rejects_symlink_sources(tmp_path: Path) -> None:
    outside = tmp_path / "outside.brep"
    outside.write_bytes(SOURCE)
    root = tmp_path / "seed"
    (root / "sources").mkdir(parents=True)
    (root / "sources" / "neutral.brep").symlink_to(outside)
    entry = _entry()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "resources": [entry.model_dump(mode="json")],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemGeometrySeedLoadError, match="missing or unsafe"):
        load_system_geometry_seed_directory(root)


def test_fixture_loader_reads_only_manifest_declared_safe_sources(tmp_path: Path) -> None:
    root = tmp_path / "seed"
    (root / "sources").mkdir(parents=True)
    (root / "sources" / "neutral.brep").write_bytes(SOURCE)
    (root / "unlisted.bin").write_bytes(b"must not be loaded")
    entry = _entry()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "resources": [entry.model_dump(mode="json")],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_system_geometry_seed_directory(root)

    assert loaded.manifest.resources == (entry,)
    assert loaded.sources == {entry.source_path: SOURCE}


def test_fixture_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    root = tmp_path / "seed"
    root.mkdir()
    (root / "manifest.json").write_text(
        '{"resources":[],"resources":[]}',
        encoding="utf-8",
    )

    with pytest.raises(SystemGeometrySeedLoadError, match="duplicate key"):
        load_system_geometry_seed_directory(root)


class _RoleQueryResult:
    def __init__(self, row) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _RoleConnection:
    def __init__(self, row) -> None:
        self._row = row

    async def execute(self, _statement):
        return _RoleQueryResult(self._row)


class _RoleConnectionContext:
    def __init__(self, row) -> None:
        self._row = row

    async def __aenter__(self):
        return _RoleConnection(self._row)

    async def __aexit__(self, *_args):
        return None


class _RoleDatabase:
    def __init__(self, row) -> None:
        self.engine = SimpleNamespace(connect=lambda: _RoleConnectionContext(row))


@pytest.mark.asyncio
async def test_trusted_cli_accepts_only_curator_members_without_privilege_bypass() -> None:
    accepted = SimpleNamespace(rolsuper=False, rolbypassrls=False, is_member=True,
                               is_api=False, is_mcp=False, is_runner=False)
    await _require_trusted_system_curator(_RoleDatabase(accepted))

    for rejected in (
        SimpleNamespace(**(vars(accepted) | {"is_member": False})),
        SimpleNamespace(**(vars(accepted) | {"rolsuper": True})),
        SimpleNamespace(**(vars(accepted) | {"rolbypassrls": True})),
        SimpleNamespace(**(vars(accepted) | {"is_api": True})),
        SimpleNamespace(**(vars(accepted) | {"is_mcp": True})),
        SimpleNamespace(**(vars(accepted) | {"is_runner": True})),
        None,
    ):
        with pytest.raises(RuntimeError, match="cad2bim_system_curator"):
            await _require_trusted_system_curator(_RoleDatabase(rejected))
