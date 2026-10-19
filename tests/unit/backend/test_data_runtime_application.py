from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactService, ArtifactStream


def _saved_transform_reader(*, long_cell=False):
    from CAD2BIM_backend.domain.data_runtime import DataTransformOperationDetail
    detail = DataTransformOperationDetail.model_validate({
        "engine": "python_arrow", "ifc_source": {"model_id": "private-model", "revision_id": "private-revision", "revision_no": 3},
        "parameters": [{"name": "limit", "kind": "integer", "value": 17}],
        "inline_tables": [{"input_name": "original", "columns": [
            {"name": "key", "type": "integer", "nullable": False},
            {"name": "text", "type": "string", "nullable": True}],
            "rows": [{"values": [{"kind": "integer", "value": i},
                ({"kind": "null"} if i == 4 else {"kind": "string", "value": "界\n" * 30_000 if long_cell else f"original-{i}"})]} for i in range(1 if long_cell else 5)]}],
        "outputs": [{"payload_name": "result", "format": "parquet", "description": "Saved declaration", "logical_key_fields": ["key"]}],
        "output_package_name": "Original",
    })
    published = SimpleNamespace(catalog_entry=SimpleNamespace(alias="op_004"), operation=SimpleNamespace(
        operation_kind=CriticalOperationKind.DATA_TRANSFORM, status=CriticalOperationStatus.FAILED,
        tool_name="data_derive", detail=detail, description_artifact=None))
    reader = object.__new__(DataRuntimeService)
    reader._operation_by_alias = AsyncMock(return_value=published)
    return reader, published


async def test_saved_transform_input_pages_preserve_original_values_without_output():
    reader, published = _saved_transform_reader()
    scope = object()
    schema = await reader.get_transform_input(scope, "op_004", input_kind="inline_schema", input_name="original", column_count=1)
    assert schema.total_columns == 2 and schema.next_column == 1 and not schema.complete
    metadata = await reader.get_transform_input(scope, "op_004", input_kind="metadata", section="inline_tables")
    assert metadata.metadata.items[0].row_count == 5
    assert "original-0" not in metadata.model_dump_json()
    first = await reader.get_transform_input(scope, "op_004", input_kind="inline_rows", input_name="original", start_row=1, row_count=2, select=["text", "key"])
    assert first.start_row == 1 and first.next_row == 3 and first.total_rows == 5
    assert first.rows[0].values[0].value == "original-1" and first.rows[0].values[1].value == 1
    last = await reader.get_transform_input(scope, "op_004", input_kind="inline_rows", input_name="original", start_row=3)
    assert last.complete and last.next_row is None and last.returned_count == 2
    assert last.rows[-1].values[1].kind == "null"
    parameter = await reader.get_transform_input(scope, "op_004", input_kind="parameter", parameter_name="limit")
    assert parameter.parameter.value == 17
    with pytest.raises(DataRuntimeApplicationError, match="unique original columns"):
        await reader.get_transform_input(scope, "op_004", input_kind="inline_rows", input_name="original", select=["unknown"])
    with pytest.raises(DataRuntimeApplicationError, match="not found"):
        await reader.get_transform_input(scope, "op_004", input_kind="inline_rows", input_name="unknown")
    assert all(call.args == (scope, "op_004") for call in reader._operation_by_alias.await_args_list)
    assert published.operation.status is CriticalOperationStatus.FAILED


async def test_saved_transform_large_text_uses_character_intervals_and_preserves_null_types():
    reader, published = _saved_transform_reader(long_cell=True)
    with pytest.raises(DataRuntimeApplicationError) as caught:
        await reader.get_transform_input(object(), "op_004", input_kind="inline_rows", input_name="original")
    assert caught.value.code == "TRANSFORM_INPUT_CELL_TOO_LARGE" and "inline_text" in str(caught.value)
    narrow = await reader.get_transform_input(object(), "op_004", input_kind="inline_rows", input_name="original", select=["key"])
    assert narrow.rows[0].values[0].value == 0
    chunks = []; start = 0
    while True:
        part = await reader.get_transform_input(object(), "op_004", input_kind="inline_text", input_name="original", row_index=0, column_name="text", start_character=start, character_count=16_384)
        assert len(part.model_dump_json().encode()) <= 64 * 1024
        chunks.append(part.text)
        if part.complete:
            break
        assert part.next_character > start
        start = part.next_character
    assert "".join(chunks) == published.operation.detail.inline_tables[0].rows[0].values[1].value


def test_transform_metadata_all_four_lists_are_byte_bounded_and_recoverable():
    from CAD2BIM_backend.application.data_runtime_service import transform_metadata_page
    from CAD2BIM_backend.domain.data_runtime import TransformOutputRecord, TransformInputBindingRecord
    reader, published = _saved_transform_reader()
    inputs = tuple(TransformInputBindingRecord(ordinal=i+1, input_name=f"source_{i}",
        package_id=f"package-{i}", member_id=f"member-{i}", payload_name="input") for i in range(16))
    packages = {v.input_name: SimpleNamespace(package=SimpleNamespace(package_id=v.package_id),
        catalog_entry=SimpleNamespace(alias=f"data_{i+1:03d}"), members=[SimpleNamespace(member_id=v.member_id,
            logical_key_fields=("key", "source_key"))]) for i,v in enumerate(inputs)}
    detail = published.operation.detail.model_copy(update={"inputs": inputs, "outputs": tuple(
        TransformOutputRecord(payload_name=f"output_{i}", description="界" * 500,
            logical_key_fields=("key",)) for i in range(16))})
    for section in ("inputs", "outputs", "parameters", "inline_tables"):
        start = 0; recovered = []
        while True:
            page = transform_metadata_page(detail, section, packages, start_index=start, item_count=2, byte_budget=2400)
            assert len(page.model_dump_json().encode()) <= 2400
            recovered.extend(page.items)
            if page.complete:
                break
            assert page.next_index > start
            start = page.next_index
        assert len(recovered) == len(getattr(detail, section))


from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeApplicationError,
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
    OperationCodeReference,
)
from CAD2BIM_backend.application.exact_artifact_reader import (
    ExactArtifactReadError,
)
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef,
    CatalogEntryPage,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataTransformOperationDetail,
    PackageMemberKind,
    SOURCE_L0_SOURCE_MEMBER_NAME,
    TransformInputBindingRecord,
    TransformOutputRecord,
    ValidatedSource,
)
from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import (
    InMemoryIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_mcp.direct.application import DirectApplicationFacade


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"
PAYLOAD_COUNTS = (
    ("records", 7),
    ("raw_tags", 17),
    ("entities", 1),
    ("geometry_elements", 2),
    ("xdata_tags", 0),
    ("relationships", 0),
)
EXPECTED_DESCRIPTION = """# minimal_line.dxf — CAD L0 Source Package

## Summary

Deterministic source-fact extraction for the exact `minimal_line.dxf` byte version.

## Intended use

Query exact CAD records, tags, entities, geometry, XDATA, and source relationships
before deriving IFC modeling decisions.

## Contents

- `records`: 7 rows
- `raw_tags`: 17 rows
- `entities`: 1 rows
- `geometry_elements`: 2 rows
- `xdata_tags`: 0 rows
- `relationships`: 0 rows

## Method

Textual DXF group-code/value pairs are scanned in source order. Query tables use
explicit Arrow schemas and package-local stable keys. No business meaning, topology,
unit conversion, or IFC decision is inferred.

## Scope and completeness

Typed geometry covers LINE, POINT, LWPOLYLINE, POLYLINE, VERTEX, and SEQEND.
The exact source bytes remain authoritative in the source Artifact.

## Known limitations

Unsupported typed entity projections: None.
DWG, binary DXF, XREF, IMAGE, and UNDERLAY dependencies are rejected in phase one
rather than converted or ignored.
"""


def _runtime(tmp_path: Path):
    application, facade, _state, sessions, resolver = _runtime_with_state(tmp_path)
    return application, facade, sessions, resolver


def _runtime_with_state(
    tmp_path: Path,
):
    state = MemoryState()
    repository = InMemoryControlPlaneRepository(state)
    identity_directory = InMemoryIdentityDirectory()
    subject = ConfiguredLocalSubject(subject="configured-local-user")
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "storage"))

    application = DataRuntimeService(repository, artifacts)
    sessions = DirectSessionService(repository, identity_directory, subject.subject)
    resolver = DirectScopeResolver(repository, identity_directory, subject.subject)
    facade = DirectApplicationFacade(application, sessions, resolver)
    return application, facade, state, sessions, resolver


def _minimal_source() -> ValidatedSource:
    path = FIXTURE_ROOT / "minimal_line.dxf"
    return ValidatedSource(
        filename=path.name,
        media_type="application/dxf",
        content=path.read_bytes(),
        source_identity=f"approved-root:drawings/{path.name}",
        adapter_metadata={"root_relative_path": f"drawings/{path.name}"},
    )


@pytest.mark.parametrize("metadata", ["exact", "missing", "wrong_run", "wrong_revision", "failed"])
async def test_trusted_baseline_operation_reads_original_exact_revision_only(tmp_path, metadata):
    from CAD2BIM_backend.domain.data_runtime import IfcTrustedBaselineLoadOperationDetail
    from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity

    application, _facade, sessions, resolver = _runtime(tmp_path)
    session = await sessions.open_direct_session("Baseline provenance read")
    scope = await resolver.resolve(session.binding.direct_session_alias, command_id="read-baseline")
    original_scope = IfcRunIdentity(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id, run_id="original-run")
    failed = metadata == "failed"
    operation = SimpleNamespace(repair_parent_operation_id=None, result_package_id=None,
        detail=IfcTrustedBaselineLoadOperationDetail(source_asset=ArtifactVersionRef(
            artifact_id="source", version_id="source-version", sha256="a" * 64),
            result=None if failed else "baseline_loaded"),
        run_id="original-run", result_ifc_revision_id=None if failed else "original-revision",
        status=CriticalOperationStatus.FAILED if failed else CriticalOperationStatus.SUCCEEDED)
    exact = None if metadata == "missing" else SimpleNamespace(
        scope=original_scope.model_copy(update={"run_id": "other-run"}) if metadata == "wrong_run" else original_scope,
        revision_id="other-revision" if metadata == "wrong_revision" else "original-revision")
    reader = AsyncMock(return_value=exact)
    application._ifc_repository = SimpleNamespace(get_exact=reader)
    if metadata in {"missing", "wrong_run", "wrong_revision"}:
        with pytest.raises(DataRuntimeApplicationError) as caught:
            await application._inspect_operation(scope, SimpleNamespace(operation=operation))
        assert caught.value.code == "OPERATION_INTEGRITY_ERROR"
    else:
        result = await application._inspect_operation(scope, SimpleNamespace(operation=operation))
        assert result.committed_ifc_revision is (None if failed else exact)
        assert not result.input_packages and result.result_package is None
    if failed:
        reader.assert_not_awaited()
    else:
        reader.assert_awaited_once_with(original_scope, revision_id="original-revision")


def _assert_provider_safe(*results: Any) -> None:
    payload = json.dumps(
        [result.model_dump(mode="json") for result in results],
        ensure_ascii=False,
        sort_keys=True,
    )
    for internal_marker in (
        "artifact_id",
        "version_id",
        "session_id",
        "task_id",
        "run_id",
        "package_id",
        "operation_id",
        "sha256",
        "digest",
        "root_relative_path",
    ):
        assert internal_marker not in payload
    for internal_value_prefix in (
        "artifact_",
        "artifact_version_",
        "task_",
        "run_",
        "catalog_",
    ):
        assert internal_value_prefix not in payload


def test_data_runtime_error_message_is_bounded_by_utf8_bytes() -> None:
    error = DataRuntimeApplicationError("CAD_IMPORT_FAILED", "错" * 2_000)

    assert 0 < len(str(error).encode("utf-8")) <= 2_000
    assert str(error).encode("utf-8").decode("utf-8") == str(error)


async def test_normalized_session_name_resumes_the_stable_first_alias(
    tmp_path: Path,
) -> None:
    _application, facade, _sessions, _resolver = _runtime(tmp_path)

    opened = await facade.open_direct_session("  Plant   Room  ")
    resumed = await facade.open_direct_session("plant room")

    assert opened.direct_session_alias == "direct_001"
    assert opened.resumed is False
    assert resumed.direct_session_alias == "direct_001"
    assert resumed.session_name == opened.session_name == "  Plant   Room  "
    assert resumed.resumed is True
    _assert_provider_safe(opened, resumed)


async def test_minimal_import_replays_without_duplicate_catalog_entries(
    tmp_path: Path,
) -> None:
    application, facade, _sessions, resolver = _runtime(tmp_path)
    session = await facade.open_direct_session("Minimal line")
    source = _minimal_source()

    # Backend processing prepares a user upload; MCP only discovers/reads its result.
    scope = await resolver.resolve(session.direct_session_alias, command_id="upload-fixture")
    upload = ValidatedSource(filename=source.filename, media_type=source.media_type,
        content=source.content, source_identity=source.source_identity, adapter_metadata=source.adapter_metadata)
    imported = await application.import_cad_source(scope, upload)
    replayed = await application.import_cad_source(scope, upload)
    description = await facade.get_package_description(
        session.direct_session_alias,
        "data_001",
    )
    package_directory = await facade.list_data_packages(
        session.direct_session_alias,
        None,
        20,
    )
    operation_directory = await facade.list_operations(
        session.direct_session_alias,
        None,
        20,
    )
    provider_operation = await facade.get_operation(
        session.direct_session_alias,
        "op_001",
    )
    scope = await resolver.resolve(session.direct_session_alias, command_id="command_catalog")
    application_catalog = await application.get_catalog(scope)
    package_page = await application.list_package_catalog(scope)
    operation_page = await application.list_operation_catalog(scope)
    operation_detail = await application.get_operation(scope, "op_001")

    assert imported.published.operation.catalog_entry.alias == "op_001"
    assert imported.published.package.catalog_entry.alias == "data_001"
    assert imported.published.package.package.source_filename == "minimal_line.dxf"
    assert replayed == imported

    assert len(application_catalog.packages) == 1
    assert len(application_catalog.operations) == 1
    assert package_page.complete is True
    assert package_page.next_before_sequence is None
    assert package_page.packages == (application_catalog.packages[0],)
    assert package_page.producing_operations == {imported.published.package.package.package_id: imported.published.operation}
    assert operation_page.complete is True
    assert operation_page.next_before_sequence is None
    assert operation_page.operations[0].operation.operation_id == application_catalog.operations[0].operation.operation_id
    assert operation_page.operations[0].catalog_entry == application_catalog.operations[0].catalog_entry
    assert "detail" not in operation_page.operations[0].operation.model_dump()
    assert operation_detail.operation == application_catalog.operations[0]
    assert operation_detail.repair_parent_operation is None
    assert operation_detail.result_package == application_catalog.packages[0]
    assert operation_detail.input_packages == {}
    assert application_catalog.packages[0].package.description_title == (
        "minimal_line.dxf — CAD L0 Source Package"
    )
    assert application_catalog.packages[0].package.summary == (
        "Deterministic source-fact extraction for the exact minimal_line.dxf byte version."
    )
    assert application_catalog.packages[0].package.section_titles == (
        "Summary",
        "Intended use",
        "Contents",
        "Method",
        "Scope and completeness",
        "Known limitations",
    )
    assert description.package_alias == "data_001"
    assert description.package_name == "minimal_line.dxf — CAD L0"
    assert description.markdown == EXPECTED_DESCRIPTION
    assert package_directory.complete is True
    assert package_directory.next_before_sequence is None
    assert package_directory.packages[0].package_alias == "data_001"
    assert package_directory.packages[0].package_kind == "source_l0"
    assert operation_directory.complete is True
    assert operation_directory.operations[0].operation_alias == "op_001"
    assert provider_operation.operation.primary_result.package_alias == "data_001"
    assert provider_operation.operation.operation_alias == operation_directory.operations[0].operation_alias
    assert provider_operation.detail.detail_kind == "source_ingestion"
    _assert_provider_safe(
        session,
        description,
        package_directory,
        operation_directory,
        provider_operation,
    )


async def test_package_description_maps_async_stream_failure_to_integrity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _facade, _state, sessions, resolver = _runtime_with_state(tmp_path)
    session = await sessions.open_direct_session("Broken description stream")
    scope = await resolver.resolve(
        session.binding.direct_session_alias,
        command_id="command_broken_description_stream",
    )
    source = _minimal_source()
    imported = await application.import_cad_source(
        scope,
        ValidatedSource(
            filename=source.filename,
            media_type=source.media_type,
            content=source.content,
            source_identity=source.source_identity,
            adapter_metadata=source.adapter_metadata,
        ),
    )
    original_stream_version = application._artifacts.stream_version

    async def broken_stream_version(owner_id: str, artifact_id: str, version_id: str):
        stream = await original_stream_version(owner_id, artifact_id, version_id)

        async def chunks():
            yield b"partial-markdown"
            raise RuntimeError("injected description stream failure")

        return ArtifactStream(artifact=stream.artifact, chunks=chunks())

    monkeypatch.setattr(application._artifacts, "stream_version", broken_stream_version)
    with pytest.raises(DataRuntimeApplicationError) as stream_failure:
        await application.get_package_description(
            scope,
            imported.published.package.catalog_entry.alias,
        )

    assert stream_failure.value.code == "PACKAGE_INTEGRITY_ERROR"
    assert "injected description stream failure" not in str(stream_failure.value)


@pytest.mark.parametrize("tool_name", ["data_transform", "data_repair_transform"])
async def test_retired_transform_producer_has_no_current_code_read(
    tmp_path: Path, monkeypatch, tool_name: str,
) -> None:
    application, _facade, _sessions, _resolver = _runtime(tmp_path)
    published = SimpleNamespace(operation=SimpleNamespace(
        operation_kind=CriticalOperationKind.DATA_TRANSFORM,
        tool_name=tool_name,
        result_package_id=None,
    ))
    monkeypatch.setattr(application, "_operation_by_alias", AsyncMock(return_value=published))
    with pytest.raises(DataRuntimeApplicationError) as rejected:
        await application.get_transformation_code(
            None,
            OperationCodeReference(reference_kind="operation", operation_alias="op_001"),
        )
    assert rejected.value.code == "TRANSFORMATION_CODE_NOT_AVAILABLE"


async def test_failed_transform_operation_keeps_exact_readable_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _facade, _state, sessions, resolver = _runtime_with_state(tmp_path)
    session = await sessions.open_direct_session("Failed transform code")
    scope = await resolver.resolve(
        session.binding.direct_session_alias,
        command_id="command_failed_transform_code",
    )
    source = _minimal_source()
    imported = await application.import_cad_source(
        scope,
        ValidatedSource(
            filename=source.filename,
            media_type=source.media_type,
            content=source.content,
            source_identity=source.source_identity,
            adapter_metadata=source.adapter_metadata,
        ),
    )
    entity_member = next(
        member
        for member in imported.published.package.members
        if member.member_name == "payloads/entities/part-00000.parquet"
    )
    sql = "SELECT entity_key FROM entities"
    artifact = await application._artifacts.upload_bytes(
        owner_id=scope.owner_id,
        task_id=scope.identity.task_id or "",
        filename="transformation.sql",
        content_type="application/sql",
        content=sql.encode("utf-8"),
        idempotency_key="failed-transform-code",
        kind="data_transform_code",
        run_id=scope.run_id,
    )
    description_artifact = await application._artifacts.upload_bytes(
        owner_id=scope.owner_id,
        task_id=scope.identity.task_id or "",
        filename="description.md",
        content_type="text/markdown",
        content=b"# Failed transform\n\n## Summary\n\nExact repair source.\n",
        idempotency_key="failed-transform-description",
        kind="data_package_description",
        run_id=scope.run_id,
    )
    now = datetime.now(timezone.utc)
    failed = CriticalOperationRecord(
        operation_id="operation_failed_transform",
        owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id or "",
        run_id=scope.run_id,
        operation_kind=CriticalOperationKind.DATA_TRANSFORM,
        tool_name="data_derive",
        intent_summary="Failed transform — exercise exact code retrieval.",
        idempotency_key="failed-transform",
        request_fingerprint="a" * 64,
        status=CriticalOperationStatus.FAILED,
        error_code="TRANSFORM_EXECUTION_FAILED",
        error_message="DuckDB rejected the query",
        error_phase=CriticalOperationErrorPhase.EXECUTION,
        code_artifact=ArtifactVersionRef(
            artifact_id=artifact.artifact_id,
            version_id=artifact.version_id,
            sha256=artifact.sha256,
        ),
        description_artifact=ArtifactVersionRef(
            artifact_id=description_artifact.artifact_id,
            version_id=description_artifact.version_id,
            sha256=description_artifact.sha256,
        ),
        detail=DataTransformOperationDetail(
            inputs=(
                TransformInputBindingRecord(
                    ordinal=1,
                    input_name="entities",
                    package_id=imported.published.package.package.package_id,
                    member_id=entity_member.member_id,
                    payload_name="entities",
                ),
            ),
            outputs=(
                TransformOutputRecord(
                    payload_name="selected_entities",
                    description="Selected source entities",
                    logical_key_fields=("entity_key",),
                ),
            ),
            output_package_name="failed_selected_entities",
        ),
        started_at=now,
        finished_at=now,
    )
    async def publish_failed_transform(record: CriticalOperationRecord):
        declared = record.detail.model_dump(mode="json")
        await application._repository.prepare_data_transform_operation(
            scope,
            record.model_copy(update={
                "status": CriticalOperationStatus.PREPARED,
                "error_code": None,
                "error_message": None,
                "error_phase": None,
                "finished_at": None,
            }),
            request_identity=record.idempotency_key,
            request_digest=sha256_digest(declared),
            request_payload=declared,
            resolved_command=declared,
            deadline_at=record.started_at + timedelta(seconds=30),
        )
        claim = await application._repository.claim_data_transform_execution(
            scope, record.operation_id, owner="data-runtime-application-fixture", lease_seconds=30,
        )
        assert claim is not None
        return await application._repository.record_failed_operation(
            scope, record, execution_claim=claim,
        )

    published = await publish_failed_transform(failed)

    inspected = await application.get_operation(scope, published.catalog_entry.alias)
    code = await application.get_transformation_code(
        scope,
        OperationCodeReference(
            reference_kind="operation",
            operation_alias=published.catalog_entry.alias,
        ),
    )

    assert inspected.operation == published
    assert inspected.repair_parent_operation is None
    assert inspected.result_package is None
    assert inspected.input_packages["entities"] == imported.published.package
    from CAD2BIM_mcp.direct.operation.application import _operation_get_result
    projected = _operation_get_result(inspected)
    assert projected.detail.inputs[0].logical_key_fields == entity_member.logical_key_fields
    from CAD2BIM_mcp.direct.transform.common_inputs import TransformInputBinding
    assert "logical_key_fields" not in TransformInputBinding.model_fields
    assert code.operation == published
    assert code.result_package is None
    assert code.code == sql

    repaired_record = failed.model_copy(
        update={
            "operation_id": "operation_repaired_transform",
            "tool_name": "data_derive",
            "idempotency_key": "repaired-transform",
            "request_fingerprint": "b" * 64,
            "repair_parent_operation_id": failed.operation_id,
        }
    )
    repaired = await publish_failed_transform(repaired_record)
    repaired_inspection = await application.get_operation(
        scope,
        repaired.catalog_entry.alias,
    )
    assert repaired_inspection.operation == repaired
    assert repaired_inspection.repair_parent_operation == published

    original_get_operation = application._repository.get_operation

    async def missing_repair_parent(target_scope, operation_id: str):
        if operation_id == failed.operation_id:
            return None
        return await original_get_operation(target_scope, operation_id)

    monkeypatch.setattr(application._repository, "get_operation", missing_repair_parent)
    with pytest.raises(DataRuntimeApplicationError) as missing_parent:
        await application.get_operation(scope, repaired.catalog_entry.alias)
    assert missing_parent.value.code == "OPERATION_INTEGRITY_ERROR"
    monkeypatch.undo()

    monkeypatch.setattr(
        application._exact_artifacts,
        "read",
        AsyncMock(side_effect=ExactArtifactReadError("injected exact read failure")),
    )
    with pytest.raises(DataRuntimeApplicationError) as stream_failure:
        await application.get_transformation_code(
            scope,
            OperationCodeReference(
                reference_kind="operation",
                operation_alias=published.catalog_entry.alias,
            ),
        )

    assert stream_failure.value.code == "OPERATION_INTEGRITY_ERROR"
    assert "injected exact read failure" not in str(stream_failure.value)


async def test_terminal_operation_catalog_delegates_owner_filters_and_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _facade, _state, sessions, resolver = _runtime_with_state(tmp_path)
    session = await sessions.open_direct_session("Terminal operation catalog")
    scope = await resolver.resolve(
        session.binding.direct_session_alias,
        command_id="command_terminal_operation_catalog",
    )
    repository_list = AsyncMock(
        return_value=CatalogEntryPage(
            entries=(),
            complete=True,
            next_before_sequence=None,
        )
    )
    monkeypatch.setattr(
        application._repository,
        "list_terminal_operation_catalog",
        repository_list,
        raising=False,
    )

    result = await application.list_terminal_operation_catalog(
        scope,
        tool_names=(
            "data_derive",
            "data_repair_transform",
            "cad_compute_measurements",
        ),
        statuses=(CriticalOperationStatus.FAILED,),
        before_sequence=29,
        page_size=12,
    )

    repository_list.assert_awaited_once_with(
        scope,
        tool_names=(
            "data_derive",
            "data_repair_transform",
            "cad_compute_measurements",
        ),
        statuses=(CriticalOperationStatus.FAILED,),
        before_sequence=29,
        page_size=12,
    )
    assert result.operations == ()
    assert result.complete is True
    assert result.next_before_sequence is None


async def test_split_catalogs_use_independent_keyset_pages(
    tmp_path: Path,
) -> None:
    application, _facade, _state, sessions, resolver = _runtime_with_state(tmp_path)
    session = await sessions.open_direct_session("Catalog keysets")
    scope = await resolver.resolve(
        session.binding.direct_session_alias,
        command_id="command_catalog_keysets",
    )
    opened = _minimal_source()
    published = []
    for ordinal in (1, 2):
        published.append(
            await application.import_cad_source(
                scope,
                ValidatedSource(
                    filename=f"minimal_line_{ordinal}.dxf",
                    media_type=opened.media_type,
                    content=opened.content,
                    source_identity=f"approved-root:drawings/minimal_line_{ordinal}.dxf",
                ),
            )
        )

    package_first = await application.list_package_catalog(scope, page_size=1)
    package_second = await application.list_package_catalog(
        scope,
        before_sequence=package_first.next_before_sequence,
        page_size=1,
    )
    operation_first = await application.list_operation_catalog(scope, page_size=1)
    operation_second = await application.list_operation_catalog(
        scope,
        before_sequence=operation_first.next_before_sequence,
        page_size=1,
    )

    assert package_first.complete is False
    assert package_first.next_before_sequence == 2
    assert package_first.packages == (published[1].published.package,)
    assert package_second.complete is True
    assert package_second.next_before_sequence is None
    assert package_second.packages == (published[0].published.package,)
    assert operation_first.complete is False
    assert operation_first.next_before_sequence == 2
    assert operation_first.operations[0].catalog_entry == published[1].published.operation.catalog_entry
    assert operation_second.complete is True
    assert operation_second.operations[0].catalog_entry == published[0].published.operation.catalog_entry


async def test_source_is_exact_package_member_and_replay_reuses_artifact(
    tmp_path: Path,
) -> None:
    application, _facade, state, sessions, resolver = _runtime_with_state(tmp_path)
    session = await sessions.open_direct_session("Source member")
    opened = _minimal_source()
    source = ValidatedSource(
        filename=opened.filename,
        media_type=opened.media_type,
        content=opened.content,
        source_identity=opened.source_identity,
        adapter_metadata=opened.adapter_metadata,
    )
    scope = await resolver.resolve(
        session.binding.direct_session_alias,
        command_id="command_source_member",
    )

    imported = await application.import_cad_source(
        scope,
        source,
    )
    durable_artifact_count = len(state.artifacts)
    replayed = await application.import_cad_source(
        scope,
        source,
    )

    members = imported.published.package.members
    assert len(members) == 9
    assert {member.member_name for member in members} == {
        SOURCE_L0_SOURCE_MEMBER_NAME,
        "manifest.json",
        "description.md",
        *(f"payloads/{name}/part-00000.parquet" for name, _count in PAYLOAD_COUNTS),
    }
    source_member = next(
        member for member in members if member.member_name == SOURCE_L0_SOURCE_MEMBER_NAME
    )
    assert source_member.member_kind is PackageMemberKind.SOURCE
    assert source_member.media_type == "application/dxf"
    assert source_member.row_count is None
    assert source_member.artifact == imported.source_artifact
    assert imported.manifest["source"]["member_name"] == SOURCE_L0_SOURCE_MEMBER_NAME
    assert {"schema", "runtime"}.isdisjoint(imported.manifest)
    assert imported.manifest["extraction"] == {
        "operation_id": imported.published.operation.operation.operation_id,
    }
    assert "root_relative_path" not in json.dumps(dict(imported.manifest), sort_keys=True)
    assert "artifact_version_ref" not in imported.manifest["source"]
    assert "artifact_version_ref" in imported.manifest["description_member"]
    assert all(
        "artifact_version_ref" in payload["members"][0]
        for payload in imported.manifest["payloads"].values()
    )
    assert tuple(
        member.member_name for member in members if member.member_kind is PackageMemberKind.PAYLOAD
    ) == tuple(f"payloads/{name}/part-00000.parquet" for name, _count in PAYLOAD_COUNTS)
    assert replayed.source_artifact == source_member.artifact
    assert len(state.artifacts) == durable_artifact_count == 9


async def test_existing_exact_source_artifact_is_reused_without_reupload(
    tmp_path: Path,
) -> None:
    application, _facade, state, sessions, resolver = _runtime_with_state(tmp_path)
    session = await sessions.open_direct_session("Existing source artifact")
    scope = await resolver.resolve(
        session.binding.direct_session_alias,
        command_id="command_existing_source",
    )
    opened = _minimal_source()
    raw = await application._artifacts.upload_bytes(
        owner_id=scope.owner_id,
        task_id=scope.identity.task_id or "",
        filename=opened.filename,
        content_type=opened.media_type,
        content=opened.content,
        idempotency_key="preexisting-source",
        kind="cad_source",
        run_id=scope.run_id,
    )

    imported = await application.import_cad_source(
        scope,
        ValidatedSource(
            filename=opened.filename,
            media_type=opened.media_type,
            artifact_version=ArtifactVersionRef(
                artifact_id=raw.artifact_id,
                version_id=raw.version_id,
                sha256=raw.sha256,
            ),
            source_identity="uploaded-artifact:minimal-line",
        ),
    )

    assert imported.source_artifact.version_id == raw.version_id
    assert len(state.artifacts) == 9


@pytest.mark.parametrize(
    ("source", "error_code"),
    (
        (
            ValidatedSource(
                filename="broken.dxf",
                media_type="application/dxf",
                content=b"0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n",
                source_identity="approved-root:drawings/broken.dxf",
                adapter_metadata={"root_relative_path": "drawings/broken.dxf"},
            ),
            "MALFORMED_ASCII_DXF",
        ),
        (
            ValidatedSource(
                filename="drawing.dwg",
                media_type="application/dxf",
                content=b"AC1027binary",
                source_identity="approved-root:drawings/drawing.dwg",
                adapter_metadata={"root_relative_path": "drawings/drawing.dwg"},
            ),
            "UNSUPPORTED_SOURCE_FORMAT",
        ),
    ),
)
async def test_failed_import_replays_one_error_and_publishes_no_package(
    tmp_path: Path,
    source: ValidatedSource,
    error_code: str,
) -> None:
    application, facade, _sessions, resolver = _runtime(tmp_path)
    session = await facade.open_direct_session(f"Failure {error_code}")

    scope = await resolver.resolve(session.direct_session_alias, command_id="upload-fixture")
    upload = ValidatedSource(filename=source.filename, media_type=source.media_type,
        content=source.content, source_identity=source.source_identity, adapter_metadata=source.adapter_metadata)
    with pytest.raises(DataRuntimeApplicationError) as first_error:
        await application.import_cad_source(scope, upload)
    with pytest.raises(DataRuntimeApplicationError) as replay_error:
        await application.import_cad_source(scope, upload)

    assert first_error.value.code == error_code
    assert first_error.value.operation_alias == "op_001"
    assert replay_error.value.code == first_error.value.code
    assert replay_error.value.operation_alias == first_error.value.operation_alias
    assert str(replay_error.value) == str(first_error.value)

    scope = await resolver.resolve(session.direct_session_alias, command_id="command_failure")
    application_catalog = await application.get_catalog(scope)
    assert application_catalog.packages == ()
    assert len(application_catalog.operations) == 1
    _assert_provider_safe(session)


@pytest.mark.asyncio
async def test_cancelled_dxf_extraction_keeps_private_directory_until_thread_stops(monkeypatch) -> None:
    from CAD2BIM_backend.application import data_runtime_service as module

    entered, release = threading.Event(), threading.Event()
    paths = []

    def extract(_content, staging, *, source_name, check_cancelled):
        paths.append(staging.parent)
        entered.set()
        assert release.wait(2)
        assert staging.parent.is_dir()
        check_cancelled()
        raise AssertionError("cancelled extraction must not continue")

    async def run():
        async with DataRuntimeService._stage_l0(b"source", "source.dxf"):
            raise AssertionError("cancelled extraction must not publish")

    monkeypatch.setattr(module, "extract_ascii_dxf_l0", extract)
    task = asyncio.create_task(run())
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert paths[0].is_dir()
    finally:
        release.set()
    async with asyncio.timeout(1):
        while paths[0].exists():
            await asyncio.sleep(0.005)
