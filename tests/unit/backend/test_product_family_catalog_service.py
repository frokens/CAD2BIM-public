from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from CAD2BIM_backend.application.product_family_catalog_service import (
    ProductFamilyCatalogService,
)
from CAD2BIM_backend.application.product_family_asset_service import (
    ProductFamilyAssetIntegrityError,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceRecord,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    geometry_resource_content_digest,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.product_family import (
    PRODUCT_FAMILY_PROGRAM_ENTRY,
    ProductFamilyContract,
    ProductFamilyGeometryBindingRecord,
    ProductFamilyGeometryPurpose,
    ProductFamilyListEntry,
    ProductFamilyRecord,
    ProductFamilyVersionAssetRecord,
    ProductFamilyVersionAssetRole,
    ProductFamilyVersionBundle,
    ProductFamilyVersionRecord,
    product_family_content_digest,
)
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyDiscoveryPage as ProductFamilyPage,
    ProductFamilyMetadataPage, ProductFamilyVersionMetadata,
    ProductFamilyProgramSourcePage,
    ProductFamilyStoredRejection, ProductFamilyValidationIssue,
    ProductFamilyCatalogIntegrityError,
    ProductFamilyCatalogListQuery,
    ProductFamilyCatalogNotFoundError,
    ProductFamilyCatalogReferenceError,
    ProductFamilyCatalogRootInfo,
    ProductFamilyCatalogVersionInfo,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.source_text.source_bundle import pack_source_bundle


NOW = datetime(2026, 9, 4, tzinfo=UTC)
SOURCE_BUNDLE = pack_source_bundle({"main.py": b"def realize(ctx):\n    return None\n"})


def _codec() -> CursorCodec:
    key = base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode()
    return CursorCodec(key)


def _scope(*, task_id: str = "task-1") -> TenantExecutionContext:
    return TenantExecutionContext(
        tenant_id="tenant-1",
        principal_id="principal-1",
        task_id=task_id,
        actor_kind=TenantActorKind.USER,
        command_id="command-current",
    )


def _geometry() -> GeometryResourceVersionBundle:
    resource = GeometryResourceRecord(
        resource_id="geometry-1",
        resource_kind=GeometryResourceKind.MAPPED_MODEL,
        owner_scope=ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.SYSTEM),
        display_name="Mapped body",
        created_at=NOW,
    )
    version_id = "geometry-version-1"
    manifest = {"schema": "geometry_resource_manifest_v1"}
    contents = (
        (
            GeometryResourceAssetRole.CANONICAL_GEOMETRY,
            "occt_brep_v1",
            "model.brep",
            "application/octet-stream",
            b"brep",
        ),
        (
            GeometryResourceAssetRole.MANIFEST,
            "json",
            "manifest.json",
            "application/json",
            canonical_json(manifest).encode(),
        ),
        (
            GeometryResourceAssetRole.SOURCE_BUNDLE,
            "source_bundle_v1",
            "source.zip",
            "application/zip",
            SOURCE_BUNDLE,
        ),
    )
    assets = tuple(
        GeometryResourceVersionAssetRecord(
            resource_version_id=version_id,
            asset_role=role,
            asset_format=asset_format,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=NOW,
        )
        for role, asset_format, filename, content_type, content in contents
    )
    manifest_digest = sha256_digest(manifest)
    version = GeometryResourceVersionRecord(
        resource_version_id=version_id,
        resource_id=resource.resource_id,
        version_no=1,
        origin_kind="test_fixture",
        content_digest=geometry_resource_content_digest(
            resource_kind=resource.resource_kind,
            manifest_digest=manifest_digest,
            assets=assets,
        ),
        manifest_digest=manifest_digest,
        manifest=manifest,
        created_at=NOW,
    )
    return GeometryResourceVersionBundle(resource=resource, version=version, assets=assets)


def _family_bundle(*, version_no: int = 1) -> ProductFamilyVersionBundle:
    family = ProductFamilyRecord(
        family_id="family-1",
        owner_scope=ResourceOwnerScope(
            scope_kind=ResourceOwnerScopeKind.TASK,
            tenant_id="tenant-1",
            task_id="task-1",
        ),
        created_at=NOW,
    )
    version_id = f"family-version-{version_no}"
    geometry = _geometry()
    binding = ProductFamilyGeometryBindingRecord(
        family_version_id=version_id,
        binding_key="body",
        geometry_resource_id=geometry.resource.resource_id,
        geometry_resource_version_id=geometry.version.resource_version_id,
        geometry_content_digest=geometry.version.content_digest,
        expected_resource_kind=geometry.resource.resource_kind,
        geometry_purpose=ProductFamilyGeometryPurpose.PRODUCT_COMPONENT,
    )
    standard_geometry = b"formal-standard-brep"
    standard_manifest = canonical_json(
        {"schema": "product_family_standard_model_manifest_v2"}
    ).encode()
    asset_payloads = (
        (
            ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
            "source_bundle_v1",
            "source.zip",
            "application/zip",
            SOURCE_BUNDLE,
        ),
        (
            ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY,
            "occt_brep_v1",
            "standard_model.brep",
            "application/vnd.opencascade.brep",
            standard_geometry,
        ),
        (
            ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST,
            "json",
            "standard_model.json",
            "application/json",
            standard_manifest,
        ),
    )
    assets = tuple(
        ProductFamilyVersionAssetRecord(
            family_version_id=version_id,
            asset_role=role,
            asset_format=asset_format,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=NOW,
        )
        for role, asset_format, filename, content_type, content in asset_payloads
    )
    contract = ProductFamilyContract.model_validate(
        {
            "product_structure_kind": "single_product",
            "standard_model": {},
            "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
            "geometry_fragments": [
                {
                    "fragment_key": "body_shape",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}],
                }
            ],
            "interfaces": [],
        }
    )
    contract_digest = sha256_digest(contract)
    version = ProductFamilyVersionRecord(
        family_version_id=version_id,
        family_id=family.family_id,
        version_no=version_no,
        display_name="Regulator",
        purpose="Gas pressure regulation",
        family_contract=contract,
        contract_digest=contract_digest,
        content_digest=product_family_content_digest(
            contract_digest=contract_digest,
            program_entry=PRODUCT_FAMILY_PROGRAM_ENTRY,
            geometry_bindings=(binding,),
            assets=assets,
        ),
        created_at=NOW,
    )
    return ProductFamilyVersionBundle(
        family=family,
        version=version,
        geometry_bindings=(binding,),
        assets=assets,
    )


def _service(
    repository: AsyncMock,
    *,
    geometry: GeometryResourceVersionBundle | None = None,
) -> tuple[ProductFamilyCatalogService, AsyncMock, Mock, AsyncMock]:
    geometry_resources = AsyncMock()
    geometry_resources.get_version_by_id.return_value = geometry or _geometry()
    geometry_catalog = Mock()
    geometry_catalog.version_ref.return_value = "cur.geometry-version-ref"
    family_assets = AsyncMock()
    return (
        ProductFamilyCatalogService(
            repository,
            geometry_resources,
            geometry_catalog,
            family_assets,
            _codec(),
        ),
        geometry_resources,
        geometry_catalog,
        family_assets,
    )


def _metadata(bundle) -> ProductFamilyVersionMetadata:
    return ProductFamilyVersionMetadata(
        family=bundle.family, family_version_id=bundle.version.family_version_id,
        version_no=bundle.version.version_no,
        parent_family_version_id=bundle.version.parent_family_version_id,
        display_name=bundle.version.display_name, purpose=bundle.version.purpose,
        product_structure_kind=bundle.version.product_structure_kind,
        contract_digest=bundle.version.contract_digest, content_digest=bundle.version.content_digest,
        created_at=bundle.version.created_at,
    )


@pytest.mark.asyncio
async def test_list_issues_scope_bound_refs_and_cursor_preserves_query() -> None:
    bundle = _family_bundle()
    repository = AsyncMock()
    repository.list_product_families.side_effect = (
        ProductFamilyPage(
            entries=(ProductFamilyListEntry(family=bundle.family, latest_version=bundle),),
            complete=False,
            next_before_created_at=bundle.family.created_at,
            next_before_family_id=bundle.family.family_id,
        ),
        ProductFamilyPage(entries=(), complete=True),
    )
    service, _, _, family_assets = _service(repository)
    query = ProductFamilyCatalogListQuery(
        owner_scopes=(ResourceOwnerScopeKind.TASK,),
        purpose_contains="pressure",
        page_size=7,
    )

    first = await service.list(_scope(), query, None)
    assert first.returned_count == 1
    family_assets.verify_bundle.assert_awaited_once_with(bundle)
    assert first.next_cursor is not None
    assert "family-1" not in first.families[0].family_root_ref
    assert "family-version-1" not in first.families[0].latest_version.family_version_ref

    second = await service.list(_scope(), None, first.next_cursor)
    assert second.families == ()
    repository.get_latest_product_family_version.assert_not_awaited()
    continued = repository.list_product_families.await_args_list[1].kwargs["query"]
    assert continued.owner_scopes == query.owner_scopes
    assert continued.purpose_contains == "pressure"
    assert continued.page_size == 7
    assert continued.before_family_id == bundle.family.family_id

    repository.reset_mock()
    with pytest.raises(ProductFamilyCatalogReferenceError):
        await service.list(_scope(task_id="task-2"), None, first.next_cursor)
    repository.list_product_families.assert_not_awaited()


@pytest.mark.asyncio
async def test_root_info_lists_versions_but_exact_resolution_rejects_root_ref() -> None:
    bundle = _family_bundle()
    repository = AsyncMock()
    repository.list_product_families.return_value = ProductFamilyPage(
        entries=(ProductFamilyListEntry(family=bundle.family, latest_version=bundle),),
        complete=True,
    )
    repository.get_product_family.return_value = bundle.family
    repository.list_product_family_versions.return_value = ProductFamilyMetadataPage(
        versions=(_metadata(bundle),), complete=True
    )
    service, _, _, family_assets = _service(repository)

    listed = await service.list(_scope(), ProductFamilyCatalogListQuery(), None)
    root_ref = listed.families[0].family_root_ref
    result = await service.info(_scope(), root_ref)
    assert isinstance(result, ProductFamilyCatalogRootInfo)
    assert result.target_kind == "family_root"
    assert result.family.latest_version.version_no == 1
    assert result.versions[0].family_version_ref == (
        result.family.latest_version.family_version_ref
    )
    assert family_assets.verify_bundle.await_count == 1  # List only; history reads metadata.

    repository.get_exact_product_family_version.reset_mock()
    with pytest.raises(ProductFamilyCatalogReferenceError, match="exact"):
        await service.resolve_exact_version(_scope(), root_ref)
    repository.get_exact_product_family_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_version_info_reauthorizes_geometry_and_hides_internal_ids() -> None:
    bundle = _family_bundle()
    geometry = _geometry()
    repository = AsyncMock()
    repository.get_exact_product_family_version.return_value = bundle
    repository.get_latest_product_family_version.return_value = _family_bundle(version_no=2)
    service, geometry_resources, geometry_catalog, family_assets = _service(
        repository, geometry=geometry
    )
    version_ref = service.version_ref(_scope(), bundle)

    result = await service.info(_scope(), version_ref)
    assert isinstance(result, ProductFamilyCatalogVersionInfo)
    assert result.target_kind == "family_version"
    assert result.family_version_ref == version_ref
    assert result.usage_instructions == bundle.version.usage_instructions
    assert result.geometry_bindings[0].geometry_resource_version_ref == (
        "cur.geometry-version-ref"
    )
    assert result.assets[0].asset_role == "realization_source_bundle"
    dumped = result.model_dump_json()
    assert "validation_scenarios" not in dumped
    assert "scenario_count" not in dumped
    for internal in (
        bundle.family.family_id,
        bundle.version.family_version_id,
        geometry.resource.resource_id,
        geometry.version.resource_version_id,
    ):
        assert internal not in dumped
    geometry_resources.get_version_by_id.assert_awaited_once_with(
        _scope(), resource_version_id=geometry.version.resource_version_id
    )
    geometry_catalog.version_ref.assert_called_once_with(_scope(), geometry)
    family_assets.verify_bundle.assert_awaited_once_with(bundle)
    assert await service.resolve_exact_version(_scope(), version_ref) == bundle
    repository.get_latest_product_family_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_program_source_list_and_visible_id_ref_reauthorize_source_owner() -> None:
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyProgramSource
    bundle = _family_bundle()
    source = ProductFamilyProgramSource(
        family=bundle.family, family_version_id=bundle.version.family_version_id,
        version_no=bundle.version.version_no, created_at=bundle.version.created_at,
        source_asset=bundle.assets[0],
    )
    repository = AsyncMock()
    repository.list_product_family_program_sources.return_value = ProductFamilyProgramSourcePage(sources=(source,), complete=True)
    repository.get_product_family_program_source.return_value = source
    service, _, _, family_assets = _service(repository)

    programs = await service.list_program_sources(
        _scope(),
        before_created_at=None,
        before_family_version_id=None,
        limit=21,
    )
    version_ref = await service.program_source_ref_for_visible_id(
        _scope(),
        bundle.version.family_version_id,
    )

    assert programs.sources == (source,)
    assert programs.rejections == ()
    assert version_ref == service.version_ref(_scope(), bundle)
    repository.list_product_family_program_sources.assert_awaited_once_with(
        _scope(),
        before_created_at=None,
        before_family_version_id=None,
        limit=21,
    )
    repository.get_product_family_program_source.assert_awaited_once_with(
        _scope(),
        family_version_id=bundle.version.family_version_id,
    )
    family_assets.verify_bundle.assert_not_awaited()
    assert await service.resolve_program_source(_scope(), version_ref) == source
    with pytest.raises(ProductFamilyCatalogReferenceError):
        await service.resolve_program_source(_scope(task_id="other-task"), version_ref)
    # A previously issued locator is only navigation: removal from the visible
    # source-owner projection must still deny the next exact read.
    repository.get_product_family_program_source.return_value = None
    with pytest.raises(ProductFamilyCatalogNotFoundError):
        await service.resolve_program_source(_scope(), version_ref)
    family_assets.verify_bundle.assert_not_awaited()


def test_program_source_rejects_retired_metadata_and_wrong_asset_owner() -> None:
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyProgramSource

    bundle = _family_bundle()
    source = ProductFamilyProgramSource(
        family=bundle.family,
        family_version_id=bundle.version.family_version_id,
        version_no=bundle.version.version_no,
        created_at=bundle.version.created_at,
        source_asset=bundle.assets[0],
    )
    with pytest.raises(ValueError, match="extra_forbidden"):
        ProductFamilyProgramSource.model_validate(source.model_dump() | {
            "contract_schema": "product_family_contract_v5", "formal_contract": True,
        })
    with pytest.raises(ValueError, match="exact version"):
        ProductFamilyProgramSource.model_validate(source.model_dump() | {
            "family_version_id": "another-family-version",
        })


@pytest.mark.asyncio
async def test_exact_version_info_reads_parent_identity_without_loading_parent_contract_or_assets() -> None:
    parent = _family_bundle(version_no=1)
    child_base = _family_bundle(version_no=2)
    child = child_base.model_copy(
        update={
            "version": child_base.version.model_copy(
                update={"parent_family_version_id": parent.version.family_version_id}
            )
        }
    )
    repository = AsyncMock()
    repository.get_exact_product_family_version.return_value = child
    repository.get_product_family_version_identity.return_value = parent.version.family_version_id
    service, _, _, family_assets = _service(repository)

    result = await service.info(_scope(), service.version_ref(_scope(), child))

    assert isinstance(result, ProductFamilyCatalogVersionInfo)
    assert result.parent_family_version_ref == service.version_ref(_scope(), parent)
    family_assets.verify_bundle.assert_awaited_once_with(child)
    repository.get_exact_product_family_version.assert_awaited_once()
    repository.get_product_family_version_identity.assert_awaited_once_with(
        _scope(), family_id=child.family.family_id, family_version_id=parent.version.family_version_id,
    )
    repository.get_product_family_version_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_hidden_or_changed_exact_dependencies_fail_closed() -> None:
    bundle = _family_bundle()
    repository = AsyncMock()
    repository.get_exact_product_family_version.return_value = bundle
    service, geometry_resources, _, family_assets = _service(repository)
    version_ref = service.version_ref(_scope(), bundle)

    geometry_resources.get_version_by_id.return_value = None
    with pytest.raises(ProductFamilyCatalogIntegrityError, match="unreadable or changed"):
        await service.info(_scope(), version_ref)

    geometry_resources.get_version_by_id.return_value = _geometry()
    family_assets.verify_bundle.side_effect = ProductFamilyAssetIntegrityError(
        "product family version asset is missing"
    )
    with pytest.raises(ProductFamilyCatalogIntegrityError, match="asset is missing"):
        await service.info(_scope(), version_ref)

    repository.get_exact_product_family_version.return_value = None
    with pytest.raises(ProductFamilyCatalogNotFoundError):
        await service.resolve_exact_version(_scope(), version_ref)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_id", [None, "different-version"])
async def test_parent_identity_must_be_visible_and_match_the_exact_parent(parent_id) -> None:
    child = _family_bundle(version_no=2)
    child = child.model_copy(update={"version": child.version.model_copy(update={
        "parent_family_version_id": "old-parent",
    })})
    repository = AsyncMock()
    repository.get_exact_product_family_version.return_value = child
    repository.get_product_family_version_identity.return_value = parent_id
    service, _, _, assets = _service(repository)
    with pytest.raises(ProductFamilyCatalogIntegrityError, match="parent version"):
        await service.info(_scope(), service.version_ref(_scope(), child))
    assets.verify_bundle.assert_awaited_once_with(child)
    repository.get_product_family_version_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_root_continuation_reads_metadata_without_contract_or_asset_loading() -> None:
    bundle = _family_bundle(version_no=2)
    repository = AsyncMock()
    repository.get_product_family.return_value = bundle.family
    repository.list_product_family_versions.return_value = ProductFamilyMetadataPage(
        versions=(_metadata(_family_bundle()),), complete=True,
    )
    repository.get_latest_product_family_version_id.return_value = bundle.version.family_version_id
    repository.get_product_family_version_metadata.return_value = _metadata(bundle)
    service, _, _, assets = _service(repository)
    root_ref = service._reference(_scope(), reference_kind="family_root", family_id=bundle.family.family_id)
    result = await service.info(_scope(), root_ref, before_version_no=2, page_size=1)
    assert result.family.latest_version.version_no == 2
    assert result.versions[0].version_no == 1
    assets.verify_bundle.assert_not_awaited()
    repository.get_exact_product_family_version.assert_not_awaited()
    repository.get_latest_product_family_version.assert_not_awaited()


def _rejected(bundle):
    return ProductFamilyStoredRejection(
        family=bundle.family, family_version_id=bundle.version.family_version_id,
        version_no=bundle.version.version_no, created_at=bundle.version.created_at,
        issues=(ProductFamilyValidationIssue(field_path="family_contract.interfaces.0.allowed_kinds", reason="Current constraint kind is invalid"),
                ProductFamilyValidationIssue(field_path="family_contract.interfaces.1.allowed_kinds", reason="Current constraint kind is invalid")),
    )


@pytest.mark.asyncio
async def test_all_rejected_family_page_reports_each_issue_and_advances_to_valid_root() -> None:
    rejected = _family_bundle()
    valid = _family_bundle(version_no=2)
    repository = AsyncMock()
    repository.list_product_families.side_effect = (
        ProductFamilyPage(entries=(), rejections=(_rejected(rejected),), complete=False,
            next_before_created_at=rejected.family.created_at, next_before_family_id=rejected.family.family_id),
        ProductFamilyPage(entries=(ProductFamilyListEntry(family=valid.family, latest_version=valid),), complete=True),
    )
    service, _, _, assets = _service(repository)
    first = await service.list(_scope(), ProductFamilyCatalogListQuery(page_size=1), None)
    assert first.families == () and first.returned_count == 0 and first.next_cursor
    assert len(first.rejections) == 2
    assert first.rejections[0].family_version_ref == service.version_ref(_scope(), rejected)
    assert first.rejections[0].code == "PRODUCT_FAMILY_CONTRACT_INVALID"
    assert set(first.rejections[0].model_dump()) == {"family_version_ref", "version_no", "code", "field_path", "reason"}
    assert rejected.version.family_version_id not in first.rejections[0].model_dump_json()
    assets.verify_bundle.assert_not_awaited()
    second = await service.list(_scope(), None, first.next_cursor)
    assert second.returned_count == 1 and second.next_cursor is None
    query = repository.list_product_families.await_args.kwargs["query"]
    assert query.before_family_id == rejected.family.family_id
    assets.verify_bundle.assert_awaited_once_with(valid)


@pytest.mark.asyncio
async def test_source_rejection_page_projects_scope_reference_without_executable_source() -> None:
    bundle = _family_bundle()
    repository = AsyncMock()
    repository.list_product_family_program_sources.return_value = ProductFamilyProgramSourcePage(
        sources=(), rejections=(_rejected(bundle),), complete=False,
        next_before_created_at=bundle.version.created_at,
        next_before_family_version_id=bundle.version.family_version_id,
    )
    service, _, _, assets = _service(repository)
    page = await service.list_program_sources(_scope(), before_created_at=None, before_family_version_id=None, limit=1)
    assert page.sources == () and len(page.rejections) == 2 and not page.complete
    assert page.rejections[0].family_version_ref == service.version_ref(_scope(), bundle)
    assert page.next_before_family_version_id == bundle.version.family_version_id
    assets.verify_bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_family_list_technical_database_and_asset_failures_are_not_rejections() -> None:
    repository = AsyncMock()
    service, _, _, assets = _service(repository)
    failure = RuntimeError("database connection interrupted")
    repository.list_product_families.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        await service.list(_scope(), ProductFamilyCatalogListQuery(), None)
    assert caught.value is failure
    bundle = _family_bundle()
    repository.list_product_families.side_effect = None
    repository.list_product_families.return_value = ProductFamilyPage(
        entries=(ProductFamilyListEntry(family=bundle.family, latest_version=bundle),), complete=True,
    )
    assets.verify_bundle.side_effect = ProductFamilyAssetIntegrityError("asset bytes are corrupt")
    with pytest.raises(ProductFamilyCatalogIntegrityError, match="corrupt"):
        await service.list(_scope(), ProductFamilyCatalogListQuery(), None)


@pytest.mark.asyncio
async def test_invalid_inputs_fail_before_repository_access() -> None:
    repository = AsyncMock()
    service, _, _, _ = _service(repository)

    with pytest.raises(ValueError, match="exactly one"):
        await service.list(_scope(), None, None)
    with pytest.raises(ValueError, match="exactly one"):
        await service.list(_scope(), ProductFamilyCatalogListQuery(), "cur.invalid")
    with pytest.raises(ProductFamilyCatalogReferenceError):
        await service.info(_scope(), "cur.invalid")
    repository.list_product_families.assert_not_awaited()
    repository.get_product_family.assert_not_awaited()
