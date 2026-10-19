"""Draft saving and definition-based publication; real execution has owner tests."""
from contextlib import asynccontextmanager
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftService, AuthoringDraftError
from CAD2BIM_backend.application.authoring_publication_service import AuthoringPublicationService
from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringService, GeometryAuthoringApplicationError
from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringService
from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
from CAD2BIM_backend.domain.authoring_draft import (
    AuthoringDraftRecord, AuthoringDraftCommitResult, AuthoringPublicationCommitResult,
    AuthoringPublicationCommitRejected,
)
from CAD2BIM_backend.domain.data_runtime import (
    AuthoringExecutionState, AuthoringExecutionClaim, PublishedCriticalOperation, DataCatalogEntry, CatalogResourceKind,
)
from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import SourceTextGeometryAuthoringSource
from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter
from CAD2BIM_backend.ports.product_families import ProductFamilyRepository
from tests.unit.backend.test_geometry_inspection_service import _setup
from tests.unit.backend.test_product_realization_preview_service import _geometry, _request


def published(value):
    return PublishedCriticalOperation(operation=value, catalog_entry=DataCatalogEntry(
        catalog_entry_id="catalog-a", owner_id=value.owner_id, tenant_id=value.tenant_id,
        task_id=value.task_id, run_id=value.run_id, resource_kind=CatalogResourceKind.OPERATION,
        alias="op_001", ordinal=1, catalog_revision=1, operation_id=value.operation_id, created_at=value.started_at))


class Repository:
    def __init__(self):
        self.roots, self.drafts, self.operations, self.versions = {}, {}, {}, {}
        self.store_calls = 0
        self.prepared = {}
        self.family_versions = {}
        family_reader = create_autospec(ProductFamilyRepository, instance=True, spec_set=True)
        self.get_product_family_version_by_id = family_reader.get_product_family_version_by_id
        self.get_product_family_version_by_id.side_effect = (
            lambda context, *, family_version_id: self.family_versions.get(family_version_id)
        )

    async def get_authoring_execution_state(self, scope, operation_id, **kwargs):
        record = self.operations.get(operation_id)
        operation = record.operation if record else self.prepared.get(operation_id)
        return AuthoringExecutionState(operation=operation, execution_generation=0) if operation else None

    async def begin_authoring_publication(self, scope, *, operation, expected_drafts):
        self.prepared[operation.operation_id] = operation
        return operation

    async def claim_authoring_execution(self, scope, operation_id, **kwargs):
        return AuthoringExecutionClaim(operation_id=operation_id, owner="owner", generation=1, token="t"*32)

    async def finalize_authoring_publication_operation(self, scope, *, operation, execution_claim):
        self.operations[operation.operation_id] = published(operation)
        return self.operations[operation.operation_id]

    async def get_operation(self, scope, operation_id):
        return self.operations.get(operation_id)

    async def get_authoring_save_for_snapshot(self, scope, **kwargs):
        return None

    async def get_resource(self, scope, *, resource_id):
        return self.roots.get(resource_id)

    async def load_build_inputs(self, scope, inputs):
        assert not inputs
        return ()

    async def get_geometry_draft(self, scope, *, resource_id):
        return self.drafts.get(resource_id)

    async def get_authoring_draft_snapshot(self, scope, *, draft):
        if self.drafts.get(draft.root_id) != draft or draft.owner_scope.tenant_id != scope.identity.tenant_id:
            raise AuthoringDraftError("AUTHORING_DRAFT_NOT_AVAILABLE", "The authorized root snapshot is unavailable")
        return next(record for record in self.artifacts.records.values()
            if record.artifact_id == draft.snapshot.artifact_id and record.version_id == draft.snapshot.version_id)

    async def get_product_family_draft(self, scope, *, family_id):
        return self.drafts.get(family_id)

    async def get_product_family(self, scope, *, family_id):
        return self.roots.get(family_id)

    get_product_family_authoring_root = get_product_family

    async def find_current_authoring_publication(self, scope, **kwargs):
        return None

    async def find_published_geometry_draft(self, scope, **kwargs):
        return None

    async def get_latest_product_family_version(self, scope, *, family_id):
        return None

    async def get_latest_product_family_version_id(self, scope, *, family_id):
        return None

    async def commit_geometry_draft(self, scope, *, resource, snapshot, expected_generation, operation):
        return self.commit("geometry", resource.resource_id, resource, snapshot, expected_generation, operation)

    async def commit_product_family_draft(self, scope, *, family, snapshot, expected_generation, operation):
        return self.commit("family", family.family_id, family, snapshot, expected_generation, operation)

    def commit(self, kind, key, root, snapshot, expected, operation):
        prior = self.drafts.get(key)
        assert (prior.generation if prior else None) == expected
        self.roots[key] = root
        draft = AuthoringDraftRecord(root_kind=kind, root_id=key, owner_scope=root.owner_scope,
            generation=(expected or 0)+1, snapshot=snapshot)
        self.drafts[key], self.operations[operation.operation_id] = draft, published(operation)
        return AuthoringDraftCommitResult(draft=draft, operation=self.operations[operation.operation_id])

    async def store_candidate_assets(self, scope, bundle, contents):
        assert len(contents) == len(bundle.assets)
        self.store_calls += 1
        return bundle

    async def commit_authoring_publication(self, scope, *, operation, geometry_publications=(), family_publication=None, expected_drafts, execution_claim):
        assert all(self.drafts[item.root_id] == item for item in expected_drafts)
        result = (tuple(item.to_bundle(version_no=1) for item in geometry_publications)
            or tuple(self.versions[item.resource_version_id] for item in operation.detail.geometry_versions))
        self.operations[operation.operation_id] = published(operation)
        self.versions.update({item.version.resource_version_id:item for item in result})
        if family_publication:
            self.family_versions[family_publication.version.family_version_id] = family_publication.to_bundle(version_no=1)
        return AuthoringPublicationCommitResult(operation=published(operation), geometry_versions=result,
            family_version=family_publication.to_bundle(version_no=1) if family_publication else None)

    async def get_exact_version(self, scope, *, resource_id, resource_version_id):
        return self.versions.get(resource_version_id)


@asynccontextmanager
async def _runtime_scope(*args, **kwargs):
    yield


def setup_services():
    artifacts, _, codec, scope, inspections, _ = _setup()
    repository = Repository()
    repository.artifacts = artifacts
    drafts = AuthoringDraftService(artifacts, repository, codec)
    sources = SourceTextGeometryAuthoringSource()
    geometry = GeometryAuthoringService(repository, repository, sources,
        source_patch=TextSourcePatchAdapter(), drafts=drafts, catalog=None)
    family = ProductFamilyAuthoringService(repository, artifacts, None, None,
        ProductRealizationInputService(None, repository, runtime=None, family_repository=repository),
        sources, source_patch=TextSourcePatchAdapter(), drafts=drafts, geometry_authoring=geometry)
    runtime = SimpleNamespace(invocation_scope=_runtime_scope, execution_scope=_runtime_scope,
        build_fixed=AsyncMock(side_effect=AssertionError("unexpected fixed build")),
        precheck_batch=Mock(side_effect=AssertionError("unexpected standard precheck")),
        instantiate_batch=Mock(side_effect=AssertionError("unexpected standard build")))
    publication = AuthoringPublicationService(repository, drafts, geometry, family, inspections,
        geometry_runtime=runtime, realization_runtime=runtime)
    return scope, repository, geometry, family, publication


def component_route(scope):
    from CAD2BIM_backend.domain.control_plane import ExecutionMode, ExecutionProtocolStatus
    from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
    return SimpleNamespace(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
        principal_id=scope.identity.principal_id, task_id=scope.identity.task_id,
        execution_mode=ExecutionMode.GRAPH, module="component", stage_key="component",
        execution_protocol_status=ExecutionProtocolStatus.PINNED,
        execution_protocol=ExecutionProtocolBinding(bundle_id="component_chat", module="component", stage_key="component"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,module,pinned,expected", [
    ("graph", "component", True, "tenant"),
    ("graph", "component", False, None),
    ("graph", "workflow", True, "task"),
    ("direct_mcp", "component", True, "task"),
])
async def test_authoring_owner_uses_authenticated_run_route_not_task_metadata(mode, module, pinned, expected):
    from CAD2BIM_backend.domain.control_plane import ExecutionMode, ExecutionProtocolStatus
    from CAD2BIM_backend.ifc_application_resources import resolve_authoring_resource_owner
    scope, _, _, _, _ = setup_services()
    scope = scope.model_copy(update={"execution_mode": ExecutionMode(mode)})
    run = component_route(scope)
    run.module = module
    if not pinned:
        run.execution_protocol_status = ExecutionProtocolStatus.QUARANTINED
        run.execution_protocol = None
    repository = SimpleNamespace(get_run_with_execution_identity=AsyncMock(return_value=run),
        get_task=AsyncMock(return_value=SimpleNamespace(metadata={"task_type": "component"})))
    if expected is None:
        with pytest.raises(AuthoringDraftError, match="server-selected"):
            await resolve_authoring_resource_owner(repository, scope)
    else:
        owner = await resolve_authoring_resource_owner(repository, scope)
        assert owner.scope_kind.value == expected and owner.tenant_id == scope.identity.tenant_id
        assert owner.task_id == (scope.identity.task_id if expected == "task" else None)
    repository.get_task.assert_not_awaited()
    if mode == "direct_mcp":
        repository.get_run_with_execution_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_tenant_family_snapshot_reissues_dependencies_and_edits_from_a_new_task():
    from CAD2BIM_backend.domain.control_plane import ExecutionMode
    from CAD2BIM_backend.ifc_application_resources import resolve_authoring_resource_owner
    scope, repository, geometry, family, _ = setup_services()
    scope = scope.model_copy(update={"execution_mode": ExecutionMode.GRAPH})
    next_scope = scope.model_copy(update={"owner_id": "owner-b", "run_id": "run-b",
        "identity": scope.identity.model_copy(update={"task_id": "task-b", "principal_id": "principal-b"})})
    runs = {value.run_id: component_route(value) for value in (scope, next_scope)}
    repository.get_run_with_execution_identity = AsyncMock(side_effect=lambda owner, run_id: runs[run_id])
    geometry._drafts._owner_resolver = lambda current: resolve_authoring_resource_owner(repository, current)
    saved_geometry = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="g")
    contract = _request().family_contract.model_dump(mode="json", by_alias=True)
    contract["standard_model"] = None
    definition = {"family_contract": contract, "geometry_bindings": [{"binding_key": "body_geometry",
        "source": {"source_kind": "draft", "draft_ref": geometry.draft_reference(scope, saved_geometry.draft)},
        "expected_resource_kind": "modeling_function", "geometry_purpose": "shape_fragment"}]}
    declaration = {"mode": "create", "display_name": "Shared fitting", "purpose": "Shared assembly",
        "usage_instructions": {"action": "set", "value": "Use dimensions in metres."}, "source": {
            "source_kind": "inline", "source_files": [{"path": "main.py", "source":
                "def realize(ctx):\n    return None\ndef standard_model(ctx):\n    return None\n"}],
            "definition": {"action": "set", "value": definition}}}
    saved = await family.save_draft_from_aliases(scope, declaration=declaration, request_identity="f")
    original_ref = family.draft_reference(scope, saved.draft)
    reference = family.draft_reference(next_scope, saved.draft)
    with pytest.raises(AuthoringDraftError, match="not available"):
        await family.read_draft(next_scope, draft_ref=original_ref)
    _, original_content = await family.read_draft(scope, draft_ref=original_ref)
    _, next_content = await family.read_draft(next_scope, draft_ref=reference)
    assert next_content == original_content
    binding = next_content.definition.geometry_bindings[0]
    assert binding.source.resource_id == saved_geometry.draft.root_id
    assert "draft_ref" not in binding.source.model_dump()
    info = await family.draft_info(next_scope, draft_ref=reference)
    projected = info.geometry_bindings[0].source.draft_ref
    assert geometry._drafts.resolve_reference(next_scope, projected, root_kind="geometry") == saved_geometry.draft.root_id
    with pytest.raises(AuthoringDraftError):
        geometry._drafts.resolve_reference(scope, projected, root_kind="geometry")
    updated = await family.save_draft_from_aliases(next_scope, request_identity="edit", declaration={
        "mode": "update", "draft_ref": reference, "expected_edit_token": 1,
        "definition": {"action": "edit", "geometry_bindings": {"set": [info.geometry_bindings[0].model_dump(mode="json")] }},
        "usage_instructions": {"action": "set", "value": "Continue this shared fitting from the new conversation."}})
    assert updated.draft.generation == 2 and updated.draft.owner_scope == saved.draft.owner_scope
    assert updated.operation.operation.task_id == "task-b" and updated.operation.operation.owner_id == "owner-b"
    replay = await family.save_draft_from_aliases(scope, declaration=declaration, request_identity="f")
    assert replay == saved


@pytest.mark.asyncio
async def test_fixed_snapshot_literal_rejection_settles_only_an_unadmitted_original_action():
    scope, repository, geometry, _, publication = setup_services()
    saved = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="saved")
    rejected = {"mode": "update", "draft_ref": geometry.draft_reference(scope, saved.draft),
        "expected_edit_token": 1, "source_changes": [{"change_kind": "literal_edit", "path": "main.py", "edits": [{
            "old_text": "missing literal from the immutable snapshot", "new_text": "replacement"}]}]}
    with pytest.raises(GeometryAuthoringApplicationError) as failed:
        await geometry.save_draft_from_aliases(scope, declaration=rejected, request_identity="literal-rejected")
    assert failed.value.diagnostic["failure"] == "match_not_found" and repository.drafts[saved.draft.root_id] == saved.draft
    assert await geometry.request_rejection_status(scope, request_identity="literal-rejected", error=failed.value) == "not_started"
    assert await geometry.request_rejection_status(scope, request_identity="saved", error=failed.value) == "unknown"
    assert await publication.request_rejection_status(scope, request_identity="uncertain",
        error=AuthoringDraftError("PUBLICATION_RECONCILIATION_REQUIRED", "Original action is still uncertain")) == "unknown"
    repository.get_authoring_execution_state = AsyncMock(return_value=SimpleNamespace(operation="PREPARED"))
    assert await geometry.request_rejection_status(scope, request_identity="literal-rejected", error=failed.value) == "unknown"
    repository.get_authoring_execution_state = AsyncMock(return_value=None)
    repository.get_operation = AsyncMock(side_effect=ConnectionError("database query failed"))
    assert await geometry.request_rejection_status(scope, request_identity="literal-rejected", error=failed.value) == "unknown"


@pytest.mark.asyncio
async def test_stale_generation_remains_rejected_and_does_not_settle_an_unknown_error():
    scope, repository, geometry, _, _ = setup_services()
    saved = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="saved")
    reference = geometry.draft_reference(scope, saved.draft)
    await geometry.save_draft_from_aliases(scope, request_identity="advance", declaration={
        "mode": "update", "draft_ref": reference, "expected_edit_token": 1, "display_name": "Advanced draft"})
    declaration = {"mode": "update", "draft_ref": reference, "expected_edit_token": 1, "display_name": "Stale request"}
    for _ in range(2):
        with pytest.raises(GeometryAuthoringApplicationError) as rejected:
            await geometry.save_draft_from_aliases(scope, request_identity="stale", declaration=declaration)
        assert rejected.value.code == "GEOMETRY_DRAFT_CONFLICT"
    assert await geometry.request_rejection_status(scope, request_identity="stale", error=rejected.value) == "not_started"
    assert await geometry.request_rejection_status(scope, request_identity="stale",
        error=RuntimeError("Unknown backend preflight failure")) == "unknown"


@pytest.mark.asyncio
async def test_rejection_status_requires_the_validation_origin_not_a_diagnostic_field():
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError
    scope, _, geometry, family, _ = setup_services()
    with pytest.raises(GeometryAuthoringApplicationError) as rejected:
        await geometry.save_draft_from_aliases(scope, request_identity="invalid-dto",
            declaration={**geometry_create(), "display_name": ""})
    assert await geometry.request_rejection_status(scope, request_identity="invalid-dto", error=rejected.value) == "not_started"
    # A dependency/read failure can carry the same public code and field.
    # Only the input validator which proved the fixed rejection may settle it.
    assert await geometry.request_rejection_status(scope, request_identity="inherited-read",
        error=GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID", "Unable to read inherited metadata",
            diagnostic={"field": "interfaces"})) == "unknown"
    assert await family.request_rejection_status(scope, request_identity="inherited-definition",
        error=ProductFamilyAuthoringApplicationError("PRODUCT_FAMILY_INPUT_INVALID", "Unavailable exact binding",
            diagnostic={"field": "definition"})) == "unknown"


def geometry_create():
    return {"mode":"create", "display_name":"Draft fitting", "usage_instructions":{"action":"set","value":"Use dimensions in metres."},
        "source":{"source_kind":"modeling_function", "source_files":[{"path":"main.py", "source":"def build(ctx):\n    return None\n"},
            {"path":"interfaces.py", "source":"def evaluate(ctx):\n    return None\n"}],
        "parameter_schema":{"action":"set","value":{"type":"object", "properties":{"diameter":{"type":"number", "minimum":0.01, "maximum":1, "default":0.1}},
            "additionalProperties":False, "required":["diameter"]}}}}


@pytest.mark.parametrize("usage", [" ", "bad\x00text", "\ud800", "x" * 8193])
def test_saved_content_owners_validate_whole_usage_after_any_merge(usage):
    from pydantic import ValidationError
    from CAD2BIM_backend.domain.authoring_draft_content import GeometryDraftContent, FamilyDraftContent

    common = {"display_name":"Draft", "usage_instructions":usage,
        "source_files":[{"path":"main.py", "source":"def build(ctx): return None\n"}]}
    with pytest.raises(ValidationError, match="usage_instructions"):
        GeometryDraftContent.model_validate({**common, "input_metadata":{"resource_kind":"mapped_model"}})
    contract = _request().family_contract.model_dump(mode="json", by_alias=True)
    key = contract["geometry_fragments"][0]["binding_options"][0]["binding_key"]
    with pytest.raises(ValidationError, match="usage_instructions"):
        FamilyDraftContent.model_validate({**common, "purpose":"Use the fitting", "definition":{
            "family_contract":contract, "geometry_bindings":[{"binding_key":key,
                "source":{"source_kind":"published", "resource_id":"geometry-1", "resource_version_id":"geometry-version-1"},
                "expected_resource_kind":"modeling_function", "geometry_purpose":"shape_fragment"}]}})


def test_text_input_wrapper_preserves_existing_full_usage_capacity():
    from CAD2BIM_backend.domain.text_source import TextSet
    from CAD2BIM_backend.domain.version_usage import validate_version_usage_instructions

    exact_limit = "😀" * 8192
    command = TextSet(action="set", value=exact_limit)
    assert validate_version_usage_instructions(command.value, required=True) == exact_limit


@pytest.mark.asyncio
async def test_write_saves_without_execution_and_edits_inherit_exact_content():
    scope, repo, geometry, _, _ = setup_services()
    first = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="create")
    reference = geometry.draft_reference(scope, first.draft)
    with pytest.raises(GeometryAuthoringApplicationError, match="unchanged"):
        await geometry.save_draft_from_aliases(scope, request_identity="no-change", declaration={
            "mode":"update", "draft_ref":reference, "expected_edit_token":1,
            "usage_instructions":geometry_create()["usage_instructions"]})
    assert repo.drafts[first.draft.root_id] == first.draft
    second = await geometry.save_draft_from_aliases(scope, request_identity="edit", declaration={
        "mode":"update", "draft_ref":reference, "expected_edit_token":1, "usage_instructions":{"action":"set","value":"Updated explanation only."}})
    _, content = await geometry.read_draft(scope, draft_ref=reference)
    assert second.draft.generation == 2 and not repo.versions and not repo.store_calls
    assert content.source_files[0].source == geometry_create()["source"]["source_files"][0]["source"]
    assert (await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="create")) == first
    with pytest.raises(GeometryAuthoringApplicationError, match="changed"):
        await geometry.save_draft_from_aliases(scope, request_identity="stale", declaration={
            "mode":"update", "draft_ref":reference, "expected_edit_token":1, "display_name":"Wrong stale edit"})


@pytest.mark.asyncio
async def test_duplicate_source_member_is_rejected_before_any_draft_commit():
    scope, repo, geometry, _, _ = setup_services()
    declaration = geometry_create()
    declaration["source"]["source_files"] *= 2
    with pytest.raises(GeometryAuthoringApplicationError, match="invalid") as error:
        await geometry.save_draft_from_aliases(scope,declaration=declaration,request_identity="duplicate")
    assert "unique" in str(error.value.diagnostic)
    assert not repo.drafts and not repo.operations and not repo.versions


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_index", [0, 1])
async def test_draft_preview_missing_public_parameter_is_input_error(bad_index):
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError
    from CAD2BIM_backend.domain.product_realization import ProductRealizationInvocation
    from CAD2BIM_backend.internal_authoring_api import _domain_error
    scope, _, geometry, family, _ = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="g")
    contract = _request().family_contract.model_dump(mode="json", by_alias=True)
    contract.update(public_parameters=[{"parameter_kind":"number", "parameter_key":"diameter",
        "unit":"metre", "minimum":0.01, "maximum":1}], standard_model={"public_parameters":{"diameter":0.2}})
    definition = {"family_contract":contract, "geometry_bindings":[{"binding_key":"body_geometry",
        "source":{"source_kind":"draft", "draft_ref":geometry.draft_reference(scope, created.draft)},
        "expected_resource_kind":"modeling_function", "geometry_purpose":"shape_fragment"}]}
    saved = await family.save_draft_from_aliases(scope, request_identity="f", declaration={"mode":"create",
        "display_name":"Fitting", "purpose":"Test fitting",
        "usage_instructions":{"action":"set", "value":"Supply diameter for Preview."},
        "source":{"source_kind":"inline", "source_files":[{"path":"main.py", "source":
            "def realize(context):\n    return None\ndef standard_model(context):\n    return None\n"}],
            "definition":{"action":"set", "value":definition}}})
    ref = family.draft_reference(scope, saved.draft)
    good = ProductRealizationInvocation(realization_kind="standard_model", public_parameters={"diameter":0.2},
        product_origin=(0, 0, 0), length_unit={"name":"METRE"})
    bad = good.model_copy(update={"public_parameters":{}})
    with pytest.raises(ProductFamilyAuthoringApplicationError) as failed:
        await family.prepare_draft_preview(scope, draft_ref=ref, invocations=(good,) * bad_index + (bad,))
    assert failed.value.code == "PRODUCT_FAMILY_INPUT_INVALID"
    assert failed.value.phase == "preflight" and "diameter" in str(failed.value)
    assert failed.value.diagnostic["item_index"] == bad_index
    assert failed.value.diagnostic["field"] == "public_parameters"
    assert failed.value.diagnostic["reason"] == "PUBLIC_PARAMETER_INVALID"
    assert failed.value.diagnostic["correction"]
    projected = _domain_error(failed.value)
    assert projected.detail["diagnostic"] == failed.value.diagnostic
    assert projected.detail["phase"] == "preflight"
    prepared = await family.prepare_draft_preview(scope, draft_ref=ref, invocations=(good,))
    assert prepared["invocations"][0].public_parameters == {"diameter":0.2}


def test_family_draft_source_diagnostic_names_only_actual_missing_entry():
    from CAD2BIM_backend.application.authoring_draft_service import validate_draft_source

    with pytest.raises(AuthoringDraftError) as error:
        validate_draft_source(
            {"main.py": b"def realize(ctx):\n    return {}\n"},
            entry_functions=("realize", "standard_model"),
        )
    assert error.value.code == "AUTHORING_SOURCE_INVALID"
    assert str(error.value) == "main.py requires standard_model"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [16, 17, 64])
async def test_family_can_save_before_any_runtime_validation_or_publication(source_count):
    scope, repo, geometry, family, _ = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="g")
    contract = _request().family_contract.model_dump(mode="json", by_alias=True)
    key = contract["geometry_fragments"][0]["binding_options"][0]["binding_key"]
    definition = {"family_contract":contract, "geometry_bindings":[{"binding_key":key,
        "source":{"source_kind":"draft","draft_ref":geometry.draft_reference(scope,created.draft)},
        "expected_resource_kind":"modeling_function","geometry_purpose":"shape_fragment"}]}
    saved = await family.save_draft_from_aliases(scope, request_identity="f", declaration={"mode":"create",
        "display_name":"Fitting", "purpose":"Test fitting", "usage_instructions":{"action":"set","value":"Use the draft for Preview."},
        "source":{"source_kind":"inline", "source_files":[{"path":"main.py","source":
            "def realize(context):\n    return None\ndef standard_model(context):\n    return None\n"},
            *({"path":f"helper_{index}.py", "source":"VALUE = 1\n"} for index in range(source_count - 1))], "definition":{"action":"set","value":definition}}})
    assert saved.draft.root_kind == "family" and not repo.versions
    _, content = await family.read_draft(scope, draft_ref=family.draft_reference(scope, saved.draft))
    assert len(content.source_files) == source_count
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError
    if source_count == 64:
        with pytest.raises(ProductFamilyAuthoringApplicationError, match="member") as too_many:
            await family.save_draft_from_aliases(scope, request_identity="too-many-members", declaration={
                "mode":"update", "draft_ref":family.draft_reference(scope, saved.draft),
                "expected_edit_token":1, "source_changes":[{"change_kind":"add_member", "path":"extra.py", "source":"VALUE = 1\n"}],
            })
        assert too_many.value.code == "PRODUCT_FAMILY_INPUT_INVALID" and too_many.value.phase == "preflight"
        assert too_many.value.diagnostic == {"field": "source_changes", "failure": "invalid"}
    with pytest.raises(ProductFamilyAuthoringApplicationError, match="unchanged"):
        await family.save_draft_from_aliases(scope, request_identity="same-family", declaration={
            "mode":"update", "draft_ref":family.draft_reference(scope, saved.draft),
            "expected_edit_token":1, "purpose":"Test fitting"})
    assert repo.drafts[saved.draft.root_id] == saved.draft


@pytest.mark.asyncio
async def test_geometry_draft_rejects_total_utf8_over_budget_before_commit():
    scope, repo, geometry, _, _ = setup_services()
    declaration = geometry_create()
    # Each member is valid and under 64 KiB; their actual UTF-8 sum is 256 KiB + 1.
    declaration["source"]["source_files"] = [
        {"path":"main.py" if index == 0 else f"helper_{index}.py", "source":"#" + "é" * 32767 + "\n"}
        for index in range(4)
    ] + [{"path":"overflow.py", "source":"#"}]
    entry = "def build(ctx):\n    return None\n"
    declaration["source"]["source_files"][0]["source"] = entry + "#" + "é" * ((65536 - len(entry) - 1) // 2)
    declaration["source"]["source_files"][0]["source"] += "#" * (65536 - len(declaration["source"]["source_files"][0]["source"].encode("utf-8")))
    with pytest.raises(GeometryAuthoringApplicationError, match="invalid") as error:
        await geometry.save_draft_from_aliases(scope, declaration=declaration, request_identity="over-budget")
    assert "budget" in str(error.value.diagnostic)
    assert not repo.drafts and not repo.operations


@pytest.mark.asyncio
async def test_geometry_source_update_respects_runtime_total_budget():
    scope, repo, geometry, _, _ = setup_services()
    declaration = geometry_create()
    entry = "def build(ctx):\n    return None\n"
    evaluator = "def evaluate(ctx):\n    return None\n"
    declaration["source"]["source_files"] = [
        {"path":"main.py", "source": entry.ljust(65536, "#")},
        {"path":"interfaces.py", "source": evaluator.ljust(65536, "#")},
        *({"path":f"helper_{index}.py", "source":"#" * 65536} for index in range(2)),
    ]
    saved = await geometry.save_draft_from_aliases(scope, declaration=declaration, request_identity="at-budget")
    reference = geometry.draft_reference(scope, saved.draft)
    with pytest.raises(GeometryAuthoringApplicationError, match="total byte budget") as over_budget:
        await geometry.save_draft_from_aliases(scope, request_identity="over-budget-update", declaration={
            "mode":"update", "draft_ref":reference, "expected_edit_token":1,
            "source_changes":[{"change_kind":"add_member", "path":"overflow.py", "source":"#"}],
        })
    assert over_budget.value.phase == "preflight"
    assert over_budget.value.diagnostic == {"field": "source_changes", "failure": "invalid"}
    assert repo.drafts[saved.draft.root_id] == saved.draft


@pytest.mark.parametrize("kind", ["geometry", "family"])
@pytest.mark.asyncio
async def test_source_change_reads_all_members_from_real_bundle(kind):
    scope, repo, geometry, family, _ = setup_services()
    if kind == "geometry":
        service = geometry
        declaration = geometry_create()
    else:
        geometry_draft = await geometry.save_draft_from_aliases(
            scope, declaration=geometry_create(), request_identity="binding-geometry")
        contract = _request().family_contract.model_dump(mode="json", by_alias=True)
        key = contract["geometry_fragments"][0]["binding_options"][0]["binding_key"]
        service = family
        declaration = {"mode":"create", "display_name":"Fitting", "purpose":"Test fitting",
            "usage_instructions":{"action":"set","value":"Use the draft for Preview."},
            "source":{"source_kind":"inline", "source_files":[{"path":"main.py", "source":
                "def realize(context):\n    return None\ndef standard_model(context):\n    return None\n"}],
                "definition":{"action":"set","value":{"family_contract":contract, "geometry_bindings":[{"binding_key":key,
                    "source":{"source_kind":"draft", "draft_ref":geometry.draft_reference(scope, geometry_draft.draft)},
                    "expected_resource_kind":"modeling_function", "geometry_purpose":"shape_fragment"}]}}}}
    original = declaration["source"]["source_files"][0]["source"]
    created = await service.save_draft_from_aliases(scope, declaration=declaration, request_identity=f"{kind}-create")
    reference = service.draft_reference(scope, created.draft)
    added = "def helper():\n    return 1\n"
    updated = await service.save_draft_from_aliases(scope, request_identity=f"{kind}-add-member",
        declaration={"mode":"update", "draft_ref":reference, "expected_edit_token":1,
            "source_changes":[{"change_kind":"add_member", "path":"helper.py", "source":added}]})
    _, content = await service.read_draft(scope, draft_ref=reference)
    assert updated.draft.generation == 2
    expected = {item["path"]: item["source"] for item in declaration["source"]["source_files"]}
    expected["helper.py"] = added
    assert {item.path: item.source for item in content.source_files} == expected
    assert not repo.versions and not repo.store_calls

    # Follow both current Provider conversions and the actual save service, where
    # an untagged Add/Replace union previously lost the caller's replace intent.
    import json
    from contextlib import nullcontext
    from CAD2BIM_mcp.direct.errors import DirectApplicationError
    from CAD2BIM_mcp.direct.ifc.geometry.authoring_application import IfcGeometryAuthoringApplicationMixin
    from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import validate_geometry_resource_write_input
    from CAD2BIM_mcp.direct.ifc.product_family.write_application import IfcProductFamilyWriteApplicationMixin
    from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import validate_product_family_write_provider_input

    class Entry(IfcGeometryAuthoringApplicationMixin, IfcProductFamilyWriteApplicationMixin):
        _resolve_scope = AsyncMock(return_value=scope)
        _scope_binder = staticmethod(lambda _: nullcontext())
        _geometry_authoring_service = geometry
        _product_family_authoring_service = family

    entry = Entry()
    replaced = original + "# exact member replacement\n"

    async def write_member(change_kind, source, identity):
        change = {"change_kind": change_kind, "source_path": "main.py", "source": source}
        value = {"direct_session_alias": "direct_001", "mode": "update",
                 "draft_ref": reference, "expected_edit_token": repo.drafts[created.draft.root_id].generation}
        if kind == "geometry":
            request = validate_geometry_resource_write_input({**value, "source_changes": [change]})
            return await entry.write_ifc_geometry_resource(request, request_identity=identity)
        request = validate_product_family_write_provider_input({**value,
            "source_changes_json": json.dumps({"changes": [change]})})
        return await entry.write_ifc_product_family(request, request_identity=identity)

    result = await write_member("replace_member", replaced, f"{kind}-replace-member")
    _, content = await service.read_draft(scope, draft_ref=reference)
    assert result.edit_token == 3
    expected["main.py"] = replaced
    assert {item.path: item.source for item in content.source_files} == expected
    before_rejected_add = repo.drafts[created.draft.root_id]
    before_operations = len(repo.operations)
    with pytest.raises(DirectApplicationError) as caught:
        await write_member("add_member", original, f"{kind}-add-existing")
    assert caught.value.code == ("GEOMETRY_DRAFT_INPUT_INVALID" if kind == "geometry" else "PRODUCT_FAMILY_INPUT_INVALID")
    assert caught.value.phase == "preflight" and caught.value.retryable is False
    assert caught.value.diagnostic == {"field": "source_changes", "failure": "invalid"}
    assert repo.drafts[created.draft.root_id] == before_rejected_add
    assert len(repo.operations) == before_operations


@pytest.mark.asyncio
async def test_parameter_program_publishes_without_preview_sample_or_execution_and_replays():
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
    scope, repo, geometry, _, publication = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="g")
    reference = geometry.draft_reference(scope, created.draft)
    publication._inspections = SimpleNamespace(read_case_report=AsyncMock(side_effect=AssertionError("No Preview")),
        load_case_geometry=AsyncMock(side_effect=AssertionError("No Preview geometry")))
    arguments = dict(root_kind="geometry", draft_ref=reference, expected_edit_token=1, request_identity="publish")
    committed = await publication.publish(scope, **arguments)
    bundle = committed.geometry_versions[0]
    assert {a.asset_role for a in bundle.assets} == {
        GeometryResourceAssetRole.SOURCE_BUNDLE, GeometryResourceAssetRole.PARAMETER_SCHEMA}
    assert "geometry_summary" not in bundle.version.manifest
    assert "sample_output_sha256" not in bundle.version.manifest
    assert await publication.publish(scope, **arguments) == committed
    publication._geometry_runtime.build_fixed.assert_not_awaited()
    publication._inspections.load_case_geometry.assert_not_awaited()
    assert repo.store_calls == 1


def _fixed_result(request):
    from CAD2BIM_backend.domain.geometry_runtime import GeometryFixedBuildResult
    from CAD2BIM_backend.domain.canonical import sha256_digest
    from tests.unit.backend.test_product_realization_preview_service import _model_interfaces
    return GeometryFixedBuildResult(geometry=_geometry(), interfaces=_model_interfaces(), source_bundle_sha256=hashlib.sha256(request.source_bundle).hexdigest(),
        build_parameters_sha256=sha256_digest({}))


@pytest.mark.asyncio
async def test_fixed_definition_builds_without_preview_and_original_request_never_rebuilds():
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
    scope, repo, geometry, _, publication = setup_services()
    declaration = geometry_create()
    declaration["source"] = {"source_kind": "mapped_model", "source_files": declaration["source"]["source_files"]}
    created = await geometry.save_draft_from_aliases(scope, declaration=declaration, request_identity="fixed")
    publication._geometry_runtime.build_fixed.side_effect = _fixed_result
    publication._inspections = SimpleNamespace(load_case_geometry=AsyncMock(side_effect=AssertionError("Preview is not asset input")))
    arguments = dict(root_kind="geometry", draft_ref=geometry.draft_reference(scope, created.draft),
        expected_edit_token=1, request_identity="publish-fixed")
    result = await publication.publish(scope, **arguments)
    assert next(a for a in result.geometry_versions[0].assets if a.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY).sha256 == _geometry().sha256
    assert await publication.publish(scope, **arguments) == result
    publication._geometry_runtime.build_fixed.assert_awaited_once()
    publication._inspections.load_case_geometry.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_fixed_build_retains_original_failure_without_formal_versions():
    from CAD2BIM_backend.domain.data_runtime import CriticalOperationStatus
    scope, repo, geometry, _, publication = setup_services()
    declaration = geometry_create()
    declaration["source"] = {"source_kind": "mapped_model", "source_files": declaration["source"]["source_files"]}
    created = await geometry.save_draft_from_aliases(scope, declaration=declaration, request_identity="fixed")
    publication._geometry_runtime.build_fixed.side_effect = ValueError("actual fixed build failure")
    arguments = dict(root_kind="geometry", draft_ref=geometry.draft_reference(scope, created.draft),
        expected_edit_token=1, request_identity="publish-failure")
    for _ in range(2):
        with pytest.raises(AuthoringDraftError, match="actual fixed build failure") as error:
            await publication.publish(scope, **arguments)
        assert error.value.operation_alias == "op_001" and error.value.phase == "execution"
        assert error.value.operation_status == "failed"
    assert not repo.versions and not repo.store_calls
    assert list(repo.operations.values())[-1].operation.status is CriticalOperationStatus.FAILED
    publication._geometry_runtime.build_fixed.assert_awaited_once()


def _standard_execution(request):
    from CAD2BIM_backend.domain.product_realization import (
        ProductRealizationGeometryExecution, SolvedProductRealizationProposal,
        ProductRealizationComponentPlacement, ProductRealizationFragmentPlacement,
        ProductGeometryInstanceReceipt, finalize_product_realization,
    )
    from tests.unit.backend.test_product_realization_preview_service import _model_interfaces
    proposal = SolvedProductRealizationProposal(
        components=(ProductRealizationComponentPlacement(component_key="body"),),
        fragments=(ProductRealizationFragmentPlacement(fragment_key="body_shape",
            binding_key="body_geometry", geometry_parameters={}),))
    receipts = (ProductGeometryInstanceReceipt(fragment_key="body_shape", binding_key="body_geometry",
        geometry_sha256=_geometry().sha256, geometry_summary=_geometry().summary, interfaces=_model_interfaces()),)
    return ProductRealizationGeometryExecution(realization=finalize_product_realization(request, proposal, receipts), geometry=_geometry())


@pytest.mark.asyncio
@pytest.mark.parametrize("has_standard", [False, True])
async def test_family_freezes_own_standard_parameters_and_builds_repeated_fixed_dependency_once(has_standard):
    from CAD2BIM_backend.domain.product_family import ProductFamilyVersionAssetRole
    scope, repo, geometry, family, publication = setup_services()
    repo.get_latest_product_family_version = AsyncMock(side_effect=ValueError("Retired content contract is not readable"))
    declaration = geometry_create()
    declaration["source"] = {"source_kind":"mapped_model", "source_files":declaration["source"]["source_files"]}
    g = await geometry.save_draft_from_aliases(scope, declaration=declaration, request_identity="fixed")
    contract = _request().family_contract.model_dump(mode="json", by_alias=True)
    contract["geometry_fragments"][0]["binding_options"].append({"binding_key":"alternate", "parameter_sources":{"overrides": {}}})
    if has_standard:
        contract["standard_model"] = {"public_parameters": {"diameter": 0.2}}
        contract["public_parameters"] = [
            {"parameter_kind":"number", "parameter_key":"diameter", "unit":"metre", "minimum":0.01, "maximum":1},
            {"parameter_kind":"number", "parameter_key":"height", "unit":"metre", "minimum":0.01, "maximum":1, "default_value":0.3}]
    saved = await family.save_draft_from_aliases(scope, request_identity="family", declaration={
        "mode":"create", "display_name":"Formal fitting", "purpose":"Definition publication",
        "usage_instructions":{"action":"set","value":"Use the exact formal definition."},
        "source":{"source_kind":"inline", "source_files":[{"path":"main.py", "source":"def realize(ctx): pass\ndef standard_model(ctx): pass\n"}],
            "definition":{"action":"set","value":{"family_contract":contract, "geometry_bindings":[
                {"binding_key":key, "source":{"source_kind":"draft", "draft_ref":geometry.draft_reference(scope,g.draft)},
                 "expected_resource_kind":"mapped_model", "geometry_purpose":"shape_fragment"}
                for key in ("body_geometry", "alternate")]}}}})
    publication._geometry_runtime.build_fixed.side_effect = _fixed_result
    def precheck(requests):
        async def values():
            from CAD2BIM_backend.domain.product_realization import ProductGeometryInterfaceEvaluation
            execution = _standard_execution(requests[0])
            evaluations = tuple(ProductGeometryInterfaceEvaluation.model_validate(item.model_dump(
                exclude={"geometry_sha256", "geometry_summary"})) for item in execution.realization.instance_receipts)
            yield execution.realization.model_copy(update={"execution_stage": "interface_constraints_evaluated",
                "instance_receipts": (), "interface_evaluations": evaluations})
        return values()
    def instantiate(requests, prechecks, *, output_need):
        async def values():
            yield _standard_execution(requests[0]).model_copy(update={"output_need": output_need})
        return values()
    publication._realization_runtime.precheck_batch.side_effect = precheck
    publication._realization_runtime.instantiate_batch.side_effect = instantiate
    family._family_assets = SimpleNamespace(write_exact_asset=AsyncMock(side_effect=lambda family, asset, body: asset))
    publication._inspections = SimpleNamespace(load_case_geometry=AsyncMock(side_effect=AssertionError("No Preview supply")))
    result = await publication.publish(scope, root_kind="family", draft_ref=family.draft_reference(scope,saved.draft),
        expected_edit_token=1, request_identity="publish-family")
    replay = await publication.publish(scope, root_kind="family", draft_ref=family.draft_reference(scope,saved.draft),
        expected_edit_token=1, request_identity="publish-family")
    assert replay == result
    repo.get_product_family_version_by_id.assert_awaited_once_with(
        scope.identity, family_version_id=result.family_version.version.family_version_id,
    )
    publication._geometry_runtime.build_fixed.assert_awaited_once()
    publication._inspections.load_case_geometry.assert_not_awaited()
    assert len(result.geometry_versions) == 1
    assert len({b.geometry_resource_version_id for b in result.family_version.geometry_bindings}) == 1
    roles = {a.asset_role for a in result.family_version.assets}
    if has_standard:
        request = publication._realization_runtime.precheck_batch.call_args.args[0][0]
        assert request.invocation.public_parameters == {"diameter":0.2, "height":0.3}
        assert request.invocation.product_origin == (0.,0.,0.) and not request.invocation.external_constraints
        assert result.family_version.version.family_contract.standard_model.public_parameters == request.invocation.public_parameters
        assert ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY in roles
    else:
        publication._realization_runtime.precheck_batch.assert_not_called()
        assert roles == {ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE}


def test_standard_definition_reuses_defaults_but_never_invents_required_values():
    from CAD2BIM_backend.domain.product_family import ProductFamilyContract
    contract = _request().family_contract.model_dump(mode="json", by_alias=True)
    contract.update(standard_model={}, public_parameters=[
        {"parameter_kind":"number", "parameter_key":"diameter", "unit":"metre", "minimum":0.01, "maximum":1}])
    raw = ProductFamilyContract.model_validate(contract)
    assert raw.model_dump(mode="json", by_alias=True)["standard_model"] == {}
    with pytest.raises(ValueError, match="diameter"):
        ProductRealizationInputService.normalize_standard_definition(raw)
    contract["public_parameters"][0]["default_value"] = 0.2
    normalized = ProductRealizationInputService.normalize_standard_definition(ProductFamilyContract.model_validate(contract))
    assert normalized.standard_model.public_parameters == {"diameter":0.2}


@pytest.mark.asyncio
async def test_failed_preview_can_be_linked_without_suppling_assets_or_publication_qualification():
    from CAD2BIM_backend.domain.data_runtime import AuthoringPreviewSelection
    from CAD2BIM_backend.domain.preview_cases import GeometryPreviewCase, PreviewCaseReport, PreviewCaseResult
    scope, repo, geometry, _, publication = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="g")
    reference = geometry.draft_reference(scope,created.draft)
    source = (await geometry.prepare_draft_preview(scope,draft_ref=reference))["source"]
    report = PreviewCaseReport(source=source,cases=(PreviewCaseResult(
        case=GeometryPreviewCase(case_key="failed",parameters={}),execution_status="technical_error",
        expectation_check="not_checked"),))
    publication._inspections = SimpleNamespace(
        _report_operation=AsyncMock(return_value=SimpleNamespace(operation=SimpleNamespace(run_id=scope.run_id,operation_id="preview-failed"))),
        read_case_report=AsyncMock(return_value=report),load_case_geometry=AsyncMock(side_effect=AssertionError("No assets")))
    result = await publication.publish(scope,root_kind="geometry",draft_ref=reference,expected_edit_token=1,
        request_identity="publish-example",preview_examples=(AuthoringPreviewSelection(operation_alias="op_007"),))
    assert result.operation.operation.detail.preview_examples[0].operation_id == "preview-failed"
    assert result.operation.operation.detail.preview_examples[0].case_keys == ("failed",)
    publication._inspections.load_case_geometry.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_commit_conflict_is_failed_and_unknown_lost_response_is_reconciled():
    from CAD2BIM_backend.domain.control_plane import ConflictError
    scope, repo, geometry, _, publication = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="g")
    arguments = dict(root_kind="geometry",draft_ref=geometry.draft_reference(scope,created.draft),expected_edit_token=1)
    commit = repo.commit_authoring_publication
    repo.commit_authoring_publication = AsyncMock(side_effect=ConflictError("draft changed in commit"))
    with pytest.raises(AuthoringDraftError,match="draft changed"):
        await publication.publish(scope,**arguments,request_identity="conflicted")
    failure = list(repo.operations.values())[-1].operation
    assert failure.error_phase.value == "commit_fence" and not repo.versions
    async def lost_response(*args,**kwargs):
        await commit(*args,**kwargs)
        raise ConnectionError("commit response lost")
    repo.commit_authoring_publication = lost_response
    recovered = await publication.publish(scope,**arguments,request_identity="new-request")
    assert recovered.operation.operation.status.value == "succeeded"
    assert len(recovered.geometry_versions) == 1


@pytest.mark.asyncio
async def test_definite_publication_commit_rejection_finishes_original_operation_without_exposing_sql():
    scope, repo, geometry, _, publication = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="g")
    arguments = dict(root_kind="geometry", draft_ref=geometry.draft_reference(scope, created.draft),
        expected_edit_token=1, request_identity="rejected")
    commit = AsyncMock(side_effect=AuthoringPublicationCommitRejected(
        "Publication was rejected by an integrity check; the draft remains available."))
    repo.commit_authoring_publication = commit
    with pytest.raises(AuthoringDraftError) as caught:
        await publication.publish(scope, **arguments)
    assert caught.value.code == "AUTHORING_PUBLICATION_COMMIT_REJECTED"
    assert caught.value.phase == "commit_fence" and caught.value.operation_alias == "op_001"
    assert caught.value.operation_status == "failed"
    assert "SQL" not in str(caught.value) and "constraint" not in str(caught.value)
    stored = list(repo.operations.values())[-1].operation
    assert stored.status.value == "failed" and stored.error_code == caught.value.code
    assert stored.error_phase.value == "commit_fence" and not repo.versions
    with pytest.raises(AuthoringDraftError) as replay:
        await publication.publish(scope, **arguments)
    assert replay.value.operation_alias == caught.value.operation_alias
    assert replay.value.operation_status == "failed"
    commit.assert_awaited_once()

    repo.get_operation = AsyncMock(return_value=None)
    with pytest.raises(AuthoringDraftError) as unconfirmed:
        await publication._terminal_result(scope, stored)
    assert unconfirmed.value.operation_status is None and unconfirmed.value.operation_alias is None


@pytest.mark.asyncio
@pytest.mark.parametrize("active_execution", [False, True])
async def test_prepared_recovery_records_changed_draft_only_after_acquiring_execution(active_execution):
    scope, repo, geometry, _, publication = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="g")
    reference = geometry.draft_reference(scope,created.draft)
    arguments = dict(root_kind="geometry",draft_ref=reference,expected_edit_token=1,request_identity="lost")
    repo.commit_authoring_publication = AsyncMock(side_effect=ConnectionError("unknown commit outcome"))
    with pytest.raises(AuthoringDraftError) as uncertain:
        await publication.publish(scope,**arguments)
    assert uncertain.value.code == "PUBLICATION_RECONCILIATION_REQUIRED"
    assert uncertain.value.phase == "commit_fence"
    admitted = next(iter(repo.prepared.values()))
    assert admitted.status.value == "prepared"
    assert admitted.operation_id not in repo.operations and not repo.versions
    original_begin = repo.begin_authoring_publication
    repo.begin_authoring_publication = AsyncMock(return_value=admitted)
    with pytest.raises(AuthoringDraftError) as blocked:
        await publication.publish(scope, **{**arguments, "request_identity": "different"})
    assert blocked.value.code == "PUBLICATION_RECONCILIATION_REQUIRED"
    assert len(repo.prepared) == 1 and repo.commit_authoring_publication.await_count == 1
    repo.begin_authoring_publication = original_begin
    await geometry.save_draft_from_aliases(scope,request_identity="changed",declaration={
        "mode":"update","draft_ref":reference,"expected_edit_token":1,"usage_instructions":{"action":"set","value":"Changed definition guidance."}})
    if active_execution:
        repo.claim_authoring_execution = AsyncMock(return_value=None)
    with pytest.raises(AuthoringDraftError) as error:
        await publication.publish(scope,**arguments)
    if active_execution:
        assert error.value.code == "PUBLICATION_RECONCILIATION_REQUIRED"
        assert admitted.operation_id not in repo.operations
    else:
        assert error.value.code == "DRAFT_CHANGED" and error.value.phase == "commit_fence"
        assert repo.operations[admitted.operation_id].operation.status.value == "failed"
        assert repo.operations[admitted.operation_id].operation.detail == admitted.detail
    assert not repo.versions
