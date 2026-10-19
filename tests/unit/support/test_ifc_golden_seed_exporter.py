from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from tests.process_support.ifc_golden_seed.artifact_inventory import (
    ArtifactInventoryError,
    MappingArtifactReader,
    materialize_artifact_inventory,
)
from tests.process_support.ifc_golden_seed.closure import (
    SeedClosureError,
    SeedExportScope,
    SeedSnapshot,
    build_export_closure,
)
from tests.process_support.ifc_golden_seed.exporter import (
    SeedExportError,
    SeedExportMetadata,
    canonical_database_rows_digest,
    export_seed,
)
from tests.process_support.ifc_golden_seed.core import MAX_OBJECT_BYTES
from tests.process_support.ifc_golden_seed import exporter as exporter_module
from tests.process_support.ifc_golden_seed.postgres_adapter import (
    POSTGRES_SNAPSHOT_EXECUTION_OPTIONS,
)


NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


def _scope() -> SeedExportScope:
    return SeedExportScope.normalized(
        tenant_id="tenant-source",
        principal_id="principal-source",
        owner_id="owner-source",
        task_id="task-source",
        run_id="run-source",
    )


def _metadata() -> SeedExportMetadata:
    return SeedExportMetadata(
        source_commit="a" * 40,
        source_migration_head="20260830_0064",
        source_m4_round="round-6",
        source_arm="A",
        source_direct_session_alias="direct_018",
        created_at=NOW,
    )


def _synthetic_snapshot() -> tuple[SeedSnapshot, dict[tuple[str, str], bytes]]:
    first = b"first artifact split across chunks"
    late = b"late artifact"

    def scoped(**values: object) -> dict[str, object]:
        return {
            "tenant_id": "tenant-source",
            "owner_id": "owner-source",
            "task_id": "task-source",
            **values,
        }

    catalog_entries = [
        scoped(
            catalog_entry_id="catalog-p1",
            run_id="run-source",
            resource_kind="package",
            alias="data_001",
            ordinal=1,
            catalog_revision=1,
            package_id="package-1",
            operation_id=None,
            created_at=NOW,
        ),
        scoped(
            catalog_entry_id="catalog-o1",
            run_id="run-source",
            resource_kind="operation",
            alias="op_001",
            ordinal=1,
            catalog_revision=1,
            package_id=None,
            operation_id="operation-1",
            created_at=NOW,
        ),
        scoped(
            catalog_entry_id="catalog-p2",
            run_id="run-source",
            resource_kind="package",
            alias="data_002",
            ordinal=2,
            catalog_revision=2,
            package_id="package-late",
            operation_id=None,
            created_at=NOW,
        ),
        scoped(
            catalog_entry_id="catalog-o2",
            run_id="run-source",
            resource_kind="operation",
            alias="op_002",
            ordinal=2,
            catalog_revision=2,
            package_id=None,
            operation_id="operation-late",
            created_at=NOW,
        ),
    ]
    packages = [
        scoped(
            package_id="package-1",
            created_by_run_id="run-source",
            name="first",
            package_kind="derived",
            source_filename=None,
            description_title="first",
            summary="first",
            section_titles_json=[],
            idempotency_key="package-first",
            request_fingerprint="1" * 64,
            created_at=NOW,
        ),
        scoped(
            package_id="package-late",
            created_by_run_id="run-source",
            name="late",
            package_kind="derived",
            source_filename=None,
            description_title="late",
            summary="late",
            section_titles_json=[],
            idempotency_key="package-late",
            request_fingerprint="2" * 64,
            created_at=NOW,
        ),
    ]
    members = [
        scoped(
            member_id="member-1",
            package_id="package-1",
            member_name="payloads/first.parquet",
            member_kind="payload",
            artifact_id="artifact-1",
            artifact_version_id="version-1",
            artifact_sha256=hashlib.sha256(first).hexdigest(),
            media_type="application/vnd.apache.parquet",
            row_count=1,
            logical_payload_name="first",
            payload_description="first payload",
            logical_key_fields_json=["id"],
            arrow_schema_json=[{"name": "id", "type": "int64", "nullable": False}],
            logical_byte_count=len(first),
            json_kind=None,
            created_at=NOW,
        ),
        scoped(
            member_id="member-late",
            package_id="package-late",
            member_name="payloads/late.parquet",
            member_kind="payload",
            artifact_id="artifact-late",
            artifact_version_id="version-late",
            artifact_sha256=hashlib.sha256(late).hexdigest(),
            media_type="application/vnd.apache.parquet",
            row_count=1,
            logical_payload_name="late",
            payload_description="late payload",
            logical_key_fields_json=["id"],
            arrow_schema_json={
                "fields": [{"name": "id", "type": "int64", "nullable": False}]
            },
            logical_byte_count=len(late),
            json_kind=None,
            created_at=NOW,
        ),
    ]

    def operation(identity: str, result: str, fingerprint: str) -> dict[str, object]:
        return scoped(
            operation_id=identity,
            run_id="run-source",
            operation_kind="data_transform",
            initiator="direct_client",
            tool_name="data_transform",
            intent_summary="synthetic transform",
            source_filename=None,
            idempotency_key=identity,
            request_fingerprint=fingerprint,
            status="succeeded",
            result_package_id=result,
            error_code=None,
            error_message=None,
            error_phase=None,
            code_artifact_id=None,
            code_artifact_version_id=None,
            code_artifact_sha256=None,
            description_artifact_id=None,
            description_artifact_version_id=None,
            description_artifact_sha256=None,
            repair_parent_operation_id=None,
            base_ifc_revision_id=None,
            expected_ifc_write_epoch=None,
            operation_detail_json={"kind": "data_transform"},
            started_at=NOW,
            finished_at=NOW,
            created_at=NOW,
        )

    operations = [
        operation("operation-1", "package-1", "3" * 64),
        operation("operation-late", "package-late", "4" * 64),
    ]
    artifacts = [
        scoped(
            artifact_id="artifact-1",
            run_id="run-source",
            kind="data_payload",
            filename="燃气数据.parquet",
            content_type="application/vnd.apache.parquet",
            status="available",
            current_version_id="version-1",
            idempotency_key="artifact-first",
            request_fingerprint="5" * 64,
            created_at=NOW,
            updated_at=NOW,
        ),
        scoped(
            artifact_id="artifact-late",
            run_id="run-source",
            kind="data_payload",
            filename="late.parquet",
            content_type="application/vnd.apache.parquet",
            status="available",
            current_version_id="version-late",
            idempotency_key="artifact-late",
            request_fingerprint="6" * 64,
            created_at=NOW,
            updated_at=NOW,
        ),
    ]

    def version(
        artifact_id: str, version_id: str, filename: str, content: bytes
    ) -> dict[str, object]:
        return scoped(
            version_id=version_id,
            artifact_id=artifact_id,
            version_no=1,
            storage_key="source-only-storage-key",
            filename=filename,
            content_type="application/vnd.apache.parquet",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            metadata_json={"logical": "payload"},
            lineage_json={"operation_id": "operation-1"},
            created_at=NOW,
        )

    artifact_versions = [
        version("artifact-1", "version-1", "燃气数据.parquet", first),
        version("artifact-late", "version-late", "late.parquet", late),
    ]
    return (
        SeedSnapshot.from_rows(
            catalog_entries=catalog_entries,
            packages=packages,
            package_members=members,
            operations=operations,
            operation_inputs=[],
            artifacts=artifacts,
            artifact_versions=artifact_versions,
        ),
        {
            ("artifact-1", "version-1"): first,
            ("artifact-late", "version-late"): late,
        },
    )


def test_scope_is_nfkc_trimmed_and_rejects_empty_values() -> None:
    scope = SeedExportScope.normalized(
        tenant_id=" tenant ",
        principal_id=" principal ",
        owner_id=" owner ",
        task_id=" ｔａｓｋ ",
        run_id=" run ",
    )

    assert scope.task_id == "task"
    assert scope.run_id == "run"
    with pytest.raises(SeedClosureError, match="owner_id"):
        SeedExportScope.normalized(
            tenant_id="tenant",
            principal_id="principal",
            owner_id=" ",
            task_id="task",
            run_id="run",
        )


@pytest.mark.asyncio
async def test_export_freezes_frontier_normalizes_scope_and_streams_artifacts(
    tmp_path: Path,
) -> None:
    snapshot, objects = _synthetic_snapshot()

    exported = await export_seed(
        snapshot=snapshot,
        artifact_reader=MappingArtifactReader(objects, chunk_size=3),
        output_root=tmp_path / "seed",
        scope=_scope(),
        metadata=_metadata(),
        catalog_frontier=1,
        operation_frontier=1,
        include_ifc=False,
    )

    rows_bytes = exported.database_rows_path.read_bytes()
    rows = json.loads(rows_bytes)
    assert exported.manifest["source_catalog_frontier"] == 1
    assert exported.manifest["source_operation_frontier"] == 1
    assert {row["package_id"] for row in rows["packages"]} == {"package-1"}
    assert {row["operation_id"] for row in rows["operations"]} == {"operation-1"}
    assert rows["scope"] == {
        "tenant_id": "__seed_tenant__",
        "owner_id": "__seed_owner__",
        "task_id": "__seed_task__",
        "run_id": "__seed_run__",
    }
    assert rows["artifact_versions"][0]["storage_key"] == "__seed_storage_key__"
    assert rows["artifact_versions"][0]["metadata_json"] == {"logical": "payload"}
    assert rows["artifact_versions"][0]["lineage_json"] == {
        "operation_id": "operation-1"
    }
    assert rows["operations"][0]["intent_summary"] == "synthetic transform"
    assert exported.manifest["database_rows_digest"] == hashlib.sha256(rows_bytes).hexdigest()
    artifact_item = next(
        item
        for item in exported.manifest["object_inventory"]
        if item.get("source_artifact_version_id") == "version-1"
    )
    assert artifact_item["filename"] == "燃气数据.parquet"
    assert not Path(artifact_item["path"]).is_absolute()
    assert (exported.root_path / artifact_item["path"]).read_bytes() == objects[
        ("artifact-1", "version-1")
    ]


@pytest.mark.asyncio
async def test_export_rejects_operation_frontier_mismatch_without_publication(
    tmp_path: Path,
) -> None:
    snapshot, objects = _synthetic_snapshot()

    with pytest.raises(SeedClosureError, match="operation frontier"):
        await export_seed(
            snapshot=snapshot,
            artifact_reader=MappingArtifactReader(objects),
            output_root=tmp_path / "seed",
            scope=_scope(),
            metadata=_metadata(),
            catalog_frontier=1,
            operation_frontier=2,
            include_ifc=False,
        )

    assert not (tmp_path / "seed").exists()


def test_catalog_alias_collision_is_rejected() -> None:
    snapshot, _objects = _synthetic_snapshot()
    rows = list(snapshot.catalog_entries)
    rows[1] = {**rows[1], "alias": "data_001"}
    drifted = SeedSnapshot.from_rows(
        catalog_entries=rows,
        packages=snapshot.packages,
        package_members=snapshot.package_members,
        operations=snapshot.operations,
        operation_inputs=snapshot.operation_inputs,
        artifacts=snapshot.artifacts,
        artifact_versions=snapshot.artifact_versions,
    )

    with pytest.raises(SeedClosureError, match="aliases"):
        build_export_closure(drifted, _scope(), catalog_frontier=1, include_ifc=False)


def test_nonterminal_operation_is_rejected() -> None:
    snapshot, _objects = _synthetic_snapshot()
    operations = list(snapshot.operations)
    operations[0] = {**operations[0], "status": "prepared", "finished_at": None}
    drifted = SeedSnapshot.from_rows(
        catalog_entries=snapshot.catalog_entries,
        packages=snapshot.packages,
        package_members=snapshot.package_members,
        operations=operations,
        operation_inputs=snapshot.operation_inputs,
        artifacts=snapshot.artifacts,
        artifact_versions=snapshot.artifact_versions,
    )

    with pytest.raises(SeedClosureError, match="indeterminate"):
        build_export_closure(drifted, _scope(), catalog_frontier=1, include_ifc=False)


@pytest.mark.parametrize("missing_version", (False, True))
def test_trusted_baseline_source_is_in_exact_seed_closure(missing_version: bool) -> None:
    snapshot, _objects = _synthetic_snapshot()
    source_artifact = {**snapshot.artifacts[-1], "artifact_id": "trusted-source",
                       "kind": "ifc_trusted_baseline_source", "current_version_id": "trusted-version"}
    source_version = {**snapshot.artifact_versions[-1], "artifact_id": "trusted-source",
                      "version_id": "trusted-version"}
    operation = {**snapshot.operations[0], "operation_kind": "ifc_model_write",
                 "tool_name": "ifc_trusted_baseline_load", "result_package_id": None,
                 "operation_detail_json": {"detail_kind": "ifc_trusted_baseline_load",
                     "source_asset": {"artifact_id": "trusted-source", "version_id": "trusted-version",
                                      "sha256": source_version["sha256"]}, "result": "baseline_loaded"}}
    snapshot = replace(snapshot, operations=(operation, *snapshot.operations[1:]),
                       artifacts=(*snapshot.artifacts, source_artifact),
                       artifact_versions=(*snapshot.artifact_versions,
                                          *((source_version,) if not missing_version else ())))
    if missing_version:
        with pytest.raises(SeedClosureError, match="missing exact version"):
            build_export_closure(snapshot, _scope(), catalog_frontier=1, include_ifc=False)
    else:
        closure = build_export_closure(snapshot, _scope(), catalog_frontier=1, include_ifc=False)
        assert source_artifact in closure.artifacts
        assert source_version in closure.artifact_versions
        reachability = next(row for row in closure.artifact_reachability if row.artifact_id == "trusted-source")
        assert "operation:operation-1:source_asset" in reachability.reverse_references


class _FailingReader:
    async def iter_chunks(
        self, *, artifact_id: str, version_id: str, version_no: int
    ) -> AsyncIterator[bytes]:
        del artifact_id, version_id, version_no
        yield b"first"
        raise RuntimeError("synthetic interruption")


@pytest.mark.asyncio
async def test_stream_failure_removes_partial_artifact(tmp_path: Path) -> None:
    snapshot, _objects = _synthetic_snapshot()
    closure = build_export_closure(
        snapshot, _scope(), catalog_frontier=1, operation_frontier=1, include_ifc=False
    )

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        await materialize_artifact_inventory(closure, _FailingReader(), tmp_path)

    assert not list((tmp_path / "objects").rglob("*")) or not any(
        path.is_file() for path in (tmp_path / "objects").rglob("*")
    )


@pytest.mark.asyncio
async def test_stream_stops_when_declared_size_is_exceeded(tmp_path: Path) -> None:
    snapshot, objects = _synthetic_snapshot()
    first_version = snapshot.artifact_versions[0]
    drifted = SeedSnapshot.from_rows(
        catalog_entries=snapshot.catalog_entries,
        packages=snapshot.packages,
        package_members=snapshot.package_members,
        operations=snapshot.operations,
        operation_inputs=snapshot.operation_inputs,
        artifacts=snapshot.artifacts,
        artifact_versions=({**first_version, "size_bytes": 1}, *snapshot.artifact_versions[1:]),
    )
    closure = build_export_closure(
        drifted, _scope(), catalog_frontier=1, operation_frontier=1, include_ifc=False
    )

    with pytest.raises(ArtifactInventoryError, match="declared limit"):
        await materialize_artifact_inventory(
            closure, MappingArtifactReader(objects, chunk_size=2), tmp_path
        )

    assert not any(path.is_file() for path in (tmp_path / "objects").rglob("*"))


@pytest.mark.asyncio
async def test_stream_rejects_database_size_above_object_limit(tmp_path: Path) -> None:
    snapshot, objects = _synthetic_snapshot()
    first_version = snapshot.artifact_versions[0]
    drifted = SeedSnapshot.from_rows(
        catalog_entries=snapshot.catalog_entries,
        packages=snapshot.packages,
        package_members=snapshot.package_members,
        operations=snapshot.operations,
        operation_inputs=snapshot.operation_inputs,
        artifacts=snapshot.artifacts,
        artifact_versions=(
            {**first_version, "size_bytes": MAX_OBJECT_BYTES + 1},
            *snapshot.artifact_versions[1:],
        ),
    )
    closure = build_export_closure(
        drifted, _scope(), catalog_frontier=1, operation_frontier=1, include_ifc=False
    )

    with pytest.raises(ArtifactInventoryError, match="metadata"):
        await materialize_artifact_inventory(
            closure, MappingArtifactReader(objects), tmp_path
        )


@pytest.mark.asyncio
async def test_export_fsyncs_output_parent_and_refuses_existing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, objects = _synthetic_snapshot()
    synchronized: list[Path] = []
    monkeypatch.setattr(
        exporter_module,
        "_fsync_directory",
        lambda path: synchronized.append(Path(path)),
    )
    output = tmp_path / "seed"

    await export_seed(
        snapshot=snapshot,
        artifact_reader=MappingArtifactReader(objects),
        output_root=output,
        scope=_scope(),
        metadata=_metadata(),
        catalog_frontier=1,
        operation_frontier=1,
        include_ifc=False,
    )

    assert tmp_path in synchronized
    with pytest.raises(SeedExportError, match="overwrite"):
        await export_seed(
            snapshot=snapshot,
            artifact_reader=MappingArtifactReader(objects),
            output_root=output,
            scope=_scope(),
            metadata=_metadata(),
            catalog_frontier=1,
            operation_frontier=1,
            include_ifc=False,
        )


def test_database_digest_is_stable_across_target_scope_values() -> None:
    first = {
        "scope": {"tenant_id": "a", "owner_id": "b", "task_id": "c", "run_id": "d"},
        "artifact_versions": [{"storage_key": "first", "version_id": "version-1"}],
    }
    second = {
        "scope": {"tenant_id": "w", "owner_id": "x", "task_id": "y", "run_id": "z"},
        "artifact_versions": [{"storage_key": "second", "version_id": "version-1"}],
    }

    assert canonical_database_rows_digest(first) == canonical_database_rows_digest(second)


def test_postgres_snapshot_options_are_repeatable_read_and_read_only() -> None:
    assert POSTGRES_SNAPSHOT_EXECUTION_OPTIONS == {
        "isolation_level": "REPEATABLE READ",
        "postgresql_readonly": True,
    }
