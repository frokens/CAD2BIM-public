"""PostgreSQL read persistence for the ready immutable ProductFamily catalog."""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogIntegrityError, ProductFamilyProgramSource,
    ProductFamilyDiscoveryPage, ProductFamilyMetadataPage, ProductFamilyVersionMetadata,
    ProductFamilyProgramSourcePage, ProductFamilyStoredRejection,
    ProductFamilyStoredContractError, ProductFamilyValidationIssue,
)
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyContract,
    ProductFamilyGeometryBindingRecord,
    ProductFamilyGeometryPurpose,
    ProductFamilyListEntry,
    ProductFamilyListQuery,
    ProductFamilyRecord,
    ProductFamilyVersionAssetRecord,
    ProductFamilyVersionAssetRole,
    ProductFamilyVersionBundle,
    ProductFamilyVersionListQuery,
    ProductFamilyVersionRecord,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.infrastructure.postgres.models import (
    ProductFamilyGeometryBindingModel,
    ProductFamilyModel,
    ProductFamilyVersionAssetModel,
    ProductFamilyVersionModel,
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


class PostgresProductFamilyRepositoryMixin(PostgresRepositoryBase):
    """ProductFamily reads using the shared session factory and active RLS context."""

    async def delete_product_family(self, context, *, family_id: str) -> bool:
        self._require_product_family_context(context)
        return await self._delete_library_resource(context, root_kind="family", root_id=family_id)

    async def get_product_family(
        self,
        context: TenantExecutionContext,
        *,
        family_id: str,
    ) -> ProductFamilyRecord | None:
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            family = await session.scalar(
                select(ProductFamilyModel).where(
                    ProductFamilyModel.family_id == family_id,
                    exists(
                        select(1).where(
                            ProductFamilyVersionModel.family_id == ProductFamilyModel.family_id
                        )
                    ),
                )
            )
            return None if family is None else self._product_family_record(family)

    async def get_exact_product_family_version(
        self,
        context: TenantExecutionContext,
        *,
        family_id: str,
        family_version_id: str,
    ) -> ProductFamilyVersionBundle | None:
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            return await self._load_product_family_bundle(
                session,
                family_id=family_id,
                family_version_id=family_version_id,
            )

    async def get_product_family_version_by_id(
        self,
        context: TenantExecutionContext,
        *,
        family_version_id: str,
    ) -> ProductFamilyVersionBundle | None:
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            family_id = await session.scalar(
                select(ProductFamilyVersionModel.family_id).where(
                    ProductFamilyVersionModel.family_version_id == family_version_id
                )
            )
            if family_id is None:
                return None
            return await self._load_product_family_bundle(
                session,
                family_id=family_id,
                family_version_id=family_version_id,
            )

    async def get_latest_product_family_version(
        self,
        context: TenantExecutionContext,
        *,
        family_id: str,
    ) -> ProductFamilyVersionBundle | None:
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            family_version_id = await session.scalar(self._latest_family_version_id_statement(family_id))
            if family_version_id is None:
                return None
            return await self._load_product_family_bundle(
                session,
                family_id=family_id,
                family_version_id=family_version_id,
            )

    @staticmethod
    def _latest_family_version_id_statement(family_id):
        return (select(ProductFamilyVersionModel.family_version_id)
            .where(ProductFamilyVersionModel.family_id == family_id)
            .order_by(ProductFamilyVersionModel.version_no.desc(), ProductFamilyVersionModel.family_version_id.desc())
            .limit(1))

    async def get_latest_product_family_version_id(
        self, context: TenantExecutionContext, *, family_id: str,
    ) -> str | None:
        """Read a publication parent identity without loading its former content contract."""
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            return await session.scalar(self._latest_family_version_id_statement(family_id))

    async def get_product_family_version_identity(
        self, context: TenantExecutionContext, *, family_id: str, family_version_id: str,
    ) -> str | None:
        """Authorize an exact same-root identity without reading contract or summary."""
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            return await session.scalar(
                select(ProductFamilyVersionModel.family_version_id)
                .join(ProductFamilyModel, ProductFamilyModel.family_id == ProductFamilyVersionModel.family_id)
                .where(ProductFamilyModel.family_id == family_id,
                       ProductFamilyVersionModel.family_version_id == family_version_id)
            )

    async def get_product_family_version_metadata(
        self, context: TenantExecutionContext, *, family_id: str, family_version_id: str,
    ) -> ProductFamilyVersionMetadata | None:
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            row = (await session.execute(
                select(ProductFamilyModel, ProductFamilyVersionModel)
                .join(ProductFamilyVersionModel, ProductFamilyVersionModel.family_id == ProductFamilyModel.family_id)
                .where(ProductFamilyModel.family_id == family_id,
                       ProductFamilyVersionModel.family_version_id == family_version_id)
            )).one_or_none()
            return None if row is None else self._product_family_version_metadata(*row)

    async def list_product_family_versions(
        self,
        context: TenantExecutionContext,
        *,
        query: ProductFamilyVersionListQuery,
    ) -> ProductFamilyMetadataPage:
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            statement = select(ProductFamilyModel, ProductFamilyVersionModel).join(
                ProductFamilyVersionModel, ProductFamilyVersionModel.family_id == ProductFamilyModel.family_id,
            ).where(ProductFamilyModel.family_id == query.family_id)
            if query.before_version_no is not None:
                statement = statement.where(
                    ProductFamilyVersionModel.version_no < query.before_version_no
                )
            rows = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            ProductFamilyVersionModel.version_no.desc(),
                            ProductFamilyVersionModel.family_version_id.desc(),
                        ).limit(query.page_size + 1)
                    )
                ).all()
            )
            visible = rows[: query.page_size]
            complete = len(rows) <= query.page_size
            versions = tuple(self._product_family_version_metadata(*row) for row in visible)
            last = versions[-1] if versions and not complete else None
            return ProductFamilyMetadataPage(
                versions=versions,
                complete=complete,
                next_before_version_no=None if last is None else last.version_no,
            )

    async def list_product_families(
        self,
        context: TenantExecutionContext,
        *,
        query: ProductFamilyListQuery,
    ) -> ProductFamilyDiscoveryPage:
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            candidate = aliased(ProductFamilyVersionModel)
            latest = aliased(ProductFamilyVersionModel)
            latest_version_id = (
                select(candidate.family_version_id)
                .where(candidate.family_id == ProductFamilyModel.family_id)
                .order_by(candidate.version_no.desc(), candidate.family_version_id.desc())
                .limit(1)
                .correlate(ProductFamilyModel)
                .scalar_subquery()
            )
            statement = select(ProductFamilyModel, latest).join(
                latest,
                latest.family_version_id == latest_version_id,
            )
            if query.owner_scopes:
                statement = statement.where(
                    ProductFamilyModel.owner_scope.in_(
                        tuple(item.name for item in query.owner_scopes)
                    )
                )
            if query.product_structure_kinds:
                statement = statement.where(
                    latest.family_contract_json["product_structure_kind"]
                    .as_string()
                    .in_(tuple(item.value for item in query.product_structure_kinds))
                )
            if query.purpose_contains is not None:
                statement = statement.where(
                    func.strpos(
                        func.lower(latest.purpose),
                        query.purpose_contains.lower(),
                    )
                    > 0
                )
            if query.before_created_at is not None:
                assert query.before_family_id is not None
                statement = statement.where(
                    or_(
                        ProductFamilyModel.created_at < query.before_created_at,
                        and_(
                            ProductFamilyModel.created_at == query.before_created_at,
                            ProductFamilyModel.family_id < query.before_family_id,
                        ),
                    )
                )
            rows = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            ProductFamilyModel.created_at.desc(),
                            ProductFamilyModel.family_id.desc(),
                        ).limit(query.page_size + 1)
                    )
                ).all()
            )
            visible = rows[: query.page_size]
            complete = len(rows) <= query.page_size
            entries: list[ProductFamilyListEntry] = []
            rejections: list[ProductFamilyStoredRejection] = []
            for family_model, version_model in visible:
                try:
                    bundle = await self._load_product_family_bundle(
                        session,
                        family_id=family_model.family_id,
                        family_version_id=version_model.family_version_id,
                    )
                except ProductFamilyStoredContractError as exc:
                    rejections.append(self._product_family_rejection(family_model, version_model, exc))
                    continue
                if bundle is None:
                    raise RuntimeError("listed ProductFamily version became unreadable")
                entries.append(
                    ProductFamilyListEntry(
                        family=self._product_family_record(family_model),
                        latest_version=bundle,
                    )
                )
            last = visible[-1][0] if visible and not complete else None
            return ProductFamilyDiscoveryPage(
                entries=tuple(entries),
                rejections=tuple(rejections),
                complete=complete,
                next_before_created_at=None if last is None else last.created_at,
                next_before_family_id=None if last is None else last.family_id,
            )

    async def get_product_family_program_source(
        self, context: TenantExecutionContext, *, family_version_id: str,
        family_id: str | None = None,
    ) -> ProductFamilyProgramSource | None:
        self._require_product_family_context(context)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            return await self._load_product_family_program_source(
                session, family_version_id=family_version_id, family_id=family_id,
            )

    @classmethod
    async def _load_product_family_program_source(
        cls, session, *, family_version_id: str, family_id: str | None = None,
    ) -> ProductFamilyProgramSource | None:
        statement = (
            select(ProductFamilyModel, ProductFamilyVersionModel, ProductFamilyVersionAssetModel)
            .join(ProductFamilyVersionModel, ProductFamilyVersionModel.family_id == ProductFamilyModel.family_id)
            .join(ProductFamilyVersionAssetModel, ProductFamilyVersionAssetModel.family_version_id == ProductFamilyVersionModel.family_version_id)
            .where(ProductFamilyVersionModel.family_version_id == family_version_id,
                   ProductFamilyVersionAssetModel.asset_role == ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE.value)
        )
        if family_id is not None:
            statement = statement.where(ProductFamilyModel.family_id == family_id)
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        family, version, asset = row
        cls._product_family_version_record(version)
        return ProductFamilyProgramSource(
            family=cls._product_family_record(family),
            family_version_id=version.family_version_id, version_no=version.version_no,
            created_at=as_utc(version.created_at),
            source_asset=cls._product_family_asset_record(asset),
        )

    async def list_product_family_program_sources(
        self,
        context: TenantExecutionContext,
        *,
        before_created_at,
        before_family_version_id: str | None,
        limit: int,
    ) -> ProductFamilyProgramSourcePage:
        """List visible immutable versions that own an exact realization source bundle."""

        self._require_product_family_context(context)
        if (before_created_at is None) != (before_family_version_id is None):
            raise ValueError("product family program boundary is incomplete")
        if before_created_at is not None and (
            before_created_at.tzinfo is None or before_created_at.utcoffset() is None
        ):
            raise ValueError("product family program boundary must be timezone-aware")
        if before_family_version_id is not None and not (
            1 <= len(before_family_version_id) <= 80
        ):
            raise ValueError("product family program boundary version ID is invalid")
        if not 1 <= limit <= 50:
            raise ValueError("product family program limit must be between 1 and 50")
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            statement = (
                select(ProductFamilyModel, ProductFamilyVersionModel)
                .join(ProductFamilyVersionModel, ProductFamilyVersionModel.family_id == ProductFamilyModel.family_id)
                .where(
                    exists(
                        select(1).where(
                            ProductFamilyVersionAssetModel.family_version_id
                            == ProductFamilyVersionModel.family_version_id,
                            ProductFamilyVersionAssetModel.asset_role
                            == ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE.value,
                        )
                    )
                )
            )
            if before_created_at is not None:
                assert before_family_version_id is not None
                statement = statement.where(
                    or_(
                        ProductFamilyVersionModel.created_at < before_created_at,
                        and_(
                            ProductFamilyVersionModel.created_at == before_created_at,
                            ProductFamilyVersionModel.family_version_id
                            < before_family_version_id,
                        ),
                    )
                )
            rows = tuple(
                (
                    await session.execute(
                        statement.order_by(
                            ProductFamilyVersionModel.created_at.desc(),
                            ProductFamilyVersionModel.family_version_id.desc(),
                        ).limit(limit + 1)
                    )
                ).all()
            )
            versions: list[ProductFamilyProgramSource] = []
            rejections: list[ProductFamilyStoredRejection] = []
            visible = rows[:limit]
            complete = len(rows) <= limit
            for family, version in visible:
                try:
                    bundle = await self._load_product_family_program_source(
                        session,
                        family_id=family.family_id,
                        family_version_id=version.family_version_id,
                    )
                except ProductFamilyStoredContractError as exc:
                    rejections.append(self._product_family_rejection(family, version, exc))
                    continue
                if bundle is None:
                    raise RuntimeError("listed ProductFamily program became unreadable")
                versions.append(bundle)
            last = visible[-1][1] if visible and not complete else None
            return ProductFamilyProgramSourcePage(
                sources=tuple(versions), rejections=tuple(rejections), complete=complete,
                next_before_created_at=None if last is None else as_utc(last.created_at),
                next_before_family_version_id=None if last is None else last.family_version_id,
            )

    @staticmethod
    def _require_product_family_context(context: TenantExecutionContext) -> None:
        active = current_tenant_execution_context()
        if active is None:
            raise TenantContextMismatch("ProductFamily access requires a tenant execution context")
        if active != context:
            raise TenantContextMismatch(
                "ProductFamily context does not match the active invocation"
            )

    @staticmethod
    def _owner_scope(model: ProductFamilyModel) -> ResourceOwnerScope:
        return ResourceOwnerScope(
            scope_kind=ResourceOwnerScopeKind(model.owner_scope.lower()),
            tenant_id=model.owner_tenant_id,
            task_id=model.owner_task_id,
        )

    @classmethod
    def _product_family_record(cls, model: ProductFamilyModel) -> ProductFamilyRecord:
        return ProductFamilyRecord(
            family_id=model.family_id,
            owner_scope=cls._owner_scope(model),
            created_at=as_utc(model.created_at),
        )

    @staticmethod
    def _product_family_version_record(
        model: ProductFamilyVersionModel,
    ) -> ProductFamilyVersionRecord:
        try:
            contract = ProductFamilyContract.model_validate(model.family_contract_json)
        except ValidationError as exc:
            issues = tuple(ProductFamilyValidationIssue(
                field_path=".".join(str(part) for part in ("family_contract", *error["loc"])),
                # Custom validators can wrap another exception with its input. Keep that private.
                reason=("Current Family contract value failed validation" if error["type"] == "value_error"
                        # The default union message includes the offending input tag.
                        else "Expected one of: " + error["ctx"]["expected_tags"] if error["type"] == "union_tag_invalid"
                        else error["msg"]),
            ) for error in exc.errors(include_input=False, include_url=False))
            raise ProductFamilyStoredContractError(issues) from None
        return ProductFamilyVersionRecord(
            family_version_id=model.family_version_id,
            family_id=model.family_id,
            version_no=model.version_no,
            parent_family_version_id=model.parent_family_version_id,
            display_name=model.display_name,
            purpose=model.purpose,
            usage_instructions=model.usage_instructions,
            family_contract=contract,
            contract_digest=model.contract_digest,
            content_digest=model.content_digest,
            program_entry=model.program_entry,
            created_at=as_utc(model.created_at),
        )

    @classmethod
    def _product_family_version_metadata(cls, family, version) -> ProductFamilyVersionMetadata:
        return ProductFamilyVersionMetadata(
            family=cls._product_family_record(family),
            family_version_id=version.family_version_id, version_no=version.version_no,
            parent_family_version_id=version.parent_family_version_id,
            display_name=version.display_name, purpose=version.purpose,
            product_structure_kind=version.family_contract_json.get("product_structure_kind"),
            contract_digest=version.contract_digest, content_digest=version.content_digest,
            created_at=as_utc(version.created_at),
        )

    @classmethod
    def _product_family_rejection(cls, family, version, exc) -> ProductFamilyStoredRejection:
        return ProductFamilyStoredRejection(
            family=cls._product_family_record(family),
            family_version_id=version.family_version_id, version_no=version.version_no,
            created_at=as_utc(version.created_at), issues=exc.issues,
        )

    @staticmethod
    def _product_family_binding_record(
        model: ProductFamilyGeometryBindingModel,
    ) -> ProductFamilyGeometryBindingRecord:
        return ProductFamilyGeometryBindingRecord(
            family_version_id=model.family_version_id,
            binding_key=model.binding_key,
            geometry_resource_id=model.geometry_resource_id,
            geometry_resource_version_id=model.geometry_resource_version_id,
            geometry_content_digest=model.geometry_content_digest,
            expected_resource_kind=GeometryResourceKind(model.expected_resource_kind),
            geometry_purpose=ProductFamilyGeometryPurpose(model.geometry_purpose),
        )

    @staticmethod
    def _product_family_asset_record(
        model: ProductFamilyVersionAssetModel,
    ) -> ProductFamilyVersionAssetRecord:
        return ProductFamilyVersionAssetRecord(
            family_version_id=model.family_version_id,
            asset_role=ProductFamilyVersionAssetRole(model.asset_role),
            asset_format=model.asset_format,
            filename=model.filename,
            content_type=model.content_type,
            size_bytes=model.size_bytes,
            sha256=model.sha256,
            created_at=as_utc(model.created_at),
        )

    @classmethod
    async def _load_product_family_bundle(
        cls,
        session: AsyncSession,
        *,
        family_id: str,
        family_version_id: str,
    ) -> ProductFamilyVersionBundle | None:
        row = (
            await session.execute(
                select(ProductFamilyModel, ProductFamilyVersionModel)
                .join(
                    ProductFamilyVersionModel,
                    ProductFamilyVersionModel.family_id == ProductFamilyModel.family_id,
                )
                .where(
                    ProductFamilyModel.family_id == family_id,
                    ProductFamilyVersionModel.family_version_id == family_version_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        family, version = row
        bindings = tuple(
            (
                await session.scalars(
                    select(ProductFamilyGeometryBindingModel)
                    .where(ProductFamilyGeometryBindingModel.family_version_id == family_version_id)
                    .order_by(ProductFamilyGeometryBindingModel.binding_key)
                )
            ).all()
        )
        assets = tuple(
            (
                await session.scalars(
                    select(ProductFamilyVersionAssetModel)
                    .where(ProductFamilyVersionAssetModel.family_version_id == family_version_id)
                    .order_by(ProductFamilyVersionAssetModel.asset_role)
                )
            ).all()
        )
        try:
            return ProductFamilyVersionBundle(
                family=cls._product_family_record(family),
                version=cls._product_family_version_record(version),
                geometry_bindings=tuple(
                    cls._product_family_binding_record(binding) for binding in bindings
                ),
                assets=tuple(cls._product_family_asset_record(asset) for asset in assets),
            )
        except ValidationError as exc:
            raise RuntimeError("stored ProductFamily version failed integrity validation") from exc


__all__ = ["PostgresProductFamilyRepositoryMixin"]
