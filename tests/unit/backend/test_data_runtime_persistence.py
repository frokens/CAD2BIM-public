from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.control_plane import (
    ArtifactRecord,
    ConflictError,
    ExecutionMode,
    ExecutionProtocolStatus,
)
from CAD2BIM_backend.domain.data_runtime import (
    ArrowColumnRecord,
    ArtifactVersionRef,
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataCatalogEntry,
    DataTransformOperationDetail,
    DataTransformOperationState,
    IfcReadSourceRef,
    IfcSourceChangedError,
    DataPackageMemberRecord,
    DataPackageRecord,
    AuthoringDraftWriteOperationDetail,
    AuthoringPublishOperationDetail,
    AuthoringDraftSelection,
    AuthoringPublishedGeometryReference,
    JsonPayloadKind,
    OpenDirectSessionRequest,
    PackageMemberKind,
    SOURCE_L0_PAYLOAD_NAMES,
    SOURCE_L0_SOURCE_MEMBER_NAME,
    TransformInputBindingRecord,
    TransformOutputRecord,
    normalize_direct_session_name,
    source_l0_payload_member_name,
    validate_package_publication,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.data_transform import InlineTable
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.postgres.models import Base
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    PostgresDataRuntimeRepositoryMixin,
)


def _open_request(name: str = "Gas Map") -> OpenDirectSessionRequest:
    return OpenDirectSessionRequest(
        configured_subject="local-reviewer",
        session_name=name,
        normalized_session_name=normalize_direct_session_name(name),
        owner_id="tenant_local",
        tenant_id="tenant_local",
        principal_id="principal_local",
    )


def _scope(binding, *, mode: ExecutionMode, frontier: int | None):
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id=binding.tenant_id,
            principal_id=binding.principal_id,
            task_id=binding.task_id,
            actor_kind=TenantActorKind.USER,
            command_id="command_data_runtime_persistence",
        ),
        owner_id=binding.owner_id,
        run_id=binding.run_id,
        execution_mode=mode,
        catalog_frontier=frontier,
    )


def _derive_prepared(scope, *, source=None, parent=None, tool="data_derive"):
    return CriticalOperationRecord(
        operation_id="operation_derive", owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id, task_id=scope.identity.task_id, run_id=scope.run_id,
        operation_kind=CriticalOperationKind.DATA_TRANSFORM, tool_name=tool,
        intent_summary="Derive selected products", idempotency_key="derive-request",
        request_fingerprint="a" * 64, status=CriticalOperationStatus.PREPARED,
        code_artifact=ArtifactVersionRef(artifact_id="code", version_id="code-v1", sha256="b" * 64),
        description_artifact=ArtifactVersionRef(artifact_id="description", version_id="description-v1", sha256="c" * 64),
        repair_parent_operation_id=parent,
        detail=DataTransformOperationDetail(engine="python_arrow", ifc_source=source,
            inputs=() if source else (TransformInputBindingRecord(ordinal=1, input_name="rows",
                package_id="input", member_id="input-member", payload_name="rows"),),
            outputs=(TransformOutputRecord(payload_name="selected", description="Selected objects",
                logical_key_fields=("global_id",)),), output_package_name="Selection"),
        started_at=datetime.now(UTC),
    )


def test_derive_ifc_source_uses_one_relational_binding_and_keeps_write_fields_empty():
    source = IfcReadSourceRef(model_id="model", revision_id="revision", revision_no=7)
    scope = SimpleNamespace(owner_id="owner", run_id="run", identity=SimpleNamespace(tenant_id="tenant", task_id="task"))
    operation = _derive_prepared(scope, source=source)
    model = PostgresDataRuntimeRepositoryMixin._operation_model(operation)
    assert (model.source_ifc_model_id, model.source_ifc_revision_id) == ("model", "revision")
    assert "ifc_source" not in model.operation_detail_json
    assert model.base_ifc_revision_id is model.expected_ifc_write_epoch is None
    assert operation.detail.ifc_source == source and operation.detail.inputs == ()
    with pytest.raises(ValidationError, match="IFC source requires Python"):
        DataTransformOperationDetail.model_validate({**operation.detail.model_dump(), "engine": "duckdb_sql"})
    with pytest.raises(ValidationError, match="requires a Package input"):
        DataTransformOperationDetail.model_validate({**operation.detail.model_dump(), "ifc_source": None})


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["data_transform", "data_repair_transform"])
async def test_retired_producer_is_readable_but_cannot_enter_current_execution(tool):
    repository = InMemoryControlPlaneRepository(MemoryState())
    binding = (await repository.open_direct_session(_open_request("Retired producer"))).binding
    scope = _scope(binding, mode=ExecutionMode.DIRECT_MCP, frontier=None)
    old = _derive_prepared(scope, tool=tool, parent="parent" if tool == "data_repair_transform" else None)
    assert CriticalOperationRecord.model_validate(old.model_dump()).tool_name == tool
    with pytest.raises(ValueError, match="exact tool"):
        await repository.prepare_data_transform_operation(scope, old,
            request_identity="old", request_digest=sha256_digest({}), request_payload={},
            resolved_command={}, deadline_at=old.started_at + timedelta(seconds=30))
    with pytest.raises(ValidationError, match="Data Transform Operation"):
        DataTransformOperationState(operation=old, request_identity="old", request_digest="a" * 64,
            request_payload={}, resolved_command={}, deadline_at=old.started_at + timedelta(seconds=30),
            execution_generation=0)


@pytest.mark.asyncio
async def test_ifc_repair_source_check_uses_callers_transaction_and_never_gates_package_only():
    class Repository(PostgresDataRuntimeRepositoryMixin):
        _revision_row = AsyncMock()

    repository = Repository(None)
    source = IfcReadSourceRef(model_id="model", revision_id="revision", revision_no=7)
    scope = SimpleNamespace(owner_id="owner", run_id="run", identity=SimpleNamespace(tenant_id="tenant", task_id="task"))
    session = object()
    operation = _derive_prepared(scope, source=source, parent="original")
    repository._revision_row.return_value = (SimpleNamespace(ifc_model_id="model"),
        SimpleNamespace(revision_id="revision-next", revision_no=8), None, None)
    with pytest.raises(IfcSourceChangedError) as changed:
        await repository._require_ifc_read_source(session, scope, operation, publication=True)
    assert changed.value.diagnostic == {"expected_revision_no": 7, "actual_revision_no": 8}
    assert repository._revision_row.await_args.args[0] is session
    assert repository._revision_row.await_args.kwargs == {
        "current": True, "revision_id": None, "lock_model": True}
    repository._revision_row.reset_mock()
    await repository._require_ifc_read_source(session, scope, _derive_prepared(scope), publication=True)
    await repository._require_ifc_read_source(session, scope, _derive_prepared(scope, source=source), publication=True)
    repository._revision_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_artifact_repository_filters_exact_reads_by_optional_run() -> None:
    state = MemoryState()
    repository = InMemoryControlPlaneRepository(state)
    artifact = ArtifactRecord(
        artifact_id="artifact_exact",
        version_id="version_exact",
        owner_id="tenant_local",
        task_id="task_local",
        run_id="run_expected",
        kind="data_transform_code",
        filename="transform.sql",
        content_type="application/sql",
        size_bytes=8,
        sha256="a" * 64,
        idempotency_key="artifact-exact",
        created_at=datetime.now(UTC),
    )
    state.artifacts[artifact.artifact_id] = artifact

    assert (
        await repository.get_artifact(
            artifact.owner_id,
            artifact.artifact_id,
            run_id=artifact.run_id,
        )
        == artifact
    )
    assert (
        await repository.get_artifact(
            artifact.owner_id,
            artifact.artifact_id,
            run_id="run_other",
        )
        is None
    )
    assert (
        await repository.get_artifact_version(
            artifact.owner_id,
            artifact.artifact_id,
            artifact.version_id,
            run_id=artifact.run_id,
        )
        == artifact
    )
    assert (
        await repository.get_artifact_version(
            artifact.owner_id,
            artifact.artifact_id,
            artifact.version_id,
            run_id="run_other",
        )
        is None
    )


@pytest.mark.asyncio
async def test_postgres_terminal_operation_catalog_filters_before_keyset() -> None:
    captured: dict[str, object] = {}

    class CapturingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalars(self, statement):
            captured["statement"] = statement
            return SimpleNamespace(all=lambda: [])

    repository = PostgresDataRuntimeRepositoryMixin(lambda: CapturingSession())
    repository._require_data_run = AsyncMock()  # type: ignore[method-assign]
    scope = InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant_local",
            principal_id="principal_local",
            task_id="task_local",
            actor_kind=TenantActorKind.USER,
            command_id="command_terminal_operation_catalog",
        ),
        owner_id="tenant_local",
        run_id="run_local",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=31,
    )

    page = await repository.list_terminal_operation_catalog(
        scope,
        tool_names=(
            "data_transform",
            "data_repair_transform",
            "cad_compute_measurements",
        ),
        statuses=(
            CriticalOperationStatus.SUCCEEDED,
            CriticalOperationStatus.FAILED,
        ),
        require_code_artifact=True,
        before_sequence=23,
        page_size=7,
    )

    statement = captured["statement"]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "authoring_draft_write" in compiled.params.values()
    assert "system_copy" in compiled.params.values()
    tool_filter = sql.index("critical_operations.tool_name IN")
    status_filter = sql.index("critical_operations.status IN")
    code_filter = sql.index("critical_operations.code_artifact_id IS NOT NULL")
    keyset_filter = sql.index("data_catalog_entries.ordinal <")
    assert "JOIN critical_operations" in sql
    assert ["data_transform", "data_repair_transform", "cad_compute_measurements"] in compiled.params.values()
    assert "critical_operations.code_artifact_version_id IS NOT NULL" in sql
    assert "critical_operations.code_artifact_sha256 IS NOT NULL" in sql
    assert code_filter < keyset_filter
    assert ["succeeded", "failed"] in compiled.params.values()
    assert tool_filter < keyset_filter
    assert status_filter < keyset_filter
    assert compiled.params["ordinal_1"] == 23
    assert compiled.params["catalog_revision_1"] == 31
    assert "ORDER BY data_catalog_entries.ordinal DESC" in sql
    limit_key = sql.rsplit("LIMIT %(", 1)[1].split(")s", 1)[0]
    assert compiled.params[limit_key] == 8
    assert page.entries == ()
    assert page.complete is True
    assert page.next_before_sequence is None


@pytest.mark.asyncio
async def test_postgres_report_producer_keeps_terminal_contract_scope_and_frontier() -> None:
    captured = {}
    candidate, published = object(), object()

    class CapturingSession:
        execute = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, statement):
            captured["statement"] = statement
            return candidate

    session = CapturingSession()
    repository = PostgresDataRuntimeRepositoryMixin(lambda: session)
    repository._require_data_run = AsyncMock()
    repository._published_operation = AsyncMock(return_value=published)
    scope = InvocationScope(identity=TenantExecutionContext(tenant_id="tenant_local",
        principal_id="principal_local", task_id="task_local", actor_kind=TenantActorKind.USER,
        command_id="command_report_producer"), owner_id="tenant_local", run_id="run_local",
        execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=31)

    assert await repository.get_operation_for_result_package(scope, "package_report") is published
    compiled = captured["statement"].compile(dialect=postgresql.dialect())
    statement = str(compiled)
    assert ["succeeded", "failed"] in compiled.params.values()
    for field, expected in (("owner_id", scope.owner_id), ("tenant_id", scope.identity.tenant_id),
                            ("task_id", scope.identity.task_id), ("run_id", scope.run_id),
                            ("result_package_id", "package_report")):
        assert f"critical_operations.{field} =" in statement
        assert expected in compiled.params.values()
    # Reuse the current-contract predicate, including exact Apply input binding;
    # broad terminal status alone must not resurrect obsolete persisted details.
    for marker in ("ifc_products_apply", "family_version_id", "input_binding", "member_id",
                   "package_id", "payload_name"):
        assert marker in compiled.params.values()
    repository._require_data_run.assert_awaited_once_with(session, scope, lock=False)
    repository._published_operation.assert_awaited_once_with(session, candidate,
        run_id=scope.run_id, frontier=31)


@pytest.mark.asyncio
async def test_memory_failed_apply_report_producer_requires_catalog_and_frontier() -> None:
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection

    state = MemoryState()
    repository = InMemoryControlPlaneRepository(state)
    binding = (await repository.open_direct_session(_open_request("Failed Apply report"))).binding
    scope = _scope(binding, mode=ExecutionMode.DIRECT_MCP, frontier=None)
    original = _apply_inspection("rejected").operation
    identity = {"owner_id": binding.owner_id, "tenant_id": binding.tenant_id,
        "task_id": binding.task_id, "run_id": binding.run_id}
    operation = CriticalOperationRecord.model_validate({**original.operation.model_dump(), **identity})
    entry = DataCatalogEntry.model_validate({**original.catalog_entry.model_dump(), **identity,
        "catalog_revision": 2})
    state.critical_operations[operation.operation_id] = operation
    assert await repository.get_operation_for_result_package(scope, operation.result_package_id) is None
    state.data_catalog_entries[binding.run_id] = [entry]
    found = await repository.get_operation_for_result_package(scope, operation.result_package_id)
    assert found.operation == operation and found.catalog_entry == entry
    assert await repository.get_operation_for_result_package(
        scope.model_copy(update={"catalog_frontier": 1}), operation.result_package_id) is None
    assert await repository.get_operation_for_result_package(scope, "another_report") is None
    for field in identity:
        state.critical_operations[operation.operation_id] = operation.model_copy(update={field: "foreign_scope"})
        assert await repository.get_operation_for_result_package(scope, operation.result_package_id) is None


@pytest.mark.parametrize("invalid_status", [CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE])
def test_nonterminal_operation_cannot_be_a_report_producer(invalid_status) -> None:
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection

    operation = _apply_inspection("rejected").operation.operation
    with pytest.raises(ValidationError):
        CriticalOperationRecord.model_validate({**operation.model_dump(), "status": invalid_status})


def test_failed_non_apply_operation_cannot_be_a_package_producer() -> None:
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection

    operation = _apply_inspection("rejected").operation.operation
    with pytest.raises(ValidationError, match="failed operation cannot contain a result"):
        CriticalOperationRecord.model_validate({**operation.model_dump(),
            "operation_kind": CriticalOperationKind.DATA_TRANSFORM})


@pytest.mark.asyncio
async def test_memory_terminal_operation_catalog_filters_code_before_page_limit() -> None:
    state = MemoryState()
    repository = InMemoryControlPlaneRepository(state)
    opened = await repository.open_direct_session(_open_request("Code filter"))
    binding = opened.binding
    scope = _scope(binding, mode=ExecutionMode.DIRECT_MCP, frontier=None)
    now = datetime.now(UTC)
    source = CriticalOperationRecord(
        operation_id="operation_geometry_source",
        owner_id=binding.owner_id,
        tenant_id=binding.tenant_id,
        task_id=binding.task_id,
        run_id=binding.run_id,
        operation_kind=CriticalOperationKind.GEOMETRY_RESOURCE_WRITE,
        tool_name="ifc_geometry_resource_write",
        intent_summary="Create one geometry resource.",
        idempotency_key="geometry-source",
        request_fingerprint="a" * 64,
        status=CriticalOperationStatus.SUCCEEDED,
        code_artifact=ArtifactVersionRef(
            artifact_id="artifact_geometry_source",
            version_id="version_geometry_source",
            sha256="b" * 64,
        ),
        target_geometry_resource_id="geometry_resource",
        detail=AuthoringDraftWriteOperationDetail(root_kind="geometry", root_id="geometry_resource",
            expected_generation=None, result_generation=1),
        started_at=now,
        finished_at=now,
    )
    publication = CriticalOperationRecord(
        operation_id="operation_geometry_publication",
        owner_id=binding.owner_id,
        tenant_id=binding.tenant_id,
        task_id=binding.task_id,
        run_id=binding.run_id,
        operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish",
        intent_summary="Replace exact fixed-model interfaces.",
        idempotency_key="geometry-publication",
        request_fingerprint="c" * 64,
        status=CriticalOperationStatus.SUCCEEDED,
        target_geometry_resource_id="geometry_resource",
        result_geometry_resource_version_id="geometry_version_publication",
        detail=AuthoringPublishOperationDetail(root_kind="geometry", root_id="geometry_resource",
            expected_drafts=(AuthoringDraftSelection(root_kind="geometry", root_id="geometry_resource",
                generation=1, snapshot=source.code_artifact),),
            preview_examples=(),
            geometry_versions=(AuthoringPublishedGeometryReference(resource_id="geometry_resource",
                resource_version_id="geometry_version_publication"),)),
        started_at=now,
        finished_at=now,
    )
    state.critical_operations.update(
        {source.operation_id: source, publication.operation_id: publication}
    )
    state.data_catalog_entries[binding.run_id] = [
        DataCatalogEntry(
            catalog_entry_id="catalog_geometry_source",
            owner_id=binding.owner_id,
            tenant_id=binding.tenant_id,
            task_id=binding.task_id,
            run_id=binding.run_id,
            resource_kind=CatalogResourceKind.OPERATION,
            alias="op_001",
            ordinal=1,
            catalog_revision=1,
            operation_id=source.operation_id,
            created_at=now,
        ),
        DataCatalogEntry(
            catalog_entry_id="catalog_geometry_publication",
            owner_id=binding.owner_id,
            tenant_id=binding.tenant_id,
            task_id=binding.task_id,
            run_id=binding.run_id,
            resource_kind=CatalogResourceKind.OPERATION,
            alias="op_002",
            ordinal=2,
            catalog_revision=2,
            operation_id=publication.operation_id,
            created_at=now,
        ),
    ]

    page = await repository.list_terminal_operation_catalog(
        scope,
        tool_names=("ifc_geometry_resource_write",),
        statuses=(CriticalOperationStatus.SUCCEEDED,),
        require_code_artifact=True,
        page_size=1,
    )

    assert [entry.alias for entry in page.entries] == ["op_001"]
    assert page.complete is True
    assert page.next_before_sequence is None


def test_direct_session_name_normalization_is_small_and_not_a_path() -> None:
    assert normalize_direct_session_name("  Ｇａｓ  MAP ") == "gas map"
    with pytest.raises(ValueError, match="path separators"):
        normalize_direct_session_name("gas/map")
    with pytest.raises(ValueError, match="control characters"):
        normalize_direct_session_name("gas\nmap")
    with pytest.raises(ValueError, match="100"):
        normalize_direct_session_name("x" * 101)


def test_m2a_schema_and_migration_backfill_legacy_source_projections() -> None:
    packages = Base.metadata.tables["data_packages"]
    operations = Base.metadata.tables["critical_operations"]
    assert packages.c.description_title.nullable is False
    assert packages.c.section_titles_json.nullable is False
    assert operations.c.operation_detail_json.nullable is False

    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim"
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260821_0059:20260822_0060", sql=True)
    sql = " ".join(output.getvalue().split())

    assert "description_title = name" in sql
    assert all(
        title in sql
        for title in (
            "Summary",
            "Intended use",
            "Contents",
            "Method",
            "Scope and completeness",
            "Known limitations",
        )
    )
    assert "ALTER TABLE data_packages ALTER COLUMN description_title SET NOT NULL" in sql
    assert "source_member.member_name = 'source/original.dxf'" in sql
    assert "source_version.size_bytes" in sql
    assert "source_version.artifact_id = source_member.artifact_id" in sql
    assert "source_version.version_id = source_member.artifact_version_id" in sql
    assert "source_version.sha256 = source_member.artifact_sha256" in sql
    assert "source_version.size_bytes > 0" in sql
    assert "operation.status = 'succeeded'" in sql
    assert "'size_bytes', NULL" in sql
    assert "WHERE operation.status = 'failed'" in sql


def test_m2c_json_migration_renders_exact_member_contracts() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim"
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260822_0060:20260823_0061", sql=True)
    sql = " ".join(output.getvalue().split())

    assert "ADD COLUMN json_kind VARCHAR(16)" in sql
    assert "ck_data_package_members_json_kind_valid" in sql
    assert "ck_data_package_members_payload_format_metadata_valid" in sql
    assert "code/transformation.py" in sql
    assert "text/x-python" in sql
    assert "payloads/%.json" in sql
    assert "schemas/%.schema.json" in sql
    assert "application/schema+json" in sql


def test_0063_repair_persistence_migration_is_exact_and_backfills_descriptions() -> None:
    operations = Base.metadata.tables["critical_operations"]
    assert operations.c.description_artifact_id.nullable is True
    assert operations.c.description_artifact_version_id.nullable is True
    assert operations.c.description_artifact_sha256.nullable is True
    assert operations.c.repair_parent_operation_id.nullable is True

    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim"
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260824_0062:20260828_0063", sql=True)
    sql = " ".join(output.getvalue().split())

    assert "ADD COLUMN description_artifact_id VARCHAR(80)" in sql
    assert "ADD COLUMN repair_parent_operation_id VARCHAR(80)" in sql
    assert "member.member_kind = 'description'" in sql
    assert "member.member_name = 'description.md'" in sql
    assert "operation.status = 'succeeded'" in sql
    assert "data_repair_transform" in sql
    assert "code_validation" in sql
    assert "fk_critical_operations_exact_description_artifact_version" in sql
    assert "fk_critical_operations_repair_parent" in sql


@pytest.mark.asyncio
async def test_same_subject_and_normalized_name_resume_the_same_direct_session() -> None:
    repository = InMemoryControlPlaneRepository()

    opened = await repository.open_direct_session(_open_request("Gas Map"))
    resumed = await repository.open_direct_session(_open_request("  GAS MAP "))

    assert opened.resumed is False
    assert opened.binding.direct_session_alias == "direct_001"
    assert opened.binding.session_name == "Gas Map"
    assert resumed.resumed is True
    assert resumed.binding == opened.binding
    run = await repository.get_run(opened.binding.owner_id, opened.binding.run_id)
    assert run is not None and run.execution_mode is ExecutionMode.DIRECT_MCP
    assert await repository.claim_next_run("runner-1") is None


@pytest.mark.asyncio
async def test_import_publication_is_atomic_and_response_loss_replays_aliases() -> None:
    state = MemoryState()
    repository = InMemoryControlPlaneRepository(state)
    opened = await repository.open_direct_session(_open_request())
    binding = opened.binding
    direct_scope = _scope(binding, mode=ExecutionMode.DIRECT_MCP, frontier=None)
    now = datetime.now(UTC)
    fingerprint = "c" * 64
    package = DataPackageRecord(
        package_id="package_gas_l0",
        owner_id=binding.owner_id,
        tenant_id=binding.tenant_id,
        task_id=binding.task_id,
        created_by_run_id=binding.run_id,
        name="gas_full_map_l0",
        package_kind="source_l0",
        source_filename="gas.dxf",
        summary="Complete L0 extraction of gas.dxf.",
        idempotency_key="package-import-1",
        request_fingerprint=fingerprint,
        created_at=now,
    )
    member_contracts = (
        (
            SOURCE_L0_SOURCE_MEMBER_NAME,
            PackageMemberKind.SOURCE,
            "application/dxf",
            None,
        ),
        ("manifest.json", PackageMemberKind.MANIFEST, "application/json", None),
        ("description.md", PackageMemberKind.DESCRIPTION, "text/markdown", None),
        *(
            (
                source_l0_payload_member_name(name),
                PackageMemberKind.PAYLOAD,
                "application/vnd.apache.parquet",
                0,
            )
            for name in sorted(SOURCE_L0_PAYLOAD_NAMES)
        ),
    )
    members: list[DataPackageMemberRecord] = []
    for index, (name, kind, media_type, row_count) in enumerate(member_contracts, start=1):
        artifact = ArtifactRecord(
            artifact_id=f"artifact_{index}",
            version_id=f"version_{index}",
            owner_id=binding.owner_id,
            task_id=binding.task_id,
            run_id=binding.run_id,
            kind="data_package_member",
            filename=name,
            content_type=media_type,
            size_bytes=2,
            sha256=f"{index:064x}",
            idempotency_key=f"artifact-{index}",
            created_at=now,
        )
        state.artifacts[artifact.artifact_id] = artifact
        members.append(
            DataPackageMemberRecord(
                member_id=f"member_{index}",
                owner_id=binding.owner_id,
                tenant_id=binding.tenant_id,
                task_id=binding.task_id,
                package_id=package.package_id,
                member_name=name,
                member_kind=kind,
                artifact=ArtifactVersionRef(
                    artifact_id=artifact.artifact_id,
                    version_id=artifact.version_id,
                    sha256=artifact.sha256,
                ),
                media_type=media_type,
                row_count=row_count,
                created_at=now,
            )
        )
    operation = CriticalOperationRecord(
        operation_id="operation_import",
        owner_id=binding.owner_id,
        tenant_id=binding.tenant_id,
        task_id=binding.task_id,
        run_id=binding.run_id,
        tool_name="cad_import_source",
        source_filename="gas.dxf",
        idempotency_key="import-1",
        request_fingerprint=fingerprint,
        status=CriticalOperationStatus.SUCCEEDED,
        result_package_id=package.package_id,
        started_at=now,
        finished_at=now,
    )

    published = await repository.publish_import_result(
        direct_scope,
        package,
        members,
        operation,
    )
    replayed = await repository.publish_import_result(
        direct_scope,
        package,
        members,
        operation,
    )

    assert published.package.catalog_entry.alias == "data_001"
    assert published.operation.catalog_entry.alias == "op_001"
    assert published.package.catalog_entry.catalog_revision == 1
    assert published.operation.catalog_entry.catalog_revision == 1
    assert replayed == published
    assert tuple(
        item.alias
        for item in await repository.list_catalog(
            direct_scope,
        )
    ) == ("data_001", "op_001")
    operation_replay = await repository.get_operation_replay(
        direct_scope,
        tool_names=("cad_import_source",),
        request_fingerprint=fingerprint,
    )
    assert operation_replay == published.operation

    with pytest.raises(ValueError, match="six payloads"):
        await repository.publish_import_result(
            direct_scope,
            package,
            members[:-1],
            operation,
        )
    invalid_source_media = tuple(
        member.model_copy(update={"media_type": "application/octet-stream"})
        if member.member_name == SOURCE_L0_SOURCE_MEMBER_NAME
        else member
        for member in members
    )
    with pytest.raises(ValueError, match="source/original.dxf"):
        await repository.publish_import_result(
            direct_scope,
            package,
            invalid_source_media,
            operation,
        )
    with pytest.raises(ValueError, match="fingerprints must match"):
        await repository.publish_import_result(
            direct_scope,
            package.model_copy(update={"request_fingerprint": "d" * 64}),
            members,
            operation,
        )

    second_package = package.model_copy(
        update={
            "package_id": "package_gas_l0_second",
            "idempotency_key": "package-import-2",
            "request_fingerprint": "e" * 64,
        }
    )
    second_members = []
    for index, member in enumerate(members, start=10):
        artifact = ArtifactRecord(
            artifact_id=f"artifact_{index}",
            version_id=f"version_{index}",
            owner_id=binding.owner_id,
            task_id=binding.task_id,
            run_id=binding.run_id,
            kind="data_package_member",
            filename=member.member_name,
            content_type=member.media_type,
            size_bytes=2,
            sha256=f"{index:064x}",
            idempotency_key=f"artifact-{index}",
            created_at=now,
        )
        state.artifacts[artifact.artifact_id] = artifact
        second_members.append(
            member.model_copy(
                update={
                    "member_id": f"member_{index}",
                    "package_id": second_package.package_id,
                    "artifact": ArtifactVersionRef(
                        artifact_id=artifact.artifact_id,
                        version_id=artifact.version_id,
                        sha256=artifact.sha256,
                    ),
                }
            )
        )
    second_operation = operation.model_copy(
        update={
            "operation_id": "operation_import_second",
            "idempotency_key": "import-2",
            "request_fingerprint": "e" * 64,
            "result_package_id": second_package.package_id,
        }
    )
    second = await repository.publish_import_result(
        direct_scope,
        second_package,
        second_members,
        second_operation,
    )
    assert second.package.catalog_entry.catalog_revision == 2
    assert second.operation.catalog_entry.catalog_revision == 2

    entities_member = next(
        member
        for member in members
        if member.member_name == source_l0_payload_member_name("entities")
    )
    derived_fingerprint = "a" * 64
    derived_package = DataPackageRecord(
        package_id="package_layer_counts",
        owner_id=binding.owner_id,
        tenant_id=binding.tenant_id,
        task_id=binding.task_id,
        created_by_run_id=binding.run_id,
        name="layer_entity_counts",
        package_kind="derived",
        description_title="Entity counts by layer",
        summary="Counts CAD entities grouped by their source layer.",
        section_titles=("Summary", "Columns"),
        idempotency_key="package-transform-1",
        request_fingerprint=derived_fingerprint,
        created_at=now,
    )
    derived_contracts = (
        ("manifest.json", PackageMemberKind.MANIFEST, "application/json"),
        ("description.md", PackageMemberKind.DESCRIPTION, "text/markdown"),
        ("code/transformation.sql", PackageMemberKind.CODE, "application/sql"),
        (
            "payloads/layer_counts/part-00000.parquet",
            PackageMemberKind.PAYLOAD,
            "application/vnd.apache.parquet",
        ),
    )
    derived_members: list[DataPackageMemberRecord] = []
    for index, (name, kind, media_type) in enumerate(derived_contracts, start=100):
        artifact = ArtifactRecord(
            artifact_id=f"artifact_{index}",
            version_id=f"version_{index}",
            owner_id=binding.owner_id,
            task_id=binding.task_id,
            run_id=binding.run_id,
            kind="data_package_member",
            filename=name,
            content_type=media_type,
            size_bytes=64,
            sha256=f"{index:064x}",
            idempotency_key=f"artifact-{index}",
            created_at=now,
        )
        state.artifacts[artifact.artifact_id] = artifact
        is_payload = kind is PackageMemberKind.PAYLOAD
        derived_members.append(
            DataPackageMemberRecord(
                member_id=f"member_{index}",
                owner_id=binding.owner_id,
                tenant_id=binding.tenant_id,
                task_id=binding.task_id,
                package_id=derived_package.package_id,
                member_name=name,
                member_kind=kind,
                artifact=ArtifactVersionRef(
                    artifact_id=artifact.artifact_id,
                    version_id=artifact.version_id,
                    sha256=artifact.sha256,
                ),
                media_type=media_type,
                row_count=2 if is_payload else None,
                logical_payload_name="layer_counts" if is_payload else None,
                payload_description="Entity count for each CAD layer." if is_payload else None,
                logical_key_fields=("layer",) if is_payload else (),
                arrow_schema=(
                    (
                        ArrowColumnRecord(name="layer", type="string", nullable=False),
                        ArrowColumnRecord(name="entity_count", type="int64", nullable=False),
                    )
                    if is_payload
                    else ()
                ),
                logical_byte_count=64 if is_payload else None,
                created_at=now,
            )
        )
    code_member = next(
        member for member in derived_members if member.member_kind is PackageMemberKind.CODE
    )
    description_member = next(
        member for member in derived_members if member.member_kind is PackageMemberKind.DESCRIPTION
    )
    derived_operation = CriticalOperationRecord(
        operation_id="operation_transform",
        owner_id=binding.owner_id,
        tenant_id=binding.tenant_id,
        task_id=binding.task_id,
        run_id=binding.run_id,
        operation_kind=CriticalOperationKind.DATA_TRANSFORM,
        tool_name="data_derive",
        intent_summary=(f"{derived_package.description_title} — {derived_package.summary}"),
        idempotency_key="transform-1",
        request_fingerprint=derived_fingerprint,
        status=CriticalOperationStatus.SUCCEEDED,
        result_package_id=derived_package.package_id,
        code_artifact=code_member.artifact,
        description_artifact=description_member.artifact,
        detail=DataTransformOperationDetail(
            inputs=(
                TransformInputBindingRecord(
                    ordinal=1,
                    input_name="entities_input",
                    package_id=package.package_id,
                    member_id=entities_member.member_id,
                    payload_name="entities",
                ),
            ),
            outputs=(
                TransformOutputRecord(
                    payload_name="layer_counts",
                    description="Entity count for each CAD layer.",
                    logical_key_fields=("layer",),
                ),
            ),
            output_package_name=derived_package.name,
        ),
        started_at=now,
        finished_at=now,
    )
    declared_transform = derived_operation.detail.model_dump(mode="json")
    await repository.prepare_data_transform_operation(
        direct_scope,
        derived_operation.model_copy(update={
            "status": CriticalOperationStatus.PREPARED,
            "result_package_id": None,
            "finished_at": None,
        }),
        request_identity=derived_operation.idempotency_key,
        request_digest=sha256_digest(declared_transform),
        request_payload=declared_transform,
        resolved_command=declared_transform,
        deadline_at=now + timedelta(seconds=30),
    )
    derived_claim = await repository.claim_data_transform_execution(
        direct_scope,
        derived_operation.operation_id,
        owner="data-runtime-persistence-fixture",
        lease_seconds=30,
    )
    assert derived_claim is not None
    await repository.stage_data_transform_candidate(
        direct_scope,
        derived_operation.operation_id,
        derived_package,
        derived_members,
        execution_claim=derived_claim,
    )
    derived = await repository.publish_package_result(
        direct_scope,
        derived_package,
        derived_members,
        derived_operation,
        execution_claim=derived_claim,
    )
    assert derived.package.catalog_entry.alias == "data_003"
    assert derived.operation.catalog_entry.alias == "op_003"
    assert derived.package.members[-1].logical_payload_name == "layer_counts"

    failed_operation = operation.model_copy(
        update={
            "operation_id": "operation_failed_third",
            "idempotency_key": "import-3",
            "request_fingerprint": "f" * 64,
            "status": CriticalOperationStatus.FAILED,
            "result_package_id": None,
            "error_code": "MALFORMED_ASCII_DXF",
            "error_message": "The source is malformed.",
        }
    )
    failed = await repository.record_failed_operation(direct_scope, failed_operation)
    failed_replay = await repository.record_failed_operation(
        direct_scope,
        failed_operation,
    )
    assert failed.catalog_entry.catalog_revision == 4
    assert failed_replay == failed
    revision_one_scope = direct_scope.model_copy(update={"catalog_frontier": 1})
    assert (
        await repository.get_operation_replay(
            revision_one_scope,
            tool_names=(second_operation.tool_name,),
            request_fingerprint=second_operation.request_fingerprint,
        )
        == second.operation
    )
    assert (
        await repository.get_operation_replay(
            revision_one_scope,
            tool_names=(failed_operation.tool_name,),
            request_fingerprint=failed_operation.request_fingerprint,
        )
        == failed
    )
    package_page = await repository.list_package_catalog(direct_scope, page_size=2)
    assert tuple(entry.alias for entry in package_page.entries) == ("data_003", "data_002")
    assert package_page.complete is False
    assert package_page.next_before_sequence == 2
    final_package_page = await repository.list_package_catalog(
        direct_scope,
        before_sequence=package_page.next_before_sequence,
        page_size=2,
    )
    assert tuple(entry.alias for entry in final_package_page.entries) == ("data_001",)
    assert final_package_page.complete is True
    operation_page = await repository.list_operation_catalog(direct_scope, page_size=2)
    assert tuple(entry.alias for entry in operation_page.entries) == ("op_004", "op_003")
    assert operation_page.complete is False
    assert operation_page.next_before_sequence == 3
    final_operation_page = await repository.list_operation_catalog(
        direct_scope,
        before_sequence=operation_page.next_before_sequence,
        page_size=2,
    )
    assert tuple(entry.alias for entry in final_operation_page.entries) == ("op_002", "op_001")
    assert final_operation_page.complete is True
    transform_page = await repository.list_operation_catalog(
        direct_scope,
        page_size=1,
        operation_kind=CriticalOperationKind.DATA_TRANSFORM,
    )
    assert tuple(entry.alias for entry in transform_page.entries) == ("op_003",)
    assert transform_page.complete is True
    assert transform_page.next_before_sequence is None
    source_page = await repository.list_operation_catalog(
        direct_scope,
        page_size=1,
        operation_kind=CriticalOperationKind.SOURCE_INGESTION,
    )
    assert tuple(entry.alias for entry in source_page.entries) == ("op_004",)
    assert source_page.complete is False
    assert source_page.next_before_sequence == 4
    source_next = await repository.list_operation_catalog(
        direct_scope,
        before_sequence=source_page.next_before_sequence,
        page_size=1,
        operation_kind=CriticalOperationKind.SOURCE_INGESTION,
    )
    assert tuple(entry.alias for entry in source_next.entries) == ("op_002",)
    assert source_next.complete is False
    assert source_next.next_before_sequence == 2
    assert (
        await repository.get_catalog_entry_by_alias(
            direct_scope,
            CatalogResourceKind.OPERATION,
            "op_003",
        )
        == derived.operation.catalog_entry
    )
    assert tuple(entry.alias for entry in await repository.list_catalog(revision_one_scope)) == (
        "data_001",
        "op_001",
    )
    assert tuple(
        (entry.alias, entry.catalog_revision)
        for entry in await repository.list_catalog(direct_scope)
    ) == (
        ("data_001", 1),
        ("op_001", 1),
        ("data_002", 2),
        ("op_002", 2),
        ("data_003", 3),
        ("op_003", 3),
        ("op_004", 4),
    )

    graph_run = state.runs[binding.run_id].model_copy(
        update={
            "execution_mode": ExecutionMode.GRAPH,
            "execution_protocol_status": ExecutionProtocolStatus.QUARANTINED,
            "execution_protocol_quarantine_reason": "test.graph",
        }
    )
    state.runs[binding.run_id] = graph_run
    graph_scope = _scope(binding, mode=ExecutionMode.GRAPH, frontier=None)
    assert len(await repository.list_catalog(graph_scope)) == 7
    with pytest.raises(ConflictError, match="(?i)execution mode|scope|run"):
        await repository.list_catalog(direct_scope)


def test_successful_operation_cannot_carry_an_error() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="cannot contain an error"):
        CriticalOperationRecord(
            operation_id="operation_bad",
            owner_id="tenant_local",
            tenant_id="tenant_local",
            task_id="task_1",
            run_id="run_1",
            tool_name="cad_import_source",
            source_filename="gas.dxf",
            idempotency_key="bad",
            request_fingerprint="d" * 64,
            status=CriticalOperationStatus.SUCCEEDED,
            error_code="UNEXPECTED",
            started_at=now,
            finished_at=now,
        )


def test_m2c_mixed_package_contract_and_repository_mapping_are_exact() -> None:
    now = datetime.now(UTC)
    fingerprint = "9" * 64
    package = DataPackageRecord(
        package_id="package_mixed",
        owner_id="tenant_local",
        tenant_id="tenant_local",
        task_id="task_1",
        created_by_run_id="run_1",
        name="mixed_outputs",
        package_kind="derived",
        description_title="Mixed outputs",
        summary="One tabular result and one hierarchical summary.",
        section_titles=("Summary", "Contents"),
        idempotency_key="package-mixed",
        request_fingerprint=fingerprint,
        created_at=now,
    )

    def member(
        index: int,
        name: str,
        kind: PackageMemberKind,
        media_type: str,
        **metadata,
    ) -> DataPackageMemberRecord:
        return DataPackageMemberRecord(
            member_id=f"member_{index}",
            owner_id=package.owner_id,
            tenant_id=package.tenant_id,
            task_id=package.task_id,
            package_id=package.package_id,
            member_name=name,
            member_kind=kind,
            artifact=ArtifactVersionRef(
                artifact_id=f"artifact_{index}",
                version_id=f"version_{index}",
                sha256=f"{index:064x}",
            ),
            media_type=media_type,
            created_at=now,
            **metadata,
        )

    members = (
        member(1, "manifest.json", PackageMemberKind.MANIFEST, "application/json"),
        member(2, "description.md", PackageMemberKind.DESCRIPTION, "text/markdown"),
        member(3, "code/transformation.py", PackageMemberKind.CODE, "text/x-python"),
        member(
            4,
            "payloads/counts/part-00000.parquet",
            PackageMemberKind.PAYLOAD,
            "application/vnd.apache.parquet",
            row_count=1,
            logical_payload_name="counts",
            payload_description="Tabular counts.",
            logical_key_fields=("name",),
            arrow_schema=(ArrowColumnRecord(name="name", type="string", nullable=False),),
            logical_byte_count=32,
        ),
        member(
            5,
            "payloads/summary.json",
            PackageMemberKind.PAYLOAD,
            "application/json",
            logical_payload_name="summary",
            payload_description="Hierarchical summary.",
            logical_byte_count=48,
            json_kind=JsonPayloadKind.DOCUMENT,
        ),
        member(
            6,
            "schemas/summary.schema.json",
            PackageMemberKind.SCHEMA,
            "application/schema+json",
        ),
    )
    inline = InlineTable.model_validate(
        {
            "input_name": "thresholds",
            "columns": [{"name": "minimum", "type": "integer", "nullable": False}],
            "rows": [{"values": [{"kind": "integer", "value": 2}]}],
        }
    )
    operation = CriticalOperationRecord(
        operation_id="operation_mixed",
        owner_id=package.owner_id,
        tenant_id=package.tenant_id,
        task_id=package.task_id,
        run_id=package.created_by_run_id,
        operation_kind=CriticalOperationKind.DATA_TRANSFORM,
        tool_name="data_derive",
        intent_summary=f"{package.description_title} — {package.summary}",
        idempotency_key="operation-mixed",
        request_fingerprint=fingerprint,
        status=CriticalOperationStatus.SUCCEEDED,
        result_package_id=package.package_id,
        code_artifact=members[2].artifact,
        description_artifact=members[1].artifact,
        detail=DataTransformOperationDetail(
            engine="python_arrow",
            inputs=(
                TransformInputBindingRecord(
                    ordinal=1,
                    input_name="source",
                    package_id="package_source",
                    member_id="member_source",
                    payload_name="entities",
                ),
            ),
            inline_tables=(inline,),
            outputs=(
                TransformOutputRecord(
                    payload_name="counts",
                    description="Tabular counts.",
                    logical_key_fields=("name",),
                ),
                TransformOutputRecord(
                    payload_name="summary",
                    format="json",
                    description="Hierarchical summary.",
                    json_kind=JsonPayloadKind.DOCUMENT,
                    schema_json='{"type":"object"}',
                ),
            ),
            output_package_name=package.name,
        ),
        started_at=now,
        finished_at=now,
    )

    validate_package_publication(package, members, operation)
    assert operation.detail.outputs[1].schema_json == '{"type":"object"}'
    operation_model = PostgresDataRuntimeRepositoryMixin._operation_model(operation)
    assert operation_model.operation_detail_json["engine"] == "python_arrow"
    assert operation_model.operation_detail_json["inline_tables"][0]["input_name"] == "thresholds"
    assert operation_model.operation_detail_json["outputs"][1]["schema_json"] == (
        '{"type":"object"}'
    )
    assert operation_model.description_artifact_id == members[1].artifact.artifact_id
    json_model = PostgresDataRuntimeRepositoryMixin._member_model(members[4])
    assert json_model.json_kind == "document"
    assert PostgresDataRuntimeRepositoryMixin._member_record(json_model) == members[4]

    with pytest.raises(ValueError, match="1..16 declared payloads"):
        validate_package_publication(package, members[:-1], operation)
    extra_parquet_schema = member(
        7,
        "schemas/counts.schema.json",
        PackageMemberKind.SCHEMA,
        "application/schema+json",
    )
    with pytest.raises(ValueError, match="1..16 declared payloads"):
        validate_package_publication(package, members + (extra_parquet_schema,), operation)

    with pytest.raises(ValueError, match="DESCRIPTION"):
        validate_package_publication(
            package,
            members,
            operation.model_copy(
                update={
                    "description_artifact": ArtifactVersionRef(
                        artifact_id="artifact_other",
                        version_id="version_other",
                        sha256="f" * 64,
                    )
                }
            ),
        )


@pytest.mark.asyncio
async def test_repair_operation_requires_exact_terminal_parent_in_same_scope() -> None:
    state = MemoryState()
    repository = InMemoryControlPlaneRepository(state)
    opened = await repository.open_direct_session(_open_request("Repair persistence"))
    binding = opened.binding
    scope = _scope(binding, mode=ExecutionMode.DIRECT_MCP, frontier=None)
    now = datetime.now(UTC)
    code = ArtifactVersionRef(
        artifact_id="artifact_code",
        version_id="version_code",
        sha256="a" * 64,
    )
    description = ArtifactVersionRef(
        artifact_id="artifact_description",
        version_id="version_description",
        sha256="b" * 64,
    )
    detail = DataTransformOperationDetail(
        inputs=(
            TransformInputBindingRecord(
                ordinal=1,
                input_name="source",
                package_id="package_source",
                member_id="member_source",
                payload_name="entities",
            ),
        ),
        outputs=(
            TransformOutputRecord(
                payload_name="result",
                description="Repaired result.",
                logical_key_fields=("entity_key",),
            ),
        ),
        output_package_name="repaired_result",
    )
    parent = CriticalOperationRecord(
        operation_id="operation_parent",
        owner_id=binding.owner_id,
        tenant_id=binding.tenant_id,
        task_id=binding.task_id,
        run_id=binding.run_id,
        operation_kind=CriticalOperationKind.DATA_TRANSFORM,
        tool_name="data_derive",
        intent_summary="Failed transform.",
        idempotency_key="parent",
        request_fingerprint="c" * 64,
        status=CriticalOperationStatus.FAILED,
        error_code="TRANSFORM_CODE_INVALID",
        error_message="SyntaxError",
        error_phase=CriticalOperationErrorPhase.CODE_VALIDATION,
        code_artifact=code,
        description_artifact=description,
        detail=detail,
        started_at=now,
        finished_at=now,
    )
    state.critical_operations[parent.operation_id] = parent
    repaired = parent.model_copy(
        update={
            "operation_id": "operation_repair",
            "tool_name": "data_derive",
            "idempotency_key": "repair",
            "request_fingerprint": "d" * 64,
            "repair_parent_operation_id": parent.operation_id,
        }
    )

    repository._require_repair_parent(scope, repaired)
    model = PostgresDataRuntimeRepositoryMixin._operation_model(repaired)
    assert model.repair_parent_operation_id == parent.operation_id
    assert model.description_artifact_sha256 == description.sha256

    succeeded_parent = parent.model_copy(
        update={
            "status": CriticalOperationStatus.SUCCEEDED,
            "result_package_id": "package_original_result",
            "error_code": None,
            "error_message": None,
            "error_phase": None,
        }
    )
    state.critical_operations[parent.operation_id] = succeeded_parent
    repository._require_repair_parent(scope, repaired)
    state.critical_operations[parent.operation_id] = parent

    with pytest.raises(ConflictError, match="exact DESCRIPTION"):
        repository._require_operation_artifacts(
            parent.model_copy(update={"description_artifact": None})
        )

    with pytest.raises(ConflictError, match="repairable transform"):
        repository._require_repair_parent(
            scope,
            repaired.model_copy(update={"repair_parent_operation_id": "operation_missing"}),
        )
    with pytest.raises(ValidationError, match="requires parent Operation"):
        CriticalOperationRecord.model_validate(
            {**repaired.model_dump(mode="json", exclude={"repair_parent_operation_id"}),
             "tool_name": "data_repair_transform"}
        )


def test_transform_output_schema_json_is_exact_but_legacy_optional() -> None:
    legacy = TransformOutputRecord(
        payload_name="summary",
        format="json",
        description="Legacy JSON output.",
        json_kind=JsonPayloadKind.DOCUMENT,
    )
    assert legacy.schema_json is None
    exact = legacy.model_copy(update={"schema_json": '{"type":"object"}'})
    assert exact.schema_json == '{"type":"object"}'
    with pytest.raises(ValidationError, match="cannot contain schema_json"):
        TransformOutputRecord(
            payload_name="rows",
            description="Parquet rows.",
            logical_key_fields=("id",),
            schema_json='{"type":"array"}',
        )
