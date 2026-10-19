"""PostgreSQL persistence for Operation-backed ProductFamily authoring."""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from CAD2BIM_backend.domain.control_plane import ConflictError, IdempotencyConflictError
from CAD2BIM_backend.domain.data_runtime import (
    AuthoringExecutionClaim,
    AuthoringExecutionState,
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.identity import TenantActorKind
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScopeKind
from CAD2BIM_backend.ports.identity import IdentityAccessDenied
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyVersionBundle,
)
from CAD2BIM_backend.domain.product_family_authoring_contracts import ProductFamilyAuthoringVersionDraft
from CAD2BIM_backend.domain.product_family_authoring import (
    ProductFamilyAuthoringCommitResult,
    ProductFamilyAuthoringPublication,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    CriticalOperationModel,
    DataCatalogEntryModel,
    ProductFamilyGeometryBindingModel,
    ProductFamilyModel,
    ProductFamilyVersionAssetModel,
    ProductFamilyVersionModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import PostgresRepositoryBase
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    PostgresDataRuntimeRepositoryMixin,
    _current_operation_contract_predicate,
    append_catalog_entry,
    next_catalog_revision,
)
from CAD2BIM_backend.infrastructure.postgres.repository_product_family import (
    PostgresProductFamilyRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import PostgresGeometryResourceRepositoryMixin
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    current_tenant_execution_context,
    set_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import require_system_curator
from CAD2BIM_backend.ports.product_family_authoring import (
    ProductFamilyAuthoringCommitFenceError,
    ProductFamilyAuthoringCommitOutcomeUnknownError,
)


def _repair_draft_chain(version_draft: dict[str, object]) -> tuple[str, str | None]:
    draft = ProductFamilyAuthoringVersionDraft.model_validate(version_draft)
    return draft.family_id, draft.parent_family_version_id


class PostgresProductFamilyAuthoringRepositoryMixin(PostgresRepositoryBase):
    """Publish Task ProductFamily versions through the shared transaction owner."""

    async def get_product_family_authoring_root(self, scope, *, family_id):
        from .repository_authoring_draft import require_context, private_root_predicate, read_snapshot
        require_context(scope)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            if scope.identity.actor_kind is TenantActorKind.SYSTEM_CURATOR:
                await require_system_curator(session)
            root = await session.scalar(select(ProductFamilyModel).where(
                ProductFamilyModel.family_id == family_id, private_root_predicate(ProductFamilyModel, scope)))
            if root is None or root.draft_artifact_version_id is None:
                return None
            await read_snapshot(session, scope, root.draft_artifact_version_id, root=root)
            return PostgresProductFamilyRepositoryMixin._product_family_record(root)

    async def find_current_authoring_publication(self, scope, *, root_kind, root_id, expected_drafts):
        from .repository_authoring_draft import find_current_publication
        return await find_current_publication(self, scope, root_kind=root_kind,
            root_id=root_id, expected_drafts=expected_drafts)

    async def find_published_geometry_draft(self, scope, *, draft):
        from .repository_authoring_draft import find_published_geometry_draft
        return await find_published_geometry_draft(self, scope, draft=draft)

    async def get_product_family_draft(self, scope, *, family_id):
        from .repository_authoring_draft import get_draft
        return await get_draft(self, scope, root_kind="family", root_id=family_id)

    async def list_product_family_drafts(self, scope, *, after_root_id=None, limit=50):
        from .repository_authoring_draft import list_drafts
        return await list_drafts(self, scope, root_kind="family", after_root_id=after_root_id, limit=limit)

    async def commit_product_family_draft(self, scope, *, family, snapshot, expected_generation, operation):
        from .repository_authoring_draft import commit_draft
        return await commit_draft(self, scope, root_kind="family", root_record=family,
            snapshot=snapshot, expected_generation=expected_generation, operation=operation,
            new_root=self._new_family_model, require_same_root=self._require_same_family_root)

    async def begin_authoring_publication(self, scope, *, operation, expected_drafts):
        from .repository_authoring_draft import begin_publication
        return await begin_publication(self, scope, operation=operation, expected_drafts=expected_drafts)

    async def finalize_authoring_publication_operation(self, scope, *, operation, execution_claim):
        from .repository_authoring_draft import finalize_publication_failure
        return await finalize_publication_failure(self, scope, operation=operation, execution_claim=execution_claim)

    async def commit_authoring_publication(self, scope, *, operation, geometry_publications=(),
                                            family_publication=None, expected_drafts, execution_claim):
        from .repository_authoring_draft import commit_publication
        return await commit_publication(self, scope, operation=operation,
            geometry_publications=geometry_publications, family_publication=family_publication,
            expected_drafts=expected_drafts, execution_claim=execution_claim)

    async def authorize_system_family_publication(self, scope: InvocationScope) -> None:
        if scope.identity.actor_kind is not TenantActorKind.SYSTEM_CURATOR:
            raise IdentityAccessDenied("SYSTEM publication requires a curator actor")
        if current_tenant_execution_context() != scope.identity:
            raise IdentityAccessDenied("SYSTEM publication requires the active management identity")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await require_system_curator(session)
            run = await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope, lock=False)
            if run.module != "system_library" or run.stage_key != "publication":
                raise IdentityAccessDenied("SYSTEM publication requires its management Run")

    @staticmethod
    async def _require_system_copy_authority(session, scope, operation) -> None:
        if operation.detail.mode != "system_copy":
            return
        if scope.identity.actor_kind is not TenantActorKind.SYSTEM_CURATOR:
            raise IdentityAccessDenied("SYSTEM copy requires a curator management identity")
        await require_system_curator(session)
        plan = operation.detail.system_copy
        if plan is None or plan.target_owner.scope_kind is not ResourceOwnerScopeKind.SYSTEM:
            raise ConflictError("SYSTEM copy has no exact SYSTEM owner plan")
        run = await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope, lock=False)
        if run.module != "system_library" or run.stage_key != "publication":
            raise IdentityAccessDenied("SYSTEM copy requires its management Run")

    @staticmethod
    async def _require_system_copy_dependencies(session, operation) -> None:
        if operation.detail.mode != "system_copy":
            return
        for item in operation.detail.system_copy.geometry_copies:
            target = item.target_version
            actual = await PostgresGeometryResourceRepositoryMixin._load_geometry_bundle(
                session, resource_id=target.resource.resource_id,
                resource_version_id=target.version.resource_version_id,
            )
            if actual != target or target.resource.owner_scope.scope_kind is not ResourceOwnerScopeKind.SYSTEM:
                raise ConflictError("SYSTEM copy Geometry closure changed before Family commit")

    async def get_product_family_authoring_execution_state(
        self, scope: InvocationScope, operation_id: str,
    ) -> AuthoringExecutionState | None:
        return await PostgresDataRuntimeRepositoryMixin.get_authoring_execution_state(
            self, scope, operation_id,
            operation_kind=CriticalOperationKind.PRODUCT_FAMILY_WRITE,
        )

    async def claim_product_family_authoring_execution(
        self, scope: InvocationScope, operation_id: str,
    ) -> AuthoringExecutionClaim | None:
        return await PostgresDataRuntimeRepositoryMixin.claim_authoring_execution(
            self, scope, operation_id,
            operation_kind=CriticalOperationKind.PRODUCT_FAMILY_WRITE,
        )

    async def reclaim_product_family_authoring_execution(
        self, scope: InvocationScope, operation_id: str, *,
        expected_owner: str, expected_generation: int,
    ) -> AuthoringExecutionClaim | None:
        return await PostgresDataRuntimeRepositoryMixin.reclaim_authoring_execution(
            self, scope, operation_id,
            operation_kind=CriticalOperationKind.PRODUCT_FAMILY_WRITE,
            expected_owner=expected_owner, expected_generation=expected_generation,
        )

    async def prepare_product_family_authoring_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
    ) -> CriticalOperationRecord:
        self._require_prepared_product_family_operation(operation)
        try:
            async with self._sessions() as session, session.begin():
                await set_tenant_execution_context(session, scope.identity)
                await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope)
                PostgresDataRuntimeRepositoryMixin._require_record_scope(scope, operation)
                await self._require_system_copy_authority(session, scope, operation)
                await PostgresDataRuntimeRepositoryMixin._require_operation_artifacts(
                    session, operation
                )
                await self._require_product_family_source_artifact(session, operation)
                await self._require_product_family_repair_parent(session, operation)
                await self._require_product_family_base(session, operation)
                replay = await self._find_product_family_replay(session, operation)
                if replay is not None:
                    stored = await PostgresDataRuntimeRepositoryMixin._operation_record(
                        session, replay
                    )
                    self._require_same_product_family_request(stored, operation)
                    return stored
                session.add(PostgresDataRuntimeRepositoryMixin._operation_model(operation))
                await session.flush()
                return operation
        except IntegrityError as exc:
            replay = await self._reconcile_product_family_authoring_preparation(scope, operation)
            if replay is not None:
                return replay
            raise ConflictError("ProductFamily Operation could not be prepared") from exc

    async def complete_product_family_authoring_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> PublishedCriticalOperation:
        if (
            operation.operation_kind is not CriticalOperationKind.PRODUCT_FAMILY_WRITE
            or operation.status is not CriticalOperationStatus.FAILED
        ):
            raise ValueError("ProductFamily completion requires a FAILED Operation")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope)
            stored_model = await self._require_product_family_operation_model(
                session, scope, operation.operation_id, lock=True
            )
            stored = await PostgresDataRuntimeRepositoryMixin._operation_record(
                session, stored_model
            )
            self._require_same_product_family_request(stored, operation)
            if stored.status is not CriticalOperationStatus.PREPARED:
                self._require_same_product_family_terminal(stored, operation)
                published = await PostgresDataRuntimeRepositoryMixin._published_operation(
                    self, session, stored_model, run_id=scope.run_id
                )
                if published is None:
                    raise ConflictError("failed ProductFamily Operation has no Catalog alias")
                return published
            PostgresDataRuntimeRepositoryMixin.require_authoring_claim(stored_model, execution_claim)
            self._apply_product_family_authoring_terminal(stored_model, operation)
            entry = await append_catalog_entry(
                session,
                owner_id=operation.owner_id,
                tenant_id=operation.tenant_id,
                task_id=operation.task_id,
                run_id=operation.run_id,
                kind=CatalogResourceKind.OPERATION,
                catalog_revision=await next_catalog_revision(session, operation.run_id),
                operation_id=operation.operation_id,
            )
            await session.flush()
            return PublishedCriticalOperation(
                operation=operation,
                catalog_entry=PostgresDataRuntimeRepositoryMixin._catalog_record(entry),
            )

    async def mark_product_family_authoring_indeterminate(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> CriticalOperationRecord:
        if (
            operation.operation_kind is not CriticalOperationKind.PRODUCT_FAMILY_WRITE
            or operation.status is not CriticalOperationStatus.INDETERMINATE
        ):
            raise ValueError("ProductFamily pause requires an INDETERMINATE Operation")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope)
            stored_model = await self._require_product_family_operation_model(
                session, scope, operation.operation_id, lock=True
            )
            stored = await PostgresDataRuntimeRepositoryMixin._operation_record(
                session, stored_model
            )
            self._require_same_product_family_request(stored, operation)
            if stored.status is not CriticalOperationStatus.PREPARED:
                self._require_same_product_family_terminal(stored, operation)
                return stored
            PostgresDataRuntimeRepositoryMixin.require_authoring_claim(stored_model, execution_claim)
            self._apply_product_family_authoring_terminal(stored_model, operation)
            stored_model.authoring_execution_owner = None
            stored_model.authoring_execution_token_sha256 = None
            await session.flush()
            return operation

    async def reopen_product_family_authoring_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> CriticalOperationRecord:
        self._require_prepared_product_family_operation(operation)
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope)
            stored_model = await self._require_product_family_operation_model(
                session, scope, operation.operation_id, lock=True
            )
            stored = await PostgresDataRuntimeRepositoryMixin._operation_record(
                session, stored_model
            )
            self._require_same_product_family_request(stored, operation)
            if stored.status is CriticalOperationStatus.PREPARED:
                return stored
            if stored.status is not CriticalOperationStatus.INDETERMINATE:
                raise ConflictError("only an INDETERMINATE ProductFamily Operation can reopen")
            PostgresDataRuntimeRepositoryMixin.require_authoring_claim(stored_model, execution_claim)
            stored_model.status = CriticalOperationStatus.PREPARED.value
            stored_model.error_code = None
            stored_model.error_message = None
            stored_model.error_phase = None
            stored_model.finished_at = None
            await session.flush()
            return operation

    async def commit_product_family_authoring_success(
        self,
        scope: InvocationScope,
        publication: ProductFamilyAuthoringPublication,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> ProductFamilyAuthoringCommitResult:
        operation = publication.operation
        try:
            async with self._sessions() as session, session.begin():
                await set_tenant_execution_context(session, scope.identity)
                await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope)
                PostgresDataRuntimeRepositoryMixin._require_record_scope(scope, operation)
                await self._require_system_copy_authority(session, scope, operation)
                stored_model = await self._require_product_family_operation_model(
                    session, scope, operation.operation_id, lock=True
                )
                stored = await PostgresDataRuntimeRepositoryMixin._operation_record(
                    session, stored_model
                )
                self._require_same_product_family_request(stored, operation)
                if stored.status is CriticalOperationStatus.SUCCEEDED:
                    return await self._require_product_family_authoring_success_replay(
                        session, scope, stored_model, publication
                    )
                if stored.status is not CriticalOperationStatus.PREPARED:
                    raise ConflictError("ProductFamily Operation is already terminal")
                PostgresDataRuntimeRepositoryMixin.require_authoring_claim(stored_model, execution_claim)
                await PostgresDataRuntimeRepositoryMixin._require_operation_artifacts(
                    session, operation
                )
                await self._require_product_family_source_artifact(session, operation)
                await self._require_product_family_repair_parent(session, operation)
                await self._require_product_family_base(session, operation)
                await self._require_system_copy_dependencies(session, operation)
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:family_id, 0))"),
                    {"family_id": publication.family.family_id},
                )
                family = await session.scalar(
                    select(ProductFamilyModel).where(
                        ProductFamilyModel.family_id == publication.family.family_id
                    )
                )
                if family is None:
                    if operation.base_product_family_version_id is not None:
                        raise ConflictError("ProductFamily revision root is missing")
                    version_no = 1
                    session.add(self._new_family_model(publication.family))
                    await session.flush()
                else:
                    if operation.base_product_family_version_id is None:
                        raise ConflictError("ProductFamily create target already exists")
                    self._require_same_family_root(family, publication.family)
                    await self._require_product_family_base(session, operation)
                    maximum = await session.scalar(
                        select(func.max(ProductFamilyVersionModel.version_no)).where(
                            ProductFamilyVersionModel.family_id == publication.family.family_id
                        )
                    )
                    version_no = int(maximum or 0) + 1
                bundle = self._publication_bundle(publication, version_no=version_no)
                committed = await self._insert_product_family_bundle(session, bundle)
                self._apply_product_family_authoring_terminal(stored_model, operation)
                entry = await append_catalog_entry(
                    session,
                    owner_id=operation.owner_id,
                    tenant_id=operation.tenant_id,
                    task_id=operation.task_id,
                    run_id=operation.run_id,
                    kind=CatalogResourceKind.OPERATION,
                    catalog_revision=await next_catalog_revision(session, operation.run_id),
                    operation_id=operation.operation_id,
                )
                await session.flush()
                return ProductFamilyAuthoringCommitResult(
                    operation=PublishedCriticalOperation(
                        operation=operation,
                        catalog_entry=PostgresDataRuntimeRepositoryMixin._catalog_record(entry),
                    ),
                    family_version=committed,
                )
        except IntegrityError as exc:
            try:
                reconciled = await self._reconcile_product_family_authoring_success(
                    scope, publication
                )
            except ConflictError as reconcile_error:
                raise ProductFamilyAuthoringCommitFenceError(
                    str(reconcile_error)
                ) from reconcile_error
            if reconciled is not None:
                return reconciled
            raise ProductFamilyAuthoringCommitFenceError(
                "ProductFamily success failed its database integrity fence"
            ) from exc
        except ConflictError as exc:
            raise ProductFamilyAuthoringCommitFenceError(str(exc)) from exc
        except SQLAlchemyError as exc:
            raise ProductFamilyAuthoringCommitOutcomeUnknownError(
                "ProductFamily database commit outcome is unknown"
            ) from exc

    @classmethod
    async def _insert_product_family_bundle(cls, session, bundle):
        """Insert a complete Family in the caller's dependency publication transaction."""
        session.add(cls._new_version_model(bundle.version))
        await session.flush()
        session.add_all(cls._new_binding_model(binding) for binding in bundle.geometry_bindings)
        session.add_all(cls._new_asset_model(asset) for asset in bundle.assets)
        await session.flush()
        committed = await PostgresProductFamilyRepositoryMixin._load_product_family_bundle(
            session, family_id=bundle.family.family_id, family_version_id=bundle.version.family_version_id)
        if committed is None:
            raise RuntimeError("committed ProductFamily version is unreadable")
        cls._require_same_publication(committed, bundle)
        return committed

    async def get_product_family_authoring_operation_state(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> CriticalOperationRecord | None:
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await PostgresDataRuntimeRepositoryMixin._require_data_run(session, scope, lock=False)
            model = await self._find_product_family_operation_model(session, scope, operation_id)
            return (
                None
                if model is None
                else await PostgresDataRuntimeRepositoryMixin._operation_record(session, model)
            )

    async def get_product_family_authoring_operation(
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
                    == CriticalOperationKind.PRODUCT_FAMILY_WRITE.value,
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

    @staticmethod
    def _require_prepared_product_family_operation(operation: CriticalOperationRecord) -> None:
        if (
            operation.operation_kind is not CriticalOperationKind.PRODUCT_FAMILY_WRITE
            or operation.status is not CriticalOperationStatus.PREPARED
        ):
            raise ValueError("ProductFamily preparation requires a PREPARED Operation")
        draft = operation.detail.version_draft
        if draft.family_contract.standard_model is None:
            raise ValueError("New ProductFamily drafts require a formal standard model capability")

    @staticmethod
    async def _find_product_family_replay(session, operation):
        replay = await session.scalar(
            select(CriticalOperationModel).where(
                _current_operation_contract_predicate(),
                CriticalOperationModel.run_id == operation.run_id,
                CriticalOperationModel.idempotency_key == operation.idempotency_key,
            )
        )
        if replay is not None:
            if replay.request_fingerprint != operation.request_fingerprint:
                raise IdempotencyConflictError(
                    "ProductFamily idempotency key was reused with different input"
                )
            return replay
        return await session.scalar(
            select(CriticalOperationModel).where(
                _current_operation_contract_predicate(),
                CriticalOperationModel.run_id == operation.run_id,
                CriticalOperationModel.tool_name == operation.tool_name,
                CriticalOperationModel.request_fingerprint == operation.request_fingerprint,
            )
        )

    async def _find_product_family_operation_model(self, session, scope, operation_id):
        return await session.scalar(
            select(CriticalOperationModel).where(
                _current_operation_contract_predicate(),
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.run_id == scope.run_id,
                CriticalOperationModel.operation_id == operation_id,
                CriticalOperationModel.operation_kind
                == CriticalOperationKind.PRODUCT_FAMILY_WRITE.value,
            )
        )

    async def _require_product_family_operation_model(self, session, scope, operation_id, *, lock):
        statement = select(CriticalOperationModel).where(
            _current_operation_contract_predicate(),
            CriticalOperationModel.owner_id == scope.owner_id,
            CriticalOperationModel.tenant_id == scope.identity.tenant_id,
            CriticalOperationModel.task_id == scope.identity.task_id,
            CriticalOperationModel.run_id == scope.run_id,
            CriticalOperationModel.operation_id == operation_id,
            CriticalOperationModel.operation_kind
            == CriticalOperationKind.PRODUCT_FAMILY_WRITE.value,
        )
        if lock:
            statement = statement.with_for_update()
        model = await session.scalar(statement)
        if model is None:
            raise ConflictError("ProductFamily Operation is not visible")
        return model

    @staticmethod
    async def _require_product_family_repair_parent(session, operation) -> None:
        parent_id = operation.repair_parent_operation_id
        if parent_id is None:
            return
        repairable_phases = (
            CriticalOperationErrorPhase.CODE_VALIDATION.value,
            CriticalOperationErrorPhase.EXECUTION.value,
            CriticalOperationErrorPhase.OUTPUT_VALIDATION.value,
        )
        parent = await session.scalar(
            select(CriticalOperationModel).where(
                _current_operation_contract_predicate(),
                CriticalOperationModel.owner_id == operation.owner_id,
                CriticalOperationModel.tenant_id == operation.tenant_id,
                CriticalOperationModel.task_id == operation.task_id,
                CriticalOperationModel.run_id == operation.run_id,
                CriticalOperationModel.operation_id == parent_id,
                CriticalOperationModel.operation_kind
                == CriticalOperationKind.PRODUCT_FAMILY_WRITE.value,
                CriticalOperationModel.status == CriticalOperationStatus.FAILED.value,
                CriticalOperationModel.error_phase.in_(repairable_phases),
                CriticalOperationModel.target_product_family_id
                == operation.target_product_family_id,
                CriticalOperationModel.base_product_family_version_id
                == operation.base_product_family_version_id,
            )
        )
        if parent is None:
            raise ConflictError("ProductFamily repair parent is not an exact failed chain parent")
        child_detail = operation.detail.model_dump(mode="json")
        child_draft = child_detail.get("version_draft")
        parent_detail = parent.operation_detail_json
        parent_draft = (
            parent_detail.get("version_draft") if isinstance(parent_detail, dict) else None
        )
        try:
            matching_chain = (
                isinstance(parent_draft, dict) and isinstance(child_draft, dict)
                and _repair_draft_chain(parent_draft) == _repair_draft_chain(child_draft)
            )
        except (KeyError, ValueError):
            matching_chain = False
        if (
            parent.error_phase not in repairable_phases
            or not isinstance(child_draft, dict)
            or not isinstance(parent_draft, dict)
            or not matching_chain
        ):
            raise ConflictError("ProductFamily repair parent is not an exact failed chain parent")

    @staticmethod
    async def _require_product_family_base(session, operation) -> None:
        base_id = operation.base_product_family_version_id
        if base_id is None:
            return
        exact = await session.scalar(
            select(ProductFamilyVersionModel.family_version_id).where(
                ProductFamilyVersionModel.family_id == operation.target_product_family_id,
                ProductFamilyVersionModel.family_version_id == base_id,
            )
        )
        if exact is None:
            raise ConflictError("ProductFamily exact base version is not visible")

    @staticmethod
    async def _require_product_family_source_artifact(session, operation) -> None:
        reference = operation.code_artifact
        if reference is None:
            raise ConflictError("ProductFamily authoring requires an exact source ArtifactVersion")
        exact = await session.scalar(
            select(ArtifactVersionModel.version_id)
            .join(ArtifactModel, ArtifactModel.artifact_id == ArtifactVersionModel.artifact_id)
            .where(
                ArtifactVersionModel.owner_id == operation.owner_id,
                ArtifactVersionModel.tenant_id == operation.tenant_id,
                ArtifactVersionModel.task_id == operation.task_id,
                ArtifactVersionModel.artifact_id == reference.artifact_id,
                ArtifactVersionModel.version_id == reference.version_id,
                ArtifactVersionModel.sha256 == reference.sha256,
                ArtifactModel.kind == "product_family_authoring_source",
                ArtifactModel.status.in_(("ready", "available")),
            )
        )
        if exact is None:
            raise ConflictError("ProductFamily exact source ArtifactVersion is not available")

    @staticmethod
    def _product_family_request(record: CriticalOperationRecord) -> dict[str, object]:
        return record.model_dump(
            mode="json",
            exclude={
                "status",
                "result_product_family_version_id",
                "error_code",
                "error_message",
                "error_phase",
                "finished_at",
            },
        )

    @classmethod
    def _require_same_product_family_request(cls, stored, incoming) -> None:
        if cls._product_family_request(stored) != cls._product_family_request(incoming):
            raise IdempotencyConflictError("ProductFamily Operation identity changed")

    @staticmethod
    def _require_same_product_family_terminal(stored, incoming) -> None:
        if stored != incoming:
            raise IdempotencyConflictError("ProductFamily terminal replay changed")

    @staticmethod
    def _apply_product_family_authoring_terminal(model, operation) -> None:
        model.status = operation.status.value
        model.result_product_family_version_id = operation.result_product_family_version_id
        model.error_code = operation.error_code
        model.error_message = operation.error_message
        model.error_phase = None if operation.error_phase is None else operation.error_phase.value
        model.finished_at = operation.finished_at
        model.authoring_execution_owner = None
        model.authoring_execution_token_sha256 = None

    @staticmethod
    def _new_family_model(record):
        return ProductFamilyModel(
            family_id=record.family_id,
            owner_scope=record.owner_scope.scope_kind.name,
            owner_tenant_id=record.owner_scope.tenant_id,
            owner_task_id=record.owner_scope.task_id,
            created_at=record.created_at,
        )

    @staticmethod
    def _new_version_model(record):
        return ProductFamilyVersionModel(
            family_version_id=record.family_version_id,
            family_id=record.family_id,
            version_no=record.version_no,
            parent_family_version_id=record.parent_family_version_id,
            display_name=record.display_name,
            purpose=record.purpose,
            usage_instructions=record.usage_instructions,
            family_contract_json=record.family_contract.model_dump(mode="json", by_alias=True),
            contract_digest=record.contract_digest,
            content_digest=record.content_digest,
            program_entry=record.program_entry,
            created_at=record.created_at,
        )

    @staticmethod
    def _new_binding_model(record):
        return ProductFamilyGeometryBindingModel(
            family_version_id=record.family_version_id,
            binding_key=record.binding_key,
            geometry_resource_id=record.geometry_resource_id,
            geometry_resource_version_id=record.geometry_resource_version_id,
            geometry_content_digest=record.geometry_content_digest,
            expected_resource_kind=record.expected_resource_kind.value,
            geometry_purpose=record.geometry_purpose.value,
        )

    @staticmethod
    def _new_asset_model(record):
        return ProductFamilyVersionAssetModel(
            family_version_id=record.family_version_id,
            asset_role=record.asset_role.value,
            asset_format=record.asset_format,
            filename=record.filename,
            content_type=record.content_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            created_at=record.created_at,
        )

    @staticmethod
    def _publication_bundle(publication, *, version_no: int) -> ProductFamilyVersionBundle:
        return publication.to_bundle(version_no=version_no)

    @staticmethod
    def _require_same_family_root(model, record) -> None:
        if (
            model.owner_scope != record.owner_scope.scope_kind.name
            or model.owner_tenant_id != record.owner_scope.tenant_id
            or model.owner_task_id != record.owner_scope.task_id
        ):
            raise ConflictError("ProductFamily root identity changed")

    @staticmethod
    def _require_same_publication(stored, expected) -> None:
        if stored != expected:
            raise IdempotencyConflictError("ProductFamily publication changed")

    async def _require_product_family_authoring_success_replay(
        self, session, scope, model, publication
    ):
        committed = await PostgresProductFamilyRepositoryMixin._load_product_family_bundle(
            session,
            family_id=publication.family.family_id,
            family_version_id=publication.version.family_version_id,
        )
        if committed is None:
            raise ConflictError("successful ProductFamily result is missing")
        expected = self._publication_bundle(
            publication,
            version_no=committed.version.version_no,
        )
        self._require_same_publication(committed, expected)
        published = await PostgresDataRuntimeRepositoryMixin._published_operation(
            self, session, model, run_id=scope.run_id
        )
        if published is None:
            raise ConflictError("successful ProductFamily Operation has no Catalog alias")
        return ProductFamilyAuthoringCommitResult(
            operation=published,
            family_version=committed,
        )

    async def _reconcile_product_family_authoring_preparation(self, scope, operation):
        try:
            current = await self.get_product_family_authoring_operation_state(
                scope, operation_id=operation.operation_id
            )
        except SQLAlchemyError:
            return None
        if current is None:
            return None
        self._require_same_product_family_request(current, operation)
        return current

    async def _reconcile_product_family_authoring_success(self, scope, publication):
        try:
            async with self._sessions() as session:
                await set_tenant_execution_context(session, scope.identity)
                model = await self._find_product_family_operation_model(
                    session, scope, publication.operation.operation_id
                )
                if model is None or model.status != CriticalOperationStatus.SUCCEEDED.value:
                    return None
                return await self._require_product_family_authoring_success_replay(
                    session, scope, model, publication
                )
        except SQLAlchemyError:
            return None


__all__ = ["PostgresProductFamilyAuthoringRepositoryMixin"]
