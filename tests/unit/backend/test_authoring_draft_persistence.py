"""Focused repository boundary checks; real PostgreSQL guards are integration-owned."""

from base64 import urlsafe_b64encode
from copy import deepcopy
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftRecord, AuthoringPublicationCommitRejected
from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftService
from CAD2BIM_backend.application.opaque_reference_service import encode_operation_reference
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.control_plane import ConflictError, ExecutionMode
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, AuthoringDraftSelection, AuthoringDraftWriteOperationDetail,
    AuthoringPreviewExampleReference, AuthoringPublishedGeometryReference, AuthoringExecutionClaim,
    AuthoringPublishOperationDetail, CatalogResourceKind, CriticalOperationKind,
    CriticalOperationRecord, CriticalOperationStatus, DataCatalogEntry, PublishedCriticalOperation,
    GeometryInspectionOperationDetail,
    CriticalOperationErrorPhase,
    PublishedOperationCatalogSummary,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind, GeometryResourceRecord
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.infrastructure.postgres import repository_authoring_draft as drafts
from CAD2BIM_backend.infrastructure.postgres import repository_data_runtime as operations
from CAD2BIM_backend.infrastructure.postgres.models import GeometryResourceModel, DataCatalogEntryModel
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context

NOW = datetime(2026, 9, 30, tzinfo=UTC)
SNAPSHOT = ArtifactVersionRef(artifact_id="draft-a", version_id="draft-v2", sha256="a" * 64)
OWNER = ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.TASK, tenant_id="tenant-a", task_id="task-a")
CLAIM = AuthoringExecutionClaim(operation_id="operation-a", owner="executor-a", generation=1, token="x" * 32)


def scope():
    return InvocationScope(identity=TenantExecutionContext(tenant_id="tenant-a", principal_id="principal-a",
        task_id="task-a", actor_kind=TenantActorKind.USER, command_id="command-a"),
        owner_id="owner-a", run_id="run-a", execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)


def operation(**changes):
    values = dict(operation_id="operation-a", owner_id="owner-a", tenant_id="tenant-a", task_id="task-a",
        run_id="run-a", operation_kind=CriticalOperationKind.GEOMETRY_RESOURCE_WRITE,
        tool_name="ifc_geometry_resource_write", intent_summary="Save the editable geometry draft",
        idempotency_key="write-a", request_fingerprint="b" * 64, status=CriticalOperationStatus.SUCCEEDED,
        code_artifact=SNAPSHOT, target_geometry_resource_id="geometry-a",
        detail=AuthoringDraftWriteOperationDetail(root_kind="geometry", root_id="geometry-a",
            expected_generation=1, result_generation=2), started_at=NOW, finished_at=NOW)
    values.update(changes)
    if values["operation_kind"] is CriticalOperationKind.AUTHORING_PUBLISH:
        values["code_artifact"] = None
    return CriticalOperationRecord(**values)


def published(value):
    return PublishedCriticalOperation(operation=value, catalog_entry=DataCatalogEntry(
        catalog_entry_id="catalog-a", owner_id=value.owner_id, tenant_id=value.tenant_id,
        task_id=value.task_id, run_id=value.run_id, resource_kind=CatalogResourceKind.OPERATION,
        alias="op_001", ordinal=1, catalog_revision=1, operation_id=value.operation_id, created_at=NOW))


class Session:
    def __init__(self, roots):
        self.scalar = AsyncMock(side_effect=roots)
        self.execute = AsyncMock()
        self.flush = AsyncMock()
        self.add = Mock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self


def publication_with_noncurrent_family_input(*, origin_run_id="run-a"):
    from tests.unit.backend.test_product_family_operation_projection import _draft

    family = _draft()
    family_payload = family.model_dump(mode="json")
    family_payload["family_contract"]["interfaces"] = [{
        "interface_key": "axis", "owner_component_key": "body", "interface_kind": "external",
        "source": {"fragment_key": "body_shape", "model_interface_key": "axis"},
        "external_constraint": {"allowed_kinds": ["fixed_point", "infinite_axis"],
            "allowed_directions": ["same"], "maximum_position_tolerance_m": 0.01,
            "maximum_angular_tolerance_rad": 0.01},
    }]
    contract = type(family.family_contract).model_validate(family_payload["family_contract"])
    family_payload.update(family_contract=contract, contract_digest=sha256_digest(contract))
    family = type(family).model_validate(family_payload)
    detail = AuthoringPublishOperationDetail(root_kind="family", root_id=family.family_id,
        expected_drafts=(AuthoringDraftSelection(root_kind="family", root_id=family.family_id,
            generation=2, snapshot=SNAPSHOT), AuthoringDraftSelection(root_kind="geometry",
            root_id="geometry_internal", generation=2, snapshot=SNAPSHOT)),
        geometry_versions=(AuthoringPublishedGeometryReference(resource_id="geometry_internal",
            resource_version_id="geometry_version_internal"),), family_version=family)
    current = operation(run_id=origin_run_id, operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish", target_geometry_resource_id=None,
        target_product_family_id=family.family_id, result_product_family_version_id=family.family_version_id,
        detail=detail)
    model = operations.PostgresDataRuntimeRepositoryMixin._operation_model(current)
    # This is a rejection witness, not a legacy fixture accepted by online DTOs.
    model.operation_detail_json["family_version"]["family_contract"]["interfaces"][0][
        "external_constraint"]["allowed_kinds"][1] = "retired_anchor_kind"
    entry = DataCatalogEntryModel(**published(current).catalog_entry.model_dump())
    return model, entry


@pytest.mark.asyncio
@pytest.mark.parametrize("root_kind,origin_run_id", [("geometry", "run-a"), ("family", "run-source")])
async def test_publication_navigation_reads_identity_without_decoding_family_result(monkeypatch, root_kind, origin_run_id):
    model, entry = publication_with_noncurrent_family_input(origin_run_id=origin_run_id)
    retained = deepcopy(model.operation_detail_json)
    session = Session([model, entry])
    repository = operations.PostgresDataRuntimeRepositoryMixin(lambda: session)
    monkeypatch.setattr(operations, "set_tenant_execution_context", AsyncMock())
    monkeypatch.setattr(repository, "_require_data_run", AsyncMock())
    codec = CursorCodec(urlsafe_b64encode(b"n" * 32).decode().rstrip("="))
    service = AuthoringDraftService(None, repository, codec)
    result = await service.publication_navigation(scope(), root_kind=root_kind,
        version_id="geometry_version_internal" if root_kind == "geometry" else "family_version_internal_2")
    assert result == {"availability": "available", "operation_ref": encode_operation_reference(
        codec, scope().identity, origin_run_id=origin_run_id, operation_id=model.operation_id),
        "operation_alias": "op_001" if origin_run_id == scope().run_id else None}
    # The same complete Operation remains subject to the current DTO; navigation
    # must neither interpret nor repair that immutable business payload.
    session.scalar = AsyncMock(side_effect=[model, entry])
    with pytest.raises(ValidationError) as caught:
        await repository.get_operation_by_locator(scope(), origin_run_id=origin_run_id,
            operation_id=model.operation_id)
    assert caught.value.errors(include_input=False)[0]["loc"] == (
        "family_version", "family_contract", "interfaces", 0, "external_constraint", "allowed_kinds", 1)
    assert model.operation_detail_json == retained


@pytest.mark.asyncio
async def test_operation_catalog_summary_uses_the_same_identity_projection(monkeypatch):
    model, entry = publication_with_noncurrent_family_input()
    session = Session([model, entry])
    repository = operations.PostgresDataRuntimeRepositoryMixin(lambda: session)
    monkeypatch.setattr(operations, "set_tenant_execution_context", AsyncMock())
    monkeypatch.setattr(repository, "_require_data_run", AsyncMock())
    result = await repository.get_operation_summary(scope(), model.operation_id)
    assert isinstance(result, PublishedOperationCatalogSummary)
    assert result.operation.operation_id == model.operation_id and result.catalog_entry.alias == "op_001"


@pytest.mark.asyncio
async def test_publication_navigation_requires_catalog_entry(monkeypatch):
    model, _ = publication_with_noncurrent_family_input()
    session = Session([model, None])
    repository = operations.PostgresDataRuntimeRepositoryMixin(lambda: session)
    monkeypatch.setattr(operations, "set_tenant_execution_context", AsyncMock())
    monkeypatch.setattr(repository, "_require_data_run", AsyncMock())
    service = AuthoringDraftService(None, repository, None)
    assert await service.publication_navigation(scope(), root_kind="geometry",
        version_id="geometry_version_internal") == {"availability": "unavailable"}


def setup_transaction(monkeypatch, roots):
    session = Session(roots)
    monkeypatch.setattr(drafts, "set_tenant_execution_context", AsyncMock())
    monkeypatch.setattr(drafts.Operations, "_require_data_run", AsyncMock())
    monkeypatch.setattr(drafts.Operations, "_authoring_model", AsyncMock(return_value=SimpleNamespace(status="prepared")))
    monkeypatch.setattr(drafts.Operations, "require_authoring_claim", Mock())
    monkeypatch.setattr(drafts, "read_snapshot", AsyncMock(return_value=SNAPSHOT))
    return SimpleNamespace(_sessions=lambda: session), session


def root(generation=1, root_id="geometry-a"):
    return GeometryResourceModel(resource_id=root_id, resource_kind="mapped_model", owner_scope="TASK",
        owner_tenant_id="tenant-a", owner_task_id="task-a", display_name="Body", created_at=NOW,
        draft_generation=generation, draft_artifact_version_id="draft-v1")


@pytest.mark.asyncio
@pytest.mark.parametrize("current_generation", [1, 2])
async def test_draft_write_cas_changes_only_root_and_commits_original_operation(monkeypatch, current_generation):
    stored = root(current_generation)
    repository, session = setup_transaction(monkeypatch, [stored])
    op = operation()
    monkeypatch.setattr(drafts, "draft_replay", AsyncMock(return_value=None))
    commit = AsyncMock(return_value=published(op))
    monkeypatch.setattr(drafts, "publish_operation", commit)
    record = GeometryResourceRecord(resource_id="geometry-a", resource_kind=GeometryResourceKind.MAPPED_MODEL,
        owner_scope=OWNER, display_name="Body", created_at=NOW)
    with bind_tenant_execution_context(scope().identity):
        invocation = drafts.commit_draft(repository, scope(), root_kind="geometry", root_record=record,
            snapshot=SNAPSHOT, expected_generation=1, operation=op, new_root=Mock(), require_same_root=Mock())
        if current_generation == 2:
            with pytest.raises(ConflictError, match="draft changed"):
                await invocation
            assert stored.draft_artifact_version_id == "draft-v1"
            commit.assert_not_awaited()
            session.flush.assert_not_awaited()
        else:
            result = await invocation
            assert result.draft.snapshot == SNAPSHOT and result.draft.generation == 2
            assert result.operation.operation == op
            assert stored.draft_artifact_version_id == SNAPSHOT.version_id
            commit.assert_awaited_once()
        session.add.assert_not_called()  # No formal Version, binding or asset row is admitted by Write.


@pytest.mark.asyncio
async def test_replay_reads_original_snapshot_without_reading_current_root(monkeypatch):
    op = operation()
    stored = SimpleNamespace()
    monkeypatch.setattr(drafts, "find_operation", AsyncMock(return_value=stored))
    monkeypatch.setattr(drafts.Operations, "_operation_record", AsyncMock(return_value=op))
    monkeypatch.setattr(drafts.Operations, "_published_operation", AsyncMock(return_value=published(op)))
    session = Session([])
    replay = await drafts.draft_replay(object(), session, scope(), op, OWNER)
    assert replay.draft.generation == 2 and replay.draft.snapshot == SNAPSHOT
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_response_loss_reconciles_the_original_operation(monkeypatch):
    from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftCommitResult

    class LostCommitSession(Session):
        lost = False

        async def __aexit__(self, *args):
            if not self.lost:
                self.lost = True
                raise OperationalError("COMMIT", {}, ConnectionError("response lost"))
            return False

    op = operation()
    original = AuthoringDraftCommitResult(operation=published(op), draft=AuthoringDraftRecord(
        root_kind="geometry", root_id="geometry-a", owner_scope=OWNER, generation=2, snapshot=SNAPSHOT))
    repository, _ = setup_transaction(monkeypatch, [])
    session = LostCommitSession([root()])
    repository._sessions = lambda: session
    replay = AsyncMock(side_effect=[None, original])
    monkeypatch.setattr(drafts, "draft_replay", replay)
    monkeypatch.setattr(drafts, "publish_operation", AsyncMock(return_value=published(op)))
    record = GeometryResourceRecord(resource_id="geometry-a", resource_kind=GeometryResourceKind.MAPPED_MODEL,
        owner_scope=OWNER, display_name="Body", created_at=NOW)
    with bind_tenant_execution_context(scope().identity):
        result = await drafts.commit_draft(repository, scope(), root_kind="geometry", root_record=record,
            snapshot=SNAPSHOT, expected_generation=1, operation=op, new_root=Mock(), require_same_root=Mock())
    assert result == original and replay.await_count == 2


@pytest.mark.asyncio
async def test_publication_checks_entire_closure_before_any_version_insert(monkeypatch):
    selections = tuple(AuthoringDraftRecord(root_kind="geometry", root_id=name, owner_scope=OWNER,
        generation=1, snapshot=SNAPSHOT) for name in ("geometry-a", "geometry-b"))
    detail = AuthoringPublishOperationDetail(root_kind="geometry", root_id="geometry-a",
        expected_drafts=tuple(AuthoringDraftSelection(root_kind=item.root_kind, root_id=item.root_id,
            generation=item.generation, snapshot=item.snapshot) for item in selections),
        geometry_versions=tuple(AuthoringPublishedGeometryReference(resource_id=item.root_id,
            resource_version_id=item.root_id + "-v1") for item in selections))
    op = operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish", detail=detail, result_geometry_resource_version_id="geometry-a-v1")
    proposals = tuple(SimpleNamespace(operation=op, resource=SimpleNamespace(resource_id=item.root_id),
        version=SimpleNamespace(resource_version_id=item.root_id + "-v1")) for item in selections)
    first = root(1)
    first.draft_artifact_version_id = SNAPSHOT.version_id
    repository, session = setup_transaction(monkeypatch, [first, root(2, "geometry-b")])
    monkeypatch.setattr(drafts, "publication_replay", AsyncMock(return_value=None))
    with bind_tenant_execution_context(scope().identity), pytest.raises(ConflictError, match="draft changed after publication"):
        await drafts.commit_publication(repository, scope(), operation=op, geometry_publications=proposals,
            family_publication=None, expected_drafts=selections, execution_claim=CLAIM)
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_integrity_rejection_is_classified_without_replaying_or_exposing_sql(monkeypatch):
    selection = AuthoringDraftRecord(root_kind="geometry", root_id="geometry-a", owner_scope=OWNER,
        generation=1, snapshot=SNAPSHOT)
    detail = AuthoringPublishOperationDetail(root_kind="geometry", root_id="geometry-a",
        expected_drafts=(AuthoringDraftSelection(root_kind="geometry", root_id="geometry-a",
            generation=1, snapshot=SNAPSHOT),),
        geometry_versions=(AuthoringPublishedGeometryReference(resource_id="geometry-a",
            resource_version_id="geometry-a-v1"),))
    op = operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish", detail=detail,
        result_geometry_resource_version_id="geometry-a-v1")
    repository, _ = setup_transaction(monkeypatch, [])
    rejection = IntegrityError("INSERT INTO private_table", {}, ValueError("private constraint name"))
    replay = AsyncMock(side_effect=rejection)
    monkeypatch.setattr(drafts, "publication_replay", replay)
    with bind_tenant_execution_context(scope().identity), pytest.raises(AuthoringPublicationCommitRejected) as caught:
        await drafts.commit_publication(repository, scope(), operation=op, geometry_publications=(),
            family_publication=None, expected_drafts=(selection,), execution_claim=CLAIM)
    assert caught.value.__cause__ is rejection
    assert "private" not in str(caught.value) and "INSERT" not in str(caught.value)
    replay.assert_awaited_once()


def test_runtime_draft_filter_does_not_expose_shared_system_roots():
    statement = drafts.private_root_predicate(GeometryResourceModel, scope())
    assert set(statement.compile().params.values()) == {"TASK", "TENANT", "tenant-a", "task-a"}


@pytest.mark.asyncio
async def test_concurrently_published_geometry_rejects_entire_prepared_family_closure(monkeypatch):
    from tests.unit.backend.test_product_family_authoring_contracts import _draft
    from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import PostgresGeometryResourceRepositoryMixin as Geometry
    from CAD2BIM_backend.infrastructure.postgres.repository_product_family_authoring import PostgresProductFamilyAuthoringRepositoryMixin as Family

    family_version = _draft()
    selections = tuple(AuthoringDraftRecord(root_kind=kind, root_id=name, owner_scope=OWNER,
        generation=1, snapshot=SNAPSHOT) for kind, name in (
            ("family", family_version.family_id), ("geometry", "geometry-a"), ("geometry", "geometry-b")))
    detail = AuthoringPublishOperationDetail(root_kind="family", root_id=family_version.family_id,
        expected_drafts=tuple(AuthoringDraftSelection(root_kind=item.root_kind, root_id=item.root_id,
            generation=item.generation, snapshot=item.snapshot) for item in selections),
        geometry_versions=tuple(AuthoringPublishedGeometryReference(resource_id=name, resource_version_id=name + "-new")
            for name in ("geometry-a", "geometry-b")), family_version=family_version)
    op = operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH, tool_name="ifc_authoring_publish",
        target_geometry_resource_id=None, target_product_family_id=family_version.family_id,
        result_product_family_version_id=family_version.family_version_id, detail=detail)
    proposals = tuple(SimpleNamespace(operation=op, resource=SimpleNamespace(resource_id=ref.resource_id),
        version=SimpleNamespace(resource_version_id=ref.resource_version_id)) for ref in detail.geometry_versions)
    family = SimpleNamespace(operation=op, version=family_version)
    repository, session = setup_transaction(monkeypatch, [])
    monkeypatch.setattr(drafts, "publication_replay", AsyncMock(return_value=None))
    cas = AsyncMock(return_value={})
    monkeypatch.setattr(drafts, "load_current_draft_roots", cas)
    monkeypatch.setattr(drafts, "current_publication", AsyncMock(return_value=None))
    lookup = AsyncMock(side_effect=[None, (object(), object())])
    monkeypatch.setattr(drafts, "published_geometry_draft", lookup)
    insert_geometry, insert_family, receipt = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(Geometry, "_insert_geometry_bundle", insert_geometry)
    monkeypatch.setattr(Family, "_insert_product_family_bundle", insert_family)
    monkeypatch.setattr(drafts, "publish_operation", receipt)
    with bind_tenant_execution_context(scope().identity), pytest.raises(ConflictError, match="submit a new publication request"):
        await drafts.commit_publication(repository, scope(), operation=op,
            geometry_publications=proposals, family_publication=family, expected_drafts=selections, execution_claim=CLAIM)
    cas.assert_awaited_once_with(session, scope(), selections)
    assert lookup.await_count == 2
    insert_geometry.assert_not_awaited()
    insert_family.assert_not_awaited()
    receipt.assert_not_awaited()
    assert tuple(ref.resource_version_id for ref in op.detail.geometry_versions) == ("geometry-a-new", "geometry-b-new")


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_run", ["run-a", "previous-run"])
async def test_unchanged_publication_reuses_versions_and_issues_only_needed_run_receipt(monkeypatch, previous_run):
    from CAD2BIM_backend.domain.authoring_draft import AuthoringPublicationCommitResult
    from tests.unit.backend.test_geometry_resource_contracts import _bundle

    bundle = _bundle(GeometryResourceKind.MAPPED_MODEL)
    root_id = bundle.resource.resource_id
    selection = AuthoringDraftRecord(root_kind="geometry", root_id=root_id,
        owner_scope=OWNER, generation=1, snapshot=SNAPSHOT)
    detail = AuthoringPublishOperationDetail(root_kind="geometry", root_id=root_id,
        expected_drafts=(AuthoringDraftSelection(root_kind="geometry", root_id=root_id, generation=1, snapshot=SNAPSHOT),),
        geometry_versions=(AuthoringPublishedGeometryReference(resource_id=root_id, resource_version_id="new-candidate"),))
    incoming = operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH, tool_name="ifc_authoring_publish",
        target_geometry_resource_id=root_id, result_geometry_resource_version_id="new-candidate", detail=detail)
    previous = incoming.model_copy(update={"operation_id": "previous-operation", "run_id": previous_run,
        "result_geometry_resource_version_id": bundle.version.resource_version_id,
        "detail": detail.model_copy(update={"geometry_versions": (AuthoringPublishedGeometryReference(
            resource_id=root_id, resource_version_id=bundle.version.resource_version_id),)})})
    existing = AuthoringPublicationCommitResult(operation=published(previous), geometry_versions=(bundle,))
    stored = root(root_id=root_id)
    stored.draft_artifact_version_id = SNAPSHOT.version_id
    repository, session = setup_transaction(monkeypatch, [stored])
    monkeypatch.setattr(drafts, "publication_replay", AsyncMock(return_value=None))
    monkeypatch.setattr(drafts, "current_publication", AsyncMock(return_value=existing))
    receipt = AsyncMock(side_effect=lambda repository, session, scope, record, claim: published(record))
    monkeypatch.setattr(drafts, "finish_publication", receipt)
    proposal = SimpleNamespace(operation=incoming, resource=SimpleNamespace(resource_id=root_id),
        version=SimpleNamespace(resource_version_id="new-candidate"))
    with bind_tenant_execution_context(scope().identity):
        result = await drafts.commit_publication(repository, scope(), operation=incoming,
            geometry_publications=(proposal,), family_publication=None, expected_drafts=(selection,), execution_claim=CLAIM)
    assert result.geometry_versions == (bundle,)
    assert result.operation.operation.run_id == scope().run_id
    assert result.operation.operation.result_geometry_resource_version_id == bundle.version.resource_version_id
    assert receipt.await_count == 1
    assert result.operation.operation.operation_id == incoming.operation_id
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_geometry_draft_lookup_finds_family_publication_without_mislabeling_its_receipt(monkeypatch):
    from sqlalchemy.dialects import postgresql
    from tests.unit.backend.test_geometry_resource_contracts import _bundle
    from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import PostgresGeometryResourceRepositoryMixin as Geometry

    bundle = _bundle(GeometryResourceKind.MAPPED_MODEL)
    selection = AuthoringDraftRecord(root_kind="geometry", root_id=bundle.resource.resource_id,
        owner_scope=OWNER, generation=2, snapshot=SNAPSHOT)
    original = SimpleNamespace(operation=SimpleNamespace(detail=SimpleNamespace(root_kind="family", root_id="family-a",
        geometry_versions=(AuthoringPublishedGeometryReference(resource_id=bundle.resource.resource_id,
            resource_version_id=bundle.version.resource_version_id),))))
    model = SimpleNamespace(run_id="source-run")
    session = Session([model])
    load_operation = AsyncMock(return_value=original)
    monkeypatch.setattr(drafts.Operations, "_published_operation", load_operation)
    monkeypatch.setattr(Geometry, "_load_geometry_bundle", AsyncMock(return_value=bundle))
    result = await drafts.published_geometry_draft(None, session, scope(), draft=selection)
    assert result == bundle
    assert not hasattr(result, "operation")
    compiled = session.scalar.await_args.args[0].compile(dialect=postgresql.dialect())
    assert "task-a" in compiled.params.values() and "owner-a" in compiled.params.values()
    assert [{"root_kind": "geometry", "root_id": selection.root_id, "generation": 2,
        "snapshot": SNAPSHOT.model_dump(mode="json")}] in compiled.params.values()
    assert [{"resource_id": selection.root_id}] in compiled.params.values()
    assert "@>" in str(compiled)
    load_operation.assert_awaited_once_with(None, session, model, run_id="source-run")


@pytest.mark.asyncio
@pytest.mark.parametrize("exact", [True, False])
async def test_geometry_receipt_without_proposals_requires_exact_previously_published_draft(monkeypatch, exact):
    from tests.unit.backend.test_geometry_resource_contracts import _bundle
    from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import PostgresGeometryResourceRepositoryMixin as Geometry

    bundle = _bundle(GeometryResourceKind.MAPPED_MODEL)
    root_id, version_id = bundle.resource.resource_id, bundle.version.resource_version_id
    selection = AuthoringDraftRecord(root_kind="geometry", root_id=root_id, owner_scope=OWNER,
        generation=1, snapshot=SNAPSHOT)
    detail = AuthoringPublishOperationDetail(root_kind="geometry", root_id=root_id,
        expected_drafts=(AuthoringDraftSelection(root_kind="geometry", root_id=root_id, generation=1, snapshot=SNAPSHOT),),
        geometry_versions=(AuthoringPublishedGeometryReference(resource_id=root_id, resource_version_id=version_id),))
    incoming = operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH, tool_name="ifc_authoring_publish",
        target_geometry_resource_id=root_id, result_geometry_resource_version_id=version_id, detail=detail)
    stored = root(root_id=root_id)
    stored.draft_artifact_version_id = SNAPSHOT.version_id
    repository, session = setup_transaction(monkeypatch, [stored])
    monkeypatch.setattr(drafts, "publication_replay", AsyncMock(return_value=None))
    monkeypatch.setattr(drafts, "current_publication", AsyncMock(return_value=None))
    monkeypatch.setattr(drafts, "published_geometry_draft", AsyncMock(return_value=bundle if exact else None))
    monkeypatch.setattr(drafts.Operations, "_require_operation_artifacts", AsyncMock())
    insert = AsyncMock()
    monkeypatch.setattr(Geometry, "_insert_geometry_bundle", insert)
    monkeypatch.setattr(Geometry, "_load_geometry_bundle", AsyncMock(return_value=bundle))
    receipt = AsyncMock(return_value=published(incoming))
    monkeypatch.setattr(drafts, "finish_publication", receipt)
    with bind_tenant_execution_context(scope().identity):
        call = drafts.commit_publication(repository, scope(), operation=incoming,
            geometry_publications=(), family_publication=None, expected_drafts=(selection,), execution_claim=CLAIM)
        if exact:
            result = await call
            assert result.geometry_versions == (bundle,) and result.family_version is None
            assert result.operation.operation.detail.preview_examples == ()
            receipt.assert_awaited_once()
        else:
            with pytest.raises(ConflictError, match="exact published draft"):
                await call
            receipt.assert_not_awaited()
    insert.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["geometry", "family", "publish", "draft_preview", "published_preview"])
async def test_current_operation_database_mapping_preserves_exact_source_and_outputs(kind):
    changes = {}
    if kind == "family":
        changes = dict(operation_kind=CriticalOperationKind.PRODUCT_FAMILY_WRITE,
            tool_name="ifc_product_family_write", target_geometry_resource_id=None,
            target_product_family_id="family-a", detail=AuthoringDraftWriteOperationDetail(
                root_kind="family", root_id="family-a", expected_generation=1, result_generation=2))
    elif kind == "publish":
        changes = dict(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
            tool_name="ifc_authoring_publish", result_geometry_resource_version_id="geometry-v1",
            detail=AuthoringPublishOperationDetail(root_kind="geometry", root_id="geometry-a",
                expected_drafts=(AuthoringDraftSelection(root_kind="geometry", root_id="geometry-a", generation=2, snapshot=SNAPSHOT),),
                geometry_versions=(AuthoringPublishedGeometryReference(resource_id="geometry-a", resource_version_id="geometry-v1"),)))
    elif kind.endswith("preview"):
        draft = kind == "draft_preview"
        changes = dict(operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION,
            tool_name="ifc_geometry_preview", code_artifact=None, target_geometry_resource_id=None,
            status=CriticalOperationStatus.PREPARED, finished_at=None,
            detail=GeometryInspectionOperationDetail(result_kind="case_report",
                source_type="draft" if draft else "published", source_draft_artifact=SNAPSHOT if draft else None,
                source_kind="geometry_sample", source_id="geometry-a",
                source_version_id=SNAPSHOT.version_id if draft else "geometry-v1", source_content_digest="c" * 64,
                source_version_ref="cur.source", request_artifact=SNAPSHOT))
    original = operation(**changes)
    model = drafts.Operations._operation_model(original)
    session = Session([])
    restored = await drafts.Operations._operation_record(session, model)
    assert restored == original
    if kind == "draft_preview":
        assert model.inspection_source_geometry_version_id is None
        assert restored.detail.source_draft_artifact == SNAPSHOT
    elif kind == "published_preview":
        assert model.inspection_source_geometry_version_id == "geometry-v1"
    session.scalar.assert_not_awaited()


def test_draft_migration_generates_upgrade_sql_without_database():
    buffer = StringIO()
    config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("sqlalchemy.url", "postgresql://offline:offline@localhost/offline")
    command.upgrade(config, "20260929_0099:20260930_0100", sql=True)
    sql = buffer.getvalue()
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "geometry_preview_case_report_v1" in sql
    assert "'gd','fd'" in sql


def test_publication_admission_and_failure_do_not_require_preview_or_outputs():
    from pydantic import ValidationError
    detail = AuthoringPublishOperationDetail(root_kind="geometry",root_id="geometry-a",
        expected_drafts=(AuthoringDraftSelection(root_kind="geometry",root_id="geometry-a",
            generation=2,snapshot=SNAPSHOT),))
    prepared = operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish",detail=detail,status=CriticalOperationStatus.PREPARED,finished_at=None)
    failed = CriticalOperationRecord.model_validate({**prepared.model_dump(),
        "status":"failed","finished_at":NOW,"error_code":"GEOMETRY_BUILD_FAILED",
        "error_message":"Original build failed","error_phase":CriticalOperationErrorPhase.EXECUTION})
    assert failed.detail.preview_examples == () and failed.detail.geometry_versions == ()
    with pytest.raises(ValidationError):
        CriticalOperationRecord.model_validate({**prepared.model_dump(),"status":"succeeded","finished_at":NOW})
    with pytest.raises(ValidationError):
        AuthoringPublishOperationDetail.model_validate({**detail.model_dump(),"evidence":[]})


@pytest.mark.asyncio
async def test_publication_admission_replays_saved_failure_without_rereading_current_draft(monkeypatch):
    detail = AuthoringPublishOperationDetail(root_kind="geometry",root_id="geometry-a",
        expected_drafts=(AuthoringDraftSelection(root_kind="geometry",root_id="geometry-a",
            generation=2,snapshot=SNAPSHOT),))
    prepared = operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish",detail=detail,status=CriticalOperationStatus.PREPARED,finished_at=None)
    failed = CriticalOperationRecord.model_validate({**prepared.model_dump(),"status":"failed",
        "finished_at":NOW,"error_code":"DRAFT_CHANGED","error_message":"Original draft changed",
        "error_phase":CriticalOperationErrorPhase.COMMIT_FENCE})
    repository, session = setup_transaction(monkeypatch, [])
    monkeypatch.setattr(drafts,"find_operation",AsyncMock(return_value=drafts.Operations._operation_model(failed)))
    reread = AsyncMock(side_effect=AssertionError("replay must use saved admission"))
    monkeypatch.setattr(drafts,"load_current_draft_roots",reread)
    selected = AuthoringDraftRecord(root_kind="geometry",root_id="geometry-a",owner_scope=OWNER,
        generation=2,snapshot=SNAPSHOT)
    with bind_tenant_execution_context(scope().identity):
        assert await drafts.begin_publication(repository,scope(),operation=prepared,expected_drafts=(selected,)) == failed
    reread.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_publication_admission_returns_other_run_prepared_root_before_build(monkeypatch):
    detail = AuthoringPublishOperationDetail(root_kind="geometry", root_id="geometry-a",
        expected_drafts=(AuthoringDraftSelection(root_kind="geometry", root_id="geometry-a",
            generation=2, snapshot=SNAPSHOT),))
    incoming = operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish", detail=detail, status=CriticalOperationStatus.PREPARED,
        finished_at=None)
    original = incoming.model_copy(update={"operation_id": "operation-from-other-run",
        "run_id": "run-other", "idempotency_key": "authoring-publish:original"})
    repository, session = setup_transaction(monkeypatch,
        [drafts.Operations._operation_model(original)])
    monkeypatch.setattr(drafts, "find_operation", AsyncMock(return_value=None))
    reread = AsyncMock(side_effect=AssertionError("blocked publication must not reread or insert"))
    monkeypatch.setattr(drafts, "load_current_draft_roots", reread)
    selected = AuthoringDraftRecord(root_kind="geometry", root_id="geometry-a", owner_scope=OWNER,
        generation=2, snapshot=SNAPSHOT)
    with bind_tenant_execution_context(scope().identity):
        admitted = await drafts.begin_publication(repository, scope(), operation=incoming,
            expected_drafts=(selected,))
    assert admitted == original
    assert session.execute.await_count == 1  # The existing target-root advisory lock.
    reread.assert_not_awaited()
    session.add.assert_not_called()


def test_independent_publication_migration_preserves_records_and_reuses_existing_tables():
    buffer=StringIO()
    config=Config(str(Path(__file__).resolve().parents[3]/"alembic.ini"),output_buffer=buffer)
    config.set_main_option("sqlalchemy.url","postgresql://offline:offline@localhost/offline")
    command.upgrade(config,"20260930_0100:20261001_0101",sql=True)
    sql=buffer.getvalue()
    assert "NOT VALID" in sql and "publication admitted request is immutable" in sql
    assert "CREATE TABLE" not in sql and "CREATE INDEX" not in sql
    assert "family_version_id" in sql and "preview_examples" in sql
