"""PostgreSQL persistence for Operation-backed geometry authoring writes."""

from __future__ import annotations

from sqlalchemy import select

from CAD2BIM_backend.domain.data_runtime import (
    CatalogResourceKind,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.postgres.models import (
    CriticalOperationModel,
    DataCatalogEntryModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import PostgresRepositoryBase
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    PostgresDataRuntimeRepositoryMixin,
    _current_operation_contract_predicate,
)
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import (
    PostgresGeometryResourceRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    set_tenant_execution_context,
)


class PostgresGeometryAuthoringRepositoryMixin(PostgresRepositoryBase):
    """Authoring persistence through the shared control-plane session factory."""

    async def get_authoring_draft_snapshot(self, scope, *, draft):
        from .repository_authoring_draft import get_draft_snapshot
        return await get_draft_snapshot(self, scope, draft=draft)

    async def get_geometry_draft(self, scope, *, resource_id):
        from .repository_authoring_draft import get_draft
        return await get_draft(self, scope, root_kind="geometry", root_id=resource_id)

    async def list_geometry_drafts(self, scope, *, after_root_id=None, limit=50):
        from .repository_authoring_draft import list_drafts
        return await list_drafts(self, scope, root_kind="geometry", after_root_id=after_root_id, limit=limit)

    async def commit_geometry_draft(self, scope, *, resource, snapshot, expected_generation, operation):
        from .repository_authoring_draft import commit_draft
        return await commit_draft(self, scope, root_kind="geometry", root_record=resource,
            snapshot=snapshot, expected_generation=expected_generation, operation=operation,
            new_root=PostgresGeometryResourceRepositoryMixin._new_geometry_resource_model,
            require_same_root=PostgresGeometryResourceRepositoryMixin._require_same_geometry_root)


    async def get_geometry_authoring_operation_state(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> CriticalOperationRecord | None:
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope, lock=False)
            model = await self._find_geometry_operation_model(session, scope, operation_id)
            return (
                None
                if model is None
                else await PostgresDataRuntimeRepositoryMixin._operation_record(session, model)
            )

    async def get_geometry_authoring_operation(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> PublishedCriticalOperation | None:
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope, lock=False)
            model = await session.scalar(
                select(CriticalOperationModel)
                .join(
                    DataCatalogEntryModel,
                    DataCatalogEntryModel.operation_id == CriticalOperationModel.operation_id,
                )
                .where(
                    _current_operation_contract_predicate(),
                    CriticalOperationModel.owner_id == scope.owner_id,
                    CriticalOperationModel.task_id == scope.identity.task_id,
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.operation_id == operation_id,
                    CriticalOperationModel.operation_kind
                    == CriticalOperationKind.GEOMETRY_RESOURCE_WRITE.value,
                    CriticalOperationModel.status.in_(
                        (
                            CriticalOperationStatus.SUCCEEDED.value,
                            CriticalOperationStatus.FAILED.value,
                        )
                    ),
                    DataCatalogEntryModel.run_id == scope.run_id,
                    DataCatalogEntryModel.resource_kind == CatalogResourceKind.OPERATION.value,
                )
            )
            if model is None:
                return None
            return await PostgresDataRuntimeRepositoryMixin._published_operation(
                self,
                session,
                model,
                run_id=scope.run_id,
                frontier=None,
            )


    async def _find_geometry_operation_model(self, session, scope, operation_id):
        return await session.scalar(
            select(CriticalOperationModel).where(
                _current_operation_contract_predicate(),
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.run_id == scope.run_id,
                CriticalOperationModel.operation_id == operation_id,
                CriticalOperationModel.operation_kind
                == CriticalOperationKind.GEOMETRY_RESOURCE_WRITE.value,
            )
        )


__all__ = ["PostgresGeometryAuthoringRepositoryMixin"]
