from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.backend.product_family_fixtures import current_family_contract

from CAD2BIM_backend.application.product_family_asset_service import (
    ProductFamilyAssetIntegrityError,
    ProductFamilyAssetService,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyGeometryBindingRecord,
    ProductFamilyGeometryPurpose,
    ProductFamilyRecord,
    ProductFamilyContract,
    ProductFamilyVersionAssetRecord,
    ProductFamilyVersionAssetRole,
    ProductFamilyVersionBundle,
    ProductFamilyVersionRecord,
    product_family_content_digest,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyProgramSource
from CAD2BIM_backend.domain.product_realization import (
    ProductFamilyStandardModelManifest,
)
from CAD2BIM_backend.infrastructure.source_text.source_bundle import pack_source_bundle
from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import (
    SourceTextGeometryAuthoringSource,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage


NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _source_only_bundle(content: bytes) -> ProductFamilyVersionBundle:
    family = ProductFamilyRecord(
        family_id="family-1",
        owner_scope=ResourceOwnerScope(
            scope_kind="task",
            tenant_id="tenant-1",
            task_id="task-1",
        ),
        created_at=NOW,
    )
    binding = ProductFamilyGeometryBindingRecord(
        family_version_id="family-version-1",
        binding_key="body",
        geometry_resource_id="geometry-1",
        geometry_resource_version_id="geometry-version-1",
        geometry_content_digest="a" * 64,
        expected_resource_kind=GeometryResourceKind.MAPPED_MODEL,
        geometry_purpose=ProductFamilyGeometryPurpose.PRODUCT_COMPONENT,
    )
    asset = ProductFamilyVersionAssetRecord(
        family_version_id="family-version-1",
        asset_role=ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
        asset_format="source_bundle_v1",
        filename="source.zip",
        content_type="application/zip",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        created_at=NOW,
    )
    contract = current_family_contract(binding_key="body")
    contract_digest = sha256_digest(contract)
    version = ProductFamilyVersionRecord(
        family_version_id="family-version-1",
        family_id=family.family_id,
        version_no=1,
        display_name="Valve family",
        purpose="Exercise exact ProductFamily version assets",
        family_contract=contract,
        contract_digest=contract_digest,
        content_digest=product_family_content_digest(
            contract_digest=contract_digest,
            program_entry="main.py:realize",
            geometry_bindings=(binding,),
            assets=(asset,),
        ),
        created_at=NOW,
    )
    return ProductFamilyVersionBundle(
        family=family,
        version=version,
        geometry_bindings=(binding,),
        assets=(asset,),
    )


def _formal_standard_bundle(
    content: bytes,
    *,
    manifest_updates: dict | None = None,
    contract_updates: dict | None = None,
) -> tuple[ProductFamilyVersionBundle, tuple[bytes, ...]]:
    source_only = _source_only_bundle(content)
    contract_payload = source_only.version.family_contract.model_dump(mode="json")
    contract_payload["standard_model"] = {}
    contract_payload.update(contract_updates or {})
    contract = ProductFamilyContract.model_validate(contract_payload)
    contract_digest = sha256_digest(contract)
    summary = GeometrySummary(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        bounds=GeometryBounds(minimum_m=(0, 0, 0), maximum_m=(1, 1, 1)),
        topology=GeometryTopologySummary(
            vertex_count=8,
            edge_count=12,
            wire_count=6,
            face_count=6,
            shell_count=1,
            solid_count=1,
            triangle_count=None,
            boundary_edge_count=0,
        ),
        surface_area_m2=6,
        volume_m3=1,
    )
    geometry = b"formal-standard-brep"
    formal_manifest = ProductFamilyStandardModelManifest(
        family_version_id=source_only.version.family_version_id,
        contract_digest=contract_digest,
        source_bundle_sha256=hashlib.sha256(content).hexdigest(),
        binding_digest=sha256_digest([
            {
                "binding_key": "body",
                "resource_kind": "mapped_model",
                "geometry_content_digest": "a" * 64,
            }
        ]),

        geometry_sha256=hashlib.sha256(geometry).hexdigest(),
        geometry_summary=summary,
    )
    manifest_payload = formal_manifest.model_dump(mode="json", by_alias=True)
    manifest_payload.update(manifest_updates or {})
    manifest = canonical_json(manifest_payload).encode()
    geometry_asset = ProductFamilyVersionAssetRecord(
        family_version_id=source_only.version.family_version_id,
        asset_role=ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY,
        asset_format="occt_brep_v1",
        filename="standard_model.brep",
        content_type="application/vnd.opencascade.brep",
        size_bytes=len(geometry),
        sha256=hashlib.sha256(geometry).hexdigest(),
        created_at=NOW,
    )
    manifest_asset = ProductFamilyVersionAssetRecord(
        family_version_id=source_only.version.family_version_id,
        asset_role=ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST,
        asset_format="json",
        filename="standard_model.json",
        content_type="application/json",
        size_bytes=len(manifest),
        sha256=hashlib.sha256(manifest).hexdigest(),
        created_at=NOW,
    )
    assets = (source_only.assets[0], geometry_asset, manifest_asset)
    version = source_only.version.model_copy(update={
        "family_contract": contract,
        "contract_digest": contract_digest,
        "content_digest": product_family_content_digest(
            contract_digest=contract_digest,
            program_entry=source_only.version.program_entry,
            geometry_bindings=source_only.geometry_bindings,
            assets=assets,
        ),
    })
    return ProductFamilyVersionBundle(
        family=source_only.family,
        version=version,
        geometry_bindings=source_only.geometry_bindings,
        assets=assets,
    ), (content, geometry, manifest)


@pytest.mark.asyncio
async def test_product_family_asset_service_reads_the_exact_canonical_bundle(
    tmp_path: Path,
) -> None:
    content = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    bundle, payloads = _formal_standard_bundle(content)
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(storage, SourceTextGeometryAuthoringSource())
    for asset, payload in zip(bundle.assets, payloads, strict=True):
        await storage.write_bytes(
            service.storage_key(bundle, asset),
            payload,
            filename=asset.filename,
            content_type=asset.content_type,
        )

    await service.verify_bundle(bundle)
    assert await service.read_exact_asset(bundle, bundle.assets[0]) == payloads[0]


@pytest.mark.asyncio
async def test_product_family_asset_service_reads_current_source_only_family_without_standard_capability(
    tmp_path: Path,
) -> None:
    content = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(
        storage,
        SourceTextGeometryAuthoringSource(),
    )

    bundle = _source_only_bundle(content)
    asset = bundle.assets[0]
    await storage.write_bytes(service.storage_key(bundle,asset),content,
        filename=asset.filename,content_type=asset.content_type)
    await service.verify_bundle(bundle)
    assert await service.read_exact_asset(bundle,asset) == content
    with pytest.raises(ProductFamilyAssetIntegrityError, match="no standard model assets"):
        await service.standard_model_assets(bundle)


@pytest.mark.asyncio
async def test_formal_standard_manifest_reads_without_validation_report(
    tmp_path: Path,
) -> None:
    content = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    bundle, payloads = _formal_standard_bundle(content)
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(storage, SourceTextGeometryAuthoringSource())
    for asset, payload in zip(bundle.assets, payloads, strict=True):
        await storage.write_bytes(
            service.storage_key(bundle, asset),
            payload,
            filename=asset.filename,
            content_type=asset.content_type,
        )

    await service.verify_bundle(bundle)
    manifest, geometry = await service.standard_model_assets(bundle)

    assert "schema" not in manifest.model_dump()
    assert geometry == payloads[1]
    assert ProductFamilyVersionAssetRole.VALIDATION_REPORT not in {
        asset.asset_role for asset in bundle.assets
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest_updates",
    [
        {"source_bundle_sha256": "0" * 64},
        {"binding_digest": "0" * 64},
        {"geometry_sha256": "0" * 64},
        {"schema": "product_family_standard_model_manifest_v2"},
        {"abi_profile": "product_realization_v5"},
        {"runtime_profile": "product_realization_python_v1"},
        {"budget_profile": "product_realization_budget_v1"},
        {"retired_extra_field": True},
        {"modeling_execution_contract": {"retired_implementation_fingerprint": "obsolete"},
         "modeling_geometry_execution_contracts": {"body": "obsolete"}},
    ],
)
async def test_formal_standard_manifest_rejects_exact_fact_drift(
    tmp_path: Path,
    manifest_updates: dict,
) -> None:
    content = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    bundle, payloads = _formal_standard_bundle(
        content,
        manifest_updates=manifest_updates,
    )
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(storage, SourceTextGeometryAuthoringSource())
    for asset, payload in zip(bundle.assets, payloads, strict=True):
        await storage.write_bytes(
            service.storage_key(bundle, asset),
            payload,
            filename=asset.filename,
            content_type=asset.content_type,
        )

    with pytest.raises(ProductFamilyAssetIntegrityError, match="manifest disagrees"):
        await service.verify_bundle(bundle)




@pytest.mark.asyncio
async def test_current_program_source_retains_exact_bytes_and_rejects_corruption(
    tmp_path: Path,
) -> None:
    content = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    bundle = _source_only_bundle(content)
    source = ProductFamilyProgramSource(
        family=bundle.family, family_version_id=bundle.version.family_version_id,
        version_no=1, created_at=NOW, source_asset=bundle.assets[0],
    )
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(storage, SourceTextGeometryAuthoringSource())
    await storage.write_bytes(service.candidate_storage_key(source.family, source.source_asset),
                              content, filename=source.source_asset.filename,
                              content_type=source.source_asset.content_type)
    assert await service.read_program_source(source) == content
    payload = next(tmp_path.rglob("payload"))
    assert payload.read_bytes() == content
    payload.write_bytes(b"corrupt")
    with pytest.raises(ProductFamilyAssetIntegrityError, match="missing or corrupt"):
        await service.read_program_source(source)


@pytest.mark.asyncio
async def test_retired_source_metadata_is_rejected_without_rewriting_asset_bytes(
    tmp_path: Path,
) -> None:
    content = pack_source_bundle({"main.py": b"archived_source = True\n"})
    bundle = _source_only_bundle(content)
    source = ProductFamilyProgramSource(
        family=bundle.family, family_version_id=bundle.version.family_version_id,
        version_no=1, created_at=NOW, source_asset=bundle.assets[0],
    )
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(storage, SourceTextGeometryAuthoringSource())
    await storage.write_bytes(
        service.candidate_storage_key(source.family, source.source_asset),
        content,
        filename=source.source_asset.filename,
        content_type=source.source_asset.content_type,
    )

    with pytest.raises(ValueError, match="extra_forbidden"):
        ProductFamilyProgramSource.model_validate(source.model_dump() | {
            "abi_profile": "product_realization_v5", "formal_contract": True,
        })
    assert next(tmp_path.rglob("payload")).read_bytes() == content


@pytest.mark.asyncio
async def test_product_family_exact_read_uses_one_verified_payload_scan_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    bundle, _ = _formal_standard_bundle(content)
    initial_storage = LocalStorage(tmp_path)
    initial_service = ProductFamilyAssetService(
        initial_storage,
        SourceTextGeometryAuthoringSource(),
    )
    asset = bundle.assets[0]
    await initial_storage.write_bytes(
        initial_service.storage_key(bundle, asset),
        content,
        filename=asset.filename,
        content_type=asset.content_type,
    )
    restarted_storage = LocalStorage(tmp_path)
    restarted_service = ProductFamilyAssetService(
        restarted_storage,
        SourceTextGeometryAuthoringSource(),
    )
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

    assert await restarted_service.read_exact_asset(bundle, asset) == content
    assert observed_bytes == len(content)


@pytest.mark.asyncio
async def test_product_family_asset_service_fails_closed_when_bytes_are_missing(
    tmp_path: Path,
) -> None:
    content = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    service = ProductFamilyAssetService(
        LocalStorage(tmp_path),
        SourceTextGeometryAuthoringSource(),
    )
    bundle, _ = _formal_standard_bundle(content)

    with pytest.raises(ProductFamilyAssetIntegrityError, match="missing or corrupt"):
        await service.verify_bundle(bundle)


@pytest.mark.asyncio
async def test_product_family_asset_service_rejects_storage_metadata_or_digest_drift(
    tmp_path: Path,
) -> None:
    expected = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    changed = pack_source_bundle({"main.py": b"def realize(ctx):\n    return {'changed': True}\n"})
    bundle, _ = _formal_standard_bundle(expected)
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(storage, SourceTextGeometryAuthoringSource())
    await storage.write_bytes(
        service.storage_key(bundle, bundle.assets[0]),
        changed,
        filename=bundle.assets[0].filename,
        content_type=bundle.assets[0].content_type,
    )

    with pytest.raises(ProductFamilyAssetIntegrityError, match="metadata or digest"):
        await service.verify_bundle(bundle)


@pytest.mark.asyncio
async def test_product_family_asset_service_projects_corrupt_storage_as_integrity_error(
    tmp_path: Path,
) -> None:
    content = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})
    bundle, _ = _formal_standard_bundle(content)
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(storage, SourceTextGeometryAuthoringSource())
    await storage.write_bytes(
        service.storage_key(bundle, bundle.assets[0]),
        content,
        filename=bundle.assets[0].filename,
        content_type=bundle.assets[0].content_type,
    )
    payload = next(tmp_path.rglob("payload"))
    payload.write_bytes(b"corrupt")

    with pytest.raises(ProductFamilyAssetIntegrityError, match="missing or corrupt"):
        await service.verify_bundle(bundle)


@pytest.mark.asyncio
async def test_product_family_asset_service_rejects_noncanonical_source_zip(
    tmp_path: Path,
) -> None:
    content = b"not-a-source-bundle"
    bundle, _ = _formal_standard_bundle(content)
    storage = LocalStorage(tmp_path)
    service = ProductFamilyAssetService(storage, SourceTextGeometryAuthoringSource())
    await storage.write_bytes(
        service.storage_key(bundle, bundle.assets[0]),
        content,
        filename=bundle.assets[0].filename,
        content_type=bundle.assets[0].content_type,
    )

    with pytest.raises(ProductFamilyAssetIntegrityError, match="canonical bundle"):
        await service.verify_bundle(bundle)
