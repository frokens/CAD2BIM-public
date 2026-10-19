"""PostgreSQL persistence for Task-scoped geometry query attempts."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from CAD2BIM_backend.domain.control_plane import ConflictError, ResourceNotFoundError
from CAD2BIM_backend.domain.geometry_query import (
    GeometryQueryArtifactRef,
    GeometryQueryAttemptPreparation,
    GeometryQueryAttemptRecord,
    GeometryQueryAttemptStatus,
    GeometryQueryAttemptTerminal,
    GeometryQueryDiagnostic,
    GeometryQueryTargetKind,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    GeometryQueryAttemptModel,
    GeometryResourceVersionModel,
    ProductFamilyVersionModel,
    RunModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import (
    PostgresRepositoryBase,
    as_utc,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantContextMismatch,
    current_tenant_execution_context,
    set_tenant_execution_context,
)


class PostgresGeometryQueryRepositoryMixin(PostgresRepositoryBase):
    """Geometry query attempts using the shared control-plane session factory."""

    async def create_geometry_query_attempt(
        self,
        context: TenantExecutionContext,
        preparation: GeometryQueryAttemptPreparation,
    ) -> GeometryQueryAttemptRecord:
        preparation = GeometryQueryAttemptPreparation.model_validate(preparation.model_dump(mode="python"))
        task_id = self._require_geometry_query_context(context)
        try:
            async with self._sessions() as session, session.begin():
                await set_tenant_execution_context(session, context)
                replay = await session.scalar(
                    select(GeometryQueryAttemptModel).where(
                        GeometryQueryAttemptModel.tenant_id == context.tenant_id,
                        GeometryQueryAttemptModel.task_id == task_id,
                        GeometryQueryAttemptModel.request_identity == preparation.request_identity,
                    )
                )
                if replay is not None:
                    record = await self._geometry_query_record(session, replay)
                    self._require_same_preparation(record, preparation)
                    return record

                collision = await session.get(
                    GeometryQueryAttemptModel,
                    preparation.attempt_id,
                )
                if collision is not None:
                    raise ConflictError("geometry query attempt_id already exists")

                await self._require_geometry_query_inputs(
                    session,
                    context=context,
                    task_id=task_id,
                    preparation=preparation,
                )
                row = GeometryQueryAttemptModel(
                    attempt_id=preparation.attempt_id,
                    tenant_id=context.tenant_id,
                    task_id=task_id,
                    origin_run_id=preparation.origin_run_id,
                    target_kind=preparation.target_kind.value,
                    resource_version_id=preparation.resource_version_id,
                    family_version_id=preparation.family_version_id,
                    target_artifact_version_id=(
                        None
                        if preparation.target_artifact is None
                        else preparation.target_artifact.version_id
                    ),
                    target_geometry_format=(
                        None
                        if preparation.target_geometry_format is None
                        else preparation.target_geometry_format.value
                    ),
                    inspection_selection_json=(
                        None if preparation.selection is None
                        else preparation.selection.model_dump(mode="json")
                    ),
                    inspection_coordinate_space_json=(
                        None if preparation.coordinate_space is None
                        else preparation.coordinate_space.model_dump(mode="json")
                    ),
                    source_artifact_version_id=preparation.source_artifact.version_id,
                    result_artifact_version_id=None,
                    repair_parent_attempt_id=preparation.repair_parent_attempt_id,
                    request_identity=preparation.request_identity,
                    request_digest=preparation.request_digest,
                    status=GeometryQueryAttemptStatus.PREPARED.value,
                    diagnostics_json=[],
                    created_at=preparation.created_at,
                    finished_at=None,
                )
                session.add(row)
                await session.flush()
                return await self._geometry_query_record(session, row)
        except IntegrityError as exc:
            replay = await self._reconcile_geometry_query_preparation(context, preparation)
            if replay is not None:
                return replay
            raise ConflictError("geometry query attempt could not be created") from exc

    async def complete_geometry_query_attempt(
        self,
        context: TenantExecutionContext,
        *,
        attempt_id: str,
        terminal: GeometryQueryAttemptTerminal,
    ) -> GeometryQueryAttemptRecord:
        task_id = self._require_geometry_query_context(context)
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, context)
            row = await session.scalar(
                select(GeometryQueryAttemptModel)
                .where(
                    GeometryQueryAttemptModel.tenant_id == context.tenant_id,
                    GeometryQueryAttemptModel.task_id == task_id,
                    GeometryQueryAttemptModel.attempt_id == attempt_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ResourceNotFoundError("geometry query attempt not found")

            current = await self._geometry_query_record(session, row)
            if current.status is not GeometryQueryAttemptStatus.PREPARED:
                self._require_same_terminal(current, terminal)
                return current

            if terminal.finished_at < current.created_at:
                raise ConflictError("geometry query attempt cannot finish before creation")
            if terminal.result_artifact is not None:
                await self._require_exact_artifact(
                    session,
                    tenant_id=context.tenant_id,
                    task_id=task_id,
                    expected=terminal.result_artifact,
                )

            row.status = terminal.status.value
            row.result_artifact_version_id = (
                None if terminal.result_artifact is None else terminal.result_artifact.version_id
            )
            row.diagnostics_json = [
                diagnostic.model_dump(mode="json") for diagnostic in terminal.diagnostics
            ]
            row.finished_at = terminal.finished_at
            await session.flush()
            return await self._geometry_query_record(session, row)

    async def get_geometry_query_attempt(
        self,
        context: TenantExecutionContext,
        *,
        attempt_id: str,
    ) -> GeometryQueryAttemptRecord | None:
        task_id = self._require_geometry_query_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            row = await session.scalar(
                select(GeometryQueryAttemptModel).where(
                    GeometryQueryAttemptModel.tenant_id == context.tenant_id,
                    GeometryQueryAttemptModel.task_id == task_id,
                    GeometryQueryAttemptModel.attempt_id == attempt_id,
                )
            )
            return None if row is None else await self._geometry_query_record(session, row)

    async def list_terminal_geometry_query_attempts(
        self,
        context: TenantExecutionContext,
        *,
        statuses: tuple[GeometryQueryAttemptStatus, ...],
        before_created_at: datetime | None,
        before_attempt_id: str | None,
        limit: int,
    ) -> tuple[GeometryQueryAttemptRecord, ...]:
        task_id = self._require_geometry_query_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            statement = select(GeometryQueryAttemptModel).where(
                GeometryQueryAttemptModel.tenant_id == context.tenant_id,
                GeometryQueryAttemptModel.task_id == task_id,
                GeometryQueryAttemptModel.status.in_(tuple(status.value for status in statuses)),
                GeometryQueryAttemptModel.target_kind.in_((
                    GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION.value,
                    GeometryQueryTargetKind.GEOMETRY_INSPECTION.value,
                    GeometryQueryTargetKind.FAMILY_STANDARD_MODEL.value,
                )),
            )
            if before_created_at is not None:
                statement = statement.where(
                    or_(
                        GeometryQueryAttemptModel.created_at < before_created_at,
                        (GeometryQueryAttemptModel.created_at == before_created_at)
                        & (GeometryQueryAttemptModel.attempt_id < before_attempt_id),
                    )
                )
            rows = (
                await session.scalars(
                    statement.order_by(
                        GeometryQueryAttemptModel.created_at.desc(),
                        GeometryQueryAttemptModel.attempt_id.desc(),
                    ).limit(limit)
                )
            ).all()
            return tuple([await self._geometry_query_record(session, row) for row in rows])

    async def _reconcile_geometry_query_preparation(
        self,
        context: TenantExecutionContext,
        preparation: GeometryQueryAttemptPreparation,
    ) -> GeometryQueryAttemptRecord | None:
        task_id = self._require_geometry_query_context(context)
        try:
            async with self._sessions() as session:
                await set_tenant_execution_context(session, context)
                row = await session.scalar(
                    select(GeometryQueryAttemptModel).where(
                        GeometryQueryAttemptModel.tenant_id == context.tenant_id,
                        GeometryQueryAttemptModel.task_id == task_id,
                        GeometryQueryAttemptModel.request_identity == preparation.request_identity,
                    )
                )
                if row is None:
                    return None
                record = await self._geometry_query_record(session, row)
                self._require_same_preparation(record, preparation)
                return record
        except IntegrityError:
            return None

    async def _require_geometry_query_inputs(
        self,
        session: AsyncSession,
        *,
        context: TenantExecutionContext,
        task_id: str,
        preparation: GeometryQueryAttemptPreparation,
    ) -> None:
        if preparation.origin_run_id is not None:
            run = await session.scalar(
                select(RunModel.run_id).where(
                    RunModel.run_id == preparation.origin_run_id,
                    RunModel.tenant_id == context.tenant_id,
                    RunModel.task_id == task_id,
                )
            )
            if run is None:
                raise ResourceNotFoundError("geometry query origin Run not found in Task")

        if preparation.target_kind is GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION:
            version = await session.scalar(
                select(GeometryResourceVersionModel.resource_version_id).where(
                    GeometryResourceVersionModel.resource_version_id
                    == preparation.resource_version_id
                )
            )
            if version is None:
                raise ResourceNotFoundError("geometry query resource version not visible")
        elif preparation.target_kind is GeometryQueryTargetKind.GEOMETRY_INSPECTION:
            assert preparation.target_artifact is not None
            await self._require_inspection_target_artifact(
                session,
                tenant_id=context.tenant_id,
                task_id=task_id,
                expected=preparation.target_artifact,
            )
        elif preparation.target_kind is GeometryQueryTargetKind.FAMILY_STANDARD_MODEL:
            version = await session.scalar(select(ProductFamilyVersionModel.family_version_id).where(
                ProductFamilyVersionModel.family_version_id == preparation.family_version_id,
            ))
            if version is None:
                raise ResourceNotFoundError("geometry query Family version not visible")
        else:
            raise ValueError("new geometry queries cannot use retired preview targets")

        await self._require_exact_artifact(
            session,
            tenant_id=context.tenant_id,
            task_id=task_id,
            expected=preparation.source_artifact,
        )

        if preparation.repair_parent_attempt_id is not None:
            parent = await session.scalar(
                select(GeometryQueryAttemptModel).where(
                    GeometryQueryAttemptModel.tenant_id == context.tenant_id,
                    GeometryQueryAttemptModel.task_id == task_id,
                    GeometryQueryAttemptModel.attempt_id == preparation.repair_parent_attempt_id,
                )
            )
            if parent is None:
                raise ResourceNotFoundError("geometry query repair parent not found")
            parent_record = await self._geometry_query_record(session, parent)
            if parent_record.status is GeometryQueryAttemptStatus.PREPARED:
                raise ConflictError("geometry query repair parent must be terminal")
            if (
                parent_record.target_kind is not preparation.target_kind
                or parent_record.resource_version_id != preparation.resource_version_id
                or parent_record.family_version_id != preparation.family_version_id
                or parent_record.target_artifact != preparation.target_artifact
                or parent_record.target_geometry_format is not preparation.target_geometry_format
                or parent_record.selection != preparation.selection
                or parent_record.coordinate_space != preparation.coordinate_space
            ):
                raise ConflictError("geometry query repair must preserve the exact target")

    @staticmethod
    async def _require_exact_artifact(
        session: AsyncSession,
        *,
        tenant_id: str,
        task_id: str,
        expected: GeometryQueryArtifactRef,
    ) -> ArtifactVersionModel:
        artifact = await session.scalar(
            select(ArtifactVersionModel).where(
                ArtifactVersionModel.tenant_id == tenant_id,
                ArtifactVersionModel.task_id == task_id,
                ArtifactVersionModel.artifact_id == expected.artifact_id,
                ArtifactVersionModel.version_id == expected.version_id,
                ArtifactVersionModel.sha256 == expected.sha256,
            )
        )
        if artifact is None:
            raise ResourceNotFoundError("exact geometry query ArtifactVersion not found")
        return artifact

    @staticmethod
    async def _require_inspection_target_artifact(
        session: AsyncSession,
        *,
        tenant_id: str,
        task_id: str,
        expected: GeometryQueryArtifactRef,
    ) -> ArtifactVersionModel:
        row = (
            await session.execute(
                select(ArtifactVersionModel, ArtifactModel)
                .join(
                    ArtifactModel,
                    (ArtifactModel.artifact_id == ArtifactVersionModel.artifact_id)
                    & (ArtifactModel.owner_id == ArtifactVersionModel.owner_id)
                    & (ArtifactModel.task_id == ArtifactVersionModel.task_id),
                )
                .where(
                    ArtifactVersionModel.tenant_id == tenant_id,
                    ArtifactVersionModel.task_id == task_id,
                    ArtifactVersionModel.artifact_id == expected.artifact_id,
                    ArtifactVersionModel.version_id == expected.version_id,
                    ArtifactVersionModel.sha256 == expected.sha256,
                )
            )
        ).one_or_none()
        if row is None:
            raise ResourceNotFoundError(
                "exact geometry inspection ArtifactVersion not found"
            )
        version, artifact = row
        metadata = version.metadata_json
        if (
            artifact.kind != "geometry_inspection"
            or version.content_type != "application/vnd.cad2bim.geometry-inspection+zip"
            or metadata.get("source_kind") not in {"geometry_sample", "family_realization"}
            or not isinstance(metadata.get("request_digest"), str)
            or len(metadata["request_digest"]) != 64
            or any(character not in "0123456789abcdef" for character in metadata["request_digest"])
        ):
            raise ResourceNotFoundError(
                "exact geometry inspection ArtifactVersion is invalid"
            )
        return version

    @staticmethod
    def _require_geometry_query_context(context: TenantExecutionContext) -> str:
        active = current_tenant_execution_context()
        if active is None:
            raise TenantContextMismatch("geometry query access requires a tenant execution context")
        if active != context:
            raise TenantContextMismatch(
                "geometry query context does not match the active invocation"
            )
        if context.task_id is None:
            raise TenantContextMismatch("geometry query access requires a Task")
        return context.task_id

    @staticmethod
    async def _artifact_ref(
        session: AsyncSession,
        *,
        tenant_id: str,
        task_id: str,
        version_id: str,
    ) -> GeometryQueryArtifactRef:
        artifact = await session.scalar(
            select(ArtifactVersionModel).where(
                ArtifactVersionModel.tenant_id == tenant_id,
                ArtifactVersionModel.task_id == task_id,
                ArtifactVersionModel.version_id == version_id,
            )
        )
        if artifact is None:
            raise RuntimeError("geometry query attempt lost its exact ArtifactVersion")
        return GeometryQueryArtifactRef(
            artifact_id=artifact.artifact_id,
            version_id=artifact.version_id,
            sha256=artifact.sha256,
        )

    @classmethod
    async def _geometry_query_record(
        cls,
        session: AsyncSession,
        row: GeometryQueryAttemptModel,
    ) -> GeometryQueryAttemptRecord:
        source = await cls._artifact_ref(
            session,
            tenant_id=row.tenant_id,
            task_id=row.task_id,
            version_id=row.source_artifact_version_id,
        )
        result = (
            None
            if row.result_artifact_version_id is None
            else await cls._artifact_ref(
                session,
                tenant_id=row.tenant_id,
                task_id=row.task_id,
                version_id=row.result_artifact_version_id,
            )
        )
        target = (
            None
            if row.target_artifact_version_id is None
            else await cls._artifact_ref(
                session,
                tenant_id=row.tenant_id,
                task_id=row.task_id,
                version_id=row.target_artifact_version_id,
            )
        )
        try:
            return GeometryQueryAttemptRecord(
                attempt_id=row.attempt_id,
                tenant_id=row.tenant_id,
                task_id=row.task_id,
                origin_run_id=row.origin_run_id,
                target_kind=GeometryQueryTargetKind(row.target_kind),
                resource_version_id=row.resource_version_id,
                family_version_id=row.family_version_id,
                target_artifact=target,
                target_geometry_format=row.target_geometry_format,
                selection=row.inspection_selection_json,
                coordinate_space=row.inspection_coordinate_space_json,
                source_artifact=source,
                result_artifact=result,
                repair_parent_attempt_id=row.repair_parent_attempt_id,
                request_identity=row.request_identity,
                request_digest=row.request_digest,
                status=GeometryQueryAttemptStatus(row.status),
                diagnostics=tuple(
                    GeometryQueryDiagnostic.model_validate(item) for item in row.diagnostics_json
                ),
                created_at=as_utc(row.created_at),
                finished_at=None if row.finished_at is None else as_utc(row.finished_at),
            )
        except (ValidationError, ValueError) as exc:
            raise RuntimeError("stored geometry query attempt failed validation") from exc

    @staticmethod
    def _require_same_preparation(
        stored: GeometryQueryAttemptRecord,
        incoming: GeometryQueryAttemptPreparation,
    ) -> None:
        stored_identity = {
            "origin_run_id": stored.origin_run_id,
            "target_kind": stored.target_kind,
            "resource_version_id": stored.resource_version_id,
            "family_version_id": stored.family_version_id,
            "target_artifact": (
                None
                if stored.target_artifact is None
                else stored.target_artifact.model_dump(mode="json")
            ),
            "target_geometry_format": stored.target_geometry_format,
            "selection": None if stored.selection is None else stored.selection.model_dump(),
            "coordinate_space": None if stored.coordinate_space is None else stored.coordinate_space.model_dump(),
            "source_artifact": stored.source_artifact.model_dump(mode="json"),
            "repair_parent_attempt_id": stored.repair_parent_attempt_id,
            "request_identity": stored.request_identity,
            "request_digest": stored.request_digest,
        }
        incoming_identity = incoming.model_dump(exclude={"attempt_id", "created_at"})
        if stored_identity != incoming_identity:
            raise ConflictError("geometry query request identity was reused with different inputs")

    @staticmethod
    def _require_same_terminal(
        stored: GeometryQueryAttemptRecord,
        incoming: GeometryQueryAttemptTerminal,
    ) -> None:
        if (
            stored.status is not incoming.status
            or stored.result_artifact != incoming.result_artifact
            or stored.diagnostics != incoming.diagnostics
        ):
            raise ConflictError("geometry query attempt already has a different terminal state")


__all__ = ["PostgresGeometryQueryRepositoryMixin"]
