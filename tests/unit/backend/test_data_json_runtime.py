from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.artifact_service import ArtifactStream
from CAD2BIM_backend.application.data_json_service import (
    DataJsonApplicationError,
    DataJsonPolicy,
    DataJsonService,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode
from CAD2BIM_backend.domain.data_json import (
    JsonChildWithPreview,
    JsonChildWithoutPreview,
    JsonChildrenQuery,
    JsonErrorCode,
    JsonReadRequest,
    JsonRecordsQuery,
    JsonValueQuery,
)
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef,
    CatalogResourceKind,
    DataCatalogEntry,
    DataPackageMemberRecord,
    DataPackageRecord,
    JsonPayloadKind,
    PackageMemberKind,
    PublishedDataPackage,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_mcp.direct.query.json_inputs import (
    DirectDataReadJsonInput,
    DirectJsonChildrenInput,
    DirectJsonContinuationInput,
    DirectJsonRecordsInput,
    DirectJsonValueInput,
    to_domain_json_query,
    validate_direct_data_read_json_input,
)


def _cursor_key(byte: bytes = b"j") -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=")


def _scope(*, command_id: str = "command_json", tenant: str = "tenant_json") -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id=tenant,
            principal_id="principal_json",
            task_id="task_json",
            actor_kind=TenantActorKind.USER,
            command_id=command_id,
        ),
        owner_id="owner_json",
        run_id="run_json",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=1,
    )


class _Artifacts:
    def __init__(self, values: dict[str, tuple[ArtifactRecord, bytes]]) -> None:
        self.values = values

    async def stream_version(self, owner_id: str, artifact_id: str, version_id: str):
        record, content = self.values[artifact_id]
        assert owner_id == record.owner_id
        assert version_id == record.version_id

        async def chunks():
            midpoint = len(content) // 2
            yield content[:midpoint]
            yield content[midpoint:]

        return ArtifactStream(artifact=record, chunks=chunks())


class _Payloads:
    def __init__(self, package: PublishedDataPackage) -> None:
        self.package = package

    async def package_by_alias(self, _scope: InvocationScope, alias: str):
        if alias != self.package.catalog_entry.alias:
            raise AssertionError("test requested an unexpected alias")
        return self.package


def _runtime(
    value: object,
    *,
    json_kind: JsonPayloadKind = JsonPayloadKind.DOCUMENT,
    key: str | None = None,
    policy: DataJsonPolicy | None = None,
) -> tuple[DataJsonService, PublishedDataPackage, _Artifacts]:
    payload = canonical_json(value).encode("utf-8")
    schema = b'{"$schema":"https://json-schema.org/draft/2020-12/schema"}'
    now = datetime.now(UTC)
    records: dict[str, tuple[ArtifactRecord, bytes]] = {}

    def artifact(name: str, content: bytes, media_type: str) -> ArtifactRecord:
        digest = hashlib.sha256(content).hexdigest()
        record = ArtifactRecord(
            artifact_id=f"artifact_{name}",
            version_id=f"version_{name}",
            owner_id="owner_json",
            task_id="task_json",
            kind="data_payload" if name == "payload" else "data_schema",
            filename=f"{name}.json",
            content_type=media_type,
            size_bytes=len(content),
            sha256=digest,
            idempotency_key=f"json-{name}",
            created_at=now,
            run_id="run_json",
        )
        records[record.artifact_id] = (record, content)
        return record

    payload_artifact = artifact("payload", payload, "application/json")
    schema_artifact = artifact("schema", schema, "application/schema+json")
    package_record = DataPackageRecord(
        package_id="package_json",
        owner_id="owner_json",
        tenant_id="tenant_json",
        task_id="task_json",
        created_by_run_id="run_json",
        name="json_fixture",
        package_kind="derived",
        summary="Immutable JSON fixture for bounded reads.",
        idempotency_key="package-json-fixture",
        request_fingerprint="a" * 64,
        created_at=now,
    )
    common = {
        "owner_id": "owner_json",
        "tenant_id": "tenant_json",
        "task_id": "task_json",
        "package_id": package_record.package_id,
        "created_at": now,
    }
    payload_member = DataPackageMemberRecord(
        member_id="member_json_payload",
        member_name="payloads/topology_summary.json",
        member_kind=PackageMemberKind.PAYLOAD,
        artifact=ArtifactVersionRef(
            artifact_id=payload_artifact.artifact_id,
            version_id=payload_artifact.version_id,
            sha256=payload_artifact.sha256,
        ),
        media_type="application/json",
        logical_payload_name="topology_summary",
        payload_description="Topology summary fixture.",
        logical_byte_count=len(payload),
        json_kind=json_kind,
        **common,
    )
    schema_member = DataPackageMemberRecord(
        member_id="member_json_schema",
        member_name="schemas/topology_summary.schema.json",
        member_kind=PackageMemberKind.SCHEMA,
        artifact=ArtifactVersionRef(
            artifact_id=schema_artifact.artifact_id,
            version_id=schema_artifact.version_id,
            sha256=schema_artifact.sha256,
        ),
        media_type="application/schema+json",
        **common,
    )
    catalog = DataCatalogEntry(
        catalog_entry_id="catalog_json",
        owner_id="owner_json",
        tenant_id="tenant_json",
        task_id="task_json",
        run_id="run_json",
        resource_kind=CatalogResourceKind.PACKAGE,
        alias="data_020",
        ordinal=20,
        catalog_revision=1,
        package_id=package_record.package_id,
        created_at=now,
    )
    package = PublishedDataPackage(
        package=package_record,
        members=(payload_member, schema_member),
        catalog_entry=catalog,
    )
    artifacts = _Artifacts(records)
    codec = CursorCodec(key or _cursor_key())
    service = DataJsonService(_Payloads(package), artifacts, codec, policy)
    return service, package, artifacts


def test_direct_json_inputs_are_complete_closed_branches() -> None:
    inputs = (
        {
            "direct_session_alias": "direct_001",
            "query": {
                "mode": "value",
                "package_alias": "data_020",
                "payload_name": "topology_summary",
                "pointer": "/metrics",
            },
        },
        {
            "direct_session_alias": "direct_001",
            "query": {
                "mode": "children",
                "package_alias": "data_020",
                "payload_name": "topology_summary",
                "pointer": "",
            },
        },
        {
            "direct_session_alias": "direct_001",
            "query": {
                "mode": "records",
                "package_alias": "data_020",
                "payload_name": "topology_summary",
            },
        },
        {"direct_session_alias": "direct_001", "cursor": "cur.opaque"},
    )

    parsed = [validate_direct_data_read_json_input(value) for value in inputs]
    assert isinstance(parsed[0], DirectJsonValueInput)
    assert isinstance(parsed[1], DirectJsonChildrenInput)
    assert isinstance(parsed[2], DirectJsonRecordsInput)
    assert isinstance(parsed[3], DirectJsonContinuationInput)
    assert to_domain_json_query(parsed[1].query).page_size == 50
    assert to_domain_json_query(parsed[2].query).page_size == 100

    with pytest.raises(ValidationError):
        validate_direct_data_read_json_input(
            {
                "direct_session_alias": "direct_001",
                "cursor": "cur.opaque",
                "query": inputs[0]["query"],
            }
        )
    with pytest.raises(ValidationError, match="4,096 UTF-8 bytes"):
        validate_direct_data_read_json_input(
            {
                "direct_session_alias": "direct_001",
                "query": {
                    "mode": "value",
                    "package_alias": "data_020",
                    "payload_name": "topology_summary",
                    "pointer": "/" + "中" * 1_366,
                },
            }
        )

    schema = TypeAdapter(DirectDataReadJsonInput).json_schema()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert not ({"allOf", "if", "then", "else", "not"} & value.keys())
            assert value.get("additionalProperties") is not True
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(schema)


async def test_value_supports_rfc6901_and_returns_complete_canonical_json() -> None:
    service, _package, _artifacts = _runtime({"a/b": {"~key": {"items": [1, 2], "ok": True}}})

    result = await service.read_json(
        _scope(),
        JsonReadRequest(
            query=JsonValueQuery(
                mode="value",
                package_alias="data_020",
                payload_name="topology_summary",
                pointer="/a~1b/~0key",
            )
        ),
    )

    assert result.pointer == "/a~1b/~0key"
    assert result.node_type == "object"
    assert result.value_json == '{"items":[1,2],"ok":true}'
    assert result.size_bytes == len(result.value_json.encode("utf-8"))


async def test_children_pages_complete_items_and_cursor_survives_restart() -> None:
    value = {
        "small": {"answer": 42},
        "large": "x" * 5_000,
        "last": [True, None],
    }
    first_service, package, artifacts = _runtime(value)
    request = JsonReadRequest(
        query=JsonChildrenQuery(
            mode="children",
            package_alias="data_020",
            payload_name="topology_summary",
            pointer="",
            page_size=2,
        )
    )

    first = await first_service.read_json(_scope(command_id="first"), request)
    replay = await first_service.read_json(_scope(command_id="retry"), request)
    restarted = DataJsonService(_Payloads(package), artifacts, CursorCodec(_cursor_key()))
    second = await restarted.read_json(
        _scope(command_id="continuation"),
        JsonReadRequest(cursor=first.page.next_cursor),
    )

    assert first == replay
    assert [item.selector for item in first.children] == ["large", "last"]
    assert isinstance(first.children[0], JsonChildWithoutPreview)
    assert isinstance(first.children[1], JsonChildWithPreview)
    assert second.page.complete is True
    assert [item.selector for item in second.children] == ["small"]
    assert len(first.model_dump_json().encode("utf-8")) <= 1024 * 1024


async def test_records_use_100_200_paging_and_complete_record_json() -> None:
    service, _package, _artifacts = _runtime(
        [{"index": index, "label": f"r{index}"} for index in range(205)],
        json_kind=JsonPayloadKind.RECORDS,
    )

    first = await service.read_json(
        _scope(),
        JsonReadRequest(
            query=JsonRecordsQuery(
                mode="records",
                package_alias="data_020",
                payload_name="topology_summary",
                page_size=200,
            )
        ),
    )
    second = await service.read_json(_scope(), JsonReadRequest(cursor=first.page.next_cursor))

    assert len(first.records) == 200
    assert first.records[0].record_json == '{"index":0,"label":"r0"}'
    assert [record.record_index for record in second.records] == list(range(200, 205))
    assert second.page.complete is True


async def test_response_budget_returns_fewer_complete_records_with_cursor() -> None:
    service, _package, _artifacts = _runtime(
        [{"text": str(index) + "x" * 300_000} for index in range(5)],
        json_kind=JsonPayloadKind.RECORDS,
    )

    first = await service.read_json(
        _scope(),
        JsonReadRequest(
            query=JsonRecordsQuery(
                mode="records",
                package_alias="data_020",
                payload_name="topology_summary",
                page_size=5,
            )
        ),
    )
    second = await service.read_json(_scope(), JsonReadRequest(cursor=first.page.next_cursor))

    assert 0 < len(first.records) < 5
    assert all(record.record_json.endswith('xxxxx"}') for record in first.records)
    assert first.page.next_cursor is not None
    assert len(first.model_dump_json().encode("utf-8")) <= 1024 * 1024
    assert second.records[0].record_index == len(first.records)


@pytest.mark.parametrize(
    ("query", "code"),
    (
        (
            JsonValueQuery(
                mode="value",
                package_alias="data_020",
                payload_name="topology_summary",
                pointer="not-a-pointer",
            ),
            "JSON_POINTER_INVALID",
        ),
        (
            JsonChildrenQuery(
                mode="children",
                package_alias="data_020",
                payload_name="topology_summary",
                pointer="/scalar",
            ),
            "JSON_READ_MODE_MISMATCH",
        ),
        (
            JsonRecordsQuery(
                mode="records",
                package_alias="data_020",
                payload_name="topology_summary",
            ),
            "JSON_READ_MODE_MISMATCH",
        ),
    ),
)
async def test_pointer_and_mode_errors_are_stable(query, code: str) -> None:
    service, _package, _artifacts = _runtime({"scalar": 1})
    with pytest.raises(DataJsonApplicationError) as raised:
        await service.read_json(_scope(), JsonReadRequest(query=query))
    assert raised.value.code == code
    assert raised.value.phase == "read"
    assert raised.value.retryable is False


async def test_value_and_record_limits_never_truncate_json() -> None:
    value_service, _package, _artifacts = _runtime({"large": "x" * (512 * 1024)})
    with pytest.raises(DataJsonApplicationError) as value_error:
        await value_service.read_json(
            _scope(),
            JsonReadRequest(
                query=JsonValueQuery(
                    mode="value",
                    package_alias="data_020",
                    payload_name="topology_summary",
                    pointer="/large",
                )
            ),
        )
    assert value_error.value.code == "JSON_VALUE_TOO_LARGE"

    record_service, _package, _artifacts = _runtime(
        [{"large": "x" * (512 * 1024)}],
        json_kind=JsonPayloadKind.RECORDS,
    )
    with pytest.raises(DataJsonApplicationError) as record_error:
        await record_service.read_json(
            _scope(),
            JsonReadRequest(
                query=JsonRecordsQuery(
                    mode="records",
                    package_alias="data_020",
                    payload_name="topology_summary",
                )
            ),
        )
    assert record_error.value.code == "JSON_VALUE_TOO_LARGE"


async def test_cursor_binds_scope_payload_schema_mode_pointer_and_position() -> None:
    service, package, artifacts = _runtime({"items": [1, 2, 3]})
    first = await service.read_json(
        _scope(),
        JsonReadRequest(
            query=JsonChildrenQuery(
                mode="children",
                package_alias="data_020",
                payload_name="topology_summary",
                pointer="/items",
                page_size=1,
            )
        ),
    )
    assert first.page.next_cursor is not None

    with pytest.raises(DataJsonApplicationError) as scope_error:
        await service.read_json(
            _scope(tenant="tenant_other"),
            JsonReadRequest(cursor=first.page.next_cursor),
        )
    assert scope_error.value.code == "JSON_CURSOR_INVALID"

    schema = package.members[1]
    altered_schema = schema.model_copy(
        update={
            "artifact": schema.artifact.model_copy(update={"sha256": "f" * 64}),
        }
    )
    altered_package = package.model_copy(update={"members": (package.members[0], altered_schema)})
    altered = DataJsonService(_Payloads(altered_package), artifacts, CursorCodec(_cursor_key()))
    with pytest.raises(DataJsonApplicationError) as binding_error:
        await altered.read_json(_scope(), JsonReadRequest(cursor=first.page.next_cursor))
    assert binding_error.value.code == "JSON_CURSOR_INVALID"


def test_json_error_code_set_is_exact_and_stable() -> None:
    assert {item.value for item in JsonErrorCode} == {
        "JSON_SCHEMA_INVALID",
        "JSON_INSTANCE_INVALID",
        "JSON_OUTPUT_BUDGET_EXCEEDED",
        "JSON_POINTER_INVALID",
        "JSON_READ_MODE_MISMATCH",
        "JSON_VALUE_TOO_LARGE",
        "JSON_CURSOR_INVALID",
    }
