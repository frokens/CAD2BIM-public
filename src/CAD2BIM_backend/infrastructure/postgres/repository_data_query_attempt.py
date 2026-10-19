"""PostgreSQL persistence for Run-scoped DataQueryAttempt facts."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from CAD2BIM_backend.domain.control_plane import ConflictError, ResourceNotFoundError
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryArtifactRef,
    DataQueryAttemptPreparation,
    DataQueryAttemptRecord,
    DataQueryAttemptStatus,
    DataQueryAttemptTerminal,
    DataQueryDiagnostic,
    DataQueryEngine,
    DataQueryExactInputRef,
    DataQueryPrepared,
    DataQueryResultSummary,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    DataCatalogEntryModel,
    DataPackageMemberModel,
    DataPackageModel,
    DataQueryAttemptModel,
    DataQueryInputModel,
    RunModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import PostgresRepositoryBase, as_utc
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantContextMismatch,
    current_tenant_execution_context,
    set_invocation_scope,
)


class PostgresDataQueryAttemptRepositoryMixin(PostgresRepositoryBase):
    async def prepare_data_query_attempt(
        self, scope: InvocationScope, preparation: DataQueryAttemptPreparation
    ) -> DataQueryPrepared:
        task_id = self._require_data_query_scope(scope)
        if preparation.run_id != scope.run_id:
            raise ConflictError("data query preparation must belong to the invocation Run")
        try:
            async with self._sessions() as session, session.begin():
                await set_invocation_scope(session, scope)
                await self._require_data_query_run(session, scope, task_id)
                replay = await session.scalar(
                    select(DataQueryAttemptModel).where(
                        DataQueryAttemptModel.owner_id == scope.owner_id,
                        DataQueryAttemptModel.task_id == task_id,
                        DataQueryAttemptModel.run_id == scope.run_id,
                        DataQueryAttemptModel.request_identity == preparation.request_identity,
                    )
                )
                if replay is not None:
                    record = await self._data_query_record(session, replay)
                    self._require_same_data_query_preparation(record, preparation)
                    return DataQueryPrepared(attempt=record, created=False)
                if await session.get(DataQueryAttemptModel, preparation.attempt_id) is not None:
                    raise ConflictError("data query attempt_id already exists")
                await self._require_preparation_inputs(session, scope, preparation)
                sequence = (
                    int(
                        await session.scalar(
                            select(
                                func.coalesce(func.max(DataQueryAttemptModel.sequence), 0)
                            ).where(
                                DataQueryAttemptModel.owner_id == scope.owner_id,
                                DataQueryAttemptModel.task_id == task_id,
                                DataQueryAttemptModel.run_id == scope.run_id,
                            )
                        )
                        or 0
                    )
                    + 1
                )
                row = DataQueryAttemptModel(
                    attempt_id=preparation.attempt_id,
                    sequence=sequence,
                    owner_id=scope.owner_id,
                    tenant_id=scope.identity.tenant_id,
                    task_id=task_id,
                    run_id=scope.run_id,
                    engine=preparation.engine.value,
                    source_artifact_id=preparation.source_artifact.artifact_id,
                    source_artifact_version_id=preparation.source_artifact.version_id,
                    repair_parent_attempt_id=preparation.repair_parent_attempt_id,
                    request_identity=preparation.request_identity,
                    request_digest=preparation.request_digest,
                    input_count=len(preparation.inputs),
                    parameters_json=list(preparation.parameters),
                    inline_tables_json=list(preparation.inline_tables),
                    page_size=preparation.page_size,
                    status=DataQueryAttemptStatus.PREPARED.value,
                    diagnostic_json=None,
                    result_summary_json=None,
                    created_at=preparation.created_at,
                    finished_at=None,
                )
                session.add(row)
                session.add_all(
                    [
                        DataQueryInputModel(
                            attempt_id=preparation.attempt_id,
                            ordinal=item.ordinal,
                            owner_id=scope.owner_id,
                            tenant_id=scope.identity.tenant_id,
                            task_id=task_id,
                            run_id=scope.run_id,
                            input_name=item.input_name,
                            package_id=item.package_id,
                            payload_member_id=item.payload_member_id,
                            schema_member_id=item.schema_member_id,
                            created_at=preparation.created_at,
                        )
                        for item in preparation.inputs
                    ]
                )
                await session.flush()
                return DataQueryPrepared(
                    attempt=await self._data_query_record(session, row), created=True
                )
        except IntegrityError as exc:
            replay = await self._reconcile_data_query_preparation(scope, preparation)
            if replay is not None:
                self._require_same_data_query_preparation(replay, preparation)
                return DataQueryPrepared(attempt=replay, created=False)
            raise ConflictError("data query attempt could not be prepared") from exc

    async def get_data_query_attempt(
        self, scope: InvocationScope, *, attempt_id: str
    ) -> DataQueryAttemptRecord | None:
        task_id = self._require_data_query_scope(scope)
        async with self._sessions() as session:
            await set_invocation_scope(session, scope)
            row = await session.scalar(
                select(DataQueryAttemptModel).where(
                    DataQueryAttemptModel.owner_id == scope.owner_id,
                    DataQueryAttemptModel.tenant_id == scope.identity.tenant_id,
                    DataQueryAttemptModel.task_id == task_id,
                    DataQueryAttemptModel.run_id == scope.run_id,
                    DataQueryAttemptModel.attempt_id == attempt_id,
                )
            )
            return None if row is None else await self._data_query_record(session, row)

    async def commit_data_query_attempt_terminal(
        self,
        scope: InvocationScope,
        *,
        attempt_id: str,
        terminal: DataQueryAttemptTerminal,
    ) -> DataQueryAttemptRecord:
        task_id = self._require_data_query_scope(scope)
        async with self._sessions() as session, session.begin():
            await set_invocation_scope(session, scope)
            row = await session.scalar(
                select(DataQueryAttemptModel)
                .where(
                    DataQueryAttemptModel.owner_id == scope.owner_id,
                    DataQueryAttemptModel.tenant_id == scope.identity.tenant_id,
                    DataQueryAttemptModel.task_id == task_id,
                    DataQueryAttemptModel.run_id == scope.run_id,
                    DataQueryAttemptModel.attempt_id == attempt_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ResourceNotFoundError("data query attempt not found")
            current = await self._data_query_record(session, row)
            if current.status is not DataQueryAttemptStatus.PREPARED:
                self._require_same_data_query_terminal(current, terminal)
                return current
            row.status = terminal.status.value
            row.diagnostic_json = (
                None if terminal.diagnostic is None else terminal.diagnostic.model_dump(mode="json")
            )
            row.result_summary_json = (
                None
                if terminal.result_summary is None
                else terminal.result_summary.model_dump(mode="json")
            )
            row.finished_at = terminal.finished_at
            await session.flush()
            return await self._data_query_record(session, row)

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
        task_id = self._require_data_query_scope(scope)
        async with self._sessions() as session:
            await set_invocation_scope(session, scope)
            statement = select(DataQueryAttemptModel).where(
                DataQueryAttemptModel.owner_id == scope.owner_id,
                DataQueryAttemptModel.tenant_id == scope.identity.tenant_id,
                DataQueryAttemptModel.task_id == task_id,
                DataQueryAttemptModel.run_id == scope.run_id,
                DataQueryAttemptModel.engine == engine.value,
                DataQueryAttemptModel.status.in_(tuple(item.value for item in statuses)),
            )
            if before_created_at is not None:
                statement = statement.where(
                    or_(
                        DataQueryAttemptModel.created_at < before_created_at,
                        (DataQueryAttemptModel.created_at == before_created_at)
                        & (DataQueryAttemptModel.attempt_id < before_attempt_id),
                    )
                )
            rows = (
                await session.scalars(
                    statement.order_by(
                        DataQueryAttemptModel.created_at.desc(),
                        DataQueryAttemptModel.attempt_id.desc(),
                    ).limit(limit)
                )
            ).all()
            return tuple([await self._data_query_record(session, row) for row in rows])

    async def _reconcile_data_query_preparation(
        self, scope: InvocationScope, preparation: DataQueryAttemptPreparation
    ) -> DataQueryAttemptRecord | None:
        try:
            return await self.get_data_query_attempt(scope, attempt_id=preparation.attempt_id)
        except IntegrityError:
            return None

    async def _require_preparation_inputs(
        self,
        session: AsyncSession,
        scope: InvocationScope,
        preparation: DataQueryAttemptPreparation,
    ) -> None:
        task_id = scope.identity.task_id or ""
        await self._require_exact_data_query_source(
            session, scope, preparation.source_artifact, preparation.engine
        )
        for item in preparation.inputs:
            filters = [
                DataCatalogEntryModel.owner_id == scope.owner_id,
                DataCatalogEntryModel.tenant_id == scope.identity.tenant_id,
                DataCatalogEntryModel.task_id == task_id,
                DataCatalogEntryModel.run_id == scope.run_id,
                DataCatalogEntryModel.resource_kind == "package",
                DataCatalogEntryModel.package_id == item.package_id,
            ]
            if scope.catalog_frontier is not None:
                filters.append(DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier)
            if (
                await session.scalar(select(DataCatalogEntryModel.catalog_entry_id).where(*filters))
                is None
            ):
                raise ResourceNotFoundError("exact data query Package is not visible in this Run")
            member = await session.scalar(
                select(DataPackageMemberModel)
                .join(
                    DataPackageModel,
                    and_(
                        DataPackageModel.owner_id == DataPackageMemberModel.owner_id,
                        DataPackageModel.task_id == DataPackageMemberModel.task_id,
                        DataPackageModel.package_id == DataPackageMemberModel.package_id,
                    ),
                )
                .where(
                    DataPackageModel.owner_id == scope.owner_id,
                    DataPackageModel.tenant_id == scope.identity.tenant_id,
                    DataPackageModel.task_id == task_id,
                    DataPackageModel.package_id == item.package_id,
                    DataPackageMemberModel.member_id == item.payload_member_id,
                    DataPackageMemberModel.member_kind == "payload",
                )
            )
            if member is None:
                raise ResourceNotFoundError("exact data query Payload binding is invalid")
            if member.media_type == "application/json":
                schema = await session.scalar(
                    select(DataPackageMemberModel).where(
                        DataPackageMemberModel.owner_id == scope.owner_id,
                        DataPackageMemberModel.tenant_id == scope.identity.tenant_id,
                        DataPackageMemberModel.task_id == task_id,
                        DataPackageMemberModel.package_id == item.package_id,
                        DataPackageMemberModel.member_id == item.schema_member_id,
                        DataPackageMemberModel.member_kind == "schema",
                        DataPackageMemberModel.media_type == "application/schema+json",
                        DataPackageMemberModel.member_name
                        == f"schemas/{member.logical_payload_name}.schema.json",
                    )
                )
                if schema is None or preparation.engine is DataQueryEngine.DUCKDB_SQL:
                    raise ConflictError("data query JSON Payload binding is invalid")
            elif member.media_type == "application/vnd.apache.parquet":
                if item.schema_member_id is not None:
                    raise ConflictError("Parquet data query input cannot bind a schema member")
            else:
                raise ConflictError("data query Payload media type is unsupported")
        if preparation.repair_parent_attempt_id is None:
            return
        parent = await session.scalar(
            select(DataQueryAttemptModel).where(
                DataQueryAttemptModel.owner_id == scope.owner_id,
                DataQueryAttemptModel.task_id == task_id,
                DataQueryAttemptModel.run_id == scope.run_id,
                DataQueryAttemptModel.attempt_id == preparation.repair_parent_attempt_id,
                DataQueryAttemptModel.engine == preparation.engine.value,
            )
        )
        if parent is None or parent.status == DataQueryAttemptStatus.PREPARED.value:
            raise ResourceNotFoundError("data query repair parent is not terminal in this Run")
        record = await self._data_query_record(session, parent)
        if (
            record.inputs != preparation.inputs
            or record.parameters != preparation.parameters
            or record.inline_tables != preparation.inline_tables
        ):
            raise ConflictError("data query repair must inherit exact inputs and request facts")

    @staticmethod
    async def _require_data_query_run(
        session: AsyncSession, scope: InvocationScope, task_id: str
    ) -> None:
        run = await session.scalar(
            select(RunModel.run_id)
            .where(
                RunModel.owner_id == scope.owner_id,
                RunModel.tenant_id == scope.identity.tenant_id,
                RunModel.task_id == task_id,
                RunModel.run_id == scope.run_id,
            )
            # Serialize per-Run sequence allocation without blocking the Run
            # KEY SHARE check of concurrent immutable source Artifacts. FOR UPDATE
            # forms a Task/Run FK lock cycle with Artifact registration.
            .with_for_update(key_share=True)
        )
        if run is None:
            raise ResourceNotFoundError("data query Run not found")

    @staticmethod
    async def _require_exact_data_query_source(
        session: AsyncSession,
        scope: InvocationScope,
        expected: DataQueryArtifactRef,
        engine: DataQueryEngine,
    ) -> None:
        exact = await session.execute(
            select(ArtifactModel, ArtifactVersionModel)
            .join(
                ArtifactVersionModel,
                and_(
                    ArtifactVersionModel.owner_id == ArtifactModel.owner_id,
                    ArtifactVersionModel.task_id == ArtifactModel.task_id,
                    ArtifactVersionModel.artifact_id == ArtifactModel.artifact_id,
                ),
            )
            .where(
                ArtifactModel.owner_id == scope.owner_id,
                ArtifactModel.tenant_id == scope.identity.tenant_id,
                ArtifactModel.task_id == scope.identity.task_id,
                ArtifactModel.run_id == scope.run_id,
                ArtifactModel.artifact_id == expected.artifact_id,
                ArtifactModel.kind == "data_query_source",
                ArtifactModel.status.in_(("ready", "available")),
                ArtifactVersionModel.version_id == expected.version_id,
                ArtifactVersionModel.version_no == 1,
                ArtifactVersionModel.sha256 == expected.sha256,
                ArtifactVersionModel.content_type
                == ("application/sql" if engine is DataQueryEngine.DUCKDB_SQL else "text/x-python"),
            )
        )
        if exact.one_or_none() is None:
            raise ResourceNotFoundError("exact data query source ArtifactVersion is invalid")

    @staticmethod
    def _require_data_query_scope(scope: InvocationScope) -> str:
        active = current_tenant_execution_context()
        if active is None or active != scope.identity or scope.identity.task_id is None:
            raise TenantContextMismatch("data query access requires the active invocation scope")
        return scope.identity.task_id

    @staticmethod
    async def _data_query_inputs(
        session: AsyncSession, attempt_id: str
    ) -> tuple[DataQueryExactInputRef, ...]:
        rows = (
            await session.scalars(
                select(DataQueryInputModel)
                .where(DataQueryInputModel.attempt_id == attempt_id)
                .order_by(DataQueryInputModel.ordinal)
            )
        ).all()
        return tuple(
            DataQueryExactInputRef(
                ordinal=row.ordinal,
                input_name=row.input_name,
                package_id=row.package_id,
                payload_member_id=row.payload_member_id,
                schema_member_id=row.schema_member_id,
            )
            for row in rows
        )

    async def _data_query_record(
        self, session: AsyncSession, row: DataQueryAttemptModel
    ) -> DataQueryAttemptRecord:
        source = await session.scalar(
            select(ArtifactVersionModel).where(
                ArtifactVersionModel.artifact_id == row.source_artifact_id,
                ArtifactVersionModel.version_id == row.source_artifact_version_id,
            )
        )
        if source is None:
            raise ResourceNotFoundError("data query exact source ArtifactVersion is missing")
        try:
            return DataQueryAttemptRecord(
                attempt_id=row.attempt_id,
                sequence=row.sequence,
                owner_id=row.owner_id,
                tenant_id=row.tenant_id,
                task_id=row.task_id,
                run_id=row.run_id,
                engine=DataQueryEngine(row.engine),
                source_artifact=DataQueryArtifactRef(
                    artifact_id=row.source_artifact_id,
                    version_id=row.source_artifact_version_id,
                    sha256=source.sha256,
                ),
                inputs=await self._data_query_inputs(session, row.attempt_id),
                parameters=tuple(row.parameters_json),
                inline_tables=tuple(row.inline_tables_json),
                page_size=row.page_size,
                repair_parent_attempt_id=row.repair_parent_attempt_id,
                request_identity=row.request_identity,
                request_digest=row.request_digest,
                status=DataQueryAttemptStatus(row.status),
                diagnostic=(
                    None
                    if row.diagnostic_json is None
                    else DataQueryDiagnostic(**row.diagnostic_json)
                ),
                result_summary=(
                    None
                    if row.result_summary_json is None
                    else DataQueryResultSummary(**row.result_summary_json)
                ),
                created_at=as_utc(row.created_at),
                finished_at=None if row.finished_at is None else as_utc(row.finished_at),
            )
        except (ValidationError, ValueError) as exc:
            raise ConflictError("persisted data query attempt is invalid") from exc

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


__all__ = ["PostgresDataQueryAttemptRepositoryMixin"]
