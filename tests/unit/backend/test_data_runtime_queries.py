from __future__ import annotations

import asyncio
import base64
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import CAD2BIM_backend.application.data_query_service as data_query_service_module
from CAD2BIM_backend.application.artifact_service import ArtifactService, ArtifactStream
from CAD2BIM_backend.application.data_query_service import (
    DataQueryApplicationError,
    DataQueryPolicy,
    DataQueryService,
)
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.domain.data_query import (
    AssignmentKeyProfileBucket,
    CadEntitiesQuery,
    CadEntityQuery,
    CadGetEntityRequest,
    CadQueryEntitiesRequest,
    CadQueryXdataRequest,
    CadXdataQuery,
    ColumnSchema,
    DataDescribePackageQuery,
    DataDescribePackageResult,
    DataQueryRowsRequest,
    DataRowsQuery,
    EntityCountBy,
    EntityFilters,
    EntityQueryMode,
    EntitySection,
    FilterCondition,
    FilterOperator,
    XdataProfileDimension,
    XdataFilters,
    XdataQueryMode,
)
from CAD2BIM_backend.domain.data_runtime import (
    ArrowColumnRecord,
    ArtifactVersionRef,
    CatalogResourceKind,
    CriticalOperationKind, CriticalOperationRecord, CriticalOperationStatus,
    DataTransformOperationDetail, TransformInputBindingRecord, TransformOutputRecord,
    DataCatalogEntry,
    DataPackageMemberRecord,
    DataPackageRecord,
    JsonPayloadKind,
    PackageMemberKind,
    SOURCE_L0_LOGICAL_KEYS,
    ValidatedSource,
)
from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import (
    InMemoryIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.storage.local import (
    DEFAULT_ALLOWED_CONTENT_TYPES,
    LocalStorage,
)


DXF_WITH_DISCOVERY_FACTS = b"""0
SECTION
2
HEADER
9
$ACADVER
1
AC1009
0
ENDSEC
0
SECTION
2
TABLES
0
TABLE
2
LAYER
0
LAYER
2
Gas
70
0
0
LAYER
2
Unused
70
0
0
ENDTAB
0
TABLE
2
APPID
0
APPID
2
APP_TEST
70
0
0
APPID
2
UNUSED_APP
70
0
0
ENDTAB
0
ENDSEC
0
SECTION
2
ENTITIES
0
LINE
5
A
330
B
8
Gas
10
0.0
20
0.0
30
0.0
11
1000.0
21
0.0
31
0.0
1001
APP_TEST
1000
Diameter=80
1000
Label=main
0
POINT
5
B
8
Other
10
25.0
20
30.0
30
0.0
1001
SINGLE
1040
12.5
0
ENDSEC
0
EOF
"""


def test_assignment_profile_preserves_canonical_value_order_without_per_row_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_identity = data_query_service_module._typed_identity
    identity_calls: list[tuple[str, object]] = []

    def tracked_identity(value: tuple[str, object]) -> str:
        identity_calls.append(value)
        return original_identity(value)

    monkeypatch.setattr(data_query_service_module, "_typed_identity", tracked_identity)
    rows = [
        {"entity_key": "e1", "appid": "APP", "_assignment_value": ("number", -0.0)},
        {"entity_key": "e1", "appid": "APP", "_assignment_value": ("number", 0.0)},
        {"entity_key": "e2", "appid": "APP", "_assignment_value": ("binary", b"\xff")},
        {"entity_key": "e3", "appid": "APP", "_assignment_value": ("string", '"')},
        {"entity_key": "e4", "appid": "APP", "_assignment_value": ("string", "A")},
    ]
    budget = data_query_service_module._Budget(DataQueryPolicy(), deadline=1.0, clock=lambda: 0.0)

    profile = AssignmentKeyProfileBucket(
        **data_query_service_module._assignment_profile("key", rows, 5, budget)
    )

    assert profile.model_dump(mode="json") == {
        "assignment_key": "key",
        "source_appids": ["APP"],
        "occurrence_count": 5,
        "entity_count": 4,
        "missing_entity_count": 1,
        "duplicate_entity_count": 1,
        "empty_value_count": 0,
        "distinct_value_count": 4,
        "value_kind_counts": {"binary": 1, "number": 2, "string": 2},
        "values": {
            "value_enumeration_complete": True,
            "returned_value_count": 4,
            "other_distinct_value_count": 0,
            "other_occurrence_count": 0,
            "items": [
                {"value": {"kind": "number", "value": -0.0}, "occurrence_count": 2},
                {"value": {"kind": "binary", "base64": "/w=="}, "occurrence_count": 1},
                {"value": {"kind": "string", "value": "A"}, "occurrence_count": 1},
                {"value": {"kind": "string", "value": '"'}, "occurrence_count": 1},
            ],
        },
        "numeric_profile": {
            "applicable": False,
            "interpretation": "not_applicable",
            "numeric_value_count": 2,
            "minimum": None,
            "maximum": None,
            "non_numeric_non_empty_count": 3,
        },
    }
    assert len(identity_calls) == profile.distinct_value_count


def _cursor_key(byte: bytes = b"q") -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=")


def test_arrow_type_metadata_uses_shared_utf8_budget() -> None:
    wide_type = "struct<" + ", ".join(f"field_{index}: int64" for index in range(40)) + ">"
    assert len(wide_type) > 255
    assert ArrowColumnRecord(name="wide", type=wide_type, nullable=True).type == wide_type
    assert ColumnSchema(name="wide", type=wide_type, nullable=True).type == wide_type

    oversized_multibyte_type = "中" * 21_846
    assert len(oversized_multibyte_type) < 65_536
    assert len(oversized_multibyte_type.encode("utf-8")) > 65_536
    with pytest.raises(ValidationError, match="65536 UTF-8 bytes"):
        ArrowColumnRecord(name="wide", type=oversized_multibyte_type, nullable=True)
    with pytest.raises(ValidationError, match="65536 UTF-8 bytes"):
        ColumnSchema(name="wide", type=oversized_multibyte_type, nullable=True)


async def _imported_runtime(tmp_path: Path):
    state = MemoryState()
    repository = InMemoryControlPlaneRepository(state)
    identities = InMemoryIdentityDirectory()
    subject = ConfiguredLocalSubject(subject="configured-local-user")
    artifacts = ArtifactService(
        repository,
        LocalStorage(
            tmp_path / "storage",
            allowed_content_types=(*DEFAULT_ALLOWED_CONTENT_TYPES, "application/schema+json"),
        ),
    )
    runtime = DataRuntimeService(repository, artifacts)
    sessions = DirectSessionService(repository, identities, subject.subject)
    resolver = DirectScopeResolver(repository, identities, subject.subject)
    session = await sessions.open_direct_session("Phase 2 query fixture")
    scope = await resolver.resolve(
        session.binding.direct_session_alias,
        command_id="command_import",
    )
    await runtime.import_cad_source(
        scope,
        ValidatedSource(
            filename="query_fixture.dxf",
            media_type="application/dxf",
            content=DXF_WITH_DISCOVERY_FACTS,
            source_identity="approved-root:drawings/query_fixture.dxf",
            adapter_metadata={"root_relative_path": "drawings/query_fixture.dxf"},
        ),
    )
    query_scope = await resolver.resolve(
        session.binding.direct_session_alias,
        command_id="command_query",
    )
    service = DataQueryService(repository, artifacts, CursorCodec(_cursor_key()))
    return service, query_scope, state, repository, artifacts, resolver


async def _add_derived_package(
    scope, state: MemoryState, artifacts: ArtifactService, *, table=None,
) -> None:
    if table is None:
        table = data_query_service_module.pa.table(
            {
                "candidate_key": ["candidate_002", "candidate_001"],
                "category": ["valve", "pipe"],
                "score": [0.7, 0.9],
            }
        )
    sink = data_query_service_module.pa.BufferOutputStream()
    data_query_service_module.pq.write_table(table, sink)
    content = sink.getvalue().to_pybytes()
    stored_schema = data_query_service_module.pq.read_schema(
        data_query_service_module.pa.BufferReader(content)
    )
    artifact = await artifacts.upload_bytes(
        owner_id=scope.owner_id,
        task_id=scope.identity.task_id,
        filename="connection_candidates.parquet",
        content_type="application/vnd.apache.parquet",
        content=content,
        idempotency_key="derived-query-fixture",
        kind="data_payload",
        run_id=scope.run_id,
    )
    now = datetime.now(UTC)
    package = DataPackageRecord(
        package_id="package_derived_query_fixture",
        owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id,
        created_by_run_id=scope.run_id,
        name="connection_candidates_v1",
        package_kind="derived",
        source_filename=None,
        description_title="Connection candidates",
        summary="Derived connection candidates for bounded follow-up queries.",
        section_titles=("Summary", "Contents", "Method"),
        idempotency_key="package-derived-query-fixture",
        request_fingerprint="d" * 64,
        created_at=now,
    )
    member = DataPackageMemberRecord(
        member_id="member_derived_candidates",
        owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id,
        package_id=package.package_id,
        member_name="payloads/connection_candidates/part-00000.parquet",
        member_kind=PackageMemberKind.PAYLOAD,
        artifact=ArtifactVersionRef(
            artifact_id=artifact.artifact_id,
            version_id=artifact.version_id,
            sha256=artifact.sha256,
        ),
        media_type="application/vnd.apache.parquet",
        row_count=table.num_rows,
        logical_payload_name="connection_candidates",
        payload_description="Candidate products and confidence scores.",
        logical_key_fields=("candidate_key",),
        arrow_schema=tuple(
            ArrowColumnRecord(name=field.name, type=str(field.type), nullable=field.nullable)
            for field in stored_schema
        ),
        logical_byte_count=table.nbytes,
        created_at=now,
    )
    catalog = DataCatalogEntry(
        catalog_entry_id="catalog_derived_query_fixture",
        owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id,
        run_id=scope.run_id,
        resource_kind=CatalogResourceKind.PACKAGE,
        alias="data_002",
        ordinal=2,
        catalog_revision=2,
        package_id=package.package_id,
        created_at=now,
    )
    async with state.lock:
        state.data_packages[package.package_id] = package
        state.data_package_members[package.package_id] = (member,)
        state.data_catalog_entries[scope.run_id].append(catalog)
    # Retain the fixture's producing relation, as a normal derived Package does.
    code = await artifacts.upload_bytes(owner_id=scope.owner_id, task_id=scope.identity.task_id,
        filename="query-fixture.sql", content_type="application/sql", content=b"SELECT * FROM entities",
        idempotency_key="query-fixture-code", kind="transformation_code", run_id=scope.run_id)
    description = await artifacts.upload_bytes(owner_id=scope.owner_id, task_id=scope.identity.task_id,
        filename="query-fixture.md", content_type="text/markdown", content=b"# Query fixture\n\nDerived query data.",
        idempotency_key="query-fixture-description", kind="data_package_description", run_id=scope.run_id)
    source = next(item for item in state.data_packages.values() if item.package_kind.value == "source_l0")
    source_member = next(item for item in state.data_package_members[source.package_id]
                         if item.member_name == "payloads/entities/part-00000.parquet")
    producer = CriticalOperationRecord(operation_id="operation-query-fixture", owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id, task_id=scope.identity.task_id, run_id=scope.run_id,
        operation_kind=CriticalOperationKind.DATA_TRANSFORM, tool_name="data_transform",
        intent_summary="Prepare derived query fixture", idempotency_key="operation-query-fixture",
        request_fingerprint="e"*64, status=CriticalOperationStatus.SUCCEEDED, result_package_id=package.package_id,
        code_artifact=ArtifactVersionRef(artifact_id=code.artifact_id, version_id=code.version_id, sha256=code.sha256),
        description_artifact=ArtifactVersionRef(artifact_id=description.artifact_id, version_id=description.version_id, sha256=description.sha256),
        detail=DataTransformOperationDetail(inputs=(TransformInputBindingRecord(ordinal=1,input_name="entities",
            package_id=source.package_id,member_id=source_member.member_id,payload_name="entities"),),
            outputs=(TransformOutputRecord(payload_name="connection_candidates",description="Query fixture",
                logical_key_fields=("candidate_key",)),), output_package_name=package.name),
        started_at=now, finished_at=now)
    async with state.lock:
        state.critical_operations[producer.operation_id] = producer
        state.data_catalog_entries[scope.run_id].append(DataCatalogEntry(
            catalog_entry_id="catalog-query-fixture-operation",owner_id=scope.owner_id,tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id,run_id=scope.run_id,resource_kind=CatalogResourceKind.OPERATION,
            alias="op_002",ordinal=2,catalog_revision=2,operation_id=producer.operation_id,created_at=now))


async def _add_json_payload_to_derived_package(
    scope,
    state: MemoryState,
    artifacts: ArtifactService,
    *,
    schema_content: bytes = b'{"type":"object","properties":{"count":{"type":"integer"}}}',
) -> tuple[DataPackageMemberRecord, DataPackageMemberRecord]:
    payload_content = b'{"count":2}'
    payload_artifact = await artifacts.upload_bytes(
        owner_id=scope.owner_id,
        task_id=scope.identity.task_id,
        filename="topology_summary.json",
        content_type="application/json",
        content=payload_content,
        idempotency_key="derived-query-json-payload",
        kind="data_payload",
        run_id=scope.run_id,
    )
    schema_artifact = await artifacts.upload_bytes(
        owner_id=scope.owner_id,
        task_id=scope.identity.task_id,
        filename="topology_summary.schema.json",
        content_type="application/schema+json",
        content=schema_content,
        idempotency_key=f"derived-query-json-schema-{len(schema_content)}",
        kind="data_schema",
        run_id=scope.run_id,
    )
    now = datetime.now(UTC)
    payload_member = DataPackageMemberRecord(
        member_id="member_derived_topology_summary",
        owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id,
        package_id="package_derived_query_fixture",
        member_name="payloads/topology_summary.json",
        member_kind=PackageMemberKind.PAYLOAD,
        artifact=ArtifactVersionRef(
            artifact_id=payload_artifact.artifact_id,
            version_id=payload_artifact.version_id,
            sha256=payload_artifact.sha256,
        ),
        media_type="application/json",
        logical_payload_name="topology_summary",
        payload_description="Bounded topology summary document.",
        logical_byte_count=len(payload_content),
        json_kind=JsonPayloadKind.DOCUMENT,
        created_at=now,
    )
    schema_member = DataPackageMemberRecord(
        member_id="member_derived_topology_summary_schema",
        owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id,
        package_id="package_derived_query_fixture",
        member_name="schemas/topology_summary.schema.json",
        member_kind=PackageMemberKind.SCHEMA,
        artifact=ArtifactVersionRef(
            artifact_id=schema_artifact.artifact_id,
            version_id=schema_artifact.version_id,
            sha256=schema_artifact.sha256,
        ),
        media_type="application/schema+json",
        created_at=now,
    )
    async with state.lock:
        state.data_package_members["package_derived_query_fixture"] = (
            *state.data_package_members["package_derived_query_fixture"],
            payload_member,
            schema_member,
        )
    return payload_member, schema_member


async def test_describe_and_generic_rows_expose_the_six_exact_l0_payloads(
    tmp_path: Path,
) -> None:
    service, scope, _state, _repository, _artifacts, _resolver = await _imported_runtime(tmp_path)

    described = await service.describe_package(
        scope, DataDescribePackageQuery(package_alias="data_001")
    )
    assert described.producing_operation_alias == "op_001"
    schema = await service.describe_package(
        scope,
        DataDescribePackageQuery(package_alias="data_001", payload_name="entities"),
    )
    rows = await service.query_rows(
        scope,
        DataQueryRowsRequest(
            query=DataRowsQuery(
                package_alias="data_001",
                payload_name="entities",
                select=(),
                filters=(
                    FilterCondition(field="layer", op=FilterOperator.EQ, value="Gas"),
                    FilterCondition(
                        field="typed_geometry_supported",
                        op=FilterOperator.EQ,
                        value=True,
                    ),
                ),
            )
        ),
    )

    assert [payload.payload_name for payload in described.payloads] == list(SOURCE_L0_LOGICAL_KEYS)
    assert {
        payload.payload_name: payload.logical_key_fields for payload in described.payloads
    } == dict(SOURCE_L0_LOGICAL_KEYS)
    assert schema.payload_schema is not None
    assert schema.payload_schema.logical_key_fields == ("entity_key",)
    assert {column.name for column in schema.payload_schema.columns} >= {
        "entity_key",
        "entity_type",
        "layer",
        "typed_geometry_supported",
    }
    assert rows.page.total_matched_count == rows.page.returned_count == 1
    assert rows.page.complete is True
    assert rows.rows[0].values == {}
    assert set(rows.rows[0].key_values) == {"entity_key"}


async def test_generic_data_queries_support_derived_payloads_but_cad_tools_reject_them(
    tmp_path: Path,
) -> None:
    service, scope, state, _repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    await _add_derived_package(scope, state, artifacts)
    durable_counts = (
        len(state.critical_operations),
        len(state.data_packages),
        len(state.artifacts),
        sum(len(entries) for entries in state.data_catalog_entries.values()),
    )

    described = await service.describe_package(
        scope,
        DataDescribePackageQuery(
            package_alias="data_002",
            payload_name="connection_candidates",
        ),
    )
    first = await service.query_rows(
        scope,
        DataQueryRowsRequest(
            query=DataRowsQuery(
                package_alias="data_002",
                payload_name="connection_candidates",
                select=("category", "score"),
                page_size=1,
            )
        ),
    )
    second = await service.query_rows(scope, DataQueryRowsRequest(cursor=first.page.next_cursor))

    assert described.package.model_dump(mode="json") == {
        "package_alias": "data_002",
        "name": "connection_candidates_v1",
        "package_kind": "derived",
        "source_filename": None,
    }
    assert [payload.model_dump(mode="json") for payload in described.payloads] == [
        {
            "payload_name": "connection_candidates",
            "format": "parquet",
            "row_count": 2,
            "logical_key_fields": ["candidate_key"],
        }
    ]
    assert described.payload_schema is not None
    assert described.payload_schema.model_dump(mode="json") == {
        "payload_name": "connection_candidates",
        "logical_key_fields": ["candidate_key"],
        "columns": [
            {"name": "candidate_key", "type": "string", "nullable": True},
            {"name": "category", "type": "string", "nullable": True},
            {"name": "score", "type": "double", "nullable": True},
        ],
    }
    assert first.rows[0].key_values == {"candidate_key": "candidate_001"}
    assert second.rows[0].key_values == {"candidate_key": "candidate_002"}
    assert second.page.complete is True

    cad_requests = (
        service.query_entities(
            scope,
            CadQueryEntitiesRequest(
                query=CadEntitiesQuery(
                    package_alias="data_002",
                    mode=EntityQueryMode.ROWS,
                )
            ),
        ),
        service.get_entity(
            scope,
            CadGetEntityRequest(
                query=CadEntityQuery(
                    package_alias="data_002",
                    entity_key="candidate_001",
                    section=EntitySection.GEOMETRY_ELEMENTS,
                )
            ),
        ),
        service.query_xdata(
            scope,
            CadQueryXdataRequest(
                query=CadXdataQuery(
                    package_alias="data_002",
                    mode=XdataQueryMode.ROWS,
                )
            ),
        ),
    )
    for request in cad_requests:
        with pytest.raises(DataQueryApplicationError) as mismatch:
            await request
        assert mismatch.value.code == "PACKAGE_KIND_MISMATCH"

    assert durable_counts == (
        len(state.critical_operations),
        len(state.data_packages),
        len(state.artifacts),
        sum(len(entries) for entries in state.data_catalog_entries.values()),
    )


@pytest.mark.parametrize("key_padding, cursor_fits", ((74, True), (130, False)))
async def test_long_in_queries_check_cursor_capacity_only_when_next_page_is_needed(
    tmp_path: Path, key_padding: int, cursor_fits: bool,
) -> None:
    service, scope, state, _repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    keys = [f"port:{index:03d}:" + "q" * key_padding for index in range(100)]
    await _add_derived_package(
        scope, state, artifacts,
        table=data_query_service_module.pa.table({"candidate_key": [keys[1], keys[0]]}),
    )
    query = DataRowsQuery(
        package_alias="data_002",
        payload_name="connection_candidates",
        select=(),
        filters=(FilterCondition(field="candidate_key", op=FilterOperator.IN, value=keys),),
        page_size=200,
    )

    single = await service.query_rows(scope, DataQueryRowsRequest(query=query))
    assert [row.key_values["candidate_key"] for row in single.rows] == keys[:2]
    assert single.page.complete is True
    assert single.page.next_cursor is None

    paged = DataQueryRowsRequest(query=query.model_copy(update={"page_size": 1}))
    if not cursor_fits:
        with pytest.raises(DataQueryApplicationError, match="smaller IN batches") as rejected:
            await service.query_rows(scope, paged)
        assert rejected.value.code == "INVALID_ARGUMENT"
        return

    first = await service.query_rows(scope, paged)
    assert first.rows[0].key_values["candidate_key"] == keys[0]
    assert first.page.next_cursor is not None
    second = await service.query_rows(scope, DataQueryRowsRequest(cursor=first.page.next_cursor))
    assert second.rows[0].key_values["candidate_key"] == keys[1]
    assert second.page.complete is True
    assert second.page.next_cursor is None


async def test_generic_rows_return_compound_values_and_charge_nested_projection(
    tmp_path: Path,
) -> None:
    service, scope, state, repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    pa = data_query_service_module.pa
    table = pa.table({
        "candidate_key": ["candidate_002", "candidate_001"],
        "event_keys": [[], ["event_a", "event_b"]],
        "details": [{"binary_items": [None]}, {"binary_items": [b"payload", None]}],
        "event_lookup": pa.array(
            [[], [("event_a", 1), ("event_b", 2)]], type=pa.map_(pa.string(), pa.int64()),
        ),
    })
    await _add_derived_package(scope, state, artifacts, table=table)
    first = await service.query_rows(scope, DataQueryRowsRequest(query=DataRowsQuery(
        package_alias="data_002", payload_name="connection_candidates",
        select=("event_keys", "details", "event_lookup"), page_size=1,
    )))
    second = await service.query_rows(scope, DataQueryRowsRequest(cursor=first.page.next_cursor))
    assert first.rows[0].key_values == {"candidate_key": "candidate_001"}
    assert first.rows[0].model_dump(mode="json")["values"] == {
        "event_keys": ["event_a", "event_b"],
        "details": {"binary_items": [{"kind": "binary_base64", "base64": "cGF5bG9hZA=="}, None]},
        "event_lookup": [["event_a", 1], ["event_b", 2]],
    }
    assert second.rows[0].values["event_keys"] == []
    assert second.page.complete and second.page.total_matched_count == 2

    key_bytes = table.select(["candidate_key"]).nbytes
    limited = DataQueryService(repository, artifacts, CursorCodec(_cursor_key()),
        policy=DataQueryPolicy(decoded_projection_byte_budget=key_bytes))
    with pytest.raises(DataQueryApplicationError) as exceeded:
        await limited.query_rows(scope, DataQueryRowsRequest(query=DataRowsQuery(
            package_alias="data_002", payload_name="connection_candidates",
            select=("event_keys",), page_size=1,
        )))
    assert exceeded.value.code == "QUERY_BUDGET_EXCEEDED"
    assert "exceeded=decoded_projection_bytes" in str(exceeded.value)
    assert f"decoded_projection_byte_budget={key_bytes}" in str(exceeded.value)
    with pytest.raises(DataQueryApplicationError) as bad_filter:
        await service.query_rows(scope, DataQueryRowsRequest(query=DataRowsQuery(
            package_alias="data_002", payload_name="connection_candidates", select=("event_keys",),
            filters=(FilterCondition(field="event_keys", op=FilterOperator.EQ, value="event_a"),),
        )))
    assert bad_filter.value.code == "FILTER_TYPE_MISMATCH"
    with pytest.raises(DataQueryApplicationError) as nonfinite:
        data_query_service_module._cell({"nested": [float("nan")]})
    assert nonfinite.value.code == "PACKAGE_INTEGRITY_ERROR"


async def test_uploaded_l0_without_visible_processing_record_has_no_invented_operation(tmp_path: Path) -> None:
    service, scope, state, repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    state.critical_operations.clear()
    before = (len(state.critical_operations),len(state.data_catalog_entries[scope.run_id]))
    described = await service.describe_package(scope,DataDescribePackageQuery(package_alias="data_001"))
    catalog = await DataRuntimeService(repository,artifacts).list_package_catalog(scope)
    assert described.producing_operation_alias is None
    assert catalog.producing_operations == {}
    assert before == (len(state.critical_operations),len(state.data_catalog_entries[scope.run_id]))


async def test_describe_lists_json_without_reading_schema_and_selected_json_reads_exact_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scope, state, _repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    await _add_derived_package(scope, state, artifacts)
    _payload_member, schema_member = await _add_json_payload_to_derived_package(
        scope, state, artifacts
    )
    original_stream_version = artifacts.stream_version
    streamed_artifact_ids: list[str] = []

    async def tracked_stream_version(owner_id: str, artifact_id: str, version_id: str):
        streamed_artifact_ids.append(artifact_id)
        return await original_stream_version(owner_id, artifact_id, version_id)

    monkeypatch.setattr(artifacts, "stream_version", tracked_stream_version)
    listed = await service.describe_package(
        scope, DataDescribePackageQuery(package_alias="data_002")
    )
    assert listed.producing_operation_alias == "op_002"

    assert [payload.model_dump(mode="json") for payload in listed.payloads] == [
        {
            "payload_name": "connection_candidates",
            "format": "parquet",
            "row_count": 2,
            "logical_key_fields": ["candidate_key"],
        },
        {
            "payload_name": "topology_summary",
            "format": "json",
            "size_bytes": 11,
            "json_kind": "document",
            "schema_available": True,
        },
    ]
    assert listed.payload_schema is None
    assert streamed_artifact_ids == []

    described = await service.describe_package(
        scope,
        DataDescribePackageQuery(
            package_alias="data_002",
            payload_name="topology_summary",
        ),
    )

    assert described.payload_schema is not None
    assert described.payload_schema.model_dump(mode="json") == {
        "payload_name": "topology_summary",
        "format": "json",
        "json_kind": "document",
        "schema_json": '{"type":"object","properties":{"count":{"type":"integer"}}}',
    }
    assert streamed_artifact_ids == [schema_member.artifact.artifact_id]

    with pytest.raises(DataQueryApplicationError) as unsupported:
        await service.query_rows(
            scope,
            DataQueryRowsRequest(
                query=DataRowsQuery(
                    package_alias="data_002",
                    payload_name="topology_summary",
                    select=(),
                )
            ),
        )
    assert unsupported.value.code == "PAYLOAD_FORMAT_UNSUPPORTED"
    assert streamed_artifact_ids == [schema_member.artifact.artifact_id]


async def test_selected_json_schema_enforces_exact_64_kib_utf8_limit(tmp_path: Path) -> None:
    service, scope, state, _repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    await _add_derived_package(scope, state, artifacts)
    await _add_json_payload_to_derived_package(
        scope,
        state,
        artifacts,
        schema_content=b"x" * 65_537,
    )

    listed = await service.describe_package(
        scope, DataDescribePackageQuery(package_alias="data_002")
    )
    assert [payload.format for payload in listed.payloads] == ["parquet", "json"]

    with pytest.raises(DataQueryApplicationError) as oversized:
        await service.describe_package(
            scope,
            DataDescribePackageQuery(
                package_alias="data_002",
                payload_name="topology_summary",
            ),
        )
    assert oversized.value.code == "PACKAGE_INTEGRITY_ERROR"


def test_describe_package_json_branches_are_closed_and_provider_safe() -> None:
    schema = DataDescribePackageResult.model_json_schema()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert not ({"if", "then", "else", "not"} & value.keys())
            assert value.get("additionalProperties") is not True
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(schema)
    assert set(schema["$defs"]["JsonPayloadSummary"]["required"]) == {
        "payload_name",
        "size_bytes",
        "json_kind",
    }
    assert set(schema["$defs"]["JsonPayloadSchema"]["required"]) == {
        "payload_name",
        "json_kind",
        "schema_json",
    }


async def test_entity_discovery_counts_declared_empty_layers_and_reads_each_section(
    tmp_path: Path,
) -> None:
    service, scope, _state, _repository, _artifacts, _resolver = await _imported_runtime(tmp_path)

    row_result = await service.query_entities(
        scope,
        CadQueryEntitiesRequest(
            query=CadEntitiesQuery(package_alias="data_001", mode=EntityQueryMode.ROWS, page_size=1)
        ),
    )
    entity_key = row_result.rows[0].entity_key
    layer_counts = await service.query_entities(
        scope,
        CadQueryEntitiesRequest(
            query=CadEntitiesQuery(
                package_alias="data_001",
                mode=EntityQueryMode.COUNT_BY,
                count_by=EntityCountBy.LAYER,
            )
        ),
    )
    type_counts = await service.query_entities(
        scope,
        CadQueryEntitiesRequest(
            query=CadEntitiesQuery(
                package_alias="data_001",
                mode=EntityQueryMode.COUNT_BY,
                count_by=EntityCountBy.ENTITY_TYPE,
            )
        ),
    )
    combined_counts = await service.query_entities(
        scope,
        CadQueryEntitiesRequest(
            query=CadEntitiesQuery(
                package_alias="data_001",
                mode=EntityQueryMode.COUNT_BY,
                count_by=EntityCountBy.LAYER_AND_ENTITY_TYPE,
            )
        ),
    )

    assert row_result.page.total_matched_count == 2
    assert row_result.page.next_cursor is not None
    assert {(item.layer, item.entity_count) for item in layer_counts.counts} == {
        ("Gas", 1),
        ("Other", 1),
        ("Unused", 0),
    }
    assert layer_counts.scope.declared_layer_count == 2
    assert layer_counts.scope.used_layer_count == 2
    assert layer_counts.scope.undeclared_used_layer_count == 1
    assert {(item.entity_type, item.entity_count) for item in type_counts.counts} == {
        ("LINE", 1),
        ("POINT", 1),
    }
    assert {
        (item.layer, item.entity_type, item.entity_count) for item in combined_counts.counts
    } == {
        ("Gas", "LINE", 1),
        ("Other", "POINT", 1),
    }

    section_counts = {}
    for section in EntitySection:
        result = await service.get_entity(
            scope,
            CadGetEntityRequest(
                query=CadEntityQuery(
                    package_alias="data_001", entity_key=entity_key, section=section
                )
            ),
        )
        section_counts[section] = result.page.total_matched_count
        assert result.entity.entity_key == entity_key
        assert result.section is section
    assert section_counts == {
        EntitySection.GEOMETRY_ELEMENTS: 2,
        EntitySection.XDATA_TAGS: 3,
        EntitySection.RELATIONSHIPS: 1,
        EntitySection.RAW_TAGS: 13,
    }


async def test_xdata_rows_and_profiles_preserve_raw_values_and_add_projection(
    tmp_path: Path,
) -> None:
    service, scope, _state, _repository, _artifacts, _resolver = await _imported_runtime(tmp_path)
    rows = await service.query_xdata(
        scope,
        CadQueryXdataRequest(
            query=CadXdataQuery(package_alias="data_001", mode=XdataQueryMode.ROWS)
        ),
    )
    appids = await service.query_xdata(
        scope,
        CadQueryXdataRequest(
            query=CadXdataQuery(
                package_alias="data_001",
                mode=XdataQueryMode.PROFILE,
                profile_dimension=XdataProfileDimension.APPID,
            )
        ),
    )
    group_codes = await service.query_xdata(
        scope,
        CadQueryXdataRequest(
            query=CadXdataQuery(
                package_alias="data_001",
                mode=XdataQueryMode.PROFILE,
                profile_dimension=XdataProfileDimension.GROUP_CODE,
            )
        ),
    )
    assignments = await service.query_xdata(
        scope,
        CadQueryXdataRequest(
            query=CadXdataQuery(
                package_alias="data_001",
                mode=XdataQueryMode.PROFILE,
                profile_dimension=XdataProfileDimension.ASSIGNMENT_KEY,
            )
        ),
    )

    projected = {row.assignment_key: row for row in rows.rows if row.assignment_key}
    assert set(projected) == {"Diameter", "Label", "SINGLE"}
    assert projected["Diameter"].raw_lexeme == "Diameter=80"
    assert projected["Diameter"].assignment_value is not None
    assert projected["Diameter"].assignment_value.value == "80"
    assert {item.appid for item in appids.profiles} == {
        "APP_TEST",
        "SINGLE",
        "UNUSED_APP",
    }
    unused = next(item for item in appids.profiles if item.appid == "UNUSED_APP")
    assert unused.declared_in_appid_table is True
    assert unused.entity_count == unused.payload_tag_count == 0
    assert {item.group_code for item in group_codes.profiles} == {1000, 1001, 1040}
    diameter = next(item for item in assignments.profiles if item.assignment_key == "Diameter")
    assert diameter.missing_entity_count == 1
    assert diameter.distinct_value_count == 1
    assert diameter.values.value_enumeration_complete is True
    assert diameter.numeric_profile.applicable is True
    assert (diameter.numeric_profile.minimum, diameter.numeric_profile.maximum) == ("80", "80")


async def test_paging_cursor_is_retryable_but_bound_to_tool_scope_and_key(
    tmp_path: Path,
) -> None:
    service, scope, _state, repository, artifacts, resolver = await _imported_runtime(tmp_path)
    first = await service.query_entities(
        scope,
        CadQueryEntitiesRequest(
            query=CadEntitiesQuery(package_alias="data_001", mode=EntityQueryMode.ROWS, page_size=1)
        ),
    )
    assert first.page.next_cursor is not None
    cursor = first.page.next_cursor

    retry = await service.query_entities(scope, CadQueryEntitiesRequest(cursor=cursor))
    assert retry == await service.query_entities(scope, CadQueryEntitiesRequest(cursor=cursor))
    restarted = DataQueryService(repository, artifacts, CursorCodec(_cursor_key()))
    assert await restarted.query_entities(scope, CadQueryEntitiesRequest(cursor=cursor)) == retry

    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    with pytest.raises(DataQueryApplicationError) as invalid:
        await service.query_entities(scope, CadQueryEntitiesRequest(cursor=tampered))
    assert invalid.value.code == "CURSOR_INVALID"
    with pytest.raises(DataQueryApplicationError) as wrong_tool:
        await service.query_rows(scope, DataQueryRowsRequest(cursor=cursor))
    assert wrong_tool.value.code == "CURSOR_TOOL_MISMATCH"

    other = await resolver.resolve("direct_001", command_id="same_scope_new_command")
    assert await service.query_entities(other, CadQueryEntitiesRequest(cursor=cursor)) == retry
    second_scope = scope.model_copy(update={"run_id": "different-run"})
    with pytest.raises(DataQueryApplicationError) as wrong_scope:
        await service.query_entities(second_scope, CadQueryEntitiesRequest(cursor=cursor))
    assert wrong_scope.value.code == "CURSOR_SCOPE_MISMATCH"

    wrong_key_service = DataQueryService(repository, artifacts, CursorCodec(_cursor_key(b"z")))
    with pytest.raises(DataQueryApplicationError) as wrong_key:
        await wrong_key_service.query_entities(scope, CadQueryEntitiesRequest(cursor=cursor))
    assert wrong_key.value.code == "CURSOR_INVALID"


async def test_invalid_queries_budgets_and_queries_have_no_durable_side_effects(
    tmp_path: Path,
) -> None:
    service, scope, state, repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    durable_counts = (
        len(state.critical_operations),
        len(state.data_packages),
        len(state.artifacts),
        sum(len(entries) for entries in state.data_catalog_entries.values()),
    )

    with pytest.raises(ValidationError):
        DataQueryRowsRequest()
    with pytest.raises(ValidationError):
        DataQueryRowsRequest(
            query=DataRowsQuery(package_alias="data_001", payload_name="entities", select=()),
            cursor="cur.invalid",
        )
    for invalid_group_code in (True, 1000.0):
        with pytest.raises(ValidationError):
            XdataFilters(group_codes=(invalid_group_code,))
    with pytest.raises(ValidationError):
        DataRowsQuery.model_validate(
            {
                "package_alias": "data_001",
                "payload_name": "entities",
                "select": [],
                "page_size": "100",
            }
        )
    with pytest.raises(ValidationError):
        EntityFilters.model_validate({"typed_geometry_supported": "false"})
    with pytest.raises(ValidationError):
        FilterCondition(field="float_value", op=FilterOperator.EQ, value=10**400)

    long_query = CadEntitiesQuery(
        package_alias="data_001",
        mode=EntityQueryMode.ROWS,
        filters=EntityFilters(
            entity_keys=tuple(f"entity-{index:03d}-" + "x" * 240 for index in range(30))
        ),
        page_size=1,
    )
    unmatched = await service.query_entities(scope, CadQueryEntitiesRequest(query=long_query))
    assert unmatched.rows == ()
    assert unmatched.page.total_matched_count == 0
    assert unmatched.page.complete is True
    assert unmatched.page.next_cursor is None
    with pytest.raises(DataQueryApplicationError) as missing_package:
        await service.describe_package(scope, DataDescribePackageQuery(package_alias="data_999"))
    assert missing_package.value.code == "PACKAGE_NOT_FOUND"
    with pytest.raises(DataQueryApplicationError) as missing_field:
        await service.query_rows(
            scope,
            DataQueryRowsRequest(
                query=DataRowsQuery(
                    package_alias="data_001", payload_name="entities", select=("unknown",)
                )
            ),
        )
    assert missing_field.value.code == "FIELD_NOT_FOUND"
    with pytest.raises(DataQueryApplicationError) as invalid_type:
        await service.query_rows(
            scope,
            DataQueryRowsRequest(
                query=DataRowsQuery(
                    package_alias="data_001",
                    payload_name="entities",
                    select=("entity_type",),
                    filters=(FilterCondition(field="entity_type", op=FilterOperator.GT, value=1),),
                )
            ),
        )
    assert invalid_type.value.code == "FILTER_TYPE_MISMATCH"

    tiny_budget = DataQueryService(
        repository,
        artifacts,
        CursorCodec(_cursor_key()),
        policy=DataQueryPolicy(scan_row_budget=1),
    )
    with pytest.raises(DataQueryApplicationError) as budget:
        await tiny_budget.query_entities(
            scope,
            CadQueryEntitiesRequest(
                query=CadEntitiesQuery(package_alias="data_001", mode=EntityQueryMode.ROWS)
            ),
        )
    assert budget.value.code == "QUERY_BUDGET_EXCEEDED"
    assert "exceeded=scan_rows" in str(budget.value)
    assert "scanned_rows=" in str(budget.value) and "scan_row_budget=1" in str(budget.value)

    tiny_byte_budget = DataQueryService(
        repository,
        artifacts,
        CursorCodec(_cursor_key()),
        policy=DataQueryPolicy(decoded_projection_byte_budget=1),
    )
    with pytest.raises(DataQueryApplicationError) as byte_budget:
        await tiny_byte_budget.query_entities(
            scope,
            CadQueryEntitiesRequest(
                query=CadEntitiesQuery(package_alias="data_001", mode=EntityQueryMode.ROWS)
            ),
        )
    assert byte_budget.value.code == "QUERY_BUDGET_EXCEEDED"
    assert "exceeded=decoded_projection_bytes" in str(byte_budget.value)
    assert "decoded_bytes=" in str(byte_budget.value)
    assert "decoded_projection_byte_budget=1" in str(byte_budget.value)

    ticks = iter((0.0, 20.0, 20.0, 20.0))
    timeout_service = DataQueryService(
        repository,
        artifacts,
        CursorCodec(_cursor_key()),
        policy=DataQueryPolicy(query_timeout_seconds=1.0),
        clock=lambda: next(ticks, 20.0),
    )
    with pytest.raises(DataQueryApplicationError) as timeout:
        await timeout_service.query_entities(
            scope,
            CadQueryEntitiesRequest(
                query=CadEntitiesQuery(package_alias="data_001", mode=EntityQueryMode.ROWS)
            ),
        )
    assert timeout.value.code == "QUERY_TIMEOUT"

    assert durable_counts == (
        len(state.critical_operations),
        len(state.data_packages),
        len(state.artifacts),
        sum(len(entries) for entries in state.data_catalog_entries.values()),
    )


async def test_rows_budget_counts_mandatory_keys_before_filtering_and_paging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scope, state, repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    await _add_derived_package(scope, state, artifacts)
    policy = DataQueryPolicy()
    assert policy.decoded_projection_byte_budget == 256 * 1024 * 1024
    assert policy.scan_row_budget == 2_000_000 and policy.query_timeout_seconds == 15.0
    projected = data_query_service_module.pa.table({"candidate_key": ["candidate_002", "candidate_001"]})
    # The requested result is empty, yet both logical-key rows must be decoded.
    byte_limit = projected.nbytes - 1
    limited = DataQueryService(repository, artifacts, CursorCodec(_cursor_key()),
        policy=DataQueryPolicy(scan_row_budget=1, decoded_projection_byte_budget=byte_limit))
    materialized_paths = []
    materialize = limited._materialize_members

    async def record_materialized_paths(*args):
        paths = await materialize(*args)
        materialized_paths.extend(paths.values())
        return paths

    monkeypatch.setattr(limited, "_materialize_members", record_materialized_paths)
    messages = []
    for page_size in (1, 200):
        with pytest.raises(DataQueryApplicationError) as caught:
            await limited.query_rows(scope, DataQueryRowsRequest(query=DataRowsQuery(
                package_alias="data_002", payload_name="connection_candidates", select=(),
                filters=(FilterCondition(field="candidate_key", op=FilterOperator.EQ, value="PRIVATE_FILTER_VALUE"),),
                page_size=page_size,
            )))
        assert caught.value.code == "QUERY_BUDGET_EXCEEDED"
        messages.append(str(caught.value))
    assert messages[0] == messages[1]
    message = messages[0]
    assert "exceeded=scan_rows,decoded_projection_bytes" in message
    assert "scanned_rows=2; scan_row_budget=1" in message
    assert f"decoded_bytes={projected.nbytes}; decoded_projection_byte_budget={byte_limit}" in message
    assert "matching_total=unknown" in message
    assert "candidate_key" in message and "select cannot remove" in message
    assert "page_size only limits returned rows" in message and "data_query_sql" in message
    assert "PRIVATE_FILTER_VALUE" not in message and str(tmp_path) not in message
    assert len(materialized_paths) == 2 and all(not path.exists() for path in materialized_paths)
    result = await service.query_rows(scope, DataQueryRowsRequest(query=DataRowsQuery(
        package_alias="data_002", payload_name="connection_candidates", select=(), page_size=1,
    )))
    assert result.page.total_matched_count == 2 and len(result.rows) == 1
    assert result.rows[0].key_values["candidate_key"] == "candidate_001"


async def test_async_deadline_and_failed_stream_remove_partial_query_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scope, _state, repository, artifacts, _resolver = await _imported_runtime(tmp_path)

    async def slow_alias_lookup(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(repository, "get_catalog_entry_by_alias", slow_alias_lookup)
    timeout_service = DataQueryService(
        repository,
        artifacts,
        CursorCodec(_cursor_key()),
        policy=DataQueryPolicy(describe_timeout_seconds=0.01),
    )
    with pytest.raises(DataQueryApplicationError) as timeout:
        await timeout_service.describe_package(
            scope, DataDescribePackageQuery(package_alias="data_001")
        )
    assert timeout.value.code == "QUERY_TIMEOUT"

    monkeypatch.undo()
    original_stream_version = artifacts.stream_version

    async def broken_stream_version(owner_id: str, artifact_id: str, version_id: str):
        stream = await original_stream_version(owner_id, artifact_id, version_id)

        async def chunks():
            yield b"partial-parquet"
            raise RuntimeError("injected stream failure")

        return ArtifactStream(artifact=stream.artifact, chunks=chunks())

    monkeypatch.setattr(artifacts, "stream_version", broken_stream_version)
    before = set(Path(tempfile.gettempdir()).glob("cad2bim-exact-payload-*.parquet"))
    with pytest.raises(DataQueryApplicationError) as stream_failure:
        await service.describe_package(
            scope,
            DataDescribePackageQuery(
                package_alias="data_001",
                payload_name="entities",
            ),
        )
    assert stream_failure.value.code == "PACKAGE_INTEGRITY_ERROR"
    assert "injected stream failure" not in str(stream_failure.value)
    after = set(Path(tempfile.gettempdir()).glob("cad2bim-exact-payload-*.parquet"))
    assert after == before


async def test_sync_arrow_scan_runs_off_loop_and_cannot_return_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scope, _state, repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    timeout_service = DataQueryService(
        repository,
        artifacts,
        CursorCodec(_cursor_key()),
        policy=DataQueryPolicy(query_timeout_seconds=0.01),
    )

    completed = threading.Event()
    observed_paths = []

    def blocking_scan(*_args, **_kwargs):
        path = _args[0]
        observed_paths.append(path)
        time.sleep(0.1)
        assert path.is_file(), "a cancelled await must not delete a running thread's input"
        completed.set()
        return 0, []

    monkeypatch.setattr(timeout_service, "_scan_member_page", blocking_scan)
    started = time.monotonic()
    with pytest.raises(DataQueryApplicationError) as timeout:
        await timeout_service.query_rows(
            scope,
            DataQueryRowsRequest(
                query=DataRowsQuery(
                    package_alias="data_001",
                    payload_name="entities",
                    select=("entity_key",),
                )
            ),
        )

    assert timeout.value.code == "QUERY_TIMEOUT"
    assert time.monotonic() - started < 0.08
    async with asyncio.timeout(1):
        while not completed.is_set() or observed_paths[0].exists():
            await asyncio.sleep(0.005)


async def test_cancelled_materialization_removes_partial_query_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scope, _state, _repository, artifacts, _resolver = await _imported_runtime(tmp_path)
    original_stream_version = artifacts.stream_version
    partial_written = asyncio.Event()
    never_finishes = asyncio.Event()

    async def cancelled_stream_version(owner_id: str, artifact_id: str, version_id: str):
        stream = await original_stream_version(owner_id, artifact_id, version_id)

        async def chunks():
            yield b"partial-parquet"
            partial_written.set()
            await never_finishes.wait()

        return ArtifactStream(artifact=stream.artifact, chunks=chunks())

    monkeypatch.setattr(artifacts, "stream_version", cancelled_stream_version)
    before = set(Path(tempfile.gettempdir()).glob("cad2bim-exact-payload-*.parquet"))
    task = asyncio.create_task(
        service.describe_package(
            scope,
            DataDescribePackageQuery(
                package_alias="data_001",
                payload_name="entities",
            ),
        )
    )
    await partial_written.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    after = set(Path(tempfile.gettempdir()).glob("cad2bim-exact-payload-*.parquet"))
    assert after == before
