"""PostgreSQL persistence for immutable reusable geometry resources."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from CAD2BIM_backend.domain.control_plane import ConflictError, IdempotencyConflictError
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogReferenceError, GeometryCatalogIntegrityError,
    GeometryResourceVersionListQuery,
    GeometryResourceVersionPage,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceListQuery,
    GeometryResourcePage,
    GeometryResourceRecord,
    GeometryResourceScope,
    GeometryResourceScopeKind,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    geometry_resource_inputs,
    require_formal_geometry_publication,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.postgres.models import (
    GeometryResourceModel,
    GeometryResourceVersionAssetModel,
    GeometryResourceVersionModel,
    GeometryResourceVersionInputModel,
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


def _current_geometry_version_predicate(version):
    """Exclude the retired format before pagination, without hiding corrupt current rows."""
    return ~version.manifest_json.bool_op("?")("validation_profile")


class PostgresGeometryResourceRepositoryMixin(PostgresRepositoryBase):
    """Geometry aggregate operations using the shared session factory."""

    async def delete_geometry_resource(self, context, *, resource_id: str) -> bool:
        self._require_geometry_context(context)
        return await self._delete_library_resource(context, root_kind="geometry", root_id=resource_id)

    async def require_geometry_resource_publication_allowed(
        self,
        context: TenantExecutionContext,
        *,
        scope: GeometryResourceScope,
    ) -> None:
        self._require_geometry_context(context)
        self._require_geometry_write_scope(context, scope)

    async def publish_geometry_resource_version(
        self,
        context: TenantExecutionContext,
        publication: GeometryResourceVersionBundle,
    ) -> GeometryResourceVersionBundle:
        await self.require_geometry_resource_publication_allowed(
            context,
            scope=publication.resource.owner_scope,
        )

        try:
            async with self._sessions() as session, session.begin():
                await set_tenant_execution_context(session, context)
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:resource_id, 0))"),
                    {"resource_id": publication.resource.resource_id},
                )
                replay = await self._load_geometry_bundle(
                    session,
                    resource_id=publication.resource.resource_id,
                    resource_version_id=publication.version.resource_version_id,
                )
                if replay is not None:
                    self._require_same_geometry_publication(replay, publication)
                    return replay

                require_formal_geometry_publication(publication.version, publication.assets)

                resource = await session.scalar(
                    select(GeometryResourceModel).where(
                        GeometryResourceModel.resource_id == publication.resource.resource_id
                    )
                )
                if resource is None:
                    if publication.version.version_no != 1:
                        raise ConflictError(
                            "the first geometry resource version must use version_no 1"
                        )
                    resource = self._new_geometry_resource_model(publication.resource)
                    session.add(resource)
                    await session.flush()
                else:
                    self._require_same_geometry_root(resource, publication.resource)
                    latest_version = await session.scalar(
                        select(func.max(GeometryResourceVersionModel.version_no)).where(
                            GeometryResourceVersionModel.resource_id == resource.resource_id
                        )
                    )
                    expected_version = int(latest_version or 0) + 1
                    if publication.version.version_no != expected_version:
                        raise ConflictError(
                            "geometry resource version_no is not the next immutable version"
                        )

                return await self._insert_geometry_bundle(session, publication)
        except SQLAlchemyError as exc:
            reconciled = await self._reconcile_geometry_publication(context, publication)
            if reconciled is not None:
                return reconciled
            raise ConflictError("geometry resource version could not be published") from exc

    @classmethod
    async def _insert_geometry_bundle(cls, session, publication):
        """Insert a complete version inside the caller's existing root transaction."""
        require_formal_geometry_publication(publication.version, publication.assets)
        session.add(cls._new_geometry_version_model(publication.version))
        # Child RLS resolves this exact parent; flush it before the asset batch.
        await session.flush()
        session.add_all(cls._new_geometry_asset_model(asset) for asset in publication.assets)
        await session.flush()
        await cls._project_geometry_inputs(session, publication)
        committed = await cls._load_geometry_bundle(session,
            resource_id=publication.resource.resource_id,
            resource_version_id=publication.version.resource_version_id)
        if committed is None:
            raise RuntimeError("committed geometry resource version is unreadable")
        cls._require_same_geometry_publication(committed, publication)
        return committed

    async def get_geometry_resource(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
    ) -> GeometryResourceRecord | None:
        self._require_geometry_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            resource = await session.scalar(
                select(GeometryResourceModel).where(
                    GeometryResourceModel.resource_id == resource_id
                )
            )
            return None if resource is None else self._geometry_resource_record(resource)

    async def get_exact_geometry_resource_version(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
        resource_version_id: str,
    ) -> GeometryResourceVersionBundle | None:
        self._require_geometry_context(context)
        try:
            async with self._sessions() as session:
                await set_tenant_execution_context(session, context)
                return await self._load_geometry_bundle(
                    session,
                    resource_id=resource_id,
                    resource_version_id=resource_version_id,
                )
        except SQLAlchemyTimeoutError as exc:
            raise TimeoutError("Geometry version database read timed out") from exc
        except DBAPIError as exc:
            if exc.connection_invalidated or getattr(exc.orig, "sqlstate", None) in {"40001", "40P01"}:
                raise ConnectionError("Geometry version database read was interrupted") from exc
            raise

    async def get_geometry_resource_version_by_id(
        self,
        context: TenantExecutionContext,
        *,
        resource_version_id: str,
    ) -> GeometryResourceVersionBundle | None:
        self._require_geometry_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            resource_id = await session.scalar(
                select(GeometryResourceVersionModel.resource_id).where(
                    GeometryResourceVersionModel.resource_version_id == resource_version_id
                )
            )
            if resource_id is None:
                return None
            return await self._load_geometry_bundle(
                session,
                resource_id=resource_id,
                resource_version_id=resource_version_id,
            )

    async def get_latest_geometry_resource_version(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
    ) -> GeometryResourceVersionBundle | None:
        self._require_geometry_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            resource_version_id = await session.scalar(
                select(GeometryResourceVersionModel.resource_version_id)
                .where(GeometryResourceVersionModel.resource_id == resource_id,
                       _current_geometry_version_predicate(GeometryResourceVersionModel))
                .order_by(
                    GeometryResourceVersionModel.version_no.desc(),
                    GeometryResourceVersionModel.resource_version_id.desc(),
                )
                .limit(1)
            )
            if resource_version_id is None:
                return None
            return await self._load_geometry_bundle(
                session,
                resource_id=resource_id,
                resource_version_id=resource_version_id,
            )

    async def list_geometry_resource_versions(
        self,
        context: TenantExecutionContext,
        *,
        query: GeometryResourceVersionListQuery,
    ) -> GeometryResourceVersionPage:
        self._require_geometry_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            statement = select(
                GeometryResourceVersionModel.resource_version_id,
                GeometryResourceVersionModel.version_no,
            ).where(GeometryResourceVersionModel.resource_id == query.resource_id,
                    _current_geometry_version_predicate(GeometryResourceVersionModel))
            if query.before_version_no is not None:
                statement = statement.where(
                    GeometryResourceVersionModel.version_no < query.before_version_no
                )
            rows = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            GeometryResourceVersionModel.version_no.desc(),
                            GeometryResourceVersionModel.resource_version_id.desc(),
                        ).limit(query.page_size + 1)
                    )
                ).all()
            )
            visible = rows[: query.page_size]
            complete = len(rows) <= query.page_size
            versions: list[GeometryResourceVersionBundle] = []
            for resource_version_id, _version_no in visible:
                bundle = await self._load_geometry_bundle(
                    session,
                    resource_id=query.resource_id,
                    resource_version_id=resource_version_id,
                )
                if bundle is None:
                    raise RuntimeError("listed geometry resource version became unreadable")
                versions.append(bundle)
            last = versions[-1] if versions and not complete else None
            return GeometryResourceVersionPage(
                versions=tuple(versions),
                complete=complete,
                next_before_version_no=(None if last is None else last.version.version_no),
            )

    async def list_geometry_resources(
        self,
        context: TenantExecutionContext,
        *,
        query: GeometryResourceListQuery,
    ) -> GeometryResourcePage:
        self._require_geometry_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            statement = select(GeometryResourceModel).where(
                exists(
                    select(1).where(
                        GeometryResourceVersionModel.resource_id
                        == GeometryResourceModel.resource_id,
                        _current_geometry_version_predicate(GeometryResourceVersionModel),
                    )
                )
            )
            if query.resource_kinds:
                statement = statement.where(
                    GeometryResourceModel.resource_kind.in_(
                        tuple(item.value for item in query.resource_kinds)
                    )
                )
            if query.before_created_at is not None:
                assert query.before_resource_id is not None
                statement = statement.where(
                    or_(
                        GeometryResourceModel.created_at < query.before_created_at,
                        and_(
                            GeometryResourceModel.created_at == query.before_created_at,
                            GeometryResourceModel.resource_id < query.before_resource_id,
                        ),
                    )
                )
            rows = tuple(
                (
                    await session.scalars(
                        statement.order_by(
                            GeometryResourceModel.created_at.desc(),
                            GeometryResourceModel.resource_id.desc(),
                        ).limit(query.page_size + 1)
                    )
                ).all()
            )
            visible = rows[: query.page_size]
            complete = len(rows) <= query.page_size
            records = tuple(self._geometry_resource_record(row) for row in visible)
            last = records[-1] if records and not complete else None
            return GeometryResourcePage(
                resources=records,
                complete=complete,
                next_before_created_at=None if last is None else last.created_at,
                next_before_resource_id=None if last is None else last.resource_id,
            )

    async def list_executable_program_versions(
        self,
        context: TenantExecutionContext,
        *,
        before_created_at: datetime | None,
        before_resource_version_id: str | None,
        limit: int,
    ) -> tuple[GeometryResourceVersionBundle, ...]:
        """List canonical executable Python bundles before applying the keyset limit."""

        self._require_geometry_context(context)
        if (before_created_at is None) != (before_resource_version_id is None):
            raise ValueError("geometry program boundary requires timestamp and version ID")
        if before_created_at is not None and (
            before_created_at.tzinfo is None or before_created_at.utcoffset() is None
        ):
            raise ValueError("geometry program boundary timestamp must be timezone-aware")
        if before_resource_version_id is not None and not (
            1 <= len(before_resource_version_id) <= 80
        ):
            raise ValueError("geometry program boundary version ID is invalid")
        if not 1 <= limit <= 51:
            raise ValueError("geometry program list limit must be between 1 and 51")

        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            has_canonical_source_bundle = exists(
                select(1).where(
                    GeometryResourceVersionAssetModel.resource_version_id
                    == GeometryResourceVersionModel.resource_version_id,
                    GeometryResourceVersionAssetModel.asset_role
                    == GeometryResourceAssetRole.SOURCE_BUNDLE.value,
                    GeometryResourceVersionAssetModel.asset_format == "source_bundle_v1",
                    GeometryResourceVersionAssetModel.content_type == "application/zip",
                )
            )
            statement = (
                select(
                    GeometryResourceVersionModel.resource_id,
                    GeometryResourceVersionModel.resource_version_id,
                )
                .join(
                    GeometryResourceModel,
                    GeometryResourceModel.resource_id
                    == GeometryResourceVersionModel.resource_id,
                )
                .where(
                    GeometryResourceModel.resource_kind
                    == GeometryResourceKind.MODELING_FUNCTION.value,
                    _current_geometry_version_predicate(GeometryResourceVersionModel),
                    GeometryResourceVersionModel.manifest_json["source_format"].as_string()
                    == "python_source_bundle_v1",
                    has_canonical_source_bundle,
                )
            )
            if before_created_at is not None:
                assert before_resource_version_id is not None
                statement = statement.where(
                    or_(
                        GeometryResourceVersionModel.created_at < before_created_at,
                        and_(
                            GeometryResourceVersionModel.created_at == before_created_at,
                            GeometryResourceVersionModel.resource_version_id
                            < before_resource_version_id,
                        ),
                    )
                )
            rows = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            GeometryResourceVersionModel.created_at.desc(),
                            GeometryResourceVersionModel.resource_version_id.desc(),
                        ).limit(limit)
                    )
                ).all()
            )
            programs: list[GeometryResourceVersionBundle] = []
            for resource_id, resource_version_id in rows:
                bundle = await self._load_geometry_bundle(
                    session,
                    resource_id=resource_id,
                    resource_version_id=resource_version_id,
                )
                if bundle is None:
                    raise RuntimeError("listed geometry program version became unreadable")
                programs.append(bundle)
            return tuple(programs)

    async def _reconcile_geometry_publication(
        self,
        context: TenantExecutionContext,
        publication: GeometryResourceVersionBundle,
    ) -> GeometryResourceVersionBundle | None:
        try:
            replay = await self.get_exact_geometry_resource_version(
                context,
                resource_id=publication.resource.resource_id,
                resource_version_id=publication.version.resource_version_id,
            )
        except SQLAlchemyError:
            return None
        if replay is None:
            return None
        self._require_same_geometry_publication(replay, publication)
        return replay

    @staticmethod
    def _require_geometry_context(context: TenantExecutionContext) -> None:
        active = current_tenant_execution_context()
        if active is None:
            raise TenantContextMismatch(
                "geometry resource access requires a tenant execution context"
            )
        if active != context:
            raise TenantContextMismatch(
                "geometry resource context does not match the active invocation"
            )

    @staticmethod
    def _require_geometry_write_scope(
        context: TenantExecutionContext,
        scope: GeometryResourceScope,
    ) -> None:
        if scope.scope_kind is GeometryResourceScopeKind.SYSTEM:
            if context.actor_kind is not TenantActorKind.SYSTEM_CURATOR:
                raise ConflictError("only the trusted system curator can publish SYSTEM resources")
            return
        if scope.scope_kind is GeometryResourceScopeKind.TENANT:
            if context.tenant_id != scope.tenant_id or context.task_id is None:
                raise ConflictError("geometry resource tenant scope does not match the invocation")
            return
        if context.tenant_id != scope.tenant_id or context.task_id != scope.task_id:
            raise ConflictError("geometry resource write scope does not match the invocation")

    @staticmethod
    def _new_geometry_resource_model(
        resource: GeometryResourceRecord,
    ) -> GeometryResourceModel:
        return GeometryResourceModel(
            resource_id=resource.resource_id,
            resource_kind=resource.resource_kind.value,
            owner_scope=resource.owner_scope.scope_kind.name,
            owner_tenant_id=resource.owner_scope.tenant_id,
            owner_task_id=resource.owner_scope.task_id,
            display_name=resource.display_name,
            created_at=resource.created_at,
        )

    @staticmethod
    def _new_geometry_version_model(
        version: GeometryResourceVersionRecord,
    ) -> GeometryResourceVersionModel:
        return GeometryResourceVersionModel(
            resource_version_id=version.resource_version_id,
            resource_id=version.resource_id,
            version_no=version.version_no,
            origin_kind=version.origin_kind,
            content_digest=version.content_digest,
            manifest_digest=version.manifest_digest,
            manifest_json=version.manifest,
            lineage_json=version.lineage,
            created_at=version.created_at,
        )

    @staticmethod
    def _new_geometry_asset_model(
        asset: GeometryResourceVersionAssetRecord,
    ) -> GeometryResourceVersionAssetModel:
        return GeometryResourceVersionAssetModel(
            resource_version_id=asset.resource_version_id,
            asset_role=asset.asset_role.value,
            asset_format=asset.asset_format,
            filename=asset.filename,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            sha256=asset.sha256,
            created_at=asset.created_at,
        )

    @staticmethod
    def _geometry_scope(model: GeometryResourceModel) -> GeometryResourceScope:
        return GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind(model.owner_scope.lower()),
            tenant_id=model.owner_tenant_id,
            task_id=model.owner_task_id,
        )

    @classmethod
    def _geometry_resource_record(
        cls,
        model: GeometryResourceModel,
    ) -> GeometryResourceRecord:
        return GeometryResourceRecord(
            resource_id=model.resource_id,
            resource_kind=GeometryResourceKind(model.resource_kind),
            owner_scope=cls._geometry_scope(model),
            display_name=model.display_name,
            created_at=as_utc(model.created_at),
        )

    @staticmethod
    def _geometry_version_record(
        model: GeometryResourceVersionModel,
    ) -> GeometryResourceVersionRecord:
        if "validation_profile" in model.manifest_json:
            raise GeometryCatalogReferenceError(
                "The Geometry version reference is unavailable"
            )
        return GeometryResourceVersionRecord(
            resource_version_id=model.resource_version_id,
            resource_id=model.resource_id,
            version_no=model.version_no,
            origin_kind=model.origin_kind,
            content_digest=model.content_digest,
            manifest_digest=model.manifest_digest,
            manifest=dict(model.manifest_json),
            lineage=dict(model.lineage_json),
            created_at=as_utc(model.created_at),
        )

    @staticmethod
    def _geometry_asset_record(
        model: GeometryResourceVersionAssetModel,
    ) -> GeometryResourceVersionAssetRecord:
        return GeometryResourceVersionAssetRecord(
            resource_version_id=model.resource_version_id,
            asset_role=GeometryResourceAssetRole(model.asset_role),
            asset_format=model.asset_format,
            filename=model.filename,
            content_type=model.content_type,
            size_bytes=model.size_bytes,
            sha256=model.sha256,
            created_at=as_utc(model.created_at),
        )

    @classmethod
    async def _project_geometry_inputs(
        cls, session: AsyncSession, bundle: GeometryResourceVersionBundle,
    ) -> None:
        for item in geometry_resource_inputs(bundle.version.manifest):
            row = (await session.execute(
                select(GeometryResourceModel, GeometryResourceVersionModel, GeometryResourceVersionAssetModel)
                .join(GeometryResourceVersionModel, GeometryResourceVersionModel.resource_id == GeometryResourceModel.resource_id)
                .join(GeometryResourceVersionAssetModel, GeometryResourceVersionAssetModel.resource_version_id == GeometryResourceVersionModel.resource_version_id)
                .where(GeometryResourceModel.resource_id == item.resource_id,
                       GeometryResourceVersionModel.resource_version_id == item.resource_version_id,
                       GeometryResourceVersionAssetModel.asset_role == GeometryResourceAssetRole.CANONICAL_GEOMETRY.value)
            )).one_or_none()
            if row is None:
                raise ConflictError("geometry input exact fixed BREP is not visible")
            root, version, asset = row
            cls._geometry_version_record(version)
            target_scope = cls._geometry_resource_record(root).owner_scope
            owner_scope = bundle.resource.owner_scope
            allowed_scope = owner_scope.permits_dependency(target_scope)
            if (not allowed_scope or root.resource_kind != GeometryResourceKind.MAPPED_MODEL.value
                or version.content_digest != item.content_digest
                or version.manifest_json.get("canonical_geometry_sha256") != item.canonical_geometry_sha256
                or asset.asset_format != "occt_brep_v1" or asset.sha256 != item.canonical_geometry_sha256
                or version.resource_version_id == bundle.version.resource_version_id):
                raise ConflictError("geometry input violates the frozen fixed BREP dependency contract")
            session.add(GeometryResourceVersionInputModel(
                resource_version_id=bundle.version.resource_version_id, input_key=item.input_key,
                target_resource_version_id=item.resource_version_id,
            ))
        await session.flush()

    @classmethod
    async def _load_geometry_bundle(
        cls,
        session: AsyncSession,
        *,
        resource_id: str,
        resource_version_id: str,
    ) -> GeometryResourceVersionBundle | None:
        row = (
            await session.execute(
                select(GeometryResourceModel, GeometryResourceVersionModel)
                .join(
                    GeometryResourceVersionModel,
                    GeometryResourceVersionModel.resource_id == GeometryResourceModel.resource_id,
                )
                .where(
                    GeometryResourceModel.resource_id == resource_id,
                    GeometryResourceVersionModel.resource_version_id == resource_version_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        resource, version = row
        try:
            version_record = cls._geometry_version_record(version)
        except ValidationError as exc:
            raise GeometryCatalogIntegrityError("stored geometry resource version failed integrity validation") from exc
        projected = (await session.execute(
            select(GeometryResourceVersionInputModel.input_key, GeometryResourceVersionInputModel.target_resource_version_id)
            .where(GeometryResourceVersionInputModel.resource_version_id == resource_version_id)
        )).all()
        expected = {(item.input_key, item.resource_version_id)
                    for item in geometry_resource_inputs(version.manifest_json)}
        if set(projected) != expected:
            raise GeometryCatalogIntegrityError("stored geometry input projection disagrees with the version manifest")
        assets = tuple(
            (
                await session.scalars(
                    select(GeometryResourceVersionAssetModel)
                    .where(
                        GeometryResourceVersionAssetModel.resource_version_id == resource_version_id
                    )
                    .order_by(GeometryResourceVersionAssetModel.asset_role)
                )
            ).all()
        )
        try:
            bundle = GeometryResourceVersionBundle(
                resource=cls._geometry_resource_record(resource),
                version=version_record,
                assets=tuple(cls._geometry_asset_record(asset) for asset in assets),
            )
            require_formal_geometry_publication(bundle.version, bundle.assets)
            return bundle
        except ValueError as exc:
            raise GeometryCatalogIntegrityError(
                "stored geometry resource version failed integrity validation"
            ) from exc

    @classmethod
    def _require_same_geometry_root(
        cls,
        stored: GeometryResourceModel,
        incoming: GeometryResourceRecord,
    ) -> None:
        current = cls._geometry_resource_record(stored)
        if current.model_dump(exclude={"created_at"}) != incoming.model_dump(
            exclude={"created_at"}
        ):
            raise IdempotencyConflictError(
                "geometry resource identity was reused with different root metadata"
            )

    @staticmethod
    def _publication_identity(
        value: GeometryResourceVersionBundle,
    ) -> dict[str, object]:
        identity = value.model_dump(
            mode="json",
            exclude={
                "resource": {"created_at"},
                "version": {"created_at"},
                "assets": {"__all__": {"created_at"}},
            },
        )
        # Asset roles are unique and the content digest already defines their
        # identity in role order.  PostgreSQL reads them back in that canonical
        # order, while callers may construct an equivalent bundle in any tuple
        # order; ordering is not an additional resource fact.
        assets = identity.get("assets")
        if isinstance(assets, list):
            identity["assets"] = sorted(
                assets,
                key=lambda asset: str(asset["asset_role"]),
            )
        return identity

    @classmethod
    def _require_same_geometry_publication(
        cls,
        stored: GeometryResourceVersionBundle,
        incoming: GeometryResourceVersionBundle,
    ) -> None:
        if cls._publication_identity(stored) != cls._publication_identity(incoming):
            raise IdempotencyConflictError(
                "geometry resource version identity was reused with different content"
            )


__all__ = ["PostgresGeometryResourceRepositoryMixin"]
