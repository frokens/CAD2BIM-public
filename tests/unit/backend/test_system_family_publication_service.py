"""Unit witnesses for dependency ordering and reuse inside one management Run."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, sentinel

import pytest

from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService
from CAD2BIM_backend.application.system_family_publication_service import SystemFamilyPublicationService
from CAD2BIM_backend.domain.data_runtime import CriticalOperationStatus
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceInput, geometry_resource_inputs
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.product_family import ProductFamilyVersionBundle, product_family_content_digest
from CAD2BIM_backend.domain.product_realization import product_geometry_binding_digest
from CAD2BIM_backend.domain.system_family_publication import SystemFamilyCopyPlan, SystemFamilyPublicationCommand
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from tests.unit.backend.product_family_fixtures import current_realization_source
from tests.unit.backend.test_geometry_resource_service import _copy_source
from tests.unit.backend.test_product_family_asset_service import _formal_standard_bundle


def _management_scope():
    return InvocationScope(identity=TenantExecutionContext(tenant_id="tenant-authoring",
        principal_id="principal-authoring", task_id="task-authoring",
        actor_kind=TenantActorKind.SYSTEM_CURATOR, command_id="command-authoring"),
        owner_id="tenant-authoring", run_id="management-run", execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None)


def _geometry_chain():
    fixed, fixed_bytes = _copy_source(identity="fixed-g0")
    frozen = GeometryResourceInput(input_key="body", resource_id=fixed.resource.resource_id,
        resource_version_id=fixed.version.resource_version_id, content_digest=fixed.version.content_digest,
        canonical_geometry_sha256=fixed.assets[0].sha256)
    consumer, consumer_bytes = _copy_source(identity="consumer-g1", inputs=(frozen,))
    return {(fixed.resource.resource_id, fixed.version.resource_version_id): (fixed, fixed_bytes),
        (consumer.resource.resource_id, consumer.version.resource_version_id): (consumer, consumer_bytes)}


def _family(geometry, identity, *, contract_updates=None):
    version_id = f"{identity}-version"
    binding_digest = product_geometry_binding_digest([("body", geometry.resource.resource_kind, geometry.version.content_digest)])
    base, contents = _formal_standard_bundle(current_realization_source(binding_key="body"),
        manifest_updates={"family_version_id": version_id, "binding_digest": binding_digest},
        contract_updates=contract_updates)
    bindings = (base.geometry_bindings[0].model_copy(update={"family_version_id": version_id,
        "geometry_resource_id": geometry.resource.resource_id,
        "geometry_resource_version_id": geometry.version.resource_version_id,
        "geometry_content_digest": geometry.version.content_digest,
        "expected_resource_kind": geometry.resource.resource_kind}),)
    assets = tuple(asset.model_copy(update={"family_version_id": version_id}) for asset in base.assets)
    version = base.version.model_copy(update={"family_id": identity, "family_version_id": version_id,
        "content_digest": product_family_content_digest(contract_digest=base.version.contract_digest,
            program_entry=base.version.program_entry, geometry_bindings=bindings, assets=assets, usage_instructions=base.version.usage_instructions)})
    bundle = ProductFamilyVersionBundle(family=base.family.model_copy(update={"family_id": identity}),
        version=version, geometry_bindings=bindings, assets=assets)
    ProductFamilyAssetService._parse_standard_model_manifest(bundle, contents[-1])
    return bundle, contents


def _boundary(geometries, families):
    async def read_geometry(*, resource_id, resource_version_id):
        return geometries[(resource_id, resource_version_id)]

    async def read_family(*, family_id, family_version_id):
        return families[(family_id, family_version_id)]

    return SimpleNamespace(source_kind="selected_library", source_reference="unit:selected-closure",
        read_geometry=AsyncMock(side_effect=read_geometry), read_family=AsyncMock(side_effect=read_family))


@pytest.mark.asyncio
async def test_geometry_closure_is_topological_complete_and_uses_real_copy_constructor() -> None:
    geometries = _geometry_chain()
    consumer = geometries[("consumer-g1", "consumer-g1-version")][0]
    family, contents = _family(consumer, "family-a")
    source = _boundary(geometries, {(family.family.family_id, family.version.family_version_id): (family, contents)})
    repository, runtime, storage = AsyncMock(), AsyncMock(), AsyncMock()
    repository.get_exact_geometry_resource_version.return_value = None
    resources = GeometryResourceService(repository, storage, geometry_runtime=runtime)
    service = SystemFamilyPublicationService(AsyncMock(), AsyncMock(), resources, source,
        bind_identity=lambda _identity: nullcontext())
    planned = await service._prepare_geometry_closure(_management_scope(), family)
    plan = SystemFamilyCopyPlan(source_family_id=family.family.family_id,
        source_family_version_id=family.version.family_version_id, source_content_digest=family.version.content_digest,
        source_owner_scope=family.family.owner_scope, source_kind=source.source_kind,
        source_reference=source.source_reference, geometry_copies=tuple(item[0] for item in planned), family_assets=family.assets)
    assert [item.source_resource_version_id for item in plan.geometry_copies] == ["fixed-g0-version", "consumer-g1-version"]
    fixed_target, consumer_target = [item.target_version for item in plan.geometry_copies]
    mapped_input, = geometry_resource_inputs(consumer_target.version.manifest)
    assert (mapped_input.resource_id, mapped_input.resource_version_id, mapped_input.content_digest) == (
        fixed_target.resource.resource_id, fixed_target.version.resource_version_id, fixed_target.version.content_digest)
    for draft, payloads in planned:
        original, _ = geometries[(draft.source_resource_id, draft.source_resource_version_id)]
        assert draft.source_content_digest == original.version.content_digest
        assert draft.source_owner_scope == original.resource.owner_scope
        assert draft.target_version.version.version_no == 1
        assert draft.target_version.resource.owner_scope == plan.target_owner
        assert len(payloads) == len(draft.target_version.assets)
        assert not draft.reuse_existing
    assert source.read_geometry.await_count == 2
    repository.publish_geometry_resource_version.assert_not_awaited()
    assert runtime.mock_calls == storage.mock_calls == []


@pytest.mark.asyncio
async def test_two_families_share_geometry_targets_in_same_management_run(tmp_path) -> None:
    geometries = _geometry_chain()
    consumer = geometries[("consumer-g1", "consumer-g1-version")][0]
    families = [_family(consumer, name) for name in ("family-a", "family-b")]
    source = _boundary(geometries, {(bundle.family.family_id, bundle.version.family_version_id): (bundle, contents)
        for bundle, contents in families})
    repository, runtime, family_authoring = AsyncMock(), AsyncMock(), AsyncMock()
    committed = {}

    async def get_exact(_context, *, resource_id, resource_version_id):
        return committed.get((resource_id, resource_version_id))

    async def publish_geometry(_context, bundle):
        key = (bundle.resource.resource_id, bundle.version.resource_version_id)
        return committed.setdefault(key, bundle)

    repository.get_exact_geometry_resource_version.side_effect = get_exact
    repository.publish_geometry_resource_version.side_effect = publish_geometry
    resources = GeometryResourceService(repository, LocalStorage(tmp_path), geometry_runtime=runtime)
    resources.store_candidate_assets = AsyncMock(wraps=resources.store_candidate_assets)
    prepared = []

    async def prepare(_scope, _command, *, source, source_assets, source_kind, source_reference, geometry_copies):
        # The Family owner is a boundary substitute; real Family commit and saved-K
        # recovery remain covered by the concentrated PostgreSQL owner witness.
        plan = SystemFamilyCopyPlan(source_family_id=source.family.family_id,
            source_family_version_id=source.version.family_version_id, source_content_digest=source.version.content_digest,
            source_owner_scope=source.family.owner_scope, source_kind=source_kind,
            source_reference=source_reference, geometry_copies=geometry_copies, family_assets=source.assets)
        prepared.append(plan)
        return SimpleNamespace(status=CriticalOperationStatus.PREPARED, operation_id=f"operation-{len(prepared)}",
            detail=SimpleNamespace(system_copy=plan))

    family_authoring.find_system_copy.return_value = None
    family_authoring.prepare_system_copy.side_effect = prepare
    family_authoring.commit_system_copy.return_value = sentinel.family_commit
    service = SystemFamilyPublicationService(repository, family_authoring, resources, source,
        bind_identity=lambda _identity: nullcontext())
    scope = _management_scope()
    for family, _ in families:
        command = SystemFamilyPublicationCommand(request_id=family.family.family_id,
            source_family_id=family.family.family_id, source_family_version_id=family.version.family_version_id)
        assert await service.publish(scope, command) is sentinel.family_commit
    assert len(prepared) == 2
    assert [(item.target_version.resource.resource_id, item.target_version.version.resource_version_id)
        for item in prepared[0].geometry_copies] == [(item.target_version.resource.resource_id,
            item.target_version.version.resource_version_id) for item in prepared[1].geometry_copies]
    assert all(not item.reuse_existing for item in prepared[0].geometry_copies)
    assert all(item.reuse_existing for item in prepared[1].geometry_copies)
    assert len(committed) == 2
    assert resources.store_candidate_assets.await_count == 2
    assert family_authoring.prepare_system_copy.await_count == 2
    assert runtime.mock_calls == []


@pytest.mark.asyncio
async def test_source_connection_failure_preserves_technical_error_without_publication() -> None:
    repository, family_authoring, resources, source = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    family_authoring.find_system_copy.return_value = None
    source.read_family.side_effect = ConnectionError("source connection unavailable")
    service = SystemFamilyPublicationService(repository, family_authoring, resources, source,
        bind_identity=lambda _identity: nullcontext())
    command = SystemFamilyPublicationCommand(request_id="connection-failure", source_family_id="family-a",
        source_family_version_id="family-a-version")
    with pytest.raises(ConnectionError, match="source connection unavailable"):
        await service.publish(_management_scope(), command)
    family_authoring.prepare_system_copy.assert_not_awaited()
    family_authoring.commit_system_copy.assert_not_awaited()
    resources.store_candidate_assets.assert_not_awaited()
    resources.publish_candidate.assert_not_awaited()
    assert repository.mock_calls == []
