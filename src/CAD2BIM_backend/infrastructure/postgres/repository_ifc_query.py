"""PostgreSQL persistence for Run-scoped exact IFC query attempts."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from CAD2BIM_backend.domain.control_plane import ConflictError, ResourceNotFoundError
from CAD2BIM_backend.domain.ifc_query import (
    IfcQueryArtifactRef,
    IfcQueryAttemptPreparation,
    IfcQueryAttemptRecord,
    IfcQueryAttemptStatus,
    IfcQueryAttemptTerminal,
    IfcQueryBudgetSummary,
    IfcQueryDiagnostic,
    IfcQueryExactInputRef,
    IfcQueryPrepared,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    DataCatalogEntryModel,
    DataPackageMemberModel,
    DataPackageModel,
    IfcModelRevisionModel,
    IfcQueryAttemptModel,
    IfcQueryInputModel,
    RunModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import (
    PostgresRepositoryBase,
    as_utc,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantContextMismatch,
    current_tenant_execution_context,
    set_invocation_scope,
)


class PostgresIfcQueryRepositoryMixin(PostgresRepositoryBase):
    """IFC query attempts using the shared control-plane session factory."""

    async def prepare_ifc_query_attempt(
        self,
        scope: InvocationScope,
        preparation: IfcQueryAttemptPreparation,
    ) -> IfcQueryPrepared:
        task_id = self._require_ifc_query_scope(scope)
        if preparation.run_id != scope.run_id:
            raise ConflictError("IFC query preparation must belong to the invocation Run")
        try:
            async with self._sessions() as session, session.begin():
                await set_invocation_scope(session, scope)
                owner_id = await self._require_run_owner(
                    session,
                    scope=scope,
                    task_id=task_id,
                )
                replay = await session.scalar(
                    select(IfcQueryAttemptModel).where(
                        IfcQueryAttemptModel.owner_id == owner_id,
                        IfcQueryAttemptModel.task_id == task_id,
                        IfcQueryAttemptModel.run_id == preparation.run_id,
                        IfcQueryAttemptModel.request_identity == preparation.request_identity,
                    )
                )
                if replay is not None:
                    record = await self._ifc_query_record(session, replay)
                    self._require_same_ifc_query_preparation(record, preparation)
                    return IfcQueryPrepared(attempt=record, created=False)

                collision = await session.get(IfcQueryAttemptModel, preparation.attempt_id)
                if collision is not None:
                    raise ConflictError("IFC query attempt_id already exists")

                await self._require_ifc_query_inputs(
                    session,
                    scope=scope,
                    owner_id=owner_id,
                    task_id=task_id,
                    preparation=preparation,
                )
                row = IfcQueryAttemptModel(
                    attempt_id=preparation.attempt_id,
                    owner_id=owner_id,
                    tenant_id=scope.identity.tenant_id,
                    task_id=task_id,
                    run_id=preparation.run_id,
                    ifc_model_id=preparation.ifc_model_id,
                    ifc_revision_id=preparation.ifc_revision_id,
                    source_artifact_id=preparation.source_artifact.artifact_id,
                    source_artifact_version_id=preparation.source_artifact.version_id,
                    result_artifact_id=None,
                    result_artifact_version_id=None,
                    repair_parent_attempt_id=preparation.repair_parent_attempt_id,
                    request_identity=preparation.request_identity,
                    request_digest=preparation.request_digest,
                    input_count=len(preparation.inputs),
                    budget_json=preparation.budget.model_dump(mode="json"),
                    status=IfcQueryAttemptStatus.PREPARED.value,
                    diagnostics_json=[],
                    created_at=preparation.created_at,
                    finished_at=None,
                )
                session.add(row)
                session.add_all(
                    [
                        IfcQueryInputModel(
                            attempt_id=preparation.attempt_id,
                            ordinal=item.ordinal,
                            owner_id=owner_id,
                            tenant_id=scope.identity.tenant_id,
                            task_id=task_id,
                            run_id=preparation.run_id,
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
                record = await self._ifc_query_record(session, row)
                return IfcQueryPrepared(attempt=record, created=True)
        except IntegrityError as exc:
            replay = await self._reconcile_ifc_query_preparation(scope, preparation)
            if replay is not None:
                return IfcQueryPrepared(attempt=replay, created=False)
            raise ConflictError("IFC query attempt could not be prepared") from exc

    async def get_ifc_query_attempt(
        self,
        scope: InvocationScope,
        *,
        attempt_id: str,
    ) -> IfcQueryAttemptRecord | None:
        task_id = self._require_ifc_query_scope(scope)
        async with self._sessions() as session:
            await set_invocation_scope(session, scope)
            row = await session.scalar(
                select(IfcQueryAttemptModel).where(
                    IfcQueryAttemptModel.owner_id == scope.owner_id,
                    IfcQueryAttemptModel.tenant_id == scope.identity.tenant_id,
                    IfcQueryAttemptModel.task_id == task_id,
                    IfcQueryAttemptModel.run_id == scope.run_id,
                    IfcQueryAttemptModel.attempt_id == attempt_id,
                )
            )
            return None if row is None else await self._ifc_query_record(session, row)

    async def list_terminal_ifc_query_attempts(
        self,
        scope: InvocationScope,
        *,
        statuses: tuple[IfcQueryAttemptStatus, ...],
        before_created_at: datetime | None,
        before_attempt_id: str | None,
        limit: int,
    ) -> tuple[IfcQueryAttemptRecord, ...]:
        task_id = self._require_ifc_query_scope(scope)
        async with self._sessions() as session:
            await set_invocation_scope(session, scope)
            statement = select(IfcQueryAttemptModel).where(
                IfcQueryAttemptModel.owner_id == scope.owner_id,
                IfcQueryAttemptModel.tenant_id == scope.identity.tenant_id,
                IfcQueryAttemptModel.task_id == task_id,
                IfcQueryAttemptModel.run_id == scope.run_id,
                IfcQueryAttemptModel.status.in_(tuple(status.value for status in statuses)),
            )
            if before_created_at is not None:
                statement = statement.where(
                    or_(
                        IfcQueryAttemptModel.created_at < before_created_at,
                        (IfcQueryAttemptModel.created_at == before_created_at)
                        & (IfcQueryAttemptModel.attempt_id < before_attempt_id),
                    )
                )
            rows = (
                await session.scalars(
                    statement.order_by(
                        IfcQueryAttemptModel.created_at.desc(),
                        IfcQueryAttemptModel.attempt_id.desc(),
                    ).limit(limit)
                )
            ).all()
            return tuple([await self._ifc_query_record(session, row) for row in rows])

    async def commit_ifc_query_attempt_terminal(
        self,
        scope: InvocationScope,
        *,
        attempt_id: str,
        terminal: IfcQueryAttemptTerminal,
    ) -> IfcQueryAttemptRecord:
        task_id = self._require_ifc_query_scope(scope)
        async with self._sessions() as session, session.begin():
            await set_invocation_scope(session, scope)
            row = await session.scalar(
                select(IfcQueryAttemptModel)
                .where(
                    IfcQueryAttemptModel.owner_id == scope.owner_id,
                    IfcQueryAttemptModel.tenant_id == scope.identity.tenant_id,
                    IfcQueryAttemptModel.task_id == task_id,
                    IfcQueryAttemptModel.run_id == scope.run_id,
                    IfcQueryAttemptModel.attempt_id == attempt_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ResourceNotFoundError("IFC query attempt not found")

            current = await self._ifc_query_record(session, row)
            if current.status is not IfcQueryAttemptStatus.PREPARED:
                self._require_same_ifc_query_terminal(current, terminal)
                return current
            if terminal.finished_at < current.created_at:
                raise ConflictError("IFC query attempt cannot finish before creation")
            if terminal.result_artifact is not None:
                await self._require_ifc_query_exact_artifact(
                    session,
                    tenant_id=scope.identity.tenant_id,
                    owner_id=current.owner_id,
                    task_id=task_id,
                    run_id=scope.run_id,
                    expected=terminal.result_artifact,
                    kind="ifc_query_result",
                )

            row.status = terminal.status.value
            row.result_artifact_id = (
                None if terminal.result_artifact is None else terminal.result_artifact.artifact_id
            )
            row.result_artifact_version_id = (
                None if terminal.result_artifact is None else terminal.result_artifact.version_id
            )
            row.diagnostics_json = [
                diagnostic.model_dump(mode="json") for diagnostic in terminal.diagnostics
            ]
            row.finished_at = terminal.finished_at
            await session.flush()
            return await self._ifc_query_record(session, row)

    async def _reconcile_ifc_query_preparation(
        self,
        scope: InvocationScope,
        preparation: IfcQueryAttemptPreparation,
    ) -> IfcQueryAttemptRecord | None:
        task_id = self._require_ifc_query_scope(scope)
        try:
            async with self._sessions() as session:
                await set_invocation_scope(session, scope)
                row = await session.scalar(
                    select(IfcQueryAttemptModel).where(
                        IfcQueryAttemptModel.owner_id == scope.owner_id,
                        IfcQueryAttemptModel.tenant_id == scope.identity.tenant_id,
                        IfcQueryAttemptModel.task_id == task_id,
                        IfcQueryAttemptModel.run_id == scope.run_id,
                        IfcQueryAttemptModel.request_identity == preparation.request_identity,
                    )
                )
                if row is None:
                    return None
                record = await self._ifc_query_record(session, row)
                self._require_same_ifc_query_preparation(record, preparation)
                return record
        except IntegrityError:
            return None

    async def _require_ifc_query_inputs(
        self,
        session: AsyncSession,
        *,
        scope: InvocationScope,
        owner_id: str,
        task_id: str,
        preparation: IfcQueryAttemptPreparation,
    ) -> None:
        revision = await session.scalar(
            select(IfcModelRevisionModel.revision_id).where(
                IfcModelRevisionModel.owner_id == owner_id,
                IfcModelRevisionModel.tenant_id == scope.identity.tenant_id,
                IfcModelRevisionModel.task_id == task_id,
                IfcModelRevisionModel.run_id == scope.run_id,
                IfcModelRevisionModel.ifc_model_id == preparation.ifc_model_id,
                IfcModelRevisionModel.revision_id == preparation.ifc_revision_id,
            )
        )
        if revision is None:
            raise ResourceNotFoundError("exact IFC query revision not found in Run")

        await self._require_ifc_query_exact_artifact(
            session,
            tenant_id=scope.identity.tenant_id,
            owner_id=owner_id,
            task_id=task_id,
            run_id=scope.run_id,
            expected=preparation.source_artifact,
            kind="ifc_query_source",
        )
        for binding in preparation.inputs:
            await self._require_exact_input(
                session,
                scope=scope,
                owner_id=owner_id,
                task_id=task_id,
                binding=binding,
            )

        if preparation.repair_parent_attempt_id is None:
            return
        parent = await session.scalar(
            select(IfcQueryAttemptModel).where(
                IfcQueryAttemptModel.owner_id == owner_id,
                IfcQueryAttemptModel.tenant_id == scope.identity.tenant_id,
                IfcQueryAttemptModel.task_id == task_id,
                IfcQueryAttemptModel.run_id == scope.run_id,
                IfcQueryAttemptModel.attempt_id == preparation.repair_parent_attempt_id,
            )
        )
        if parent is None:
            raise ResourceNotFoundError("IFC query repair parent not found")
        if parent.status == IfcQueryAttemptStatus.PREPARED.value:
            raise ConflictError("IFC query repair parent must be terminal")
        if (
            parent.ifc_model_id,
            parent.ifc_revision_id,
        ) != (
            preparation.ifc_model_id,
            preparation.ifc_revision_id,
        ):
            raise ConflictError("IFC query repair must preserve the exact IFC revision")
        parent_inputs = await self._ifc_query_inputs(
            session,
            attempt_id=parent.attempt_id,
        )
        if parent_inputs != preparation.inputs:
            raise ConflictError("IFC query repair must preserve the exact input set")

    @staticmethod
    async def _require_run_owner(
        session: AsyncSession,
        *,
        scope: InvocationScope,
        task_id: str,
    ) -> str:
        owner_id = await session.scalar(
            select(RunModel.owner_id).where(
                RunModel.owner_id == scope.owner_id,
                RunModel.tenant_id == scope.identity.tenant_id,
                RunModel.task_id == task_id,
                RunModel.run_id == scope.run_id,
            )
        )
        if owner_id is None:
            raise ResourceNotFoundError("IFC query Run not found in Task")
        return owner_id

    @staticmethod
    async def _require_ifc_query_exact_artifact(
        session: AsyncSession,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        run_id: str,
        expected: IfcQueryArtifactRef,
        kind: str,
    ) -> ArtifactVersionModel:
        row = await session.execute(
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
                ArtifactModel.owner_id == owner_id,
                ArtifactModel.tenant_id == tenant_id,
                ArtifactModel.task_id == task_id,
                ArtifactModel.run_id == run_id,
                ArtifactModel.artifact_id == expected.artifact_id,
                ArtifactModel.kind == kind,
                ArtifactModel.status.in_(("ready", "available")),
                ArtifactVersionModel.version_id == expected.version_id,
                ArtifactVersionModel.sha256 == expected.sha256,
            )
        )
        exact = row.one_or_none()
        if exact is None:
            raise ResourceNotFoundError(f"exact {kind} ArtifactVersion not found")
        return exact[1]

    @staticmethod
    async def _require_exact_input(
        session: AsyncSession,
        *,
        scope: InvocationScope,
        owner_id: str,
        task_id: str,
        binding: IfcQueryExactInputRef,
    ) -> None:
        catalog_filters = [
            DataCatalogEntryModel.owner_id == owner_id,
            DataCatalogEntryModel.tenant_id == scope.identity.tenant_id,
            DataCatalogEntryModel.task_id == task_id,
            DataCatalogEntryModel.run_id == scope.run_id,
            DataCatalogEntryModel.resource_kind == "package",
            DataCatalogEntryModel.package_id == binding.package_id,
        ]
        if scope.catalog_frontier is not None:
            catalog_filters.append(DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier)
        visible_package = await session.scalar(
            select(DataCatalogEntryModel.catalog_entry_id).where(*catalog_filters)
        )
        if visible_package is None:
            raise ResourceNotFoundError("exact IFC query Package is not visible in this Run")

        payload = await session.scalar(
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
                DataPackageModel.owner_id == owner_id,
                DataPackageModel.tenant_id == scope.identity.tenant_id,
                DataPackageModel.task_id == task_id,
                DataPackageModel.package_id == binding.package_id,
                DataPackageMemberModel.member_id == binding.payload_member_id,
                DataPackageMemberModel.member_kind == "payload",
            )
        )
        if payload is None:
            raise ResourceNotFoundError("exact IFC query Payload member not found")
        if payload.media_type == "application/json":
            if binding.schema_member_id is None:
                raise ConflictError("IFC query JSON Payload requires its exact schema member")
            schema = await session.scalar(
                select(DataPackageMemberModel).where(
                    DataPackageMemberModel.owner_id == owner_id,
                    DataPackageMemberModel.tenant_id == scope.identity.tenant_id,
                    DataPackageMemberModel.task_id == task_id,
                    DataPackageMemberModel.package_id == binding.package_id,
                    DataPackageMemberModel.member_id == binding.schema_member_id,
                    DataPackageMemberModel.member_kind == "schema",
                    DataPackageMemberModel.media_type == "application/schema+json",
                    DataPackageMemberModel.member_name
                    == f"schemas/{payload.logical_payload_name}.schema.json",
                )
            )
            if schema is None:
                raise ResourceNotFoundError("exact IFC query JSON Schema member not found")
        elif payload.media_type == "application/vnd.apache.parquet":
            if binding.schema_member_id is not None:
                raise ConflictError("IFC query Parquet Payload cannot bind a schema member")
        else:
            raise ConflictError("IFC query Payload media type is unsupported")

    @staticmethod
    def _require_ifc_query_scope(scope: InvocationScope) -> str:
        context = scope.identity
        active = current_tenant_execution_context()
        if active is None:
            raise TenantContextMismatch("IFC query access requires an execution context")
        if active != context:
            raise TenantContextMismatch("IFC query context does not match the active invocation")
        if context.task_id is None:
            raise TenantContextMismatch("IFC query access requires a Task")
        return context.task_id

    @staticmethod
    async def _ifc_query_inputs(
        session: AsyncSession,
        *,
        attempt_id: str,
    ) -> tuple[IfcQueryExactInputRef, ...]:
        rows = (
            await session.scalars(
                select(IfcQueryInputModel)
                .where(IfcQueryInputModel.attempt_id == attempt_id)
                .order_by(IfcQueryInputModel.ordinal)
            )
        ).all()
        return tuple(
            IfcQueryExactInputRef(
                ordinal=row.ordinal,
                input_name=row.input_name,
                package_id=row.package_id,
                payload_member_id=row.payload_member_id,
                schema_member_id=row.schema_member_id,
            )
            for row in rows
        )

    @staticmethod
    async def _ifc_query_artifact_ref(
        session: AsyncSession,
        *,
        owner_id: str,
        task_id: str,
        artifact_id: str,
        version_id: str,
    ) -> IfcQueryArtifactRef:
        version = await session.scalar(
            select(ArtifactVersionModel).where(
                ArtifactVersionModel.owner_id == owner_id,
                ArtifactVersionModel.task_id == task_id,
                ArtifactVersionModel.artifact_id == artifact_id,
                ArtifactVersionModel.version_id == version_id,
            )
        )
        if version is None:
            raise RuntimeError("IFC query attempt lost its exact ArtifactVersion")
        return IfcQueryArtifactRef(
            artifact_id=version.artifact_id,
            version_id=version.version_id,
            sha256=version.sha256,
        )

    @classmethod
    async def _ifc_query_record(
        cls,
        session: AsyncSession,
        row: IfcQueryAttemptModel,
    ) -> IfcQueryAttemptRecord:
        source = await cls._ifc_query_artifact_ref(
            session,
            owner_id=row.owner_id,
            task_id=row.task_id,
            artifact_id=row.source_artifact_id,
            version_id=row.source_artifact_version_id,
        )
        result = (
            None
            if row.result_artifact_version_id is None
            else await cls._ifc_query_artifact_ref(
                session,
                owner_id=row.owner_id,
                task_id=row.task_id,
                artifact_id=row.result_artifact_id,
                version_id=row.result_artifact_version_id,
            )
        )
        try:
            inputs = await cls._ifc_query_inputs(session, attempt_id=row.attempt_id)
            if len(inputs) != row.input_count:
                raise RuntimeError("IFC query attempt lost its exact input set")
            return IfcQueryAttemptRecord(
                attempt_id=row.attempt_id,
                owner_id=row.owner_id,
                tenant_id=row.tenant_id,
                task_id=row.task_id,
                run_id=row.run_id,
                ifc_model_id=row.ifc_model_id,
                ifc_revision_id=row.ifc_revision_id,
                source_artifact=source,
                result_artifact=result,
                inputs=inputs,
                repair_parent_attempt_id=row.repair_parent_attempt_id,
                request_identity=row.request_identity,
                request_digest=row.request_digest,
                budget=IfcQueryBudgetSummary.model_validate(row.budget_json),
                status=IfcQueryAttemptStatus(row.status),
                diagnostics=tuple(
                    IfcQueryDiagnostic.model_validate(item) for item in row.diagnostics_json
                ),
                created_at=as_utc(row.created_at),
                finished_at=as_utc(row.finished_at),
            )
        except (ValidationError, ValueError) as exc:
            raise RuntimeError("stored IFC query attempt failed validation") from exc

    @staticmethod
    def _require_same_ifc_query_preparation(
        stored: IfcQueryAttemptRecord,
        incoming: IfcQueryAttemptPreparation,
    ) -> None:
        stored_identity = {
            "run_id": stored.run_id,
            "ifc_model_id": stored.ifc_model_id,
            "ifc_revision_id": stored.ifc_revision_id,
            "source_artifact": stored.source_artifact.model_dump(mode="json"),
            "inputs": [item.model_dump(mode="json") for item in stored.inputs],
            "repair_parent_attempt_id": stored.repair_parent_attempt_id,
            "request_identity": stored.request_identity,
            "request_digest": stored.request_digest,
            "budget": stored.budget.model_dump(mode="json"),
        }
        incoming_identity = incoming.model_dump(
            mode="json",
            exclude={"attempt_id", "created_at"},
        )
        incoming_identity["inputs"] = list(incoming_identity["inputs"])
        if stored_identity != incoming_identity:
            raise ConflictError("IFC query request identity was reused with different inputs")

    @staticmethod
    def _require_same_ifc_query_terminal(
        stored: IfcQueryAttemptRecord,
        incoming: IfcQueryAttemptTerminal,
    ) -> None:
        if (
            stored.status is not incoming.status
            or stored.result_artifact != incoming.result_artifact
            or stored.diagnostics != incoming.diagnostics
        ):
            raise ConflictError("IFC query attempt already has a different terminal state")


__all__ = ["PostgresIfcQueryRepositoryMixin"]
