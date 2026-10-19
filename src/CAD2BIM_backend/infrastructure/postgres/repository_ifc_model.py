"""PostgreSQL persistence for the Run-bound durable IFC model kernel."""

from __future__ import annotations

import re
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from datetime import timedelta

import os
import psutil

from sqlalchemy import and_, select, update
from pydantic import ValidationError

from CAD2BIM_backend.domain.data_runtime import (
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationInitiator,
    CriticalOperationKind,
    CriticalOperationStatus,
    IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY,
    IFC_MODEL_INITIALIZE_INTENT_SUMMARY,
    IFC_PROJECT_UPDATE_INTENT_SUMMARY,
    IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
    IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
    IFC_PRODUCTS_APPLY_INTENT_SUMMARY,
    IFC_PRODUCTS_EDIT_INTENT_SUMMARY,
    IfcGeometryBasisConfigureOperationDetail,
    IfcModelInitializeOperationDetail,
    IfcProjectUpdateOperationDetail,
    IfcTrustedBaselineLoadOperationDetail,
    IfcSpatialStructureApplyOperationDetail,
    IfcProductsApplyOperationDetail,
    IfcProductsEditOperationDetail,
    ProductReportPublication, ProductEditReportPublication, ProductApplyReportPublication, product_report_committed,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, stable_internal_identity_id
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, RunStatus, TERMINAL_RUN_STATUSES
from CAD2BIM_backend.infrastructure.postgres.repository_artifact import register_artifact_in_transaction
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION
from CAD2BIM_backend.domain.ifc_project import IfcProjectSummary
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    IfcSpatialAppliedItem,
    IfcSpatialBlockerSummary,
    IfcSpatialNodeSummary,
    IfcSpatialStructureApplyResult,
)
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcArtifactVersion,
    ExactIfcRevision,
    IfcExecutionClaim,
    IFC_TARGET_SCHEMA,
    IfcInvocationIdentityConflictError,
    IfcModelAlreadyInitializedError,
    IfcModelConflictError,
    IfcModelFrozenError,
    IfcModelInfo,
    IfcModelInitializeResult,
    IfcModelIntegrityError,
    IfcModelNotInitializedError,
    IfcModelState,
    IfcRunIdentity,
    IfcRevisionInspection,
    PreparedIfcInitialize,
    PreparedIfcRevision,
    VerifiedIfcPublication,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    CriticalOperationModel,
    DataCatalogEntryModel,
    IfcModelModel,
    IfcModelRevisionModel,
    RunModel,
)
from CAD2BIM_backend.infrastructure.local_process_identity import (
    current_local_executor_owner, local_executor_stopped,
    local_guard_process_group_stopped,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import (
    PostgresRepositoryBase,
    artifact_storage_key,
    as_utc,
    database_now,
    fingerprint,
)
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    append_catalog_entry,
    next_catalog_revision,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantContextMismatch,
    current_tenant_execution_context,
)
from CAD2BIM_backend.ports.ifc_model import (
    CommittedIfcRevision,
    IfcInitializeOperationState,
    IfcRevisionOperationState,
    IfcSelectedCandidate,
)


_INITIALIZE_TOOL = "ifc_model_initialize"
_GEOMETRY_BASIS_TOOL = "ifc_model_configure_geometry_basis"
_PROJECT_UPDATE_TOOL = "ifc_project_update"
_SPATIAL_STRUCTURE_APPLY_TOOL = "ifc_spatial_structure_apply"
_PRODUCTS_APPLY_TOOL = "ifc_products_apply"
_PRODUCTS_EDIT_TOOL = "ifc_products_edit"
_PRODUCT_WRITE_TOOLS = {_PRODUCTS_APPLY_TOOL, _PRODUCTS_EDIT_TOOL}
_TRUSTED_BASELINE_TOOL = "ifc_trusted_baseline_load"
_REVISION_TOOL_INTENTS = {
    _TRUSTED_BASELINE_TOOL: IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
    _GEOMETRY_BASIS_TOOL: IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY,
    _PROJECT_UPDATE_TOOL: IFC_PROJECT_UPDATE_INTENT_SUMMARY,
    _SPATIAL_STRUCTURE_APPLY_TOOL: IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
    _PRODUCTS_APPLY_TOOL: IFC_PRODUCTS_APPLY_INTENT_SUMMARY,
    _PRODUCTS_EDIT_TOOL: IFC_PRODUCTS_EDIT_INTENT_SUMMARY,
}
_INITIALIZE_IDEMPOTENCY_PREFIX = "ifc-model-initialize:"
_IFC_REVISION_KIND = "ifc_revision"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
_IFC_FAILURE_PHASES = frozenset(
    {
        CriticalOperationErrorPhase.IFC_VALIDATION,
        CriticalOperationErrorPhase.STORAGE_VERIFICATION,
        CriticalOperationErrorPhase.COMMIT_FENCE,
    }
)


class PostgresIfcModelRepositoryMixin(PostgresRepositoryBase):
    """Atomic model/revision, Artifact, Operation, and CURRENT persistence."""

    async def claim_execution(
        self, scope: IfcRunIdentity, *, operation_id: str, request_digest: str,
    ) -> IfcExecutionClaim:
        """Claim the original O only after old local execution is proven stopped."""
        self._require_scope_context(scope)
        self._validate_digest(request_digest)
        async with self._sessions() as session, session.begin():
            run = await self._require_ifc_run(session, scope, lock=True)
            self._require_run_accepting_ifc_write(run)
            operation = await self._require_operation(session, scope, operation_id, lock=True)
            if (operation.status != CriticalOperationStatus.PREPARED.value
                    or operation.request_fingerprint != request_digest):
                raise IfcModelConflictError("IFC execution requires the original prepared request")
            now = await database_now(session)
            if operation.ifc_deadline_at is None or operation.ifc_deadline_at <= now:
                raise IfcModelConflictError("original IFC execution deadline has expired")
            if operation.ifc_execution_owner is not None:
                if not local_executor_stopped(operation.ifc_execution_owner, kind="ifc"):
                    raise IfcModelConflictError("original IFC executor is active or unknown")
                if operation.ifc_guard_pid is not None and not local_guard_process_group_stopped(
                    operation.ifc_guard_pid, operation.ifc_guard_birth_us or 0,
                ):
                    raise IfcModelConflictError("original IFC child is active or unknown")
            token = secrets.token_urlsafe(32)
            owner = current_local_executor_owner("ifc")
            operation.ifc_execution_owner = owner
            operation.ifc_execution_generation += 1
            operation.ifc_execution_token_sha256 = hashlib.sha256(token.encode()).hexdigest()
            operation.ifc_guard_pid = None
            operation.ifc_guard_birth_us = None
            await session.flush()
            return IfcExecutionClaim(
                operation_id=operation_id, request_digest=request_digest,
                owner=owner, generation=operation.ifc_execution_generation,
                token=token, deadline_at=operation.ifc_deadline_at,
                remaining_seconds=(operation.ifc_deadline_at - now).total_seconds(),
            )

    async def associate_execution_guard(
        self, scope: IfcRunIdentity, claim: IfcExecutionClaim,
        *, guardian_pid: int, guardian_birth_us: int,
    ) -> None:
        """Commit guard identity before the supervisor releases its start gate."""
        self._require_scope_context(scope)
        if guardian_pid <= 0 or guardian_birth_us <= 0:
            raise ValueError("guardian identity must be positive")
        try:
            guard = psutil.Process(guardian_pid)
            actual_birth_us = round(guard.create_time() * 1_000_000)
            actual_group = os.getpgid(guardian_pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
            raise IfcModelConflictError("IFC guardian identity is unavailable") from exc
        if actual_birth_us != guardian_birth_us or actual_group != guardian_pid:
            raise IfcModelConflictError("IFC guardian identity changed")
        async with self._sessions() as session, session.begin():
            run = await self._require_ifc_run(session, scope, lock=True)
            self._require_run_accepting_ifc_write(run)
            operation = await self._require_operation(
                session, scope, claim.operation_id, lock=True,
            )
            await self._assert_ifc_claim(session, operation, claim, for_commit=True)
            if operation.ifc_guard_pid is not None and not local_guard_process_group_stopped(
                operation.ifc_guard_pid, operation.ifc_guard_birth_us or 0,
            ):
                raise IfcModelConflictError("previous IFC guardian is still active")
            operation.ifc_guard_pid = guardian_pid
            operation.ifc_guard_birth_us = guardian_birth_us
            await session.flush()

    async def retire_execution(
        self, scope: IfcRunIdentity, claim: IfcExecutionClaim,
    ) -> None:
        """Release a concluded host task after its last child group has stopped."""
        self._require_scope_context(scope)
        async with self._sessions() as session, session.begin():
            await self._require_ifc_run(session, scope, lock=True)
            operation = await self._require_operation(
                session, scope, claim.operation_id, lock=True,
            )
            await self._assert_ifc_claim(session, operation, claim, for_commit=False)
            if operation.ifc_guard_pid is not None and not local_guard_process_group_stopped(
                operation.ifc_guard_pid, operation.ifc_guard_birth_us or 0,
            ):
                raise IfcModelConflictError("IFC child has not stopped")
            operation.ifc_execution_owner = None
            operation.ifc_execution_token_sha256 = None
            operation.ifc_guard_pid = None
            operation.ifc_guard_birth_us = None
            await session.flush()

    async def get_selected_candidate(
        self, scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
        claim: IfcExecutionClaim,
    ) -> IfcSelectedCandidate | None:
        """Read the original O's selected bytes under the current execution fence."""
        self._require_scope_context(scope)
        self._assert_candidate_prepared_identity(scope, prepared)
        async with self._sessions() as session, session.begin():
            run = await self._require_ifc_run(session, scope, lock=True)
            self._require_run_accepting_ifc_write(run)
            operation = await self._require_operation(
                session, scope, prepared.operation_id, lock=True,
            )
            self._require_candidate_operation(operation, prepared)
            await self._assert_ifc_claim(session, operation, claim, for_commit=True)
            if operation.ifc_candidate_json is None:
                return None
            candidate = self._selected_candidate(operation)
            self._assert_candidate_matches_prepared(operation, prepared, candidate)
            return candidate

    async def select_candidate(
        self, scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
        claim: IfcExecutionClaim,
        candidate: IfcSelectedCandidate,
    ) -> IfcSelectedCandidate:
        """Select complete staged bytes once; later generations cannot replace them."""
        self._require_scope_context(scope)
        self._assert_candidate_prepared_identity(scope, prepared)
        async with self._sessions() as session, session.begin():
            run = await self._require_ifc_run(session, scope, lock=True)
            self._require_run_accepting_ifc_write(run)
            operation = await self._require_operation(
                session, scope, prepared.operation_id, lock=True,
            )
            self._require_candidate_operation(operation, prepared)
            await self._assert_ifc_claim(session, operation, claim, for_commit=True)
            self._assert_candidate_matches_prepared(operation, prepared, candidate)
            if operation.ifc_candidate_json is not None:
                existing = self._selected_candidate(operation)
                if existing != candidate:
                    raise IfcModelConflictError("selected IFC candidate cannot change")
                return existing
            if candidate.origin_generation != claim.generation:
                raise IfcModelConflictError("IFC candidate came from another execution generation")
            if isinstance(prepared, PreparedIfcInitialize):
                existing_model = await session.scalar(
                    select(IfcModelModel.ifc_model_id).where(
                        IfcModelModel.run_id == scope.run_id,
                    )
                )
                if existing_model is not None:
                    raise IfcModelConflictError("the selected Run already has an IfcModel")
            else:
                model = await session.scalar(
                    select(IfcModelModel).where(
                        IfcModelModel.owner_id == scope.owner_id,
                        IfcModelModel.task_id == scope.task_id,
                        IfcModelModel.run_id == scope.run_id,
                        IfcModelModel.ifc_model_id == prepared.before.model_id,
                    ).with_for_update()
                )
                if (model is None or model.status != IfcModelState.ACTIVE.value
                        or model.current_revision_id != prepared.before.revision_id
                        or model.write_epoch != prepared.before.write_epoch):
                    raise IfcModelConflictError("IFC candidate base or CURRENT changed")
            operation.ifc_candidate_json = candidate.model_dump(mode="json")
            await session.flush()
            return candidate

    @classmethod
    def _assert_candidate_prepared_identity(
        cls, scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
    ) -> None:
        if isinstance(prepared, PreparedIfcInitialize):
            cls._assert_prepared_initialize_identity(scope, prepared)
        else:
            cls._assert_prepared_revision_identity(scope, prepared)

    @staticmethod
    def _require_candidate_operation(
        operation: CriticalOperationModel,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
    ) -> None:
        if (operation.status != CriticalOperationStatus.PREPARED.value
                or operation.request_fingerprint != prepared.request_digest
                or operation.idempotency_key != prepared.idempotency_key):
            raise IfcModelConflictError("IFC candidate requires the original prepared request")

    @staticmethod
    def _selected_candidate(operation: CriticalOperationModel) -> IfcSelectedCandidate:
        try:
            return IfcSelectedCandidate.model_validate(operation.ifc_candidate_json)
        except ValidationError as exc:
            raise IfcModelIntegrityError("selected IFC candidate is invalid") from exc

    @staticmethod
    def _assert_candidate_matches_prepared(
        operation: CriticalOperationModel,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
        candidate: IfcSelectedCandidate,
    ) -> None:
        expected_project = (
            prepared.project_global_id if isinstance(prepared, PreparedIfcInitialize)
            else prepared.before.project_global_id
        )
        if candidate.inspection.project_global_id != expected_project:
            raise IfcModelConflictError("selected IFC candidate changed project identity")
        if operation.tool_name == _TRUSTED_BASELINE_TOOL:
            detail = IfcTrustedBaselineLoadOperationDetail.model_validate(operation.operation_detail_json)
            if candidate.sha256 != detail.source_asset.sha256:
                raise IfcModelConflictError("IFC candidate differs from the registered trusted source")
        if isinstance(prepared, PreparedIfcInitialize):
            if (operation.tool_name != _INITIALIZE_TOOL
                    or candidate.project_result is not None
                    or candidate.spatial_applied is not None
                    or candidate.product_report is not None):
                raise IfcModelConflictError("initialization candidate has unrelated results")
        elif operation.tool_name == _PROJECT_UPDATE_TOOL:
            result = candidate.project_result
            if (result is None or result.global_id != expected_project
                    or result.name != candidate.inspection.project_name
                    or candidate.spatial_applied is not None):
                raise IfcModelConflictError("Project candidate lost its verified result")
        elif operation.tool_name == _SPATIAL_STRUCTURE_APPLY_TOOL:
            if (candidate.spatial_applied is None or candidate.spatial_summary is None
                    or candidate.project_result is not None):
                raise IfcModelConflictError("spatial candidate lost its result")
        elif operation.tool_name in _PRODUCT_WRITE_TOOLS:
            report = candidate.product_report
            failure_report = candidate.product_failure_report
            if (report is None or failure_report is None
                    or not product_report_committed(report)
                    or product_report_committed(failure_report)
                    or report.package.request_fingerprint != prepared.request_digest
                    or failure_report.package.request_fingerprint != prepared.request_digest
                    or candidate.project_result is not None or candidate.spatial_applied is not None):
                raise IfcModelConflictError("product candidate lost its joint report result")
            expected_type = ProductEditReportPublication if operation.tool_name == _PRODUCTS_EDIT_TOOL else ProductApplyReportPublication
            if not isinstance(report, expected_type) or not isinstance(failure_report, expected_type):
                raise IfcModelConflictError("product report belongs to another write tool")
        elif (candidate.project_result is not None or candidate.spatial_applied is not None):
            raise IfcModelConflictError("IFC candidate has an unrelated result")
        if operation.tool_name not in _PRODUCT_WRITE_TOOLS and (
                candidate.product_report is not None or candidate.product_failure_report is not None):
            raise IfcModelConflictError("unrelated IFC write supplied a product report")

    @classmethod
    def _assert_selected_publication(
        cls,
        operation: CriticalOperationModel,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
        publication: VerifiedIfcPublication,
        *,
        project_result: IfcProjectSummary | None = None,
        spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None = None,
        spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None = None,
    ) -> None:
        if operation.ifc_candidate_json is None:
            raise IfcModelConflictError("IFC publication requires the selected candidate")
        candidate = cls._selected_candidate(operation)
        cls._assert_candidate_matches_prepared(operation, prepared, candidate)
        if (not publication.inspection.matches_recorded_facts(candidate.inspection)
                or publication.artifact.size_bytes != candidate.size_bytes
                or publication.artifact.sha256 != candidate.sha256
                or project_result != candidate.project_result
                or spatial_applied != candidate.spatial_applied
                or spatial_summary != candidate.spatial_summary):
            raise IfcModelConflictError("IFC publication differs from the selected candidate")

    @staticmethod
    def _require_run_accepting_ifc_write(run: RunModel) -> None:
        if run.cancel_requested or RunStatus(run.status) in TERMINAL_RUN_STATUSES:
            raise IfcModelConflictError("Run no longer accepts IFC execution or commit")

    @staticmethod
    async def _assert_ifc_claim(
        session, operation: CriticalOperationModel, claim: IfcExecutionClaim,
        *, for_commit: bool,
    ) -> None:
        if (operation.operation_id != claim.operation_id
                or operation.request_fingerprint != claim.request_digest
                or operation.ifc_execution_owner != claim.owner
                or operation.ifc_execution_generation != claim.generation
                or operation.ifc_execution_token_sha256 is None
                or not hmac.compare_digest(
                    operation.ifc_execution_token_sha256,
                    hashlib.sha256(claim.token.encode()).hexdigest(),
                )):
            raise IfcModelConflictError("IFC execution claim is stale")
        if for_commit:
            now = await database_now(session)
            if operation.ifc_deadline_at is None or operation.ifc_deadline_at <= now:
                raise IfcModelConflictError("original IFC execution deadline has expired")

    async def prepare_initialize(
        self,
        scope: IfcRunIdentity,
        *,
        request_digest: str,
        project_global_id: str,
        project_name: str,
    ) -> PreparedIfcInitialize:
        self._require_scope_context(scope)
        self._validate_digest(request_digest)
        identities = self._initialize_identities(scope)
        async with self._sessions() as session, session.begin():
            run = await self._require_ifc_run(session, scope, lock=True)
            self._require_run_accepting_ifc_write(run)
            operation = await session.scalar(
                select(CriticalOperationModel).where(
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.idempotency_key == identities["idempotency_key"],
                )
            )
            if operation is not None:
                self._assert_initialize_replay(
                    operation,
                    request_digest=request_digest,
                    project_global_id=project_global_id,
                    project_name=project_name,
                )
                if operation.status == CriticalOperationStatus.INDETERMINATE.value:
                    raise IfcModelFrozenError(
                        "IFC invocation is paused pending server reconciliation"
                    )
                if operation.status == CriticalOperationStatus.FAILED.value:
                    raise IfcModelConflictError("IFC initialization Operation is already terminal")
                return self._prepared_initialize(
                    scope,
                    operation=operation,
                    identities=identities,
                    project_global_id=project_global_id,
                )

            existing_model = await session.scalar(
                select(IfcModelModel).where(IfcModelModel.run_id == scope.run_id)
            )
            if existing_model is not None:
                if existing_model.status == IfcModelState.FROZEN.value:
                    raise IfcModelFrozenError(
                        "IFC invocation is paused pending server reconciliation"
                    )
                raise IfcModelAlreadyInitializedError("the selected Run already has an IfcModel")

            now = await database_now(session)
            operation = CriticalOperationModel(
                operation_id=identities["operation_id"],
                owner_id=scope.owner_id,
                tenant_id=scope.tenant_id,
                task_id=scope.task_id,
                run_id=scope.run_id,
                operation_kind=CriticalOperationKind.IFC_MODEL_WRITE.value,
                initiator=self._operation_initiator(),
                tool_name=_INITIALIZE_TOOL,
                intent_summary=IFC_MODEL_INITIALIZE_INTENT_SUMMARY,
                source_filename=None,
                idempotency_key=identities["idempotency_key"],
                request_fingerprint=request_digest,
                status=CriticalOperationStatus.PREPARED.value,
                result_package_id=None,
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
                operation_detail_json=IfcModelInitializeOperationDetail(
                    project_name=project_name,
                    project_global_id=project_global_id,
                    result=None,
                ).model_dump(mode="json"),
                ifc_deadline_at=now + timedelta(
                    seconds=IFC_REVISION_INVOCATION_EXECUTION.total_seconds,
                ),
                started_at=now,
                finished_at=None,
                created_at=now,
            )
            session.add(operation)
            await session.flush()
            return self._prepared_initialize(
                scope,
                operation=operation,
                identities=identities,
                project_global_id=project_global_id,
            )

    async def commit_initialize(
        self,
        scope: IfcRunIdentity,
        *,
        prepared: PreparedIfcInitialize,
        publication: VerifiedIfcPublication,
        claim: IfcExecutionClaim,
    ) -> IfcModelInitializeResult:
        self._require_scope_context(scope)
        self._assert_prepared_initialize_identity(scope, prepared)
        self._validate_initialize_publication(scope, prepared, publication)
        conflict: str | None = None
        result: IfcModelInitializeResult | None = None
        async with self._sessions() as session, session.begin():
            run = await self._require_ifc_run(session, scope, lock=True)
            operation = await self._require_operation(
                session,
                scope,
                prepared.operation_id,
                lock=True,
            )
            if operation.status == CriticalOperationStatus.PREPARED.value:
                self._require_run_accepting_ifc_write(run)
            await self._assert_ifc_claim(
                session, operation, claim,
                for_commit=operation.status == CriticalOperationStatus.PREPARED.value,
            )
            self._assert_initialize_replay(
                operation,
                request_digest=prepared.request_digest,
                project_global_id=prepared.project_global_id,
                project_name=publication.inspection.project_name,
            )
            self._assert_selected_publication(operation, prepared, publication)
            if operation.status == CriticalOperationStatus.SUCCEEDED.value:
                committed = await self._require_committed_publication(
                    session,
                    scope,
                    operation_id=operation.operation_id,
                    publication=publication,
                )
                if (
                    committed.model_id != prepared.model_id
                    or committed.revision_id != prepared.revision_id
                    or committed.project_global_id != prepared.project_global_id
                ):
                    raise IfcModelIntegrityError(
                        "committed IFC initialization identity is inconsistent"
                    )
                result = await self._initialize_result(
                    session,
                    operation,
                    publication.inspection.to_safe_info(),
                )
            elif operation.status != CriticalOperationStatus.PREPARED.value:
                raise IfcModelConflictError("IFC initialization Operation is not prepared")
            else:
                existing_model = await session.scalar(
                    select(IfcModelModel).where(IfcModelModel.run_id == scope.run_id)
                )
                if existing_model is not None:
                    now = await database_now(session)
                    await self._fail_prepared_operation(
                        session,
                        operation,
                        error_code="IFC_ALREADY_INITIALIZED",
                        error_message="the selected Run already has an IfcModel",
                        error_phase=CriticalOperationErrorPhase.COMMIT_FENCE,
                        now=now,
                    )
                    conflict = "the selected Run already has an IfcModel"
                else:
                    now = await database_now(session)
                    await self._register_artifact(
                        session,
                        scope,
                        publication,
                        operation_id=prepared.operation_id,
                        model_id=prepared.model_id,
                        revision_id=prepared.revision_id,
                        revision_no=1,
                        now=now,
                    )
                    model = IfcModelModel(
                        ifc_model_id=prepared.model_id,
                        owner_id=scope.owner_id,
                        tenant_id=scope.tenant_id,
                        task_id=scope.task_id,
                        run_id=scope.run_id,
                        ifc_schema=IFC_TARGET_SCHEMA,
                        project_global_id=prepared.project_global_id,
                        current_revision_id=prepared.revision_id,
                        write_epoch=1,
                        status=IfcModelState.ACTIVE.value,
                        freeze_reason_code=None,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(model)
                    await session.flush()
                    session.add(
                        IfcModelRevisionModel(
                            revision_id=prepared.revision_id,
                            owner_id=scope.owner_id,
                            tenant_id=scope.tenant_id,
                            task_id=scope.task_id,
                            run_id=scope.run_id,
                            ifc_model_id=prepared.model_id,
                            revision_no=1,
                            artifact_id=publication.artifact.artifact_id,
                            artifact_version_id=publication.artifact.version_id,
                            operation_id=prepared.operation_id,
                            created_at=now,
                        )
                    )
                    operation.status = CriticalOperationStatus.SUCCEEDED.value
                    operation.operation_detail_json = IfcModelInitializeOperationDetail(
                        project_name=publication.inspection.project_name,
                        project_global_id=prepared.project_global_id,
                        result="model_initialized",
                    ).model_dump(mode="json")
                    operation.finished_at = now
                    await session.flush()
                    await self._append_operation_catalog(session, scope, operation.operation_id)
                    await session.flush()
                    result = await self._initialize_result(
                        session,
                        operation,
                        publication.inspection.to_safe_info(),
                    )
        if conflict is not None:
            raise IfcModelAlreadyInitializedError(conflict)
        if result is None:
            raise IfcModelIntegrityError("IFC initialization result was not committed")
        return result

    async def get_current(self, scope: IfcRunIdentity) -> ExactIfcRevision | None:
        self._require_scope_context(scope)
        async with self._sessions() as session:
            await self._require_ifc_run(session, scope, lock=False)
            row = await self._revision_row(session, scope, current=True)
            return self._exact_revision(scope, row) if row is not None else None

    async def get_exact(
        self,
        scope: IfcRunIdentity,
        *,
        revision_id: str,
    ) -> ExactIfcRevision | None:
        self._require_scope_context(scope)
        async with self._sessions() as session:
            await self._require_ifc_run(session, scope, lock=False)
            row = await self._revision_row(
                session,
                scope,
                revision_id=revision_id,
            )
            return self._exact_revision(scope, row) if row is not None else None

    async def get_revision_inspection(self, exact: ExactIfcRevision) -> IfcRevisionInspection | None:
        self._require_scope_context(exact.scope)
        async with self._sessions() as session:
            await self._require_ifc_run(session, exact.scope, lock=False)
            row = await self._revision_row(session, exact.scope, revision_id=exact.revision_id)
            if row is None or self._exact_revision(exact.scope, row) != exact:
                raise IfcModelIntegrityError("IFC inspection revision binding changed")
            return self._inspection_from_metadata(exact, row[3].metadata_json)

    @staticmethod
    def _inspection_from_metadata(exact: ExactIfcRevision, metadata) -> IfcRevisionInspection | None:
        if isinstance(metadata, dict) and "ifc_inspection" not in metadata:
            return None
        try:
            inspection = IfcRevisionInspection.model_validate(metadata["ifc_inspection"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise IfcModelIntegrityError("exact IFC revision inspection facts are unavailable") from exc
        if (inspection.sha256 != exact.artifact.sha256
                or inspection.size_bytes != exact.artifact.size_bytes
                or inspection.project_global_id != exact.project_global_id):
            raise IfcModelIntegrityError("IFC inspection facts differ from the exact ArtifactVersion")
        return inspection if inspection.product_body_contexts is not None else None

    async def require_recovery_allowed(self, scope: IfcRunIdentity) -> None:
        self._require_scope_context(scope)
        async with self._sessions() as session:
            run = await self._require_ifc_run(session, scope, lock=False)
            self._require_run_accepting_ifc_write(run)

    async def require_invocation_allowed(self, scope: IfcRunIdentity) -> None:
        """Fail closed when this Run has unresolved IFC technical state."""

        self._require_scope_context(scope)
        async with self._sessions() as session:
            await self._require_ifc_run(session, scope, lock=False)
            unresolved = await session.scalar(
                select(CriticalOperationModel.operation_id).where(
                    CriticalOperationModel.owner_id == scope.owner_id,
                    CriticalOperationModel.task_id == scope.task_id,
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.operation_kind
                    == CriticalOperationKind.IFC_MODEL_WRITE.value,
                    CriticalOperationModel.status
                    == CriticalOperationStatus.INDETERMINATE.value,
                )
            )
            frozen = await session.scalar(
                select(IfcModelModel.ifc_model_id).where(
                    IfcModelModel.owner_id == scope.owner_id,
                    IfcModelModel.task_id == scope.task_id,
                    IfcModelModel.run_id == scope.run_id,
                    IfcModelModel.status == IfcModelState.FROZEN.value,
                )
            )
            if unresolved is not None or frozen is not None:
                raise IfcModelFrozenError(
                    "IFC invocation is paused pending server reconciliation"
                )

    async def get_initialize_operation_state(
        self,
        scope: IfcRunIdentity,
        *,
        request_digest: str,
        project_global_id: str,
        project_name: str,
    ) -> IfcInitializeOperationState | None:
        """Read one initialization Operation without passing the public pause gate."""

        self._require_scope_context(scope)
        self._validate_digest(request_digest)
        identities = self._initialize_identities(scope)
        async with self._sessions() as session:
            await self._require_ifc_run(session, scope, lock=False)
            operation = await session.scalar(
                select(CriticalOperationModel).where(
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.idempotency_key == identities["idempotency_key"],
                )
            )
            if operation is None:
                return None
            self._assert_initialize_replay(
                operation,
                request_digest=request_digest,
                project_global_id=project_global_id,
                project_name=project_name,
            )
            return IfcInitializeOperationState(
                prepared=self._prepared_initialize(
                    scope,
                    operation=operation,
                    identities=identities,
                    project_global_id=project_global_id,
                ),
                status=CriticalOperationStatus(operation.status),
                error_code=operation.error_code,
                error_message=operation.error_message,
                error_phase=(CriticalOperationErrorPhase(operation.error_phase)
                             if operation.error_phase is not None else None),
                operation_alias=(await self._require_operation_alias(session, operation)
                                 if operation.status in {"succeeded", "failed"} else None),
            )

    async def get_revision_operation_state(
        self,
        scope: IfcRunIdentity,
        *,
        tool_name: str,
        idempotency_key: str,
        request_digest: str,
        intent_summary: str,
        operation_detail: Mapping[str, object],
    ) -> IfcRevisionOperationState | None:
        """Read one exact logical revision write without passing the pause gate."""

        self._require_scope_context(scope)
        self._validate_revision_operation_input(
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            intent_summary=intent_summary,
        )
        validated_detail = self._validate_revision_operation_detail(
            tool_name=tool_name,
            operation_detail=operation_detail,
        )
        identities = self._revision_identities(scope, idempotency_key)
        async with self._sessions() as session:
            await self._require_ifc_run(session, scope, lock=False)
            operation = await session.scalar(
                select(CriticalOperationModel).where(
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.idempotency_key == idempotency_key,
                )
            )
            if operation is None:
                return None
            self._assert_revision_replay(
                operation,
                tool_name=tool_name,
                request_digest=request_digest,
                intent_summary=intent_summary,
                operation_detail=validated_detail,
            )
            before_row = await self._revision_row(
                session,
                scope,
                revision_id=operation.base_ifc_revision_id,
            )
            if before_row is None or operation.expected_ifc_write_epoch is None:
                raise IfcModelIntegrityError("IFC write Operation lost its prepared base")
            before = self._exact_revision(
                scope,
                before_row,
                write_epoch=operation.expected_ifc_write_epoch,
            )
            detail = operation.operation_detail_json
            project_detail = (IfcProjectUpdateOperationDetail.model_validate(detail)
                              if tool_name == _PROJECT_UPDATE_TOOL else None)
            spatial_detail = (IfcSpatialStructureApplyOperationDetail.model_validate(detail)
                              if tool_name == _SPATIAL_STRUCTURE_APPLY_TOOL else None)
            product_detail = ((IfcProductsEditOperationDetail if tool_name == _PRODUCTS_EDIT_TOOL else IfcProductsApplyOperationDetail).model_validate(detail)
                              if tool_name in _PRODUCT_WRITE_TOOLS else None)
            return IfcRevisionOperationState(
                prepared=self._prepared_revision(
                    operation,
                    before=before,
                    identities=identities,
                ),
                status=CriticalOperationStatus(operation.status),
                error_code=operation.error_code,
                error_message=operation.error_message,
                error_phase=(CriticalOperationErrorPhase(operation.error_phase)
                             if operation.error_phase is not None else None),
                operation_alias=(await self._require_operation_alias(session, operation)
                                 if operation.status in {"succeeded", "failed"} else None),
                project_result=project_detail.result if project_detail is not None else None,
                spatial_result=spatial_detail.result if spatial_detail is not None else None,
                failure_blockers=(spatial_detail.failure_blockers if spatial_detail is not None else None),
                product_summary=product_detail.result if product_detail is not None else None,
                report_package_id=operation.result_package_id,
            )

    async def unfreeze_succeeded_revision(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
        revision_id: str,
    ) -> None:
        """Unfreeze only when the verified successful revision is still CURRENT."""

        self._require_scope_context(scope)
        async with self._sessions() as session, session.begin():
            await self._require_ifc_run(session, scope, lock=True)
            operation = await self._require_operation(
                session,
                scope,
                operation_id,
                lock=True,
            )
            if operation.status != CriticalOperationStatus.SUCCEEDED.value:
                raise IfcModelFrozenError(
                    "IFC model remains frozen because the Operation is not successful"
                )
            revision = await session.scalar(
                select(IfcModelRevisionModel).where(
                    IfcModelRevisionModel.owner_id == scope.owner_id,
                    IfcModelRevisionModel.task_id == scope.task_id,
                    IfcModelRevisionModel.run_id == scope.run_id,
                    IfcModelRevisionModel.operation_id == operation.operation_id,
                    IfcModelRevisionModel.revision_id == revision_id,
                )
            )
            if revision is None:
                raise IfcModelFrozenError(
                    "IFC model remains frozen because the successful revision is missing"
                )
            model = await session.scalar(
                select(IfcModelModel)
                .where(
                    IfcModelModel.owner_id == scope.owner_id,
                    IfcModelModel.task_id == scope.task_id,
                    IfcModelModel.run_id == scope.run_id,
                    IfcModelModel.ifc_model_id == revision.ifc_model_id,
                )
                .with_for_update()
            )
            if model is None or (
                model.current_revision_id != revision.revision_id
                or operation.expected_ifc_write_epoch is None
                or revision.revision_no != operation.expected_ifc_write_epoch + 1
                or model.write_epoch != operation.expected_ifc_write_epoch + 1
            ):
                raise IfcModelFrozenError(
                    "IFC model remains frozen because CURRENT contradicts the successful write"
                )
            unresolved = await session.scalar(
                select(CriticalOperationModel.operation_id).where(
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.operation_kind
                    == CriticalOperationKind.IFC_MODEL_WRITE.value,
                    CriticalOperationModel.status
                    == CriticalOperationStatus.INDETERMINATE.value,
                )
            )
            if unresolved is not None:
                raise IfcModelFrozenError(
                    "IFC model remains frozen because another write is indeterminate"
                )
            if model.status == IfcModelState.ACTIVE.value:
                return
            model.status = IfcModelState.ACTIVE.value
            model.freeze_reason_code = None
            model.updated_at = await database_now(session)
            await session.flush()

    async def reopen_indeterminate_revision(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
    ) -> None:
        """Atomically reopen one proven-uncommitted write at its original CAS frontier."""

        self._require_scope_context(scope)
        async with self._sessions() as session, session.begin():
            await self._require_ifc_run(session, scope, lock=True)
            operation = await self._require_operation(
                session,
                scope,
                operation_id,
                lock=True,
            )
            if operation.status == CriticalOperationStatus.PREPARED.value:
                return
            if operation.status != CriticalOperationStatus.INDETERMINATE.value:
                raise IfcModelFrozenError(
                    "IFC model remains frozen because the Operation cannot be reopened"
                )
            if (
                operation.base_ifc_revision_id is None
                or operation.expected_ifc_write_epoch is None
            ):
                raise IfcModelFrozenError(
                    "IFC model remains frozen because the write lost its CAS frontier"
                )
            committed_revision = await session.scalar(
                select(IfcModelRevisionModel.revision_id).where(
                    IfcModelRevisionModel.run_id == scope.run_id,
                    IfcModelRevisionModel.operation_id == operation.operation_id,
                )
            )
            identities = self._revision_identities(scope, operation.idempotency_key)
            reserved_revision = await session.scalar(
                select(IfcModelRevisionModel.revision_id).where(
                    IfcModelRevisionModel.run_id == scope.run_id,
                    IfcModelRevisionModel.revision_id == identities["revision_id"],
                )
            )
            if committed_revision is not None or reserved_revision is not None:
                raise IfcModelFrozenError(
                    "IFC model remains frozen because revision evidence is contradictory"
                )
            base = await session.scalar(
                select(IfcModelRevisionModel).where(
                    IfcModelRevisionModel.owner_id == scope.owner_id,
                    IfcModelRevisionModel.task_id == scope.task_id,
                    IfcModelRevisionModel.run_id == scope.run_id,
                    IfcModelRevisionModel.revision_id == operation.base_ifc_revision_id,
                )
            )
            if base is None:
                raise IfcModelFrozenError(
                    "IFC model remains frozen because the prepared base is missing"
                )
            if base.revision_no != operation.expected_ifc_write_epoch:
                raise IfcModelFrozenError(
                    "IFC model remains frozen because the prepared epoch is inconsistent"
                )
            model = await session.scalar(
                select(IfcModelModel)
                .where(
                    IfcModelModel.owner_id == scope.owner_id,
                    IfcModelModel.task_id == scope.task_id,
                    IfcModelModel.run_id == scope.run_id,
                    IfcModelModel.ifc_model_id == base.ifc_model_id,
                )
                .with_for_update()
            )
            if model is None or (
                model.status != IfcModelState.FROZEN.value
                or model.current_revision_id != operation.base_ifc_revision_id
                or model.write_epoch != operation.expected_ifc_write_epoch
            ):
                raise IfcModelFrozenError(
                    "IFC model remains frozen because CURRENT left the prepared CAS frontier"
                )
            other_indeterminate = await session.scalar(
                select(CriticalOperationModel.operation_id).where(
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.operation_kind
                    == CriticalOperationKind.IFC_MODEL_WRITE.value,
                    CriticalOperationModel.status
                    == CriticalOperationStatus.INDETERMINATE.value,
                    CriticalOperationModel.operation_id != operation.operation_id,
                )
            )
            if other_indeterminate is not None:
                raise IfcModelFrozenError(
                    "IFC model remains frozen because another write is indeterminate"
                )
            operation.status = CriticalOperationStatus.PREPARED.value
            operation.error_code = None
            operation.error_message = None
            operation.error_phase = None
            operation.finished_at = None
            model.status = IfcModelState.ACTIVE.value
            model.freeze_reason_code = None
            model.updated_at = await database_now(session)
            await session.flush()

    async def reopen_indeterminate_initialize(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
    ) -> None:
        """Server-only transition after an uncommitted initialize is proven safe."""

        self._require_scope_context(scope)
        async with self._sessions() as session, session.begin():
            await self._require_ifc_run(session, scope, lock=True)
            operation = await self._require_operation(
                session,
                scope,
                operation_id,
                lock=True,
            )
            if operation.tool_name != _INITIALIZE_TOOL:
                raise IfcModelConflictError("IFC Operation is not an initialization")
            if operation.status == CriticalOperationStatus.PREPARED.value:
                return
            if operation.status != CriticalOperationStatus.INDETERMINATE.value:
                raise IfcModelConflictError(
                    "only an indeterminate initialization can be reopened"
                )
            operation.status = CriticalOperationStatus.PREPARED.value
            operation.error_code = None
            operation.error_message = None
            operation.error_phase = None
            operation.finished_at = None
            await session.flush()

    async def fail_indeterminate_initialize(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        """Close an indeterminate initialize after a business rejection is proven."""

        self._require_scope_context(scope)
        self._validate_failure(
            error_code,
            error_message,
            CriticalOperationErrorPhase.COMMIT_FENCE,
        )
        async with self._sessions() as session, session.begin():
            await self._require_ifc_run(session, scope, lock=True)
            operation = await self._require_operation(
                session,
                scope,
                operation_id,
                lock=True,
            )
            if operation.tool_name != _INITIALIZE_TOOL:
                raise IfcModelConflictError("IFC Operation is not an initialization")
            if operation.status == CriticalOperationStatus.FAILED.value:
                if operation.error_code != error_code:
                    raise IfcModelConflictError(
                        "IFC initialization failure replay changed its result"
                    )
                return
            if operation.status != CriticalOperationStatus.INDETERMINATE.value:
                raise IfcModelConflictError(
                    "only an indeterminate initialization can be failed"
                )
            now = await database_now(session)
            operation.status = CriticalOperationStatus.FAILED.value
            operation.error_code = error_code
            operation.error_message = error_message
            operation.error_phase = CriticalOperationErrorPhase.COMMIT_FENCE.value
            operation.finished_at = now
            await session.flush()
            await self._append_operation_catalog(session, scope, operation.operation_id)
            await session.flush()

    async def unfreeze_reconciled_model(
        self,
        scope: IfcRunIdentity,
        *,
        model_id: str,
    ) -> None:
        """Reactivate one model only after the server reconciler verified exact bytes."""

        self._require_scope_context(scope)
        async with self._sessions() as session, session.begin():
            await self._require_ifc_run(session, scope, lock=True)
            unresolved = await session.scalar(
                select(CriticalOperationModel.operation_id).where(
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.operation_kind
                    == CriticalOperationKind.IFC_MODEL_WRITE.value,
                    CriticalOperationModel.status
                    == CriticalOperationStatus.INDETERMINATE.value,
                )
            )
            if unresolved is not None:
                raise IfcModelConflictError(
                    "indeterminate IFC Operation must be reconciled before model unfreeze"
                )
            model = await session.scalar(
                select(IfcModelModel)
                .where(
                    IfcModelModel.owner_id == scope.owner_id,
                    IfcModelModel.task_id == scope.task_id,
                    IfcModelModel.run_id == scope.run_id,
                    IfcModelModel.ifc_model_id == model_id,
                )
                .with_for_update()
            )
            if model is None:
                raise IfcModelConflictError("reconciled IfcModel was not found")
            if model.status == IfcModelState.ACTIVE.value:
                return
            model.status = IfcModelState.ACTIVE.value
            model.freeze_reason_code = None
            model.updated_at = await database_now(session)
            await session.flush()

    async def prepare_revision(
        self,
        scope: IfcRunIdentity,
        *,
        tool_name: str,
        idempotency_key: str,
        request_digest: str,
        intent_summary: str,
        operation_detail: Mapping[str, object],
    ) -> PreparedIfcRevision:
        self._require_scope_context(scope)
        self._validate_revision_operation_input(
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            intent_summary=intent_summary,
        )
        identities = self._revision_identities(scope, idempotency_key)
        validated_detail = self._validate_revision_operation_detail(
            tool_name=tool_name,
            operation_detail=operation_detail,
        )
        async with self._sessions() as session, session.begin():
            run = await self._require_ifc_run(session, scope, lock=True)
            self._require_run_accepting_ifc_write(run)
            replay = await session.scalar(
                select(CriticalOperationModel).where(
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                self._assert_revision_replay(
                    replay,
                    tool_name=tool_name,
                    request_digest=request_digest,
                    intent_summary=intent_summary,
                    operation_detail=validated_detail,
                )
                if replay.status in {
                    CriticalOperationStatus.FAILED.value,
                    CriticalOperationStatus.INDETERMINATE.value,
                }:
                    raise IfcModelConflictError("IFC write Operation is already terminal")
                before_row = await self._revision_row(
                    session,
                    scope,
                    revision_id=replay.base_ifc_revision_id,
                )
                if before_row is None or replay.expected_ifc_write_epoch is None:
                    raise IfcModelIntegrityError("prepared IFC write lost its base revision")
                before = self._exact_revision(
                    scope,
                    before_row,
                    write_epoch=replay.expected_ifc_write_epoch,
                )
                if (
                    replay.status == CriticalOperationStatus.PREPARED.value
                    and before.model_state is IfcModelState.FROZEN
                ):
                    raise IfcModelFrozenError("the selected IfcModel is frozen")
                return self._prepared_revision(
                    replay,
                    before=before,
                    identities=identities,
                )

            current_row = await self._revision_row(session, scope, current=True, lock_model=True)
            if current_row is None:
                raise IfcModelNotInitializedError("the selected Run has no IfcModel")
            before = self._exact_revision(scope, current_row)
            if before.model_state is IfcModelState.FROZEN:
                raise IfcModelFrozenError("the selected IfcModel is frozen")
            if tool_name == _PRODUCTS_EDIT_TOOL:
                edit = IfcProductsEditOperationDetail.model_validate(validated_detail)
                if edit.request.base_revision_id != before.revision_id:
                    raise IfcModelConflictError("Edit input base differs from CURRENT; reread affected products")
            if tool_name == _TRUSTED_BASELINE_TOOL:
                if before.revision_no != 1:
                    raise IfcModelConflictError("trusted baseline load requires initialization revision 1")
                detail = IfcTrustedBaselineLoadOperationDetail.model_validate(validated_detail)
                source = detail.source_asset
                source_row = await session.scalar(
                    select(ArtifactVersionModel).join(
                        ArtifactModel,
                        ArtifactModel.artifact_id == ArtifactVersionModel.artifact_id,
                    ).where(
                        ArtifactModel.owner_id == scope.owner_id,
                        ArtifactModel.tenant_id == scope.tenant_id,
                        ArtifactModel.task_id == scope.task_id,
                        ArtifactModel.run_id == scope.run_id,
                        ArtifactModel.kind == "ifc_trusted_baseline_source",
                        ArtifactVersionModel.owner_id == scope.owner_id,
                        ArtifactVersionModel.tenant_id == scope.tenant_id,
                        ArtifactVersionModel.task_id == scope.task_id,
                        ArtifactVersionModel.artifact_id == source.artifact_id,
                        ArtifactVersionModel.version_id == source.version_id,
                        ArtifactVersionModel.sha256 == source.sha256,
                    )
                )
                if source_row is None:
                    raise IfcModelIntegrityError("trusted baseline source is not an exact asset in this Run")
            now = await database_now(session)
            operation = CriticalOperationModel(
                operation_id=identities["operation_id"],
                owner_id=scope.owner_id,
                tenant_id=scope.tenant_id,
                task_id=scope.task_id,
                run_id=scope.run_id,
                operation_kind=CriticalOperationKind.IFC_MODEL_WRITE.value,
                initiator=self._operation_initiator(),
                tool_name=tool_name,
                intent_summary=intent_summary,
                source_filename=None,
                idempotency_key=idempotency_key,
                request_fingerprint=request_digest,
                status=CriticalOperationStatus.PREPARED.value,
                result_package_id=None,
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
                base_ifc_revision_id=before.revision_id,
                expected_ifc_write_epoch=before.write_epoch,
                operation_detail_json=validated_detail,
                ifc_deadline_at=now + timedelta(
                    seconds=IFC_REVISION_INVOCATION_EXECUTION.total_seconds,
                ),
                started_at=now,
                finished_at=None,
                created_at=now,
            )
            session.add(operation)
            await session.flush()
            if tool_name in _PRODUCT_WRITE_TOOLS:
                record = await self._operation_record(session, operation)
                await self._require_transform_inputs(session, scope, record)
                session.add_all(self._operation_input_models(record))
                await session.flush()
            return self._prepared_revision(
                operation,
                before=before,
                identities=identities,
            )

    async def commit_revision(
        self,
        scope: IfcRunIdentity,
        *,
        prepared: PreparedIfcRevision,
        publication: VerifiedIfcPublication,
        claim: IfcExecutionClaim,
        project_result: IfcProjectSummary | None = None,
        spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None = None,
        spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None = None,
    ) -> CommittedIfcRevision:
        self._require_scope_context(scope)
        self._assert_prepared_revision_identity(scope, prepared)
        self._validate_revision_publication(scope, prepared, publication)
        conflict: str | None = None
        committed: ExactIfcRevision | None = None
        operation_alias: str | None = None
        async with self._sessions() as session, session.begin():
            run = await self._require_ifc_run(session, scope, lock=True)
            operation = await self._require_operation(
                session,
                scope,
                prepared.operation_id,
                lock=True,
            )
            if operation.status == CriticalOperationStatus.PREPARED.value:
                self._require_run_accepting_ifc_write(run)
            await self._assert_ifc_claim(
                session, operation, claim,
                for_commit=operation.status == CriticalOperationStatus.PREPARED.value,
            )
            if (
                operation.request_fingerprint != prepared.request_digest
                or operation.idempotency_key != prepared.idempotency_key
                or operation.base_ifc_revision_id != prepared.before.revision_id
                or operation.expected_ifc_write_epoch != prepared.before.write_epoch
            ):
                raise IfcModelConflictError("prepared IFC write identity changed")
            self._assert_selected_publication(
                operation, prepared, publication,
                project_result=project_result,
                spatial_applied=spatial_applied,
                spatial_summary=spatial_summary,
            )
            if operation.status == CriticalOperationStatus.SUCCEEDED.value:
                committed = await self._require_committed_publication(
                    session,
                    scope,
                    operation_id=operation.operation_id,
                    publication=publication,
                )
                if (
                    committed.model_id != prepared.before.model_id
                    or committed.revision_id != prepared.after_revision_id
                    or committed.project_global_id != prepared.before.project_global_id
                ):
                    raise IfcModelIntegrityError("committed IFC revision identity is inconsistent")
                operation_alias = await self._require_operation_alias(session, operation)
                expected_detail = self._revision_business_result_detail(
                    operation, operation_alias=operation_alias,
                    publication=publication, project_result=project_result,
                    spatial_applied=spatial_applied, spatial_summary=spatial_summary,
                )
                if expected_detail is not None and operation.operation_detail_json != expected_detail:
                    raise IfcModelConflictError("IFC Operation replay changed its business result")
            elif operation.status != CriticalOperationStatus.PREPARED.value:
                raise IfcModelConflictError("IFC write Operation is not prepared")
            else:
                base_row = await self._revision_row(
                    session,
                    scope,
                    revision_id=prepared.before.revision_id,
                )
                stored_before = (
                    self._exact_revision(
                        scope,
                        base_row,
                        write_epoch=prepared.before.write_epoch,
                    )
                    if base_row is not None
                    else None
                )
                if stored_before is None or any(
                    (
                        stored_before.model_id != prepared.before.model_id,
                        stored_before.revision_id != prepared.before.revision_id,
                        stored_before.revision_no != prepared.before.revision_no,
                        stored_before.write_epoch != prepared.before.write_epoch,
                        stored_before.artifact != prepared.before.artifact,
                        stored_before.project_global_id != prepared.before.project_global_id,
                    )
                ):
                    raise IfcModelConflictError("prepared IFC base revision changed")
                model = await session.scalar(
                    select(IfcModelModel)
                    .where(
                        IfcModelModel.owner_id == scope.owner_id,
                        IfcModelModel.task_id == scope.task_id,
                        IfcModelModel.run_id == scope.run_id,
                        IfcModelModel.ifc_model_id == prepared.before.model_id,
                    )
                    .with_for_update()
                )
                if model is None:
                    raise IfcModelIntegrityError("prepared IfcModel no longer exists")
                if model.status == IfcModelState.FROZEN.value:
                    raise IfcModelFrozenError("the selected IfcModel is frozen")
                if (
                    model.current_revision_id != prepared.before.revision_id
                    or model.write_epoch != prepared.before.write_epoch
                ):
                    now = await database_now(session)
                    if operation.tool_name in _PRODUCT_WRITE_TOOLS:
                        selected = self._selected_candidate(operation)
                        await self._register_product_report(
                            session, scope, operation, selected.product_failure_report,
                        )
                    await self._fail_prepared_operation(
                        session,
                        operation,
                        error_code="IFC_CURRENT_CONFLICT",
                        error_message="IfcModel CURRENT or write epoch changed after prepare",
                        error_phase=CriticalOperationErrorPhase.COMMIT_FENCE,
                        now=now,
                    )
                    conflict = "IfcModel CURRENT or write epoch changed after prepare"
                else:
                    now = await database_now(session)
                    revision_no = prepared.before.revision_no + 1
                    await self._register_artifact(
                        session,
                        scope,
                        publication,
                        operation_id=prepared.operation_id,
                        model_id=model.ifc_model_id,
                        revision_id=prepared.after_revision_id,
                        revision_no=revision_no,
                        now=now,
                    )
                    session.add(
                        IfcModelRevisionModel(
                            revision_id=prepared.after_revision_id,
                            owner_id=scope.owner_id,
                            tenant_id=scope.tenant_id,
                            task_id=scope.task_id,
                            run_id=scope.run_id,
                            ifc_model_id=model.ifc_model_id,
                            revision_no=revision_no,
                            artifact_id=publication.artifact.artifact_id,
                            artifact_version_id=publication.artifact.version_id,
                            operation_id=prepared.operation_id,
                            created_at=now,
                        )
                    )
                    await session.flush()
                    cas = await session.execute(
                        update(IfcModelModel)
                        .where(
                            IfcModelModel.ifc_model_id == model.ifc_model_id,
                            IfcModelModel.current_revision_id == prepared.before.revision_id,
                            IfcModelModel.write_epoch == prepared.before.write_epoch,
                            IfcModelModel.status == IfcModelState.ACTIVE.value,
                        )
                        .values(
                            current_revision_id=prepared.after_revision_id,
                            write_epoch=prepared.before.write_epoch + 1,
                            updated_at=now,
                        )
                    )
                    if cas.rowcount != 1:
                        raise IfcModelIntegrityError("locked IfcModel failed its CURRENT CAS")
                    if operation.tool_name in _PRODUCT_WRITE_TOOLS:
                        selected = self._selected_candidate(operation)
                        await self._register_product_report(
                            session, scope, operation, selected.product_report,
                        )
                    operation.status = CriticalOperationStatus.SUCCEEDED.value
                    if operation.tool_name == _GEOMETRY_BASIS_TOOL:
                        detail = IfcGeometryBasisConfigureOperationDetail.model_validate(
                            operation.operation_detail_json
                        )
                        operation.operation_detail_json = detail.model_copy(
                            update={"result": "geometry_basis_configured"}
                        ).persisted_payload()
                    elif operation.tool_name == _TRUSTED_BASELINE_TOOL:
                        # Its terminal check applies to this first success flush;
                        # unlike spatial results it does not depend on an Alias.
                        operation.operation_detail_json = self._revision_business_result_detail(
                            operation, operation_alias="", publication=publication,
                            project_result=project_result, spatial_applied=spatial_applied,
                            spatial_summary=spatial_summary,
                        )
                    operation.finished_at = now
                    await session.flush()
                    await self._append_operation_catalog(session, scope, operation.operation_id)
                    await session.flush()
                    operation_alias = await self._require_operation_alias(session, operation)
                    business_detail = self._revision_business_result_detail(
                        operation, operation_alias=operation_alias,
                        publication=publication, project_result=project_result,
                        spatial_applied=spatial_applied, spatial_summary=spatial_summary,
                    )
                    if business_detail is not None:
                        operation.operation_detail_json = business_detail
                        await session.flush()
                    row = await self._revision_row(
                        session,
                        scope,
                        revision_id=prepared.after_revision_id,
                    )
                    if row is None:
                        raise IfcModelIntegrityError("committed IFC revision is unreadable")
                    committed = self._exact_revision(
                        scope,
                        row,
                        write_epoch=prepared.before.write_epoch + 1,
                    )
        if conflict is not None:
            raise IfcModelConflictError(conflict)
        if committed is None or operation_alias is None:
            raise IfcModelIntegrityError("IFC revision was not committed")
        return CommittedIfcRevision(
            operation_alias=operation_alias,
            exact=committed,
        )

    @staticmethod
    def _revision_business_result_detail(
        operation: CriticalOperationModel,
        *,
        operation_alias: str,
        publication: VerifiedIfcPublication,
        project_result: IfcProjectSummary | None,
        spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None,
        spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None,
    ) -> dict[str, object] | None:
        if operation.tool_name == _PROJECT_UPDATE_TOOL:
            if (project_result is None or spatial_applied is not None or spatial_summary is not None
                    or project_result.global_id != publication.inspection.project_global_id
                    or project_result.name != publication.inspection.project_name):
                raise IfcModelIntegrityError("Project result does not match the verified publication")
            detail = IfcProjectUpdateOperationDetail.model_validate(operation.operation_detail_json)
            return IfcProjectUpdateOperationDetail(
                request=detail.request, result=project_result,
            ).persisted_payload()
        if operation.tool_name == _SPATIAL_STRUCTURE_APPLY_TOOL:
            if project_result is not None or spatial_applied is None or spatial_summary is None:
                raise IfcModelIntegrityError("spatial result is missing from the verified publication")
            detail = IfcSpatialStructureApplyOperationDetail.model_validate(operation.operation_detail_json)
            result = IfcSpatialStructureApplyResult(
                operation_alias=operation_alias,
                applied_count=len(spatial_applied),
                applied=spatial_applied,
                spatial_summary=spatial_summary,
            )
            return IfcSpatialStructureApplyOperationDetail(
                request=detail.request, result=result,
            ).persisted_payload()
        if operation.tool_name in _PRODUCT_WRITE_TOOLS:
            selected = PostgresIfcModelRepositoryMixin._selected_candidate(operation)
            report = selected.product_report
            detail_type = IfcProductsEditOperationDetail if operation.tool_name == _PRODUCTS_EDIT_TOOL else IfcProductsApplyOperationDetail
            detail = detail_type.model_validate(operation.operation_detail_json)
            if (report is None or operation.result_package_id != report.package.package_id
                    or detail.result != report.summary):
                raise IfcModelIntegrityError("committed Apply lost its joint report identity")
            return detail.persisted_payload()
        if project_result is not None or spatial_applied is not None or spatial_summary is not None:
            raise IfcModelIntegrityError("unrelated IFC write supplied a business result")
        if operation.tool_name == _TRUSTED_BASELINE_TOOL:
            detail = IfcTrustedBaselineLoadOperationDetail.model_validate(operation.operation_detail_json)
            return IfcTrustedBaselineLoadOperationDetail(
                source_asset=detail.source_asset, result="baseline_loaded",
            ).persisted_payload()
        return None

    async def record_failed_ifc_operation(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
        error_code: str,
        error_message: str,
        error_phase: CriticalOperationErrorPhase,
        claim: IfcExecutionClaim,
        spatial_blockers: IfcSpatialBlockerSummary | None = None,
        product_report: ProductReportPublication | None = None,
    ) -> str:
        self._require_scope_context(scope)
        self._validate_failure(error_code, error_message, error_phase)
        async with self._sessions() as session, session.begin():
            await self._require_ifc_run(session, scope, lock=True)
            operation = await self._require_operation(
                session,
                scope,
                operation_id,
                lock=True,
            )
            await self._assert_ifc_claim(session, operation, claim, for_commit=False)
            if operation.status == CriticalOperationStatus.FAILED.value:
                stored_blockers = (
                    IfcSpatialStructureApplyOperationDetail.model_validate(
                        operation.operation_detail_json
                    ).failure_blockers
                    if operation.tool_name == _SPATIAL_STRUCTURE_APPLY_TOOL else None
                )
                if (
                    operation.error_code != error_code
                    or operation.error_message != error_message
                    or operation.error_phase != error_phase.value
                    or stored_blockers != spatial_blockers
                    or (product_report is not None and operation.result_package_id != product_report.package.package_id)
                ):
                    raise IfcModelConflictError("IFC failure replay changed its result")
                return await self._require_operation_alias(session, operation)
            if operation.status != CriticalOperationStatus.PREPARED.value:
                raise IfcModelConflictError("only a prepared IFC Operation can fail")
            if product_report is not None:
                if operation.tool_name not in _PRODUCT_WRITE_TOOLS or product_report_committed(product_report):
                    raise IfcModelIntegrityError("only failed Apply can retain its diagnostic report")
                await self._register_product_report(session, scope, operation, product_report)
            if spatial_blockers is not None:
                if operation.tool_name != _SPATIAL_STRUCTURE_APPLY_TOOL or error_code != "IFC_SPATIAL_NOT_EMPTY":
                    raise IfcModelIntegrityError("only a blocked spatial delete can retain blockers")
                detail = IfcSpatialStructureApplyOperationDetail.model_validate(operation.operation_detail_json)
                operation.operation_detail_json = IfcSpatialStructureApplyOperationDetail(
                    request=detail.request, failure_blockers=spatial_blockers,
                ).persisted_payload()
            now = await database_now(session)
            await self._fail_prepared_operation(
                session,
                operation,
                error_code=error_code,
                error_message=error_message,
                error_phase=error_phase,
                now=now,
            )
            return await self._require_operation_alias(session, operation)

    async def freeze_indeterminate(
        self,
        scope: IfcRunIdentity,
        *,
        model_id: str | None,
        operation_id: str | None,
        reason_code: str,
        claim: IfcExecutionClaim | None = None,
    ) -> None:
        self._require_scope_context(scope)
        self._validate_reason_code(reason_code)
        async with self._sessions() as session, session.begin():
            await self._require_ifc_run(session, scope, lock=True)
            now = await database_now(session)
            if operation_id is not None:
                operation = await self._require_operation(
                    session,
                    scope,
                    operation_id,
                    lock=True,
                )
                if claim is not None:
                    await self._assert_ifc_claim(session, operation, claim, for_commit=False)
                if operation.status == CriticalOperationStatus.PREPARED.value:
                    operation.status = CriticalOperationStatus.INDETERMINATE.value
                    operation.error_code = reason_code
                    operation.error_message = "IFC model state requires server reconciliation"
                    operation.error_phase = CriticalOperationErrorPhase.RECONCILE.value
                    operation.finished_at = now
                elif operation.status == CriticalOperationStatus.INDETERMINATE.value:
                    if operation.error_code != reason_code:
                        raise IfcModelConflictError("IFC Operation was frozen for another reason")
                # A committed revision remains a successful business Operation;
                # the model freeze records the later integrity incident.
            elif claim is not None:
                raise ValueError("IFC claim requires its original Operation")
            if model_id is not None:
                model = await session.scalar(
                    select(IfcModelModel)
                    .where(
                        IfcModelModel.owner_id == scope.owner_id,
                        IfcModelModel.task_id == scope.task_id,
                        IfcModelModel.run_id == scope.run_id,
                        IfcModelModel.ifc_model_id == model_id,
                    )
                    .with_for_update()
                )
                if model is None:
                    raise IfcModelConflictError("IfcModel to freeze was not found")
                if model.status == IfcModelState.FROZEN.value:
                    if model.freeze_reason_code != reason_code:
                        raise IfcModelConflictError("IfcModel was frozen for another reason")
                else:
                    model.status = IfcModelState.FROZEN.value
                    model.freeze_reason_code = reason_code
                    model.updated_at = now
            elif operation_id is None:
                raise IfcModelConflictError(
                    "indeterminate IFC state requires a model or Operation"
                )
            await session.flush()

    @staticmethod
    def _initialize_identities(scope: IfcRunIdentity) -> dict[str, str]:
        parts = (scope.tenant_id, scope.task_id, scope.run_id, _INITIALIZE_TOOL)
        return {
            "idempotency_key": f"{_INITIALIZE_IDEMPOTENCY_PREFIX}{scope.run_id}",
            "operation_id": stable_internal_identity_id(
                "op_", "cad2bim.ifc.initialize.operation", *parts
            ),
            "model_id": stable_internal_identity_id(
                "ifcmodel_", "cad2bim.ifc.initialize.model", *parts
            ),
            "revision_id": stable_internal_identity_id(
                "ifcrev_", "cad2bim.ifc.initialize.revision", *parts
            ),
            "artifact_id": stable_internal_identity_id(
                "artifact_", "cad2bim.ifc.initialize.artifact", *parts
            ),
            "artifact_version_id": stable_internal_identity_id(
                "av_", "cad2bim.ifc.initialize.artifact-version", *parts
            ),
        }

    @staticmethod
    def _revision_identities(scope: IfcRunIdentity, idempotency_key: str) -> dict[str, str]:
        parts = (scope.tenant_id, scope.task_id, scope.run_id, idempotency_key)
        return {
            "operation_id": stable_internal_identity_id(
                "op_", "cad2bim.ifc.revision.operation", *parts
            ),
            "revision_id": stable_internal_identity_id("ifcrev_", "cad2bim.ifc.revision", *parts),
            "artifact_id": stable_internal_identity_id(
                "artifact_", "cad2bim.ifc.revision.artifact", *parts
            ),
            "artifact_version_id": stable_internal_identity_id(
                "av_", "cad2bim.ifc.revision.artifact-version", *parts
            ),
        }

    @staticmethod
    def _prepared_initialize(
        scope: IfcRunIdentity,
        *,
        operation: CriticalOperationModel,
        identities: dict[str, str],
        project_global_id: str,
    ) -> PreparedIfcInitialize:
        return PreparedIfcInitialize(
            scope=scope,
            operation_id=operation.operation_id,
            idempotency_key=operation.idempotency_key,
            request_digest=operation.request_fingerprint,
            model_id=identities["model_id"],
            revision_id=identities["revision_id"],
            artifact_id=identities["artifact_id"],
            artifact_version_id=identities["artifact_version_id"],
            project_global_id=project_global_id,
            prepared_at=as_utc(operation.started_at),
        )

    @staticmethod
    def _prepared_revision(
        operation: CriticalOperationModel,
        *,
        before: ExactIfcRevision,
        identities: dict[str, str],
    ) -> PreparedIfcRevision:
        return PreparedIfcRevision(
            operation_id=operation.operation_id,
            idempotency_key=operation.idempotency_key,
            request_digest=operation.request_fingerprint,
            before=before,
            after_revision_id=identities["revision_id"],
            artifact_id=identities["artifact_id"],
            artifact_version_id=identities["artifact_version_id"],
            prepared_at=as_utc(operation.started_at),
        )

    @staticmethod
    def _require_scope_context(scope: IfcRunIdentity) -> None:
        context = current_tenant_execution_context()
        if context is None:
            raise TenantContextMismatch("IFC model access requires a tenant execution context")
        if context.tenant_id != scope.tenant_id or context.task_id != scope.task_id:
            raise TenantContextMismatch("IFC model scope does not match active tenant context")

    @staticmethod
    def _operation_initiator() -> str:
        context = current_tenant_execution_context()
        if context is None:
            raise TenantContextMismatch("IFC Operation requires a tenant execution context")
        if context.actor_kind is TenantActorKind.RUNNER:
            return CriticalOperationInitiator.RUNNER.value
        if context.actor_kind is not TenantActorKind.USER:
            raise TenantContextMismatch("actor is not authorized to prepare an IFC write")
        return CriticalOperationInitiator.DIRECT_CLIENT.value

    @staticmethod
    async def _require_ifc_run(session, scope: IfcRunIdentity, *, lock: bool) -> RunModel:
        statement = select(RunModel).where(
            RunModel.owner_id == scope.owner_id,
            RunModel.tenant_id == scope.tenant_id,
            RunModel.task_id == scope.task_id,
            RunModel.run_id == scope.run_id,
        )
        if lock:
            statement = statement.with_for_update()
        run = await session.scalar(statement)
        if run is None:
            raise IfcModelConflictError("IFC scope does not match an authoritative Run")
        return run

    @staticmethod
    async def _require_operation(
        session,
        scope: IfcRunIdentity,
        operation_id: str,
        *,
        lock: bool,
    ) -> CriticalOperationModel:
        statement = select(CriticalOperationModel).where(
            CriticalOperationModel.owner_id == scope.owner_id,
            CriticalOperationModel.task_id == scope.task_id,
            CriticalOperationModel.run_id == scope.run_id,
            CriticalOperationModel.operation_id == operation_id,
            CriticalOperationModel.operation_kind == CriticalOperationKind.IFC_MODEL_WRITE.value,
        )
        if lock:
            statement = statement.with_for_update()
        operation = await session.scalar(statement)
        if operation is None:
            raise IfcModelConflictError("IFC Operation was not found in the selected Run")
        return operation

    @staticmethod
    async def _revision_row(
        session,
        scope: IfcRunIdentity,
        *,
        current: bool = False,
        revision_id: str | None = None,
        lock_model: bool = False,
    ):
        statement = (
            select(
                IfcModelModel,
                IfcModelRevisionModel,
                ArtifactModel,
                ArtifactVersionModel,
            )
            .join(
                IfcModelRevisionModel,
                and_(
                    IfcModelRevisionModel.owner_id == IfcModelModel.owner_id,
                    IfcModelRevisionModel.task_id == IfcModelModel.task_id,
                    IfcModelRevisionModel.run_id == IfcModelModel.run_id,
                    IfcModelRevisionModel.ifc_model_id == IfcModelModel.ifc_model_id,
                ),
            )
            .join(
                ArtifactModel,
                and_(
                    ArtifactModel.owner_id == IfcModelRevisionModel.owner_id,
                    ArtifactModel.task_id == IfcModelRevisionModel.task_id,
                    ArtifactModel.run_id == IfcModelRevisionModel.run_id,
                    ArtifactModel.artifact_id == IfcModelRevisionModel.artifact_id,
                ),
            )
            .join(
                ArtifactVersionModel,
                and_(
                    ArtifactVersionModel.owner_id == IfcModelRevisionModel.owner_id,
                    ArtifactVersionModel.task_id == IfcModelRevisionModel.task_id,
                    ArtifactVersionModel.artifact_id == IfcModelRevisionModel.artifact_id,
                    ArtifactVersionModel.version_id == IfcModelRevisionModel.artifact_version_id,
                ),
            )
            .where(
                IfcModelModel.owner_id == scope.owner_id,
                IfcModelModel.tenant_id == scope.tenant_id,
                IfcModelModel.task_id == scope.task_id,
                IfcModelModel.run_id == scope.run_id,
            )
        )
        if current:
            statement = statement.where(
                IfcModelRevisionModel.revision_id == IfcModelModel.current_revision_id
            )
        elif revision_id is not None:
            statement = statement.where(IfcModelRevisionModel.revision_id == revision_id)
        else:
            raise ValueError("revision selector is required")
        if lock_model:
            statement = statement.with_for_update(of=IfcModelModel)
        return (await session.execute(statement)).one_or_none()

    @staticmethod
    def _exact_revision(
        scope: IfcRunIdentity,
        row,
        *,
        write_epoch: int | None = None,
    ) -> ExactIfcRevision:
        model, revision, artifact, version = row
        if (
            artifact.current_version_id != version.version_id
            or artifact.status != "available"
            or artifact.kind != _IFC_REVISION_KIND
            or version.version_no != 1
            or version.filename != "model.ifc"
            or version.content_type != "application/x-ifc"
            or version.size_bytes <= 0
            or _SHA256_RE.fullmatch(version.sha256) is None
        ):
            raise IfcModelIntegrityError("IFC revision Artifact binding is invalid")
        resolved_epoch = write_epoch if write_epoch is not None else revision.revision_no
        if revision.revision_id == model.current_revision_id:
            if model.write_epoch != revision.revision_no:
                raise IfcModelIntegrityError("IfcModel CURRENT epoch is inconsistent")
            resolved_epoch = model.write_epoch
        return ExactIfcRevision(
            scope=scope,
            model_id=model.ifc_model_id,
            model_state=IfcModelState(model.status),
            revision_id=revision.revision_id,
            revision_no=revision.revision_no,
            write_epoch=resolved_epoch,
            artifact=ExactIfcArtifactVersion(
                artifact_id=artifact.artifact_id,
                version_id=version.version_id,
                version_no=1,
                filename=version.filename,
                content_type=version.content_type,
                size_bytes=version.size_bytes,
                sha256=version.sha256,
            ),
            project_global_id=model.project_global_id,
        )

    @staticmethod
    async def _register_artifact(
        session,
        scope: IfcRunIdentity,
        publication: VerifiedIfcPublication,
        *,
        operation_id: str,
        model_id: str,
        revision_id: str,
        revision_no: int,
        now,
    ) -> None:
        exact = publication.artifact
        registration = {
            "artifact_id": exact.artifact_id,
            "version_id": exact.version_id,
            "version_no": 1,
            "filename": exact.filename,
            "content_type": exact.content_type,
            "size_bytes": exact.size_bytes,
            "sha256": exact.sha256,
            "kind": _IFC_REVISION_KIND,
            "run_id": scope.run_id,
            "operation_id": operation_id,
            "model_id": model_id,
            "revision_id": revision_id,
            "revision_no": revision_no,
        }
        await register_artifact_in_transaction(
            session, ArtifactRecord(
                artifact_id=exact.artifact_id, version_id=exact.version_id,
                owner_id=scope.owner_id, task_id=scope.task_id, run_id=scope.run_id,
                kind=_IFC_REVISION_KIND, filename=exact.filename, content_type=exact.content_type,
                size_bytes=exact.size_bytes, sha256=exact.sha256,
                idempotency_key=f"ifc-artifact:{exact.artifact_id}", created_at=now,
                version_metadata={"ifc_schema": IFC_TARGET_SCHEMA, "ifc_model_id": model_id,
                                  "revision_id": revision_id, "revision_no": revision_no,
                                  "ifc_inspection": publication.inspection.model_dump(mode="json")},
            ), tenant_id=scope.tenant_id, request_value=registration,
            lineage={"operation_id": operation_id},
        )

    @staticmethod
    async def _append_operation_catalog(
        session,
        scope: IfcRunIdentity,
        operation_id: str,
    ) -> DataCatalogEntryModel:
        existing = await session.scalar(
            select(DataCatalogEntryModel).where(
                DataCatalogEntryModel.run_id == scope.run_id,
                DataCatalogEntryModel.operation_id == operation_id,
            )
        )
        if existing is not None:
            return existing
        return await append_catalog_entry(
            session,
            owner_id=scope.owner_id,
            tenant_id=scope.tenant_id,
            task_id=scope.task_id,
            run_id=scope.run_id,
            kind=CatalogResourceKind.OPERATION,
            catalog_revision=await next_catalog_revision(session, scope.run_id),
            operation_id=operation_id,
        )

    async def _fail_prepared_operation(
        self,
        session,
        operation: CriticalOperationModel,
        *,
        error_code: str,
        error_message: str,
        error_phase: CriticalOperationErrorPhase,
        now,
    ) -> None:
        self._validate_failure(error_code, error_message, error_phase)
        operation.status = CriticalOperationStatus.FAILED.value
        operation.error_code = error_code
        operation.error_message = error_message
        operation.error_phase = error_phase.value
        operation.finished_at = now
        if operation.tool_name == _INITIALIZE_TOOL:
            detail = dict(operation.operation_detail_json or {})
            operation.operation_detail_json = IfcModelInitializeOperationDetail(
                project_name=detail.get("project_name"),
                project_global_id=detail.get("project_global_id"),
                result=None,
            ).model_dump(mode="json")
        await session.flush()
        scope = IfcRunIdentity(
            owner_id=operation.owner_id,
            tenant_id=operation.tenant_id,
            task_id=operation.task_id,
            run_id=operation.run_id,
        )
        await self._append_operation_catalog(session, scope, operation.operation_id)
        await session.flush()

    async def _initialize_result(
        self,
        session,
        operation: CriticalOperationModel,
        info: IfcModelInfo,
    ) -> IfcModelInitializeResult:
        entry = await session.scalar(
            select(DataCatalogEntryModel).where(
                DataCatalogEntryModel.run_id == operation.run_id,
                DataCatalogEntryModel.operation_id == operation.operation_id,
                DataCatalogEntryModel.resource_kind == CatalogResourceKind.OPERATION.value,
            )
        )
        if entry is None:
            raise IfcModelIntegrityError("terminal IFC Operation has no Catalog entry")
        return IfcModelInitializeResult(operation_alias=entry.alias, model=info)

    @staticmethod
    async def _require_operation_alias(
        session,
        operation: CriticalOperationModel,
    ) -> str:
        entry = await session.scalar(
            select(DataCatalogEntryModel).where(
                DataCatalogEntryModel.run_id == operation.run_id,
                DataCatalogEntryModel.operation_id == operation.operation_id,
                DataCatalogEntryModel.resource_kind == CatalogResourceKind.OPERATION.value,
            )
        )
        if entry is None:
            raise IfcModelIntegrityError("successful IFC Operation has no Catalog entry")
        return entry.alias

    async def _require_committed_publication(
        self,
        session,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
        publication: VerifiedIfcPublication,
    ) -> ExactIfcRevision:
        revision_id = await session.scalar(
            select(IfcModelRevisionModel.revision_id).where(
                IfcModelRevisionModel.owner_id == scope.owner_id,
                IfcModelRevisionModel.task_id == scope.task_id,
                IfcModelRevisionModel.run_id == scope.run_id,
                IfcModelRevisionModel.operation_id == operation_id,
            )
        )
        if revision_id is None:
            raise IfcModelIntegrityError("successful IFC Operation has no revision")
        row = await self._revision_row(session, scope, revision_id=revision_id)
        if row is None:
            raise IfcModelIntegrityError("successful IFC revision is unreadable")
        exact = self._exact_revision(scope, row)
        if exact.artifact != publication.artifact:
            raise IfcModelConflictError("IFC publication replay changed exact bytes")
        return exact

    @staticmethod
    def _assert_initialize_replay(
        operation: CriticalOperationModel,
        *,
        request_digest: str,
        project_global_id: str,
        project_name: str,
    ) -> None:
        if operation.tool_name != _INITIALIZE_TOOL:
            raise IfcModelConflictError("IFC initialization identity was reused")
        detail = dict(operation.operation_detail_json or {})
        if detail.get("project_global_id") != project_global_id:
            raise IfcModelConflictError("IFC initialization project identity changed")
        if operation.request_fingerprint != request_digest:
            # A completed create-only initialization rejects a new request;
            # this is neither a replay nor a concurrent CURRENT CAS attempt.
            if operation.status == CriticalOperationStatus.SUCCEEDED.value:
                raise IfcModelAlreadyInitializedError(
                    "the selected Run already has an IfcModel"
                )
            raise IfcModelConflictError("IFC initialization identity was reused")
        if detail.get("project_name") != project_name:
            raise IfcModelConflictError("IFC initialization project identity changed")

    @staticmethod
    def _assert_revision_replay(
        operation: CriticalOperationModel,
        *,
        tool_name: str,
        request_digest: str,
        intent_summary: str,
        operation_detail: Mapping[str, object],
    ) -> None:
        if (
            operation.operation_kind != CriticalOperationKind.IFC_MODEL_WRITE.value
            or operation.tool_name != tool_name
            or operation.request_fingerprint != request_digest
            or operation.intent_summary != intent_summary
            or operation.base_ifc_revision_id is None
            or operation.expected_ifc_write_epoch is None
        ):
            raise IfcInvocationIdentityConflictError(
                "IFC write idempotency identity was reused"
            )
        detail_type = {
            _TRUSTED_BASELINE_TOOL: IfcTrustedBaselineLoadOperationDetail,
            _GEOMETRY_BASIS_TOOL: IfcGeometryBasisConfigureOperationDetail,
            _PROJECT_UPDATE_TOOL: IfcProjectUpdateOperationDetail,
            _SPATIAL_STRUCTURE_APPLY_TOOL: IfcSpatialStructureApplyOperationDetail,
            _PRODUCTS_APPLY_TOOL: IfcProductsApplyOperationDetail,
            _PRODUCTS_EDIT_TOOL: IfcProductsEditOperationDetail,
        }.get(tool_name)
        if detail_type is not None:
            stored = detail_type.model_validate(operation.operation_detail_json)
            expected = detail_type.model_validate(operation_detail)
            if isinstance(stored, IfcTrustedBaselineLoadOperationDetail):
                changed = stored.source_asset != expected.source_asset
            else:
                changed = stored.request != expected.request
            if changed:
                raise IfcInvocationIdentityConflictError(
                    "IFC write request detail changed"
                )
            if isinstance(stored, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail)) and stored.input_binding != expected.input_binding:
                raise IfcInvocationIdentityConflictError("IFC product input binding changed")

    @staticmethod
    def _validate_revision_operation_detail(
        *,
        tool_name: str,
        operation_detail: Mapping[str, object],
    ) -> dict[str, object]:
        detail_type = {
            _TRUSTED_BASELINE_TOOL: IfcTrustedBaselineLoadOperationDetail,
            _GEOMETRY_BASIS_TOOL: IfcGeometryBasisConfigureOperationDetail,
            _PROJECT_UPDATE_TOOL: IfcProjectUpdateOperationDetail,
            _SPATIAL_STRUCTURE_APPLY_TOOL: IfcSpatialStructureApplyOperationDetail,
            _PRODUCTS_APPLY_TOOL: IfcProductsApplyOperationDetail,
            _PRODUCTS_EDIT_TOOL: IfcProductsEditOperationDetail,
        }.get(tool_name)
        if detail_type is not None:
            detail = detail_type.model_validate(operation_detail)
            if detail.result is not None:
                raise ValueError("prepared IFC write detail cannot contain a result")
            if (
                isinstance(detail, IfcSpatialStructureApplyOperationDetail)
                and detail.failure_blockers is not None
            ):
                raise ValueError("prepared spatial detail cannot contain failure blockers")
            return detail.persisted_payload()
        raise ValueError("trusted IFC revision operation detail is invalid")


    @classmethod
    def _assert_prepared_initialize_identity(
        cls,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize,
    ) -> None:
        identities = cls._initialize_identities(scope)
        if (
            prepared.scope != scope
            or prepared.operation_id != identities["operation_id"]
            or prepared.idempotency_key != identities["idempotency_key"]
            or prepared.model_id != identities["model_id"]
            or prepared.revision_id != identities["revision_id"]
            or prepared.artifact_id != identities["artifact_id"]
            or prepared.artifact_version_id != identities["artifact_version_id"]
        ):
            raise IfcModelConflictError("prepared IFC initialization identity changed")

    @classmethod
    def _assert_prepared_revision_identity(
        cls,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
    ) -> None:
        identities = cls._revision_identities(scope, prepared.idempotency_key)
        if (
            prepared.before.scope != scope
            or prepared.operation_id != identities["operation_id"]
            or prepared.after_revision_id != identities["revision_id"]
            or prepared.artifact_id != identities["artifact_id"]
            or prepared.artifact_version_id != identities["artifact_version_id"]
        ):
            raise IfcModelConflictError("prepared IFC revision identity changed")

    @staticmethod
    def _validate_initialize_publication(
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize,
        publication: VerifiedIfcPublication,
    ) -> None:
        if prepared.scope != scope or publication.scope != scope:
            raise IfcModelConflictError("IFC initialization scope changed")
        if (
            publication.artifact.artifact_id != prepared.artifact_id
            or publication.artifact.version_id != prepared.artifact_version_id
            or publication.inspection.project_global_id != prepared.project_global_id
        ):
            raise IfcModelConflictError("IFC initialization publication identity changed")

    @staticmethod
    def _validate_revision_publication(
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        publication: VerifiedIfcPublication,
    ) -> None:
        if prepared.before.scope != scope or publication.scope != scope:
            raise IfcModelConflictError("IFC revision scope changed")
        if (
            publication.artifact.artifact_id != prepared.artifact_id
            or publication.artifact.version_id != prepared.artifact_version_id
            or publication.inspection.project_global_id != prepared.before.project_global_id
        ):
            raise IfcModelConflictError("IFC revision publication identity changed")

    @staticmethod
    def _validate_revision_operation_input(
        *,
        tool_name: str,
        idempotency_key: str,
        request_digest: str,
        intent_summary: str,
    ) -> None:
        if tool_name not in _REVISION_TOOL_INTENTS:
            raise ValueError("trusted IFC revision tool_name is invalid")
        if not idempotency_key or len(idempotency_key) > 255:
            raise ValueError("IFC revision idempotency_key is invalid")
        if intent_summary != _REVISION_TOOL_INTENTS[tool_name]:
            raise ValueError("IFC revision intent_summary must use the stable tool template")
        PostgresIfcModelRepositoryMixin._validate_digest(request_digest)

    @staticmethod
    def _validate_digest(value: str) -> None:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("request_digest must be a lowercase SHA-256 digest")

    @staticmethod
    def _validate_failure(
        error_code: str,
        error_message: str,
        error_phase: CriticalOperationErrorPhase,
    ) -> None:
        if error_phase not in _IFC_FAILURE_PHASES:
            raise ValueError("IFC failure phase is invalid")
        if _ERROR_CODE_RE.fullmatch(error_code) is None:
            raise ValueError("IFC failure error_code is invalid")
        if not error_message or len(error_message.encode("utf-8")) > 2_000:
            raise ValueError("IFC failure error_message is invalid")

    @staticmethod
    def _validate_reason_code(reason_code: str) -> None:
        if _ERROR_CODE_RE.fullmatch(reason_code) is None:
            raise ValueError("IFC freeze reason_code is invalid")


__all__ = ["PostgresIfcModelRepositoryMixin"]
