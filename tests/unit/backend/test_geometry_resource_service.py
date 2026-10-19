from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.geometry_resource_service import (
    GeometryResourceIntegrityError,
    GeometryResourceBuildError,
    GeometryResourceService,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.control_plane import ConflictError
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceRecord,
    GeometryResourceScope,
    GeometryResourceScopeKind,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    GeometryResourceInput,
    geometry_resource_content_digest,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.ports.storage import ResourceStorageKey, StoredObject, VerifiedStorageRead


NOW = datetime(2026, 9, 2, tzinfo=UTC)


def _input_bundle(*, size_bytes: int = 4):
    base = _bundle()
    canonical_asset = base.assets[0].model_copy(update={"size_bytes": size_bytes})
    manifest = {**base.version.manifest, "canonical_geometry_sha256": canonical_asset.sha256}
    manifest_bytes = canonical_json(manifest).encode()
    assets = (canonical_asset, base.assets[1].model_copy(update={
        "size_bytes": len(manifest_bytes), "sha256": sha256_digest(manifest),
    }))
    version = GeometryResourceVersionRecord.model_validate({
        **base.version.model_dump(), "manifest": manifest, "manifest_digest": sha256_digest(manifest),
        "content_digest": geometry_resource_content_digest(
            resource_kind=base.resource.resource_kind, manifest_digest=sha256_digest(manifest), assets=assets),
    })
    bundle = GeometryResourceVersionBundle(resource=base.resource, version=version, assets=assets)
    frozen = GeometryResourceInput(input_key="body", resource_id=base.resource.resource_id,
        resource_version_id=version.resource_version_id, content_digest=version.content_digest,
        canonical_geometry_sha256=canonical_asset.sha256)
    return bundle, frozen


@pytest.mark.asyncio
async def test_named_brep_inputs_reauthorize_and_verify_exact_bytes() -> None:
    bundle, frozen = _input_bundle()
    repository, storage = AsyncMock(), AsyncMock()
    repository.get_exact_geometry_resource_version.return_value = bundle
    service = GeometryResourceService(repository, storage)
    asset = bundle.assets[0]
    key = service.resource_storage_key(bundle, asset)
    storage.read_verified.return_value = VerifiedStorageRead(
        stored=StoredObject(key=key, filename=asset.filename, content_type=asset.content_type,
            size_bytes=asset.size_bytes, sha256=asset.sha256, created_at=asset.created_at),
        content=b"brep", verified_sha256=asset.sha256,
    )
    result = await service.load_build_inputs(_context(), (frozen,))
    assert [(item.input_key, item.payload, item.sha256) for item in result] == [("body", b"brep", asset.sha256)]
    repository.get_exact_geometry_resource_version.assert_awaited_once_with(
        _context(), resource_id=frozen.resource_id, resource_version_id=frozen.resource_version_id)
    storage.read_verified.assert_awaited_once_with(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hidden", "changed", "mesh", "over_budget", "second_hidden"])
async def test_named_brep_inputs_reject_metadata_before_storage(failure) -> None:
    bundle, frozen = _input_bundle(size_bytes=33 * 1024 * 1024 if failure == "over_budget" else 4)
    repository, storage = AsyncMock(), AsyncMock()
    inputs = (frozen,)
    if failure == "changed":
        inputs = (frozen.model_copy(update={"content_digest": "0" * 64}),)
    if failure == "mesh":
        bundle = bundle.model_copy(update={"assets": (bundle.assets[0].model_copy(update={
            "asset_format": "indexed_triangle_mesh_v1"}), *bundle.assets[1:])})
    repository.get_exact_geometry_resource_version.return_value = None if failure == "hidden" else bundle
    if failure == "second_hidden":
        inputs = (frozen, frozen.model_copy(update={"input_key": "other"}))
        repository.get_exact_geometry_resource_version.side_effect = [bundle, None]
    with pytest.raises(GeometryResourceBuildError):
        await GeometryResourceService(repository, storage).load_build_inputs(_context(), inputs)
    storage.read_verified.assert_not_awaited()


def _context() -> TenantExecutionContext:
    return TenantExecutionContext(
        tenant_id="tenant-1",
        principal_id="principal-1",
        task_id="task-1",
        actor_kind=TenantActorKind.USER,
        command_id="command-1",
    )


def _bundle() -> GeometryResourceVersionBundle:
    manifest = {"resource_kind": "mapped_model"}
    manifest_content = canonical_json(manifest).encode("utf-8")
    version_id = "geometry-version-1"
    assets = (
        GeometryResourceVersionAssetRecord(
            resource_version_id=version_id,
            asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
            asset_format="occt_brep_v1",
            filename="model.brep",
            content_type="application/octet-stream",
            size_bytes=4,
            sha256=hashlib.sha256(b"brep").hexdigest(),
            created_at=NOW,
        ),
        GeometryResourceVersionAssetRecord(
            resource_version_id=version_id,
            asset_role=GeometryResourceAssetRole.MANIFEST,
            asset_format="json",
            filename="manifest.json",
            content_type="application/json",
            size_bytes=len(manifest_content),
            sha256=sha256_digest(manifest),
            created_at=NOW,
        ),
    )
    resource = GeometryResourceRecord(
        resource_id="geometry-resource-1",
        resource_kind=GeometryResourceKind.MAPPED_MODEL,
        owner_scope=GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.TASK,
            tenant_id="tenant-1",
            task_id="task-1",
        ),
        display_name="Mapped model",
        created_at=NOW,
    )
    manifest_digest = sha256_digest(manifest)
    version = GeometryResourceVersionRecord(
        resource_version_id=version_id,
        resource_id=resource.resource_id,
        version_no=1,
        origin_kind="agent_generated",
        content_digest=geometry_resource_content_digest(
            resource_kind=resource.resource_kind,
            manifest_digest=manifest_digest,
            assets=assets,
        ),
        manifest_digest=manifest_digest,
        manifest=manifest,
        created_at=NOW,
    )
    return GeometryResourceVersionBundle(
        resource=resource,
        version=version,
        assets=assets,
    )


def _copy_source(kind=GeometryResourceKind.MAPPED_MODEL, *, inputs=(), identity="source", brep=b"brep"):
    manifest = {"resource_kind": kind.value,
        "usage_instructions": "Use the registered exact geometry in metres."}
    if inputs:
        manifest["geometry_inputs"] = [item.model_dump(mode="json") for item in inputs]
    source = (GeometryResourceAssetRole.SOURCE_BUNDLE, "source_bundle_v1", "source.zip",
        "application/zip", b"registered-source-bundle")
    preview = (GeometryResourceAssetRole.PREVIEW, "png", "preview.png", "image/png", b"registered-preview")
    if kind is GeometryResourceKind.MAPPED_MODEL:
        manifest["canonical_geometry_sha256"] = hashlib.sha256(brep).hexdigest()
        specs = [(GeometryResourceAssetRole.CANONICAL_GEOMETRY, "occt_brep_v1", "model.brep",
            "application/octet-stream", brep),
            (GeometryResourceAssetRole.MANIFEST, "json", "manifest.json", "application/json",
                canonical_json(manifest).encode()), source, preview]
    else:
        specs = [source,
            (GeometryResourceAssetRole.PARAMETER_SCHEMA, "json_schema", "parameters.schema.json",
                "application/schema+json", b'{"type":"object"}'),
            (GeometryResourceAssetRole.SAMPLE_OUTPUT, "occt_brep_v1", "sample.brep",
                "application/octet-stream", b"brep"), preview]
    version_id = f"{identity}-version"
    assets = tuple(GeometryResourceVersionAssetRecord(
        resource_version_id=version_id, asset_role=role, asset_format=fmt, filename=name,
        content_type=mime, size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), created_at=NOW,
    ) for role, fmt, name, mime, content in specs)
    resource = GeometryResourceRecord(resource_id=identity, resource_kind=kind,
        owner_scope=_bundle().resource.owner_scope, display_name="Exact source", created_at=NOW)
    manifest_digest = sha256_digest(manifest)
    version = GeometryResourceVersionRecord(resource_version_id=version_id, resource_id=identity,
        version_no=2, origin_kind="agent_generated", manifest=manifest, manifest_digest=manifest_digest,
        content_digest=geometry_resource_content_digest(
            resource_kind=kind, manifest_digest=manifest_digest, assets=assets),
        lineage={"operation_id": "source-operation"}, created_at=NOW)
    return GeometryResourceVersionBundle(resource=resource, version=version, assets=assets), tuple(spec[-1] for spec in specs)


@pytest.mark.parametrize("kind", tuple(GeometryResourceKind))
def test_system_copy_preserves_all_registered_bytes_without_runtime(kind) -> None:
    source, contents = _copy_source(kind)
    runtime, repository, storage = AsyncMock(), AsyncMock(), AsyncMock()
    service = GeometryResourceService(repository, storage, geometry_runtime=runtime)
    copied, copied_contents = service.prepare_system_copy(
        source=source, source_assets=contents, target_resource_id="system-copy",
        target_resource_version_id="system-copy-version", geometry_inputs={}, created_at=NOW + timedelta(days=1))
    assert copied_contents == contents
    assert copied.resource.owner_scope == GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM)
    assert copied.resource.display_name == source.resource.display_name
    assert copied.version.version_no == 1
    assert copied.version.origin_kind == "system_copy"
    assert copied.version.manifest == source.version.manifest
    assert copied.version.content_digest == source.version.content_digest
    assert copied.version.lineage == {"source_resource_id": source.resource.resource_id,
        "source_resource_version_id": source.version.resource_version_id,
        "source_content_digest": source.version.content_digest}
    assert [(asset.asset_role, asset.asset_format, asset.filename, asset.content_type, asset.size_bytes, asset.sha256)
        for asset in copied.assets] == [(asset.asset_role, asset.asset_format, asset.filename,
            asset.content_type, asset.size_bytes, asset.sha256) for asset in source.assets]
    assert all(asset.resource_version_id == "system-copy-version" for asset in copied.assets)
    assert runtime.mock_calls == repository.mock_calls == storage.mock_calls == []


@pytest.mark.parametrize("kind", tuple(GeometryResourceKind))
def test_system_copy_remaps_inputs_and_only_manifest_asset_bytes(kind) -> None:
    dependency, dependency_contents = _copy_source(identity="dependency")
    frozen = GeometryResourceInput(input_key="body", resource_id=dependency.resource.resource_id,
        resource_version_id=dependency.version.resource_version_id, content_digest=dependency.version.content_digest,
        canonical_geometry_sha256=dependency.assets[0].sha256)
    source, contents = _copy_source(kind, inputs=(frozen,))
    target, _ = GeometryResourceService.prepare_system_copy(source=dependency, source_assets=dependency_contents,
        target_resource_id="system-dependency", target_resource_version_id="system-dependency-version",
        geometry_inputs={}, created_at=NOW)
    copied, copied_contents = GeometryResourceService.prepare_system_copy(source=source, source_assets=contents,
        target_resource_id="system-copy", target_resource_version_id="system-copy-version",
        geometry_inputs={"body": target}, created_at=NOW)
    assert copied.version.manifest["geometry_inputs"] == [{"input_key": "body",
        "resource_id": "system-dependency", "resource_version_id": "system-dependency-version",
        "content_digest": target.version.content_digest, "canonical_geometry_sha256": frozen.canonical_geometry_sha256}]
    assert source.version.manifest["geometry_inputs"] == [frozen.model_dump(mode="json")]
    assert copied.version.content_digest != source.version.content_digest
    for asset, original, changed in zip(copied.assets, contents, copied_contents, strict=True):
        if asset.asset_role is GeometryResourceAssetRole.MANIFEST:
            assert changed == canonical_json(copied.version.manifest).encode()
            assert changed != original
        else:
            assert changed == original


@pytest.mark.parametrize("failure", ["missing_bytes", "changed_bytes", "legacy", "missing_role"])
def test_system_copy_rejects_incomplete_or_noncurrent_sources(failure) -> None:
    source, contents = _copy_source()
    if failure == "missing_bytes":
        contents = contents[:-1]
    elif failure == "changed_bytes":
        contents = (*contents[:-1], b"changed-preview")
    elif failure == "missing_role":
        source = source.model_copy(update={"assets": source.assets[2:]})
        contents = contents[2:]
    else:
        source = source.model_copy(update={"version": source.version.model_copy(
            update={"manifest": source.version.manifest | {"validation_samples": [{}]}})})
    with pytest.raises(GeometryResourceIntegrityError):
        GeometryResourceService.prepare_system_copy(source=source, source_assets=contents,
            target_resource_id="system-copy", target_resource_version_id="system-copy-version",
            geometry_inputs={}, created_at=NOW)


@pytest.mark.parametrize("failure", ["private", "changed_brep", "missing_mapping", "extra_mapping"])
def test_system_copy_rejects_invalid_system_input_mapping(failure) -> None:
    dependency, dependency_contents = _copy_source(identity="dependency")
    frozen = GeometryResourceInput(input_key="body", resource_id=dependency.resource.resource_id,
        resource_version_id=dependency.version.resource_version_id, content_digest=dependency.version.content_digest,
        canonical_geometry_sha256=dependency.assets[0].sha256)
    source, contents = _copy_source(inputs=(frozen,))
    target, _ = GeometryResourceService.prepare_system_copy(source=dependency, source_assets=dependency_contents,
        target_resource_id="system-dependency", target_resource_version_id="system-dependency-version",
        geometry_inputs={}, created_at=NOW)
    mapping = {"body": target}
    if failure == "private":
        mapping["body"] = dependency
    elif failure == "changed_brep":
        changed, changed_contents = _copy_source(identity="changed-dependency", brep=b"different-brep")
        mapping["body"], _ = GeometryResourceService.prepare_system_copy(
            source=changed, source_assets=changed_contents, target_resource_id="changed-system-input",
            target_resource_version_id="changed-system-input-version", geometry_inputs={}, created_at=NOW)
    elif failure == "missing_mapping":
        mapping = {}
    else:
        mapping["extra"] = target
    with pytest.raises(GeometryResourceIntegrityError):
        GeometryResourceService.prepare_system_copy(source=source, source_assets=contents,
            target_resource_id="system-copy", target_resource_version_id="system-copy-version",
            geometry_inputs=mapping, created_at=NOW)


@pytest.mark.asyncio
async def test_saved_candidate_verifies_every_asset_and_detects_later_corruption(tmp_path: Path) -> None:
    source, contents = _copy_source()
    candidate, contents = GeometryResourceService.prepare_system_copy(source=source, source_assets=contents,
        target_resource_id="system-copy", target_resource_version_id="system-copy-version",
        geometry_inputs={}, created_at=NOW)
    repository, storage = AsyncMock(), LocalStorage(tmp_path)
    service = GeometryResourceService(repository, storage)
    stored = await service.store_candidate_assets(_context(), candidate, contents)
    await GeometryResourceService(repository, LocalStorage(tmp_path)).verify_candidate_assets(_context(), stored)
    last_key = service.resource_storage_key(stored, stored.assets[-1])
    (storage._logical_directory(last_key) / "payload").write_bytes(b"corrupt")
    with pytest.raises(GeometryResourceIntegrityError):
        await service.verify_candidate_assets(_context(), stored)
    repository.publish_geometry_resource_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_candidate_authorization_precedes_storage() -> None:
    repository, storage = AsyncMock(), AsyncMock()
    repository.require_geometry_resource_publication_allowed.side_effect = ConflictError("scope rejected")
    with pytest.raises(ConflictError, match="scope rejected"):
        await GeometryResourceService(repository, storage).verify_candidate_assets(_context(), _bundle())
    storage.read_verified.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_scope_gate_runs_before_any_storage_read() -> None:
    repository = AsyncMock()
    repository.require_geometry_resource_publication_allowed.side_effect = ConflictError(
        "scope rejected"
    )
    storage = AsyncMock()
    service = GeometryResourceService(repository, storage)

    with pytest.raises(ConflictError, match="scope rejected"):
        await service.publish_candidate(_context(), _bundle())

    storage.stat.assert_not_awaited()
    storage.read.assert_not_awaited()
    storage.read_verified.assert_not_awaited()
    repository.publish_geometry_resource_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_hidden_exact_version_does_not_reach_storage() -> None:
    repository = AsyncMock()
    repository.get_exact_geometry_resource_version.return_value = None
    storage = AsyncMock()
    service = GeometryResourceService(repository, storage)

    result = await service.read_exact_asset(
        _context(),
        resource_id="hidden-resource",
        resource_version_id="hidden-version",
        asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
    )

    assert result is None
    storage.stat.assert_not_awaited()
    storage.read.assert_not_awaited()
    storage.read_verified.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_asset_uses_one_verified_payload_scan_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    asset = bundle.assets[0]
    repository = AsyncMock()
    repository.get_exact_geometry_resource_version.return_value = bundle
    initial_storage = LocalStorage(tmp_path)
    key = GeometryResourceService.resource_storage_key(bundle, asset)
    await initial_storage.write_bytes(
        key,
        b"brep",
        filename=asset.filename,
        content_type=asset.content_type,
    )
    restarted_storage = LocalStorage(tmp_path)
    service = GeometryResourceService(repository, restarted_storage)
    original_read_chunk = restarted_storage._read_chunk
    observed_bytes = 0

    def read_chunk(descriptor: int, size: int) -> bytes:
        nonlocal observed_bytes
        chunk = original_read_chunk(descriptor, size)
        observed_bytes += len(chunk)
        return chunk

    def reject_second_checksum_scan(_path: Path) -> tuple[int, str]:
        raise AssertionError("exact asset read must use the verified-read digest")

    monkeypatch.setattr(restarted_storage, "_read_chunk", read_chunk)
    monkeypatch.setattr(restarted_storage, "_hash_file", reject_second_checksum_scan)

    result = await service.read_exact_asset(
        _context(),
        resource_id=bundle.resource.resource_id,
        resource_version_id=bundle.version.resource_version_id,
        asset_role=asset.asset_role,
    )

    assert result == (asset, b"brep")
    assert observed_bytes == 4


@pytest.mark.asyncio
async def test_exact_asset_projects_verified_storage_corruption_as_integrity_failure(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    asset = bundle.assets[0]
    repository = AsyncMock()
    repository.get_exact_geometry_resource_version.return_value = bundle
    storage = LocalStorage(tmp_path)
    key = GeometryResourceService.resource_storage_key(bundle, asset)
    await storage.write_bytes(
        key,
        b"brep",
        filename=asset.filename,
        content_type=asset.content_type,
    )
    (storage._logical_directory(key) / "payload").write_bytes(b"oops")

    with pytest.raises(GeometryResourceIntegrityError, match="asset is missing"):
        await GeometryResourceService(repository, storage).read_exact_asset(
            _context(),
            resource_id=bundle.resource.resource_id,
            resource_version_id=bundle.version.resource_version_id,
            asset_role=asset.asset_role,
        )


@pytest.mark.asyncio
async def test_exact_asset_rejects_verified_bytes_bound_to_another_storage_role() -> None:
    bundle = _bundle()
    asset = bundle.assets[0]
    repository = AsyncMock()
    repository.get_exact_geometry_resource_version.return_value = bundle
    storage = AsyncMock()
    storage.read_verified.return_value = VerifiedStorageRead(
        stored=StoredObject(
            key=ResourceStorageKey(
                scope_kind="TASK",
                scope_id="task-1",
                resource_version_id=bundle.version.resource_version_id,
                asset_role="manifest",
            ),
            filename=asset.filename,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            sha256=asset.sha256,
            created_at=asset.created_at,
        ),
        content=b"brep",
        verified_sha256=asset.sha256,
    )

    with pytest.raises(GeometryResourceIntegrityError, match="metadata or digest"):
        await GeometryResourceService(repository, storage).read_exact_asset(
            _context(),
            resource_id=bundle.resource.resource_id,
            resource_version_id=bundle.version.resource_version_id,
            asset_role=asset.asset_role,
        )


@pytest.mark.asyncio
async def test_executable_program_versions_validate_and_forward_owner_keyset() -> None:
    repository = AsyncMock()
    repository.list_executable_program_versions.return_value = (_bundle(),)
    service = GeometryResourceService(repository, AsyncMock())
    boundary = NOW - timedelta(seconds=1)

    result = await service.list_executable_program_versions(
        _context(),
        before_created_at=boundary,
        before_resource_version_id="geometry-version-boundary",
        limit=51,
    )

    assert result == (_bundle(),)
    repository.list_executable_program_versions.assert_awaited_once_with(
        _context(),
        before_created_at=boundary,
        before_resource_version_id="geometry-version-boundary",
        limit=51,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before_created_at", "before_resource_version_id", "limit", "message"),
    (
        (NOW, None, 1, "requires timestamp and version ID"),
        (None, "geometry-version-1", 1, "requires timestamp and version ID"),
        (datetime(2026, 9, 2), "geometry-version-1", 1, "timezone-aware"),
        (NOW, "", 1, "version ID is invalid"),
        (None, None, 0, "between 1 and 51"),
        (None, None, 52, "between 1 and 51"),
    ),
)
async def test_executable_program_versions_reject_invalid_owner_keysets(
    before_created_at: datetime | None,
    before_resource_version_id: str | None,
    limit: int,
    message: str,
) -> None:
    repository = AsyncMock()
    service = GeometryResourceService(repository, AsyncMock())

    with pytest.raises(ValueError, match=message):
        await service.list_executable_program_versions(
            _context(),
            before_created_at=before_created_at,
            before_resource_version_id=before_resource_version_id,
            limit=limit,
        )

    repository.list_executable_program_versions.assert_not_awaited()






@pytest.mark.asyncio
async def test_backend_mapped_intake_selects_clean_asset_assembly(monkeypatch) -> None:
    from CAD2BIM_backend.application import geometry_resource_service as service_module
    from CAD2BIM_backend.domain.geometry_runtime import (
        CanonicalGeometry,
        CanonicalGeometryFormat,
        GeometryNormalizeRequest,
        GeometrySummary,
    )

    class AssemblyObserved(RuntimeError):
        pass

    assemble = service_module.prepare_mapped_geometry_assets

    def observe_clean_assembly(**kwargs):
        assert "legacy_validation_report" not in kwargs
        prepared = assemble(**kwargs)
        assert "validation_profile" not in prepared.manifest
        assert all(
            spec[0] is not GeometryResourceAssetRole.VALIDATION_REPORT
            for spec in prepared.assets
        )
        raise AssemblyObserved

    monkeypatch.setattr(
        service_module,
        "prepare_mapped_geometry_assets",
        observe_clean_assembly,
    )
    repository = AsyncMock()
    repository.get_exact_geometry_resource_version.return_value = None
    runtime = AsyncMock()
    source = b"mapped-source"
    summary = GeometrySummary.model_validate({
        "format": "occt_brep_v1",
        "bounds": {"minimum_m": [0, 0, 0], "maximum_m": [1, 1, 1]},
        "topology": {
            "vertex_count": 8,
            "edge_count": 12,
            "wire_count": 6,
            "face_count": 6,
            "shell_count": 1,
            "solid_count": 1,
            "triangle_count": None,
            "boundary_edge_count": 0,
        },
        "surface_area_m2": 6,
        "volume_m3": 1,
    })
    runtime.normalize.return_value = CanonicalGeometry(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=source,
        sha256=hashlib.sha256(source).hexdigest(),
        summary=summary,
    )
    runtime.reopen.return_value = summary
    service = GeometryResourceService(
        repository,
        AsyncMock(),
        geometry_runtime=runtime,
        source_bundle_packer=lambda _members: b"packed-source",
    )

    with pytest.raises(AssemblyObserved):
        await service.import_mapped_asset(
            _context(),
            scope=GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TASK,
                tenant_id="tenant-1",
                task_id="task-1",
            ),
            request_key="clean-intake",
            display_name="Clean mapped intake",
            source_name="source.brep",
            source_sha256=hashlib.sha256(source).hexdigest(),
            normalization=GeometryNormalizeRequest(
                format="occt_brep_v1",
                payload=source,
            ),
            documents={},
        )
