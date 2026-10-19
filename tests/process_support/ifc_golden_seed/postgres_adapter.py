"""Read the export source from one PostgreSQL repeatable-read, read-only snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    CriticalOperationInputModel,
    CriticalOperationModel,
    DataCatalogEntryModel,
    DataPackageMemberModel,
    DataPackageModel,
    DirectSessionBindingModel,
    IfcModelModel,
    IfcModelRevisionModel,
    RunModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import artifact_storage_key

from tests.process_support.ifc_golden_seed.closure import SeedExportScope, SeedSnapshot


class PostgresSeedSnapshotError(RuntimeError):
    """The source Run could not be read with the required snapshot guarantees."""


POSTGRES_SNAPSHOT_EXECUTION_OPTIONS: dict[str, object] = {
    "isolation_level": "REPEATABLE READ",
    "postgresql_readonly": True,
}


@dataclass(frozen=True, slots=True)
class ResolvedSourceRun:
    """Server-only source binding and migration identity."""

    scope: SeedExportScope
    direct_session_alias: str
    migration_head: str


def _basename(value: str | None) -> str | None:
    if value is None:
        return None
    return PurePosixPath(value.replace("\\", "/")).name


async def _mapping_rows(
    connection: AsyncConnection,
    model: type[Any],
    fields: Sequence[str],
    *,
    scope: SeedExportScope,
    run_field: str | None,
    order_by: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    statement: Select[Any] = select(*(getattr(model, field) for field in fields)).where(
        model.tenant_id == scope.tenant_id,
        model.owner_id == scope.owner_id,
        model.task_id == scope.task_id,
    )
    if run_field is not None:
        statement = statement.where(getattr(model, run_field) == scope.run_id)
    statement = statement.order_by(*(getattr(model, field) for field in order_by))
    result = await connection.execute(statement)
    return tuple(dict(row) for row in result.mappings())


class PostgresSeedSnapshotReader:
    """Load current SQLAlchemy model rows without copying legacy M4 assumptions."""

    def __init__(self, engine: AsyncEngine) -> None:
        if engine.dialect.name != "postgresql":
            raise PostgresSeedSnapshotError("Seed export requires a PostgreSQL engine")
        self._engine = engine

    async def resolve_source_run(self, run_id: str) -> ResolvedSourceRun:
        """Resolve exactly one Direct binding without exposing credentials or paths."""

        normalized_run_id = self._normalized_run_id(run_id)
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                **POSTGRES_SNAPSHOT_EXECUTION_OPTIONS
            )
            async with connection.begin():
                await self._assert_snapshot_mode(connection)
                return await self._resolve_source_run(connection, normalized_run_id)

    async def resolve_and_load(
        self,
        run_id: str,
    ) -> tuple[ResolvedSourceRun, SeedSnapshot]:
        """Resolve identity and rows inside one repeatable-read snapshot."""

        normalized_run_id = self._normalized_run_id(run_id)
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                **POSTGRES_SNAPSHOT_EXECUTION_OPTIONS
            )
            async with connection.begin():
                await self._assert_snapshot_mode(connection)
                resolved = await self._resolve_source_run(connection, normalized_run_id)
                await connection.execute(
                    text(
                        """
                        SELECT
                            set_config('cad2bim.tenant_id', :tenant_id, true),
                            set_config('cad2bim.principal_id', :principal_id, true),
                            set_config('cad2bim.task_id', :task_id, true)
                        """
                    ),
                    {
                        "tenant_id": resolved.scope.tenant_id,
                        "principal_id": resolved.scope.principal_id,
                        "task_id": resolved.scope.task_id,
                    },
                )
                await self._require_run(connection, resolved.scope)
                snapshot = await self._load_rows(connection, resolved.scope)
                return resolved, snapshot

    @staticmethod
    def _normalized_run_id(run_id: str) -> str:
        if not isinstance(run_id, str) or not run_id.strip() or "\x00" in run_id:
            raise PostgresSeedSnapshotError("source Run id is invalid")
        return run_id.strip()

    @staticmethod
    async def _resolve_source_run(
        connection: AsyncConnection,
        normalized_run_id: str,
    ) -> ResolvedSourceRun:
        result = await connection.execute(
            select(
                DirectSessionBindingModel.tenant_id,
                DirectSessionBindingModel.principal_id,
                DirectSessionBindingModel.owner_id,
                DirectSessionBindingModel.task_id,
                DirectSessionBindingModel.run_id,
                DirectSessionBindingModel.direct_session_alias,
            ).where(DirectSessionBindingModel.run_id == normalized_run_id)
        )
        bindings = list(result.mappings())
        if len(bindings) != 1:
            raise PostgresSeedSnapshotError(
                "source Run must resolve to exactly one Direct Session binding"
            )
        migration_head = await connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        if not isinstance(migration_head, str) or not migration_head:
            raise PostgresSeedSnapshotError(
                "source database migration identity is unavailable"
            )
        # Export is read-only: its explicit current column selections below
        # and closure validation establish compatibility with the source.
        # Record the real head as provenance without requiring unrelated
        # destination write constraints to have been applied to this source.
        binding = bindings[0]
        scope = SeedExportScope.normalized(
            tenant_id=binding["tenant_id"],
            principal_id=binding["principal_id"],
            owner_id=binding["owner_id"],
            task_id=binding["task_id"],
            run_id=binding["run_id"],
        )
        return ResolvedSourceRun(
            scope,
            str(binding["direct_session_alias"]),
            str(migration_head),
        )

    async def load(self, scope: SeedExportScope) -> SeedSnapshot:
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                **POSTGRES_SNAPSHOT_EXECUTION_OPTIONS
            )
            async with connection.begin():
                await connection.execute(
                    text(
                        """
                        SELECT
                            set_config('cad2bim.tenant_id', :tenant_id, true),
                            set_config('cad2bim.principal_id', :principal_id, true),
                            set_config('cad2bim.task_id', :task_id, true)
                        """
                    ),
                    {
                        "tenant_id": scope.tenant_id,
                        "principal_id": scope.principal_id,
                        "task_id": scope.task_id,
                    },
                )
                await self._assert_snapshot_mode(connection)
                await self._require_run(connection, scope)
                return await self._load_rows(connection, scope)

    @staticmethod
    async def _assert_snapshot_mode(connection: AsyncConnection) -> None:
        isolation = await connection.scalar(text("SHOW transaction_isolation"))
        read_only = await connection.scalar(text("SHOW transaction_read_only"))
        if str(isolation).lower() != "repeatable read" or str(read_only).lower() not in {
            "on",
            "true",
        }:
            raise PostgresSeedSnapshotError(
                "database did not establish a REPEATABLE READ READ ONLY transaction"
            )

    @staticmethod
    async def _require_run(connection: AsyncConnection, scope: SeedExportScope) -> None:
        run_id = await connection.scalar(
            select(RunModel.run_id).where(
                RunModel.tenant_id == scope.tenant_id,
                RunModel.owner_id == scope.owner_id,
                RunModel.task_id == scope.task_id,
                RunModel.run_id == scope.run_id,
            )
        )
        if run_id != scope.run_id:
            raise PostgresSeedSnapshotError("source Run is absent from the normalized scope")

    @staticmethod
    async def _load_rows(
        connection: AsyncConnection, scope: SeedExportScope
    ) -> SeedSnapshot:
        catalog_entries = await _mapping_rows(
            connection,
            DataCatalogEntryModel,
            (
                "catalog_entry_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "run_id",
                "resource_kind",
                "alias",
                "ordinal",
                "catalog_revision",
                "package_id",
                "operation_id",
                "created_at",
            ),
            scope=scope,
            run_field="run_id",
            order_by=("catalog_revision", "resource_kind", "ordinal"),
        )
        packages = await _mapping_rows(
            connection,
            DataPackageModel,
            (
                "package_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "created_by_run_id",
                "name",
                "package_kind",
                "source_filename",
                "description_title",
                "summary",
                "section_titles_json",
                "idempotency_key",
                "request_fingerprint",
                "created_at",
            ),
            scope=scope,
            run_field="created_by_run_id",
            order_by=("package_id",),
        )
        packages = tuple(
            {**row, "source_filename": _basename(row.get("source_filename"))}
            for row in packages
        )
        package_members = await _mapping_rows(
            connection,
            DataPackageMemberModel,
            (
                "member_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "package_id",
                "member_name",
                "member_kind",
                "artifact_id",
                "artifact_version_id",
                "artifact_sha256",
                "media_type",
                "row_count",
                "logical_payload_name",
                "payload_description",
                "logical_key_fields_json",
                "arrow_schema_json",
                "logical_byte_count",
                "json_kind",
                "created_at",
            ),
            scope=scope,
            run_field=None,
            order_by=("package_id", "member_name", "member_id"),
        )
        operations = await _mapping_rows(
            connection,
            CriticalOperationModel,
            (
                "operation_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "run_id",
                "operation_kind",
                "initiator",
                "tool_name",
                "intent_summary",
                "source_filename",
                "idempotency_key",
                "request_fingerprint",
                "status",
                "result_package_id",
                "error_code",
                "error_message",
                "error_phase",
                "code_artifact_id",
                "code_artifact_version_id",
                "code_artifact_sha256",
                "description_artifact_id",
                "description_artifact_version_id",
                "description_artifact_sha256",
                "repair_parent_operation_id",
                "base_ifc_revision_id",
                "expected_ifc_write_epoch",
                "operation_detail_json",
                "started_at",
                "finished_at",
                "created_at",
            ),
            scope=scope,
            run_field="run_id",
            order_by=("operation_id",),
        )
        operations = tuple(
            {**row, "source_filename": _basename(row.get("source_filename"))}
            for row in operations
        )
        operation_inputs = await _mapping_rows(
            connection,
            CriticalOperationInputModel,
            (
                "input_binding_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "operation_id",
                "ordinal",
                "input_name",
                "package_id",
                "member_id",
                "payload_name",
                "created_at",
            ),
            scope=scope,
            run_field=None,
            order_by=("operation_id", "ordinal"),
        )
        artifacts = await _mapping_rows(
            connection,
            ArtifactModel,
            (
                "artifact_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "run_id",
                "kind",
                "filename",
                "content_type",
                "status",
                "current_version_id",
                "idempotency_key",
                "request_fingerprint",
                "created_at",
                "updated_at",
            ),
            scope=scope,
            run_field="run_id",
            order_by=("artifact_id",),
        )
        artifacts = tuple({**row, "filename": _basename(row["filename"])} for row in artifacts)
        artifact_versions = await _mapping_rows(
            connection,
            ArtifactVersionModel,
            (
                "version_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "artifact_id",
                "version_no",
                "storage_key",
                "filename",
                "content_type",
                "size_bytes",
                "sha256",
                "metadata_json",
                "lineage_json",
                "created_at",
            ),
            scope=scope,
            run_field=None,
            order_by=("artifact_id", "version_no"),
        )
        artifact_versions = tuple(
            {**row, "filename": _basename(row["filename"])} for row in artifact_versions
        )
        for row in artifact_versions:
            expected_storage_key = artifact_storage_key(
                scope.owner_id,
                scope.task_id,
                row["artifact_id"],
                row["version_no"],
            )
            if row["storage_key"] != expected_storage_key:
                raise PostgresSeedSnapshotError(
                    "artifact version storage identity is inconsistent with current models"
                )
        ifc_models = await _mapping_rows(
            connection,
            IfcModelModel,
            (
                "ifc_model_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "run_id",
                "ifc_schema",
                "project_global_id",
                "current_revision_id",
                "write_epoch",
                "status",
                "freeze_reason_code",
                "created_at",
                "updated_at",
            ),
            scope=scope,
            run_field="run_id",
            order_by=("ifc_model_id",),
        )
        ifc_revisions = await _mapping_rows(
            connection,
            IfcModelRevisionModel,
            (
                "revision_id",
                "tenant_id",
                "owner_id",
                "task_id",
                "run_id",
                "ifc_model_id",
                "revision_no",
                "artifact_id",
                "artifact_version_id",
                "operation_id",
                "created_at",
            ),
            scope=scope,
            run_field="run_id",
            order_by=("ifc_model_id", "revision_no"),
        )
        return SeedSnapshot.from_rows(
            catalog_entries=catalog_entries,
            packages=packages,
            package_members=package_members,
            operations=operations,
            operation_inputs=operation_inputs,
            artifacts=artifacts,
            artifact_versions=artifact_versions,
            ifc_models=ifc_models,
            ifc_revisions=ifc_revisions,
        )


__all__ = [
    "POSTGRES_SNAPSHOT_EXECUTION_OPTIONS",
    "PostgresSeedSnapshotError",
    "PostgresSeedSnapshotReader",
    "ResolvedSourceRun",
]
