"""Concurrency-safe in-memory DataQueryAttempt persistence."""

from __future__ import annotations

from datetime import datetime

from CAD2BIM_backend.domain.control_plane import ConflictError, ResourceNotFoundError
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryAttemptPreparation,
    DataQueryAttemptRecord,
    DataQueryAttemptStatus,
    DataQueryAttemptTerminal,
    DataQueryEngine,
    DataQueryPrepared,
)
from CAD2BIM_backend.domain.data_runtime import PackageMemberKind
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.memory.state import MemoryRepositoryBase


class DataQueryAttemptRepositoryMixin(MemoryRepositoryBase):
    async def prepare_data_query_attempt(
        self, scope: InvocationScope, preparation: DataQueryAttemptPreparation
    ) -> DataQueryPrepared:
        async with self._lock:
            self._require_run_scope(scope)
            if preparation.run_id != scope.run_id:
                raise ConflictError("data query preparation must belong to the invocation Run")

            replay_id = self._state.data_query_attempt_replays.get(
                (scope.run_id, preparation.request_identity)
            )
            if replay_id is not None:
                replay = self._state.data_query_attempts.get(replay_id)
                if replay is None:
                    raise ConflictError("data query request replay is missing")
                self._require_same_data_query_preparation(replay, preparation)
                return DataQueryPrepared(attempt=replay, created=False)
            if preparation.attempt_id in self._state.data_query_attempts:
                raise ConflictError("data query attempt_id already exists")

            self._require_exact_source(scope, preparation)
            self._require_exact_inputs(scope, preparation)
            self._require_data_query_repair_parent(scope, preparation)
            sequence = 1 + max(
                (
                    item.sequence
                    for item in self._state.data_query_attempts.values()
                    if item.owner_id == scope.owner_id
                    and item.task_id == scope.identity.task_id
                    and item.run_id == scope.run_id
                ),
                default=0,
            )
            record = DataQueryAttemptRecord(
                attempt_id=preparation.attempt_id,
                sequence=sequence,
                owner_id=scope.owner_id,
                tenant_id=scope.identity.tenant_id,
                task_id=scope.identity.task_id or "",
                run_id=preparation.run_id,
                engine=preparation.engine,
                source_artifact=preparation.source_artifact,
                inputs=preparation.inputs,
                parameters=preparation.parameters,
                inline_tables=preparation.inline_tables,
                page_size=preparation.page_size,
                repair_parent_attempt_id=preparation.repair_parent_attempt_id,
                request_identity=preparation.request_identity,
                request_digest=preparation.request_digest,
                status=DataQueryAttemptStatus.PREPARED,
                created_at=preparation.created_at,
            )
            self._state.data_query_attempts[record.attempt_id] = record
            self._state.data_query_attempt_replays[(record.run_id, record.request_identity)] = (
                record.attempt_id
            )
            return DataQueryPrepared(attempt=record, created=True)

    async def get_data_query_attempt(
        self, scope: InvocationScope, *, attempt_id: str
    ) -> DataQueryAttemptRecord | None:
        async with self._lock:
            self._require_run_scope(scope)
            record = self._state.data_query_attempts.get(attempt_id)
            return record if record is not None and self._in_scope(record, scope) else None

    async def commit_data_query_attempt_terminal(
        self,
        scope: InvocationScope,
        *,
        attempt_id: str,
        terminal: DataQueryAttemptTerminal,
    ) -> DataQueryAttemptRecord:
        async with self._lock:
            self._require_run_scope(scope)
            current = self._state.data_query_attempts.get(attempt_id)
            if current is None or not self._in_scope(current, scope):
                raise ResourceNotFoundError("data query attempt not found")
            if current.status is not DataQueryAttemptStatus.PREPARED:
                self._require_same_data_query_terminal(current, terminal)
                return current
            if terminal.finished_at < current.created_at:
                raise ConflictError("data query terminal timestamp precedes preparation")
            record = DataQueryAttemptRecord(
                **current.model_dump(
                    exclude={"status", "diagnostic", "result_summary", "finished_at"},
                    mode="python",
                ),
                status=terminal.status,
                diagnostic=terminal.diagnostic,
                result_summary=terminal.result_summary,
                finished_at=terminal.finished_at,
            )
            self._state.data_query_attempts[attempt_id] = record
            return record

    async def list_terminal_data_query_attempts(
        self,
        scope: InvocationScope,
        *,
        engine: DataQueryEngine,
        statuses: tuple[DataQueryAttemptStatus, ...],
        before_created_at: datetime | None,
        before_attempt_id: str | None,
        limit: int,
    ) -> tuple[DataQueryAttemptRecord, ...]:
        async with self._lock:
            self._require_run_scope(scope)
            records = [
                item
                for item in self._state.data_query_attempts.values()
                if self._in_scope(item, scope)
                and item.engine is engine
                and item.status in statuses
                and (
                    before_created_at is None
                    or item.created_at < before_created_at
                    or (
                        item.created_at == before_created_at
                        and before_attempt_id is not None
                        and item.attempt_id < before_attempt_id
                    )
                )
            ]
            records.sort(key=lambda item: (item.created_at, item.attempt_id), reverse=True)
            return tuple(records[:limit])

    def _require_exact_source(
        self, scope: InvocationScope, preparation: DataQueryAttemptPreparation
    ) -> None:
        expected = preparation.source_artifact
        artifact = self._state.artifacts.get(expected.artifact_id)
        if (
            artifact is None
            or artifact.owner_id != scope.owner_id
            or artifact.task_id != scope.identity.task_id
            or artifact.run_id != scope.run_id
            or artifact.kind != "data_query_source"
            or artifact.version_id != expected.version_id
            or artifact.sha256 != expected.sha256
            or artifact.version_no != 1
            or artifact.content_type
            != {
                DataQueryEngine.DUCKDB_SQL: "application/sql",
                DataQueryEngine.PYTHON_ARROW: "text/x-python",
            }[preparation.engine]
        ):
            raise ResourceNotFoundError("exact data query source ArtifactVersion is invalid")

    def _require_exact_inputs(
        self, scope: InvocationScope, preparation: DataQueryAttemptPreparation
    ) -> None:
        visible_entries = self._state.data_catalog_entries.get(scope.run_id, ())
        for expected in preparation.inputs:
            package = self._state.data_packages.get(expected.package_id)
            visible = next(
                (
                    item
                    for item in visible_entries
                    if item.package_id == expected.package_id
                    and (
                        scope.catalog_frontier is None
                        or item.catalog_revision <= scope.catalog_frontier
                    )
                ),
                None,
            )
            members = self._state.data_package_members.get(expected.package_id, ())
            payload = next(
                (item for item in members if item.member_id == expected.payload_member_id),
                None,
            )
            if (
                package is None
                or visible is None
                or payload is None
                or package.owner_id != scope.owner_id
                or package.tenant_id != scope.identity.tenant_id
                or package.task_id != scope.identity.task_id
                or payload.owner_id != scope.owner_id
                or payload.tenant_id != scope.identity.tenant_id
                or payload.task_id != scope.identity.task_id
                or payload.package_id != expected.package_id
                or payload.member_kind is not PackageMemberKind.PAYLOAD
            ):
                raise ResourceNotFoundError("exact data query Payload is not visible in this Run")
            self._require_exact_member_artifacts((payload,))
            if payload.media_type == "application/json":
                schema = next(
                    (item for item in members if item.member_id == expected.schema_member_id),
                    None,
                )
                if (
                    preparation.engine is DataQueryEngine.DUCKDB_SQL
                    or schema is None
                    or schema.owner_id != scope.owner_id
                    or schema.tenant_id != scope.identity.tenant_id
                    or schema.task_id != scope.identity.task_id
                    or schema.package_id != expected.package_id
                    or schema.member_kind is not PackageMemberKind.SCHEMA
                    or schema.media_type != "application/schema+json"
                ):
                    raise ConflictError("data query JSON Payload binding is invalid")
                self._require_exact_member_artifacts((schema,))
            elif payload.media_type == "application/vnd.apache.parquet":
                if expected.schema_member_id is not None:
                    raise ConflictError("Parquet data query input cannot bind a schema member")
            else:
                raise ConflictError("data query Payload media type is unsupported")

    def _require_data_query_repair_parent(
        self, scope: InvocationScope, preparation: DataQueryAttemptPreparation
    ) -> None:
        parent_id = preparation.repair_parent_attempt_id
        if parent_id is None:
            return
        parent = self._state.data_query_attempts.get(parent_id)
        if (
            parent is None
            or not self._in_scope(parent, scope)
            or parent.engine is not preparation.engine
            or parent.status is DataQueryAttemptStatus.PREPARED
        ):
            raise ResourceNotFoundError("data query repair parent is not terminal in this Run")
        if (
            parent.inputs != preparation.inputs
            or parent.parameters != preparation.parameters
            or parent.inline_tables != preparation.inline_tables
        ):
            raise ConflictError("data query repair must inherit exact inputs and request facts")

    @staticmethod
    def _in_scope(record: DataQueryAttemptRecord, scope: InvocationScope) -> bool:
        return (
            record.owner_id == scope.owner_id
            and record.tenant_id == scope.identity.tenant_id
            and record.task_id == scope.identity.task_id
            and record.run_id == scope.run_id
        )

    @staticmethod
    def _require_same_data_query_preparation(
        record: DataQueryAttemptRecord, expected: DataQueryAttemptPreparation
    ) -> None:
        stable_fields = set(DataQueryAttemptPreparation.model_fields) - {"created_at"}
        actual = record.model_dump(
            include=stable_fields, mode="json"
        )
        if actual != expected.model_dump(include=stable_fields, mode="json"):
            raise ConflictError("data query request identity was reused with different facts")

    @staticmethod
    def _require_same_data_query_terminal(
        record: DataQueryAttemptRecord, expected: DataQueryAttemptTerminal
    ) -> None:
        if (
            record.status != expected.status
            or record.diagnostic != expected.diagnostic
            or record.result_summary != expected.result_summary
            or record.finished_at != expected.finished_at
        ):
            raise ConflictError("data query terminal state is immutable")


__all__ = ["DataQueryAttemptRepositoryMixin"]
