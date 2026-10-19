"""PostgreSQL persistence for Direct sessions and immutable Data Packages."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from json import loads
from pathlib import PurePosixPath
from secrets import compare_digest, token_urlsafe
from typing import Mapping
from uuid import uuid4

from sqlalchemy import and_, cast, desc, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from CAD2BIM_backend.domain.control_plane import (
    ConflictError,
    ExecutionMode,
    ExecutionProtocolStatus,
    IdempotencyConflictError,
    RunStatus,
    TaskStatus,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.data_runtime import (
    ArrowColumnRecord,
    ArtifactVersionRef,
    AuthoringDraftWriteOperationDetail,
    AuthoringPublishOperationDetail,
    AuthoringExecutionClaim,
    AuthoringExecutionState,
    CatalogEntryPage,
    CatalogResourceKind,
    CadMeasurementOperationDetail,
    CriticalOperationErrorPhase,
    CriticalOperationInitiator,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    IfcProductsRecoveryOperation,
    DataCatalogEntry,
    DataPackageMemberRecord,
    DataPackageRecord,
    DataTransformCandidate,
    DataTransformExecutionClaim,
    DataTransformOperationState,
    DataTransformOperationDetail,
    IfcReadSourceRef,
    IfcSourceChangedError,
    DirectSessionBinding,
    IfcGeometryBasisConfigureOperationDetail,
    IfcModelInitializeOperationDetail,
    IfcProjectUpdateOperationDetail,
    IfcTrustedBaselineLoadOperationDetail,
    IfcSpatialStructureApplyOperationDetail,
    IfcProductsApplyOperationDetail,
    IfcProductsEditOperationDetail,
    GeometryInspectionOperationDetail,
    ProductFamilyWriteOperationDetail,
    ProductFamilyOperationSource,
    JsonPayloadKind,
    OpenDirectSessionRequest,
    OpenedDirectSession,
    PackageMemberKind,
    PublishedCriticalOperation,
    PublishedOperationCatalogSummary,
    CriticalOperationCatalogSummary,
    PublishedDataPackage,
    PublishedImportResult,
    PublishedPackageResult,
    SourceIngestionOperationDetail,
    TransformInputBindingRecord,
    validate_package_publication,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogReferenceError
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    CriticalOperationModel,
    CriticalOperationInputModel,
    DataCatalogEntryModel,
    DataPackageMemberModel,
    DataPackageModel,
    DirectSessionBindingModel,
    GeometryResourceVersionModel,
    GeometryResourceModel,
    ProductFamilyVersionModel,
    IfcModelRevisionModel,
    RunModel,
    TaskModel,
    TaskTenantBindingModel,
    TenantPrincipalModel,
    UserModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import (
    PostgresRepositoryBase,
    as_utc,
    database_now,
    fingerprint,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantContextMismatch,
    current_tenant_execution_context,
    set_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.data_transform.executor_identity import (
    data_transform_executor_stopped,
)
from CAD2BIM_backend.infrastructure.local_process_identity import (
    current_local_executor_owner,
    local_executor_stopped,
)


def _current_operation_contract_predicate(model=CriticalOperationModel):
    """Expose only current drafts, publication and the management SYSTEM copy."""
    detail = model.operation_detail_json
    current_draft = detail["detail_kind"].as_string() == "authoring_draft_write"
    current_copy = detail["system_copy"].as_string().is_not(None)
    return or_(
        and_(model.operation_kind == CriticalOperationKind.PRODUCT_FAMILY_WRITE.value,
             or_(current_draft, current_copy)),
        and_(model.operation_kind == CriticalOperationKind.GEOMETRY_RESOURCE_WRITE.value, current_draft),
        and_(model.operation_kind == CriticalOperationKind.IFC_MODEL_WRITE.value,
             or_(model.tool_name != "ifc_products_apply", and_(
                 detail["detail_kind"].as_string() == "ifc_products_apply",
                 detail["request"]["family_version_id"].as_string().is_not(None),
                 detail["input_binding"]["member_id"].as_string().is_not(None),
                 detail["request"]["package_id"].as_string()
                     == detail["input_binding"]["package_id"].as_string(),
                 detail["request"]["payload_name"].as_string()
                     == detail["input_binding"]["payload_name"].as_string(),
             ))),
        and_(model.operation_kind == CriticalOperationKind.AUTHORING_PUBLISH.value,
             detail["preview_examples"].is_not(None),
             ~cast(detail, JSONB).has_key("evidence")),
        model.operation_kind.not_in((
            CriticalOperationKind.PRODUCT_FAMILY_WRITE.value,
            CriticalOperationKind.GEOMETRY_RESOURCE_WRITE.value,
            CriticalOperationKind.IFC_MODEL_WRITE.value,
            CriticalOperationKind.AUTHORING_PUBLISH.value,
        )),
    )


class PostgresDataRuntimeRepositoryMixin(PostgresRepositoryBase):
    async def prepare_geometry_inspection_operation(
        self, scope: InvocationScope, prepared: CriticalOperationRecord,
    ) -> CriticalOperationRecord:
        prepared = CriticalOperationRecord.model_validate(prepared.model_dump(mode="python"))
        if (
            prepared.operation_kind is not CriticalOperationKind.GEOMETRY_INSPECTION
            or prepared.status is not CriticalOperationStatus.PREPARED
        ):
            raise ValueError("inspection admission requires its original PREPARED Operation")
        try:
            async with self._sessions() as session, session.begin():
                await set_tenant_execution_context(session, scope.identity)
                await self._require_data_run(session, scope)
                self._require_record_scope(scope, prepared)
                existing = await session.scalar(select(CriticalOperationModel).where(
                    CriticalOperationModel.owner_id == prepared.owner_id,
                    CriticalOperationModel.tenant_id == prepared.tenant_id,
                    CriticalOperationModel.task_id == prepared.task_id,
                    CriticalOperationModel.run_id == prepared.run_id,
                    CriticalOperationModel.idempotency_key == prepared.idempotency_key,
                ))
                if existing is not None:
                    stored = await self._operation_record(session, existing)
                    self._require_same_inspection_request(stored, prepared, admission=True)
                    return stored
                await self._require_inspection_artifacts(session, prepared)
                await self._require_inspection_source(session, prepared)
                session.add(self._operation_model(prepared))
                await session.flush()
                return prepared
        except IntegrityError as exc:
            existing = await self.get_authoring_execution_state(
                scope, prepared.operation_id,
                operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION,
            )
            if existing is not None:
                self._require_same_inspection_request(existing.operation, prepared, admission=True)
                return existing.operation
            raise ConflictError("geometry inspection admission conflicted with another Operation") from exc

    async def complete_geometry_inspection_operation(
        self, scope: InvocationScope, terminal: CriticalOperationRecord,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> PublishedCriticalOperation:
        terminal = CriticalOperationRecord.model_validate(terminal.model_dump(mode="python"))
        if (
            terminal.operation_kind is not CriticalOperationKind.GEOMETRY_INSPECTION
            or terminal.status not in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}
        ):
            raise ValueError("inspection completion requires its successful or failed terminal Operation")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope)
            self._require_record_scope(scope, terminal)
            model = await self._authoring_model(
                session, scope, terminal.operation_id,
                operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION, lock=True,
            )
            if model is None:
                raise ConflictError("original geometry inspection Operation is not visible")
            stored = await self._operation_record(session, model)
            self._require_same_inspection_request(stored, terminal)
            if stored.status in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}:
                if stored != terminal:
                    raise IdempotencyConflictError("geometry inspection terminal replay changed")
                published = await self._published_operation(session, model, run_id=scope.run_id)
                if published is None:
                    raise ConflictError("terminal geometry inspection Operation has no Catalog alias")
                return published
            self.require_authoring_claim(model, execution_claim)
            await self._require_inspection_artifacts(session, terminal)
            result = terminal.result_inspection_artifact
            model.status = terminal.status.value
            model.inspection_result_artifact_id = None if result is None else result.artifact_id
            model.inspection_result_artifact_version_id = None if result is None else result.version_id
            model.inspection_result_artifact_sha256 = None if result is None else result.sha256
            model.error_code = terminal.error_code
            model.error_message = terminal.error_message
            model.error_phase = None if terminal.error_phase is None else terminal.error_phase.value
            model.finished_at = terminal.finished_at
            model.authoring_execution_owner = None
            model.authoring_execution_token_sha256 = None
            entry = await append_catalog_entry(
                session, owner_id=terminal.owner_id, tenant_id=terminal.tenant_id,
                task_id=terminal.task_id, run_id=terminal.run_id,
                kind=CatalogResourceKind.OPERATION,
                catalog_revision=await next_catalog_revision(session, terminal.run_id),
                operation_id=terminal.operation_id,
            )
            await session.flush()
            return PublishedCriticalOperation(operation=terminal, catalog_entry=self._catalog_record(entry))

    @staticmethod
    def _require_same_inspection_request(stored, incoming, *, admission: bool = False) -> None:
        excluded = {
            "status", "result_inspection_artifact", "error_code", "error_message",
            "error_phase", "finished_at",
        }
        if admission:
            excluded.add("started_at")
        if stored.model_dump(mode="json", exclude=excluded) != incoming.model_dump(mode="json", exclude=excluded):
            raise IdempotencyConflictError("geometry inspection action identity was reused with different inputs")

    @staticmethod
    async def _require_inspection_source(session, operation) -> None:
        detail = operation.detail
        assert isinstance(detail, GeometryInspectionOperationDetail)
        if detail.source_type == "draft":
            reference = detail.source_draft_artifact
            statement = select(ArtifactVersionModel.version_id).join(ArtifactModel,
                ArtifactModel.artifact_id == ArtifactVersionModel.artifact_id).where(
                ArtifactVersionModel.tenant_id == operation.tenant_id,
                or_(and_(ArtifactVersionModel.owner_id == operation.owner_id,
                         ArtifactVersionModel.task_id == operation.task_id),
                    func.cad2bim_authoring_tenant_snapshot_visible(ArtifactVersionModel.version_id)),
                ArtifactVersionModel.artifact_id == reference.artifact_id,
                ArtifactVersionModel.version_id == reference.version_id,
                ArtifactVersionModel.sha256 == reference.sha256,
                ArtifactModel.kind == "authoring_draft")
            if await session.scalar(statement) is None:
                raise ConflictError("geometry inspection draft snapshot is not exact and visible")
            return
        if detail.source_kind == "geometry_sample":
            statement = select(GeometryResourceVersionModel.resource_version_id).join(
                GeometryResourceModel,
                GeometryResourceModel.resource_id == GeometryResourceVersionModel.resource_id,
            ).where(
                GeometryResourceVersionModel.resource_id == detail.source_id,
                GeometryResourceVersionModel.resource_version_id == detail.source_version_id,
                GeometryResourceVersionModel.content_digest == detail.source_content_digest,
            )
        else:
            statement = select(ProductFamilyVersionModel.family_version_id).where(
                ProductFamilyVersionModel.family_id == detail.source_id,
                ProductFamilyVersionModel.family_version_id == detail.source_version_id,
                ProductFamilyVersionModel.content_digest == detail.source_content_digest,
            )
        if await session.scalar(statement) is None:
            raise ConflictError("geometry inspection source is not the exact visible version")

    @staticmethod
    async def _require_inspection_artifacts(session, operation) -> None:
        detail = operation.detail
        assert isinstance(detail, GeometryInspectionOperationDetail)
        report = detail.result_kind == "case_report"
        for label, reference, kind, content_type in (
            ("request", detail.request_artifact, "geometry_inspection_request", "application/json"),
            ("result", operation.result_inspection_artifact,
             "geometry_preview_case_report" if report else "geometry_inspection",
             "application/json" if report else "application/vnd.cad2bim.geometry-inspection+zip"),
        ):
            if reference is None:
                continue
            row = (await session.execute(
                select(ArtifactVersionModel, ArtifactModel).join(
                    ArtifactModel,
                    (ArtifactModel.artifact_id == ArtifactVersionModel.artifact_id)
                    & (ArtifactModel.owner_id == ArtifactVersionModel.owner_id)
                    & (ArtifactModel.task_id == ArtifactVersionModel.task_id),
                ).where(
                    ArtifactVersionModel.owner_id == operation.owner_id,
                    ArtifactVersionModel.tenant_id == operation.tenant_id,
                    ArtifactVersionModel.task_id == operation.task_id,
                    ArtifactVersionModel.artifact_id == reference.artifact_id,
                    ArtifactVersionModel.version_id == reference.version_id,
                    ArtifactVersionModel.sha256 == reference.sha256,
                    ArtifactVersionModel.content_type == content_type,
                    ArtifactModel.run_id == operation.run_id,
                    ArtifactModel.kind == kind,
                    ArtifactModel.status.in_(("ready", "available")),
                )
            )).one_or_none()
            if row is None:
                raise ConflictError(f"geometry inspection {label} Artifact is not exact and visible")
            if label == "result":
                metadata = row[0].metadata_json
                if (
                    (not report and metadata.get("source_kind") != detail.source_kind)
                    or metadata.get("request_digest") != operation.request_fingerprint
                ):
                    raise ConflictError("geometry inspection result does not belong to the original request")

    async def get_authoring_execution_state(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        operation_kind: CriticalOperationKind,
    ) -> AuthoringExecutionState | None:
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            model = await self._authoring_model(
                session, scope, operation_id, operation_kind=operation_kind
            )
            return None if model is None else await self._authoring_state(session, model)

    async def claim_authoring_execution(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        operation_kind: CriticalOperationKind,
    ) -> AuthoringExecutionClaim | None:
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope)
            model = await self._authoring_model(
                session, scope, operation_id, operation_kind=operation_kind, lock=True
            )
            if (
                model is None
                or model.status not in {
                    CriticalOperationStatus.PREPARED.value,
                    CriticalOperationStatus.INDETERMINATE.value,
                }
                or model.authoring_execution_owner is not None
            ):
                return None
            return self._set_authoring_claim(model)

    async def reclaim_authoring_execution(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        operation_kind: CriticalOperationKind,
        expected_owner: str,
        expected_generation: int,
    ) -> AuthoringExecutionClaim | None:
        if expected_generation < 1:
            raise ValueError("authoring recovery requires a positive prior generation")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope)
            model = await self._authoring_model(
                session, scope, operation_id, operation_kind=operation_kind, lock=True
            )
            if (
                model is None
                or model.status not in {
                    CriticalOperationStatus.PREPARED.value,
                    CriticalOperationStatus.INDETERMINATE.value,
                }
                or model.authoring_execution_owner != expected_owner
                or model.authoring_execution_generation != expected_generation
                or not local_executor_stopped(expected_owner, kind="authoring")
            ):
                return None
            return self._set_authoring_claim(model)

    @staticmethod
    def _set_authoring_claim(model: CriticalOperationModel) -> AuthoringExecutionClaim:
        owner = current_local_executor_owner("authoring")
        token = token_urlsafe(32)
        model.authoring_execution_owner = owner
        model.authoring_execution_generation += 1
        model.authoring_execution_token_sha256 = sha256(token.encode("utf-8")).hexdigest()
        return AuthoringExecutionClaim(
            operation_id=model.operation_id,
            owner=owner,
            generation=model.authoring_execution_generation,
            token=token,
        )

    @staticmethod
    def require_authoring_claim(
        model: CriticalOperationModel, claim: AuthoringExecutionClaim
    ) -> None:
        if (
            model.operation_id != claim.operation_id
            or model.authoring_execution_owner != claim.owner
            or model.authoring_execution_generation != claim.generation
            or model.authoring_execution_token_sha256 is None
            or not compare_digest(
                model.authoring_execution_token_sha256,
                sha256(claim.token.encode("utf-8")).hexdigest(),
            )
        ):
            raise ConflictError("authoring execution claim is stale")

    @staticmethod
    async def _authoring_model(
        session,
        scope: InvocationScope,
        operation_id: str,
        *,
        operation_kind: CriticalOperationKind,
        lock: bool = False,
    ) -> CriticalOperationModel | None:
        tool_by_kind = {
            CriticalOperationKind.GEOMETRY_RESOURCE_WRITE: ("ifc_geometry_resource_write",),
            CriticalOperationKind.PRODUCT_FAMILY_WRITE: ("ifc_product_family_write",),
            CriticalOperationKind.GEOMETRY_INSPECTION: ("ifc_geometry_preview", "ifc_product_realization_preview"),
            CriticalOperationKind.AUTHORING_PUBLISH: ("ifc_authoring_publish",),
        }
        if operation_kind not in tool_by_kind:
            raise ValueError("execution claim requires a Geometry or Family build/publication kind")
        statement = select(CriticalOperationModel).where(
            CriticalOperationModel.owner_id == scope.owner_id,
            CriticalOperationModel.tenant_id == scope.identity.tenant_id,
            CriticalOperationModel.task_id == scope.identity.task_id,
            CriticalOperationModel.run_id == scope.run_id,
            CriticalOperationModel.operation_id == operation_id,
            CriticalOperationModel.operation_kind == operation_kind.value,
            CriticalOperationModel.tool_name.in_(tool_by_kind[operation_kind]),
            _current_operation_contract_predicate(),
        )
        if lock:
            statement = statement.with_for_update(of=CriticalOperationModel)
        return await session.scalar(statement)

    async def _authoring_state(
        self, session, model: CriticalOperationModel
    ) -> AuthoringExecutionState:
        return AuthoringExecutionState(
            operation=await self._operation_record(session, model),
            execution_owner=model.authoring_execution_owner,
            execution_generation=model.authoring_execution_generation,
        )

    async def open_direct_session(
        self,
        request: OpenDirectSessionRequest,
    ) -> OpenedDirectSession:
        self._require_direct_context(request)
        try:
            async with self._sessions() as session, session.begin():
                # Alias ordinals are scoped to one configured subject.  Serialize
                # allocation for that scope so two different names cannot both
                # select the same next direct_NNN ordinal.
                allocation_key = (
                    f"{len(request.tenant_id)}:{request.tenant_id}{request.configured_subject}"
                )
                await session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtext(allocation_key)))
                )
                existing = await session.scalar(
                    select(DirectSessionBindingModel).where(
                        DirectSessionBindingModel.tenant_id == request.tenant_id,
                        DirectSessionBindingModel.configured_subject == request.configured_subject,
                        DirectSessionBindingModel.normalized_session_name
                        == request.normalized_session_name,
                    )
                )
                if existing is not None:
                    return self._opened_session(existing, request, resumed=True)

                membership = await session.scalar(
                    select(TenantPrincipalModel).where(
                        TenantPrincipalModel.tenant_id == request.tenant_id,
                        TenantPrincipalModel.principal_id == request.principal_id,
                    )
                )
                owner = await session.scalar(
                    select(UserModel).where(
                        UserModel.owner_id == request.owner_id,
                        UserModel.tenant_id == request.tenant_id,
                    )
                )
                if membership is None or owner is None:
                    raise TenantContextMismatch("configured Direct identity is not resolved")

                now = await database_now(session)
                suffix = uuid4().hex
                task_id = f"task_{suffix}"
                run_id = f"run_{suffix}"
                session_id = f"direct_{uuid4().hex}"
                idempotency_key = f"direct-session:{session_id}"
                maximum = await session.scalar(
                    select(func.max(DirectSessionBindingModel.ordinal)).where(
                        DirectSessionBindingModel.tenant_id == request.tenant_id,
                        DirectSessionBindingModel.configured_subject == request.configured_subject,
                    )
                )
                ordinal = int(maximum or 0) + 1
                task = TaskModel(
                    task_id=task_id,
                    owner_id=request.owner_id,
                    tenant_id=request.tenant_id,
                    title=request.session_name,
                    instruction=None,
                    metadata_json={},
                    status=TaskStatus.RUNNING.value,
                    active_module="data_runtime",
                    workspace_uri=None,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint(request),
                    created_at=now,
                    updated_at=now,
                )
                run = RunModel(
                    run_id=run_id,
                    owner_id=request.owner_id,
                    tenant_id=request.tenant_id,
                    task_id=task_id,
                    module="data_runtime",
                    stage_key="direct",
                    input_payload={},
                    input_manifest=None,
                    input_manifest_digest=None,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint(request),
                    thread_id=f"thread_{run_id}",
                    execution_mode=ExecutionMode.DIRECT_MCP.value,
                    execution_protocol_status=ExecutionProtocolStatus.NOT_APPLICABLE.value,
                    execution_protocol=None,
                    execution_protocol_quarantine_reason=None,
                    status=RunStatus.RUNNING.value,
                    created_at=now,
                    updated_at=now,
                    started_at=now,
                )
                binding = DirectSessionBindingModel(
                    session_id=session_id,
                    direct_session_alias=f"direct_{ordinal:03d}",
                    ordinal=ordinal,
                    owner_id=request.owner_id,
                    tenant_id=request.tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    configured_subject=request.configured_subject,
                    session_name=request.session_name,
                    normalized_session_name=request.normalized_session_name,
                    principal_id=request.principal_id,
                    created_at=now,
                )
                session.add(task)
                await session.flush()
                session.add(
                    TaskTenantBindingModel(
                        tenant_id=request.tenant_id,
                        task_id=task_id,
                        principal_id=request.principal_id,
                        legacy_owner_id=request.owner_id,
                        created_at=now,
                    )
                )
                session.add(run)
                # The Direct binding has a composite FK to this exact Run.  Flush the
                # parent explicitly because no ORM relationship exists to teach the
                # unit-of-work sorter about that dependency.
                await session.flush()
                session.add(binding)
                await session.flush()
                return OpenedDirectSession(
                    binding=self._direct_session_record(binding),
                    resumed=False,
                )
        except IntegrityError as exc:
            async with self._sessions() as session:
                existing = await session.scalar(
                    select(DirectSessionBindingModel).where(
                        DirectSessionBindingModel.tenant_id == request.tenant_id,
                        DirectSessionBindingModel.configured_subject == request.configured_subject,
                        DirectSessionBindingModel.normalized_session_name
                        == request.normalized_session_name,
                    )
                )
                if existing is not None:
                    return self._opened_session(existing, request, resumed=True)
            raise ConflictError("Direct session could not be opened") from exc

    async def get_direct_session_by_alias(
        self,
        *,
        tenant_id: str,
        configured_subject: str,
        session_alias: str,
    ) -> OpenedDirectSession | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(DirectSessionBindingModel).where(
                    DirectSessionBindingModel.tenant_id == tenant_id,
                    DirectSessionBindingModel.configured_subject == configured_subject,
                    DirectSessionBindingModel.direct_session_alias == session_alias,
                )
            )
            if model is None:
                return None
            return OpenedDirectSession(
                binding=self._direct_session_record(model),
                resumed=True,
            )

    async def publish_import_result(
        self,
        scope: InvocationScope,
        package: DataPackageRecord,
        members: Sequence[DataPackageMemberRecord],
        operation: CriticalOperationRecord,
    ) -> PublishedImportResult:
        published = await self.publish_package_result(scope, package, members, operation)
        return PublishedImportResult(
            package=published.package,
            operation=published.operation,
        )

    async def get_data_transform_operation_by_identity(
        self, scope: InvocationScope, request_identity: str
    ) -> DataTransformOperationState | None:
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            model = await session.scalar(
                select(CriticalOperationModel).where(
                    CriticalOperationModel.owner_id == scope.owner_id,
                    CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                    CriticalOperationModel.task_id == scope.identity.task_id,
                    CriticalOperationModel.run_id == scope.run_id,
                    CriticalOperationModel.transform_request_identity == request_identity,
                    CriticalOperationModel.tool_name == "data_derive",
                )
            )
            return await self._transform_state(session, model) if model is not None else None

    async def prepare_data_transform_operation(
        self,
        scope: InvocationScope,
        prepared: CriticalOperationRecord,
        *,
        request_identity: str,
        request_digest: str,
        request_payload: Mapping[str, object],
        resolved_command: Mapping[str, object],
        resolved_parent_operation_alias: str | None = None,
        deadline_at: datetime,
    ) -> DataTransformOperationState:
        if prepared.status is not CriticalOperationStatus.PREPARED:
            raise ValueError("Data Transform admission requires PREPARED")
        if prepared.tool_name != "data_derive":
            raise ValueError("Data Transform admission requires its exact tool")
        if sha256_digest(request_payload) != request_digest:
            raise ValueError("Data Transform request digest does not match the declared request")
        if deadline_at <= prepared.started_at:
            raise ValueError("Data Transform deadline must follow admission")
        declared = loads(canonical_json(request_payload))
        resolved = loads(canonical_json(resolved_command))

        async def insert_or_read() -> DataTransformOperationState:
            async with self._sessions() as session, session.begin():
                await self._require_data_run(session, scope)
                self._require_record_scope(scope, prepared)
                existing = await session.scalar(
                    select(CriticalOperationModel).where(
                        CriticalOperationModel.run_id == scope.run_id,
                        CriticalOperationModel.transform_request_identity == request_identity,
                    )
                )
                if existing is not None:
                    if existing.tool_name != "data_derive":
                        raise ConflictError("The original producer is retired; create a new data derivation")
                    self._assert_transform_request(existing, request_digest, declared)
                    return await self._transform_state(session, existing)
                await self._require_operation_artifacts(session, prepared)
                await self._require_repair_parent(session, scope, prepared)
                await self._require_ifc_read_source(session, scope, prepared)
                await self._require_transform_inputs(session, scope, prepared)
                model = self._operation_model(prepared)
                model.transform_request_identity = request_identity
                model.transform_request_digest = request_digest
                model.transform_request_json = declared
                model.transform_resolved_json = {
                    "command": resolved,
                    "parent_operation_alias": resolved_parent_operation_alias,
                }
                model.transform_deadline_at = deadline_at
                session.add(model)
                await session.flush()
                session.add_all(self._operation_input_models(prepared))
                await session.flush()
                return await self._transform_state(session, model)

        try:
            return await insert_or_read()
        except IntegrityError:
            existing = await self.get_data_transform_operation_by_identity(scope, request_identity)
            if existing is None:
                raise ConflictError("Data Transform admission conflicted with another Operation")
            if existing.request_digest != request_digest or existing.request_payload != declared:
                raise IdempotencyConflictError(
                    "Data Transform request identity was reused with different input"
                )
            return existing

    async def claim_data_transform_execution(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        owner: str,
        lease_seconds: float,
    ) -> DataTransformExecutionClaim | None:
        if not owner or len(owner) > 160 or lease_seconds <= 0:
            raise ValueError("Data Transform execution claim is invalid")
        async with self._sessions() as session, session.begin():
            await self._require_data_run(session, scope)
            model = await self._transform_model(session, scope, operation_id, lock=True)
            if model is None:
                return None
            now = await database_now(session)
            if (
                model.status != CriticalOperationStatus.PREPARED.value
                or model.transform_execution_owner is not None
                or as_utc(model.transform_deadline_at) <= now
            ):
                return None
            token = token_urlsafe(32)
            model.transform_execution_owner = owner
            model.transform_execution_generation += 1
            model.transform_execution_lease_expires_at = now + timedelta(seconds=lease_seconds)
            model.transform_execution_token_sha256 = sha256(token.encode("utf-8")).hexdigest()
            await session.flush()
            return DataTransformExecutionClaim(
                operation_id=operation_id,
                owner=owner,
                generation=model.transform_execution_generation,
                token=token,
            )

    async def reclaim_data_transform_execution(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        expected_owner: str,
        expected_generation: int,
        owner: str,
        lease_seconds: float,
    ) -> DataTransformExecutionClaim | None:
        if not owner or len(owner) > 160 or expected_generation < 1 or lease_seconds <= 0:
            raise ValueError("Data Transform recovery claim is invalid")
        async with self._sessions() as session, session.begin():
            await self._require_data_run(session, scope)
            model = await self._transform_model(session, scope, operation_id, lock=True)
            if model is None or model.status != CriticalOperationStatus.PREPARED.value:
                return None
            if (
                model.transform_execution_owner != expected_owner
                or model.transform_execution_generation != expected_generation
                or not data_transform_executor_stopped(expected_owner)
            ):
                return None
            now = await database_now(session)
            if model.transform_candidate_json is None and as_utc(model.transform_deadline_at) <= now:
                return None
            token = token_urlsafe(32)
            model.transform_execution_owner = owner
            model.transform_execution_generation += 1
            model.transform_execution_lease_expires_at = now + timedelta(seconds=lease_seconds)
            model.transform_execution_token_sha256 = sha256(token.encode("utf-8")).hexdigest()
            await session.flush()
            return DataTransformExecutionClaim(
                operation_id=operation_id,
                owner=owner,
                generation=model.transform_execution_generation,
                token=token,
            )

    async def expire_data_transform_operation(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        expected_owner: str | None,
        expected_generation: int,
        failed_operation: CriticalOperationRecord,
    ) -> DataTransformOperationState | None:
        if (
            expected_generation < 0
            or failed_operation.operation_id != operation_id
            or failed_operation.status is not CriticalOperationStatus.FAILED
            or failed_operation.error_code != "TRANSFORM_TIMEOUT"
            or failed_operation.error_phase is not CriticalOperationErrorPhase.EXECUTION
        ):
            raise ValueError("expired Data Transform requires its original timeout failure")
        async with self._sessions() as session, session.begin():
            await self._require_data_run(session, scope)
            model = await self._transform_model(session, scope, operation_id, lock=True)
            if model is None or model.status != CriticalOperationStatus.PREPARED.value:
                return None
            now = await database_now(session)
            if (
                model.transform_candidate_json is not None
                or as_utc(model.transform_deadline_at) > now
                or model.transform_execution_owner != expected_owner
                or model.transform_execution_generation != expected_generation
                or (
                    expected_owner is None
                    and (expected_generation != 0 or model.transform_execution_token_sha256 is not None)
                )
                or (
                    expected_owner is not None
                    and not data_transform_executor_stopped(expected_owner)
                )
            ):
                return None
            original = await self._operation_record(session, model)
            self._assert_transform_terminal_matches(original, failed_operation)
            model.status = CriticalOperationStatus.FAILED.value
            model.error_code = failed_operation.error_code
            model.error_message = failed_operation.error_message
            model.error_phase = failed_operation.error_phase.value
            model.finished_at = failed_operation.finished_at
            await session.flush()
            await append_catalog_entry(
                session,
                owner_id=original.owner_id,
                tenant_id=original.tenant_id,
                task_id=original.task_id,
                run_id=original.run_id,
                kind=CatalogResourceKind.OPERATION,
                catalog_revision=await next_catalog_revision(session, original.run_id),
                operation_id=operation_id,
            )
            await session.flush()
            return await self._transform_state(session, model)

    async def stage_data_transform_candidate(
        self,
        scope: InvocationScope,
        operation_id: str,
        package: DataPackageRecord,
        members: Sequence[DataPackageMemberRecord],
        *,
        execution_claim: DataTransformExecutionClaim,
    ) -> DataTransformOperationState:
        member_tuple = tuple(members)
        async with self._sessions() as session, session.begin():
            await self._require_data_run(session, scope)
            model = await self._transform_model(session, scope, operation_id, lock=True)
            if model is None or model.status != CriticalOperationStatus.PREPARED.value:
                raise ConflictError("Data Transform Operation is not prepared")
            self._require_transform_claim(model, execution_claim)
            original = await self._operation_record(session, model)
            proposed = CriticalOperationRecord.model_validate(
                original.model_dump(mode="python") | {
                    "status": CriticalOperationStatus.SUCCEEDED,
                    "result_package_id": package.package_id,
                    "finished_at": await database_now(session),
                }
            )
            validate_package_publication(package, member_tuple, proposed)
            if (
                package.owner_id != scope.owner_id
                or package.tenant_id != scope.identity.tenant_id
                or package.task_id != scope.identity.task_id
                or package.created_by_run_id != scope.run_id
            ):
                raise ConflictError("Data Transform candidate Package does not match scope")
            await self._require_exact_member_artifacts(session, member_tuple)
            candidate = {
                "package": package.model_dump(mode="json"),
                "members": [item.model_dump(mode="json") for item in member_tuple],
            }
            if model.transform_candidate_json is not None:
                if model.transform_candidate_json != candidate:
                    raise ConflictError("Data Transform candidate changed for the same Operation")
            else:
                model.transform_candidate_json = candidate
                await session.flush()
            return await self._transform_state(session, model)

    async def get_result_for_operation(
        self, scope: InvocationScope, operation_id: str
    ) -> PublishedPackageResult | None:
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            model = await self._transform_model(session, scope, operation_id)
            if model is None or model.status != CriticalOperationStatus.SUCCEEDED.value:
                return None
            return await self._published_package_result(session, model)

    async def get_input_package_for_operation(
        self, scope: InvocationScope, operation_id: str, package_id: str
    ) -> PublishedDataPackage | None:
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            operation = await self._transform_model(session, scope, operation_id)
            if operation is None:
                return None
            bound = await session.scalar(
                select(CriticalOperationInputModel.input_binding_id).where(
                    CriticalOperationInputModel.operation_id == operation_id,
                    CriticalOperationInputModel.package_id == package_id,
                ).limit(1)
            )
            if bound is None:
                return None
            package = await session.scalar(
                select(DataPackageModel).where(
                    DataPackageModel.owner_id == scope.owner_id,
                    DataPackageModel.tenant_id == scope.identity.tenant_id,
                    DataPackageModel.task_id == scope.identity.task_id,
                    DataPackageModel.package_id == package_id,
                )
            )
            return (
                await self._published_package(session, package, run_id=scope.run_id)
                if package is not None
                else None
            )

    async def publish_package_result(
        self,
        scope: InvocationScope,
        package: DataPackageRecord,
        members: Sequence[DataPackageMemberRecord],
        operation: CriticalOperationRecord,
        *,
        execution_claim: DataTransformExecutionClaim | None = None,
    ) -> PublishedPackageResult:
        if operation.status is not CriticalOperationStatus.SUCCEEDED:
            raise ValueError("package result requires a succeeded operation")
        if operation.result_package_id != package.package_id:
            raise ValueError("operation result package does not match package")
        member_tuple = tuple(members)
        validate_package_publication(package, member_tuple, operation)
        if operation.tool_name in {"data_transform", "data_repair_transform"}:
            raise ValueError("retired transform producers cannot publish a new Package")
        if operation.tool_name == "data_derive":
            if execution_claim is None:
                raise ConflictError("Data Transform publication requires its execution claim")
            return await self._publish_transform_result(
                scope, package, member_tuple, operation, execution_claim
            )
        if execution_claim is not None:
            raise ValueError("execution claim is only valid for Data Transform")
        try:
            async with self._sessions() as session, session.begin():
                await self._require_data_run(
                    session,
                    scope,
                )
                if (
                    operation.run_id != package.created_by_run_id
                    or operation.owner_id != package.owner_id
                    or operation.tenant_id != package.tenant_id
                    or operation.task_id != package.task_id
                ):
                    raise ValueError("package and operation must belong to the same Run")
                self._require_record_scope(scope, operation)
                await self._require_operation_artifacts(session, operation)
                await self._require_repair_parent(session, scope, operation)
                await self._require_transform_inputs(session, scope, operation)
                replay = await session.scalar(
                    select(CriticalOperationModel).where(
                        CriticalOperationModel.run_id == operation.run_id,
                        CriticalOperationModel.idempotency_key == operation.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_operation_replay(replay, operation)
                    return await self._published_package_result(session, replay)
                fingerprint_replay = await session.scalar(
                    select(CriticalOperationModel).where(
                        CriticalOperationModel.run_id == operation.run_id,
                        CriticalOperationModel.tool_name == operation.tool_name,
                        CriticalOperationModel.request_fingerprint == operation.request_fingerprint,
                    )
                )
                if fingerprint_replay is not None:
                    return await self._published_package_result(session, fingerprint_replay)

                package_replay = await session.scalar(
                    select(DataPackageModel).where(
                        DataPackageModel.task_id == package.task_id,
                        DataPackageModel.idempotency_key == package.idempotency_key,
                    )
                )
                if package_replay is not None:
                    self._assert_package_replay(package_replay, package)
                    raise ConflictError("package exists without the requested operation")

                await self._require_exact_member_artifacts(session, member_tuple)
                package_model = self._package_model(package)
                session.add(package_model)
                # Package members and the succeeded Operation both reference this
                # exact Package through composite FKs.  These models intentionally
                # have no ORM relationships, so establish the parent first.
                await session.flush()
                session.add_all(self._member_model(member) for member in member_tuple)
                await session.flush()
                operation_model = self._operation_model(operation)
                session.add(operation_model)
                await session.flush()
                session.add_all(self._operation_input_models(operation))
                await session.flush()
                package_entry = await append_catalog_entry(
                    session,
                    owner_id=package.owner_id,
                    tenant_id=package.tenant_id,
                    task_id=package.task_id,
                    run_id=operation.run_id,
                    kind=CatalogResourceKind.PACKAGE,
                    catalog_revision=await next_catalog_revision(session, operation.run_id),
                    package_id=package.package_id,
                )
                operation_entry = await append_catalog_entry(
                    session,
                    owner_id=operation.owner_id,
                    tenant_id=operation.tenant_id,
                    task_id=operation.task_id,
                    run_id=operation.run_id,
                    kind=CatalogResourceKind.OPERATION,
                    catalog_revision=package_entry.catalog_revision,
                    operation_id=operation.operation_id,
                )
                await session.flush()
                return PublishedPackageResult(
                    package=PublishedDataPackage(
                        package=package,
                        members=member_tuple,
                        catalog_entry=self._catalog_record(package_entry),
                    ),
                    operation=PublishedCriticalOperation(
                        operation=operation,
                        catalog_entry=self._catalog_record(operation_entry),
                    ),
                )
        except IntegrityError as exc:
            async with self._sessions() as session:
                replay = await session.scalar(
                    select(CriticalOperationModel).where(
                        CriticalOperationModel.run_id == operation.run_id,
                        CriticalOperationModel.idempotency_key == operation.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_operation_replay(replay, operation)
                    return await self._published_package_result(session, replay)
                fingerprint_replay = await session.scalar(
                    select(CriticalOperationModel).where(
                        CriticalOperationModel.run_id == operation.run_id,
                        CriticalOperationModel.tool_name == operation.tool_name,
                        CriticalOperationModel.request_fingerprint == operation.request_fingerprint,
                    )
                )
                if fingerprint_replay is not None:
                    return await self._published_package_result(session, fingerprint_replay)
            raise ConflictError("import result could not be published") from exc

    async def record_failed_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *,
        execution_claim: DataTransformExecutionClaim | None = None,
    ) -> PublishedCriticalOperation:
        if operation.status is not CriticalOperationStatus.FAILED:
            raise ValueError("record_failed_operation requires failed status")
        if operation.tool_name in {"data_transform", "data_repair_transform"}:
            raise ValueError("retired transform producers cannot record a new failure")
        if operation.tool_name == "data_derive":
            if execution_claim is None:
                raise ConflictError("Data Transform failure requires its execution claim")
            return await self._fail_transform_operation(scope, operation, execution_claim)
        if execution_claim is not None:
            raise ValueError("execution claim is only valid for Data Transform")
        try:
            async with self._sessions() as session, session.begin():
                await self._require_data_run(
                    session,
                    scope,
                )
                self._require_record_scope(scope, operation)
                await self._require_operation_artifacts(session, operation)
                await self._require_repair_parent(session, scope, operation)
                await self._require_transform_inputs(session, scope, operation)
                replay = await session.scalar(
                    select(CriticalOperationModel).where(
                        CriticalOperationModel.run_id == operation.run_id,
                        CriticalOperationModel.idempotency_key == operation.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_operation_replay(replay, operation)
                    return await self._require_published_operation(session, replay)
                fingerprint_replay = await session.scalar(
                    select(CriticalOperationModel).where(
                        CriticalOperationModel.run_id == operation.run_id,
                        CriticalOperationModel.tool_name == operation.tool_name,
                        CriticalOperationModel.request_fingerprint == operation.request_fingerprint,
                    )
                )
                if fingerprint_replay is not None:
                    return await self._require_published_operation(session, fingerprint_replay)
                model = self._operation_model(operation)
                session.add(model)
                await session.flush()
                session.add_all(self._operation_input_models(operation))
                await session.flush()
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
                    catalog_entry=self._catalog_record(entry),
                )
        except IntegrityError as exc:
            async with self._sessions() as session:
                replay = await session.scalar(
                    select(CriticalOperationModel).where(
                        CriticalOperationModel.run_id == operation.run_id,
                        CriticalOperationModel.tool_name == operation.tool_name,
                        CriticalOperationModel.request_fingerprint == operation.request_fingerprint,
                    )
                )
                if replay is not None:
                    return await self._require_published_operation(session, replay)
            raise ConflictError("failed operation could not be recorded") from exc

    async def _publish_transform_result(
        self,
        scope: InvocationScope,
        package: DataPackageRecord,
        members: tuple[DataPackageMemberRecord, ...],
        operation: CriticalOperationRecord,
        claim: DataTransformExecutionClaim,
    ) -> PublishedPackageResult:
        async with self._sessions() as session, session.begin():
            await self._require_data_run(session, scope)
            model = await self._transform_model(session, scope, operation.operation_id, lock=True)
            if model is None:
                raise ConflictError("Data Transform Operation was not admitted")
            self._require_transform_claim(model, claim)
            if model.status == CriticalOperationStatus.SUCCEEDED.value:
                existing = await self._operation_record(session, model)
                self._assert_transform_terminal_matches(existing, operation)
                return await self._published_package_result(session, model)
            if model.status != CriticalOperationStatus.PREPARED.value:
                raise ConflictError("Data Transform Operation is already terminal")
            original = await self._operation_record(session, model)
            self._assert_transform_terminal_matches(original, operation)
            await self._require_ifc_read_source(session, scope, original, publication=True)
            candidate = model.transform_candidate_json
            if candidate is None or candidate != {
                "package": package.model_dump(mode="json"),
                "members": [item.model_dump(mode="json") for item in members],
            }:
                raise ConflictError("Data Transform candidate is not the admitted exact output")
            self._require_record_scope(scope, operation)
            await self._require_exact_member_artifacts(session, members)
            session.add(self._package_model(package))
            await session.flush()
            session.add_all(self._member_model(member) for member in members)
            await session.flush()
            model.status = CriticalOperationStatus.SUCCEEDED.value
            model.result_package_id = package.package_id
            model.finished_at = operation.finished_at
            await session.flush()
            package_entry = await append_catalog_entry(
                session,
                owner_id=package.owner_id,
                tenant_id=package.tenant_id,
                task_id=package.task_id,
                run_id=operation.run_id,
                kind=CatalogResourceKind.PACKAGE,
                catalog_revision=await next_catalog_revision(session, operation.run_id),
                package_id=package.package_id,
            )
            operation_entry = await append_catalog_entry(
                session,
                owner_id=operation.owner_id,
                tenant_id=operation.tenant_id,
                task_id=operation.task_id,
                run_id=operation.run_id,
                kind=CatalogResourceKind.OPERATION,
                catalog_revision=package_entry.catalog_revision,
                operation_id=operation.operation_id,
            )
            await session.flush()
            return PublishedPackageResult(
                package=PublishedDataPackage(
                    package=package,
                    members=members,
                    catalog_entry=self._catalog_record(package_entry),
                ),
                operation=PublishedCriticalOperation(
                    operation=operation,
                    catalog_entry=self._catalog_record(operation_entry),
                ),
            )

    async def _fail_transform_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        claim: DataTransformExecutionClaim,
    ) -> PublishedCriticalOperation:
        async with self._sessions() as session, session.begin():
            await self._require_data_run(session, scope)
            model = await self._transform_model(session, scope, operation.operation_id, lock=True)
            if model is None:
                raise ConflictError("Data Transform Operation was not admitted")
            self._require_transform_claim(model, claim)
            if model.status == CriticalOperationStatus.FAILED.value:
                existing = await self._operation_record(session, model)
                self._assert_transform_terminal_matches(existing, operation)
                return await self._require_published_operation(session, model)
            if model.status != CriticalOperationStatus.PREPARED.value:
                raise ConflictError("Data Transform Operation is already terminal")
            original = await self._operation_record(session, model)
            self._assert_transform_terminal_matches(original, operation)
            if model.transform_candidate_json is not None:
                if not (
                    operation.error_code == IfcSourceChangedError.code
                    and operation.error_phase is CriticalOperationErrorPhase.COMMIT_FENCE
                    and original.repair_parent_operation_id is not None
                    and isinstance(original.detail, DataTransformOperationDetail)
                    and original.detail.ifc_source is not None
                ):
                    raise ConflictError("Data Transform candidate must be reconciled before failure")
                try:
                    await self._require_ifc_read_source(session, scope, original, publication=True)
                except IfcSourceChangedError:
                    # This exact candidate cannot pass its publication fence.
                    # Discard it and record the failure under the same Run/model locks.
                    model.transform_candidate_json = None
                else:
                    raise ConflictError("The admitted IFC candidate is still eligible for publication")
            model.status = CriticalOperationStatus.FAILED.value
            model.error_code = operation.error_code
            model.error_message = operation.error_message
            model.error_phase = operation.error_phase.value if operation.error_phase else None
            model.finished_at = operation.finished_at
            await session.flush()
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
            return PublishedCriticalOperation(operation=operation, catalog_entry=self._catalog_record(entry))

    @staticmethod
    def _assert_transform_terminal_matches(
        original: CriticalOperationRecord, requested: CriticalOperationRecord
    ) -> None:
        terminal_fields = {
            "status", "result_package_id", "finished_at", "error_code", "error_message",
            "error_phase",
        }
        if original.model_dump(mode="json", exclude=terminal_fields) != requested.model_dump(
            mode="json", exclude=terminal_fields
        ):
            raise ConflictError("Data Transform terminal Operation differs from admission")
        if original.status is not CriticalOperationStatus.PREPARED and original != requested:
            raise ConflictError("Data Transform terminal result changed")

    @staticmethod
    def _assert_transform_request(model, digest: str, payload: dict[str, object]) -> None:
        if model.transform_request_digest != digest or model.transform_request_json != payload:
            raise IdempotencyConflictError("Data Transform request identity was reused with different input")

    @staticmethod
    def _require_transform_claim(model, claim: DataTransformExecutionClaim) -> None:
        if (
            model.operation_id != claim.operation_id
            or model.transform_execution_owner != claim.owner
            or model.transform_execution_generation != claim.generation
            or model.transform_execution_token_sha256 is None
            or not compare_digest(
                model.transform_execution_token_sha256,
                sha256(claim.token.encode("utf-8")).hexdigest(),
            )
        ):
            raise ConflictError("Data Transform execution claim is stale")

    @staticmethod
    async def _transform_model(session, scope, operation_id, *, lock=False):
        statement = select(CriticalOperationModel).where(
            CriticalOperationModel.owner_id == scope.owner_id,
            CriticalOperationModel.tenant_id == scope.identity.tenant_id,
            CriticalOperationModel.task_id == scope.identity.task_id,
            CriticalOperationModel.run_id == scope.run_id,
            CriticalOperationModel.operation_id == operation_id,
            CriticalOperationModel.tool_name == "data_derive",
            CriticalOperationModel.transform_request_identity.is_not(None),
        )
        if lock:
            statement = statement.with_for_update(of=CriticalOperationModel)
        return await session.scalar(statement)

    async def _transform_state(self, session, model) -> DataTransformOperationState:
        resolved = dict(model.transform_resolved_json or {})
        candidate_json = model.transform_candidate_json
        candidate = None
        if candidate_json is not None:
            candidate = DataTransformCandidate(
                package=DataPackageRecord.model_validate(candidate_json["package"]),
                members=tuple(
                    DataPackageMemberRecord.model_validate(item)
                    for item in candidate_json["members"]
                ),
            )
        return DataTransformOperationState(
            operation=await self._operation_record(session, model),
            request_identity=model.transform_request_identity,
            request_digest=model.transform_request_digest,
            request_payload=dict(model.transform_request_json),
            resolved_command=dict(resolved["command"]),
            resolved_parent_operation_alias=resolved.get("parent_operation_alias"),
            deadline_at=as_utc(model.transform_deadline_at),
            execution_owner=model.transform_execution_owner,
            execution_generation=model.transform_execution_generation,
            execution_lease_expires_at=as_utc(model.transform_execution_lease_expires_at),
            execution_token_sha256=model.transform_execution_token_sha256,
            candidate=candidate,
        )

    async def get_package(
        self,
        scope: InvocationScope,
        package_id: str,
    ) -> PublishedDataPackage | None:
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            package = await session.scalar(
                select(DataPackageModel).where(
                    DataPackageModel.owner_id == scope.owner_id,
                    DataPackageModel.task_id == scope.identity.task_id,
                    DataPackageModel.package_id == package_id,
                )
            )
            if package is None:
                return None
            return await self._published_package(
                session,
                package,
                run_id=scope.run_id,
                frontier=scope.catalog_frontier,
            )

    async def list_catalog(
        self,
        scope: InvocationScope,
        resource_kind: CatalogResourceKind | None = None,
    ) -> tuple[DataCatalogEntry, ...]:
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            statement = select(DataCatalogEntryModel).where(
                DataCatalogEntryModel.owner_id == scope.owner_id,
                DataCatalogEntryModel.run_id == scope.run_id,
            )
            if resource_kind is None:
                statement = statement.outerjoin(
                    CriticalOperationModel,
                    and_(
                        CriticalOperationModel.owner_id == DataCatalogEntryModel.owner_id,
                        CriticalOperationModel.task_id == DataCatalogEntryModel.task_id,
                        CriticalOperationModel.run_id == DataCatalogEntryModel.run_id,
                        CriticalOperationModel.operation_id
                        == DataCatalogEntryModel.operation_id,
                    ),
                ).where(
                    or_(
                        DataCatalogEntryModel.resource_kind
                        != CatalogResourceKind.OPERATION.value,
                        _current_operation_contract_predicate(),
                    )
                )
            elif resource_kind is CatalogResourceKind.OPERATION:
                statement = statement.join(
                    CriticalOperationModel,
                    and_(
                        CriticalOperationModel.owner_id == DataCatalogEntryModel.owner_id,
                        CriticalOperationModel.task_id == DataCatalogEntryModel.task_id,
                        CriticalOperationModel.run_id == DataCatalogEntryModel.run_id,
                        CriticalOperationModel.operation_id
                        == DataCatalogEntryModel.operation_id,
                    ),
                ).where(_current_operation_contract_predicate())
            if scope.catalog_frontier is not None:
                statement = statement.where(
                    DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier
                )
            if resource_kind is not None:
                statement = statement.where(
                    DataCatalogEntryModel.resource_kind == resource_kind.value
                )
            models = (
                await session.scalars(
                    statement.order_by(
                        DataCatalogEntryModel.created_at,
                        DataCatalogEntryModel.catalog_entry_id,
                    )
                )
            ).all()
            return tuple(self._catalog_record(model) for model in models)

    async def list_package_catalog(
        self,
        scope: InvocationScope,
        before_sequence: int | None = None,
        page_size: int = 20,
    ) -> CatalogEntryPage:
        return await self._list_catalog_page(
            scope,
            CatalogResourceKind.PACKAGE,
            before_sequence,
            page_size,
        )

    async def list_operation_catalog(
        self,
        scope: InvocationScope,
        before_sequence: int | None = None,
        page_size: int = 20,
        operation_kind: CriticalOperationKind | None = None,
        *, tool_name: str | None = None, outcome: CriticalOperationStatus | None = None,
    ) -> CatalogEntryPage:
        return await self._list_catalog_page(
            scope,
            CatalogResourceKind.OPERATION,
            before_sequence,
            page_size,
            operation_kind=operation_kind,
            operation_tool_names=None if tool_name is None else (tool_name,),
            operation_statuses=None if outcome is None else (outcome,),
        )

    async def list_terminal_operation_catalog(
        self,
        scope: InvocationScope,
        *,
        tool_names: Sequence[str],
        statuses: Sequence[CriticalOperationStatus],
        require_code_artifact: bool = False,
        before_sequence: int | None = None,
        page_size: int = 20,
    ) -> CatalogEntryPage:
        if not tool_names or any(not tool_name for tool_name in tool_names):
            raise ValueError("tool_names must contain at least one non-empty tool name")
        terminal_statuses = {
            CriticalOperationStatus.SUCCEEDED,
            CriticalOperationStatus.FAILED,
        }
        if not statuses or any(status not in terminal_statuses for status in statuses):
            raise ValueError("statuses must contain succeeded and/or failed")
        return await self._list_catalog_page(
            scope,
            CatalogResourceKind.OPERATION,
            before_sequence,
            page_size,
            operation_tool_names=tuple(dict.fromkeys(tool_names)),
            operation_statuses=tuple(dict.fromkeys(statuses)),
            operation_requires_code_artifact=require_code_artifact,
        )

    async def _list_catalog_page(
        self,
        scope: InvocationScope,
        resource_kind: CatalogResourceKind,
        before_sequence: int | None,
        page_size: int,
        *,
        operation_kind: CriticalOperationKind | None = None,
        operation_tool_names: Sequence[str] | None = None,
        operation_statuses: Sequence[CriticalOperationStatus] | None = None,
        operation_requires_code_artifact: bool = False,
    ) -> CatalogEntryPage:
        if before_sequence is not None and before_sequence < 1:
            raise ValueError("before_sequence must be positive")
        if not 1 <= page_size <= 50:
            raise ValueError("page_size must be between 1 and 50")
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            statement = select(DataCatalogEntryModel).where(
                DataCatalogEntryModel.owner_id == scope.owner_id,
                DataCatalogEntryModel.task_id == scope.identity.task_id,
                DataCatalogEntryModel.run_id == scope.run_id,
                DataCatalogEntryModel.resource_kind == resource_kind.value,
            )
            has_operation_filters = (
                operation_kind is not None
                or operation_tool_names is not None
                or operation_statuses is not None
                or operation_requires_code_artifact
            )
            if has_operation_filters and resource_kind is not CatalogResourceKind.OPERATION:
                raise ValueError("Operation filters require an Operation catalog")
            if resource_kind is CatalogResourceKind.OPERATION:
                statement = statement.join(
                    CriticalOperationModel,
                    and_(
                        CriticalOperationModel.owner_id == DataCatalogEntryModel.owner_id,
                        CriticalOperationModel.task_id == DataCatalogEntryModel.task_id,
                        CriticalOperationModel.run_id == DataCatalogEntryModel.run_id,
                        CriticalOperationModel.operation_id
                        == DataCatalogEntryModel.operation_id,
                    ),
                )
                statement = statement.where(
                    CriticalOperationModel.owner_id == scope.owner_id,
                    CriticalOperationModel.task_id == scope.identity.task_id,
                    CriticalOperationModel.run_id == scope.run_id,
                )
                if operation_requires_code_artifact:
                    statement = statement.where(_current_operation_contract_predicate())
                if operation_kind is not None:
                    statement = statement.where(
                        CriticalOperationModel.operation_kind == operation_kind.value,
                    )
                if operation_tool_names is not None:
                    statement = statement.where(
                        CriticalOperationModel.tool_name.in_(operation_tool_names),
                    )
                if operation_statuses is not None:
                    statement = statement.where(
                        CriticalOperationModel.status.in_(
                            tuple(status.value for status in operation_statuses)
                        ),
                    )
                if operation_requires_code_artifact:
                    statement = statement.where(
                        CriticalOperationModel.code_artifact_id.is_not(None),
                        CriticalOperationModel.code_artifact_version_id.is_not(None),
                        CriticalOperationModel.code_artifact_sha256.is_not(None),
                    )
            if before_sequence is not None:
                statement = statement.where(DataCatalogEntryModel.ordinal < before_sequence)
            if scope.catalog_frontier is not None:
                statement = statement.where(
                    DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier
                )
            models = (
                await session.scalars(
                    statement.order_by(desc(DataCatalogEntryModel.ordinal)).limit(page_size + 1)
                )
            ).all()
            complete = len(models) <= page_size
            entries = tuple(self._catalog_record(model) for model in models[:page_size])
            return CatalogEntryPage(
                entries=entries,
                complete=complete,
                next_before_sequence=None if complete else entries[-1].ordinal,
            )

    async def get_catalog_entry_by_alias(
        self,
        scope: InvocationScope,
        resource_kind: CatalogResourceKind,
        alias: str,
    ) -> DataCatalogEntry | None:
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            statement = select(DataCatalogEntryModel).where(
                DataCatalogEntryModel.owner_id == scope.owner_id,
                DataCatalogEntryModel.task_id == scope.identity.task_id,
                DataCatalogEntryModel.run_id == scope.run_id,
                DataCatalogEntryModel.resource_kind == resource_kind.value,
                DataCatalogEntryModel.alias == alias,
            )
            if resource_kind is CatalogResourceKind.OPERATION:
                statement = statement.join(
                    CriticalOperationModel,
                    and_(
                        CriticalOperationModel.owner_id == DataCatalogEntryModel.owner_id,
                        CriticalOperationModel.task_id == DataCatalogEntryModel.task_id,
                        CriticalOperationModel.run_id == DataCatalogEntryModel.run_id,
                        CriticalOperationModel.operation_id
                        == DataCatalogEntryModel.operation_id,
                    ),
                ).where(
                    CriticalOperationModel.owner_id == scope.owner_id,
                    CriticalOperationModel.task_id == scope.identity.task_id,
                    CriticalOperationModel.run_id == scope.run_id,
                    _current_operation_contract_predicate(),
                )
            if scope.catalog_frontier is not None:
                statement = statement.where(
                    DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier
                )
            model = await session.scalar(statement)
            return self._catalog_record(model) if model is not None else None

    async def get_operation_for_result_package(self, scope, package_id):
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            model = await session.scalar(select(CriticalOperationModel).where(
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.run_id == scope.run_id,
                CriticalOperationModel.result_package_id == package_id,
                CriticalOperationModel.status.in_(("succeeded", "failed")),
                _current_operation_contract_predicate(),
            ))
            return None if model is None else await self._published_operation(
                session, model, run_id=scope.run_id, frontier=scope.catalog_frontier)

    async def get_operation_by_locator(self, scope, *, origin_run_id, operation_id):
        # A frontier is an authority on its own Run, never on another Run's ordinal.
        if origin_run_id != scope.run_id and scope.catalog_frontier is not None:
            return None
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            model = await session.scalar(select(CriticalOperationModel).where(
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.run_id == origin_run_id,
                CriticalOperationModel.operation_id == operation_id,
                _current_operation_contract_predicate(),
            ))
            return None if model is None else await self._published_operation(
                session, model, run_id=origin_run_id, frontier=scope.catalog_frontier)

    @staticmethod
    def _ifc_products_recovery_statement(scope, *, origin_run_id):
        statement = select(CriticalOperationModel, DataCatalogEntryModel).outerjoin(
            DataCatalogEntryModel, and_(
                DataCatalogEntryModel.operation_id == CriticalOperationModel.operation_id,
                DataCatalogEntryModel.run_id == CriticalOperationModel.run_id,
                DataCatalogEntryModel.resource_kind == CatalogResourceKind.OPERATION.value,
            )).where(
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.run_id == origin_run_id,
                CriticalOperationModel.operation_kind == CriticalOperationKind.IFC_MODEL_WRITE.value,
                CriticalOperationModel.tool_name.in_(("ifc_products_apply", "ifc_products_edit")),
                _current_operation_contract_predicate(),
            )
        if scope.catalog_frontier is not None:
            statement = statement.where(DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier)
        return statement

    async def _ifc_products_recovery_record(self, session, model, entry):
        return IfcProductsRecoveryOperation(
            operation=await self._operation_record(session, model), created_at=as_utc(model.created_at),
            catalog_entry=None if entry is None else self._catalog_record(entry),
        )

    async def list_ifc_products_recovery_operations(
        self, scope, *, tool_name, before_created_at=None, before_operation_id=None, limit=20,
    ):
        if tool_name not in {"ifc_products_apply", "ifc_products_edit"}:
            raise ValueError("product recovery requires an implemented product write tool")
        if (before_created_at is None) != (before_operation_id is None):
            raise ValueError("Apply recovery pagination requires both original ordering fields")
        if type(limit) is not int or not 1 <= limit <= 51:
            raise ValueError("Apply recovery discovery limit must be between 1 and 51")
        statement = self._ifc_products_recovery_statement(scope, origin_run_id=scope.run_id).where(
            CriticalOperationModel.tool_name == tool_name)
        if before_created_at is not None:
            statement = statement.where(or_(
                CriticalOperationModel.created_at < before_created_at,
                and_(CriticalOperationModel.created_at == before_created_at,
                     CriticalOperationModel.operation_id < before_operation_id),
            ))
        statement = statement.order_by(CriticalOperationModel.created_at.desc(),
            CriticalOperationModel.operation_id.desc()).limit(limit)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            pairs = (await session.execute(statement)).all()
            return tuple([await self._ifc_products_recovery_record(session, model, entry)
                          for model, entry in pairs])

    async def get_ifc_products_recovery_operation(self, scope, *, origin_run_id, operation_id):
        if origin_run_id != scope.run_id and scope.catalog_frontier is not None:
            return None
        statement = self._ifc_products_recovery_statement(scope, origin_run_id=origin_run_id).where(
            CriticalOperationModel.operation_id == operation_id)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            pair = (await session.execute(statement)).one_or_none()
            return None if pair is None else await self._ifc_products_recovery_record(session, *pair)

    async def get_authoring_publication_for_version(self, scope, *, root_kind, version_id):
        """Publication provenance consumes Catalog identity, not the full result DTO."""
        if root_kind not in {"geometry", "family"}:
            raise ValueError("publication version lookup requires Geometry or Family")
        detail = CriticalOperationModel.operation_detail_json
        output = (cast(detail["geometry_versions"], JSONB).contains([{"resource_version_id": version_id}])
                  if root_kind == "geometry" else detail["family_version"]["family_version_id"].as_string() == version_id)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            statement = select(CriticalOperationModel).where(
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.operation_kind == "authoring_publish",
                CriticalOperationModel.status == "succeeded", output,
                _current_operation_contract_predicate(),
            )
            if scope.catalog_frontier is not None:
                statement = statement.where(CriticalOperationModel.run_id == scope.run_id)
            model = await session.scalar(statement.order_by(CriticalOperationModel.started_at).limit(1))
            return None if model is None else await self._published_operation(
                session, model, run_id=model.run_id, frontier=scope.catalog_frontier, summary_only=True)

    async def get_geometry_case_reports_for_snapshot(self, scope, *, resource_id, snapshot_version_id,
                                                    before=None, page_size=20):
        """Page saved Geometry reports for the exact draft fixed by publication."""
        if not 1 <= page_size <= 100:
            raise ValueError("saved Geometry report page size must be between 1 and 100")
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            detail = CriticalOperationModel.operation_detail_json
            statement = select(CriticalOperationModel).join(DataCatalogEntryModel, and_(
                DataCatalogEntryModel.operation_id == CriticalOperationModel.operation_id,
                DataCatalogEntryModel.run_id == CriticalOperationModel.run_id,
            )).where(
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.operation_kind == "geometry_inspection",
                CriticalOperationModel.status == "succeeded",
                detail["result_kind"].as_string() == "case_report",
                detail["source_kind"].as_string() == "geometry_sample",
                detail["source_type"].as_string() == "draft",
                detail["source_id"].as_string() == resource_id,
                detail["source_version_id"].as_string() == snapshot_version_id,
                _current_operation_contract_predicate(),
            )
            if scope.catalog_frontier is not None:
                statement = statement.where(CriticalOperationModel.run_id == scope.run_id,
                    DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier)
            if before is not None:
                started_at, operation_id = before
                statement = statement.where(or_(CriticalOperationModel.started_at < started_at,
                    and_(CriticalOperationModel.started_at == started_at,
                         CriticalOperationModel.operation_id < operation_id)))
            models = (await session.scalars(statement.order_by(CriticalOperationModel.started_at.desc(),
                CriticalOperationModel.operation_id.desc()).limit(page_size))).all()
            return tuple([await self._published_operation(session, model, run_id=model.run_id,
                frontier=scope.catalog_frontier) for model in models])

    async def get_authoring_save_for_snapshot(self, scope, *, root_kind, root_id, snapshot_artifact_version_id):
        if root_kind not in {"geometry", "family"}:
            raise ValueError("draft save lookup requires Geometry or Family")
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            detail = CriticalOperationModel.operation_detail_json
            statement = select(CriticalOperationModel).where(
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.status == "succeeded",
                CriticalOperationModel.code_artifact_version_id == snapshot_artifact_version_id,
                detail["detail_kind"].as_string() == "authoring_draft_write",
                detail["root_kind"].as_string() == root_kind,
                detail["root_id"].as_string() == root_id,
            )
            if scope.catalog_frontier is not None:
                statement = statement.where(CriticalOperationModel.run_id == scope.run_id)
            model = await session.scalar(statement.order_by(CriticalOperationModel.started_at).limit(1))
            return None if model is None else await self._published_operation(
                session, model, run_id=model.run_id, frontier=scope.catalog_frontier)

    async def get_operation(
        self,
        scope: InvocationScope,
        operation_id: str,
    ) -> PublishedCriticalOperation | None:
        return await self._get_operation(scope, operation_id, summary_only=False)

    async def get_operation_summary(self, scope, operation_id):
        return await self._get_operation(scope, operation_id, summary_only=True)

    async def _get_operation(self, scope, operation_id, *, summary_only):
        async with self._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            await self._require_data_run(session, scope, lock=False)
            statement = (
                select(CriticalOperationModel)
                .join(
                    DataCatalogEntryModel,
                    and_(
                        DataCatalogEntryModel.owner_id == CriticalOperationModel.owner_id,
                        DataCatalogEntryModel.task_id == CriticalOperationModel.task_id,
                        DataCatalogEntryModel.run_id == CriticalOperationModel.run_id,
                        DataCatalogEntryModel.operation_id
                        == CriticalOperationModel.operation_id,
                    ),
                )
                .where(
                    CriticalOperationModel.owner_id == scope.owner_id,
                    CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                    CriticalOperationModel.task_id == scope.identity.task_id,
                    CriticalOperationModel.operation_id == operation_id,
                    DataCatalogEntryModel.run_id == scope.run_id,
                    DataCatalogEntryModel.resource_kind == CatalogResourceKind.OPERATION.value,
                )
            )
            if not summary_only:
                statement = statement.where(_current_operation_contract_predicate())
            if scope.catalog_frontier is not None:
                statement = statement.where(
                    DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier
                )
            model = await session.scalar(statement)
            if model is not None and summary_only:
                entry = await session.scalar(select(DataCatalogEntryModel).where(
                    DataCatalogEntryModel.operation_id == model.operation_id,
                    DataCatalogEntryModel.run_id == scope.run_id))
                return PublishedOperationCatalogSummary(catalog_entry=self._catalog_record(entry),
                    operation=CriticalOperationCatalogSummary(**{key: getattr(model, key) for key in (
                        "operation_id", "run_id", "operation_kind", "initiator", "tool_name", "intent_summary",
                        "status", "started_at", "finished_at", "error_code")}))
            return (
                await self._published_operation(
                    session,
                    model,
                    run_id=scope.run_id,
                    frontier=scope.catalog_frontier,
                    summary_only=summary_only,
                )
                if model is not None
                else None
            )

    async def get_product_family_operation_source(
        self, scope: InvocationScope, operation_id: str,
    ) -> ProductFamilyOperationSource | None:
        """Reauthorize code ownership without decoding any historical draft."""
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            statement = (
                select(CriticalOperationModel, DataCatalogEntryModel)
                .join(
                    DataCatalogEntryModel,
                    and_(
                        DataCatalogEntryModel.owner_id == CriticalOperationModel.owner_id,
                        DataCatalogEntryModel.task_id == CriticalOperationModel.task_id,
                        DataCatalogEntryModel.run_id == CriticalOperationModel.run_id,
                        DataCatalogEntryModel.operation_id
                        == CriticalOperationModel.operation_id,
                    ),
                )
                .where(CriticalOperationModel.owner_id == scope.owner_id,
                       CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                       CriticalOperationModel.task_id == scope.identity.task_id,
                       CriticalOperationModel.operation_id == operation_id,
                       CriticalOperationModel.operation_kind == CriticalOperationKind.PRODUCT_FAMILY_WRITE.value,
                       CriticalOperationModel.tool_name == "ifc_product_family_write",
                       DataCatalogEntryModel.run_id == scope.run_id,
                       DataCatalogEntryModel.resource_kind == CatalogResourceKind.OPERATION.value,
                       _current_operation_contract_predicate())
            )
            if scope.catalog_frontier is not None:
                statement = statement.where(DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier)
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                return None
            model, entry = row
            parent_alias = None
            if model.repair_parent_operation_id is not None:
                parent_query = (
                    select(DataCatalogEntryModel.alias)
                    .join(
                        CriticalOperationModel,
                        and_(
                            CriticalOperationModel.owner_id
                            == DataCatalogEntryModel.owner_id,
                            CriticalOperationModel.task_id
                            == DataCatalogEntryModel.task_id,
                            CriticalOperationModel.run_id
                            == DataCatalogEntryModel.run_id,
                            CriticalOperationModel.operation_id
                            == DataCatalogEntryModel.operation_id,
                        ),
                    )
                    .where(
                    DataCatalogEntryModel.owner_id == scope.owner_id,
                    DataCatalogEntryModel.task_id == scope.identity.task_id,
                    DataCatalogEntryModel.run_id == scope.run_id,
                    DataCatalogEntryModel.operation_id == model.repair_parent_operation_id,
                    _current_operation_contract_predicate(),
                    )
                )
                if scope.catalog_frontier is not None:
                    parent_query = parent_query.where(DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier)
                parent_alias = await session.scalar(parent_query)
                if parent_alias is None:
                    raise ValueError("Family source Operation lost its visible repair parent")
            operation = await self._operation_record(session, model)
            family_draft = (isinstance(operation.detail, AuthoringDraftWriteOperationDetail)
                            and operation.detail.root_kind == "family")
            system_copy = (isinstance(operation.detail, ProductFamilyWriteOperationDetail)
                           and operation.detail.mode == "system_copy")
            if not (family_draft or system_copy):
                raise ProductFamilyCatalogReferenceError(
                    "ProductFamily source Operation is unavailable under the current contract"
                )
            return ProductFamilyOperationSource(
                operation_id=model.operation_id, catalog_entry=self._catalog_record(entry),
                detail_kind=operation.detail.detail_kind,
                code_artifact=ArtifactVersionRef(artifact_id=model.code_artifact_id,
                    version_id=model.code_artifact_version_id, sha256=model.code_artifact_sha256),
                status=model.status, started_at=as_utc(model.started_at), finished_at=as_utc(model.finished_at),
                error_phase=model.error_phase, error_code=model.error_code, error_message=model.error_message,
                parent_owner_alias=parent_alias, result_product_family_version_id=model.result_product_family_version_id,
            )


    async def get_operation_replay(
        self,
        scope: InvocationScope,
        tool_names: Sequence[str],
        request_fingerprint: str,
    ) -> PublishedCriticalOperation | None:
        if isinstance(tool_names, (str, bytes)):
            raise ValueError("tool_names must be a sequence of complete tool names")
        names = tuple(tool_names)
        if not names or len(names) != len(set(names)):
            raise ValueError("tool_names must be non-empty and unique")
        async with self._sessions() as session:
            await self._require_data_run(session, scope, lock=False)
            statement = select(CriticalOperationModel).where(
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.run_id == scope.run_id,
                CriticalOperationModel.tool_name.in_(names),
                CriticalOperationModel.request_fingerprint == request_fingerprint,
                _current_operation_contract_predicate(),
            )
            models = tuple((await session.scalars(statement)).all())
            if len(models) > 1:
                raise ConflictError("Operation replay identity is ambiguous")
            model = models[0] if models else None
            # Server-only response-loss reconcile deliberately bypasses a historical
            # public frontier while remaining scoped to the exact originating Run.
            return (
                await self._published_operation(session, model, run_id=scope.run_id)
                if model is not None
                else None
            )

    @staticmethod
    def _require_direct_context(request: OpenDirectSessionRequest) -> None:
        context = current_tenant_execution_context()
        if context is None:
            raise TenantContextMismatch("Direct session requires a tenant execution context")
        if context.tenant_id != request.tenant_id or context.principal_id != request.principal_id:
            raise TenantContextMismatch("Direct session identity does not match active context")

    @staticmethod
    def _opened_session(
        model: DirectSessionBindingModel,
        request: OpenDirectSessionRequest,
        *,
        resumed: bool,
    ) -> OpenedDirectSession:
        if model.owner_id != request.owner_id or model.principal_id != request.principal_id:
            raise ConflictError("Direct session identity binding changed")
        return OpenedDirectSession(
            binding=PostgresDataRuntimeRepositoryMixin._direct_session_record(model),
            resumed=resumed,
        )

    @staticmethod
    def _direct_session_record(model: DirectSessionBindingModel) -> DirectSessionBinding:
        return DirectSessionBinding(
            session_id=model.session_id,
            direct_session_alias=model.direct_session_alias,
            ordinal=model.ordinal,
            configured_subject=model.configured_subject,
            session_name=model.session_name,
            normalized_session_name=model.normalized_session_name,
            owner_id=model.owner_id,
            tenant_id=model.tenant_id,
            principal_id=model.principal_id,
            task_id=model.task_id,
            run_id=model.run_id,
            created_at=as_utc(model.created_at),
        )

    @staticmethod
    async def _require_data_run(
        session,
        scope: InvocationScope,
        *,
        lock: bool = True,
    ) -> RunModel:
        statement = (
            select(RunModel)
            .where(
                RunModel.owner_id == scope.owner_id,
                RunModel.tenant_id == scope.identity.tenant_id,
                RunModel.task_id == scope.identity.task_id,
                RunModel.run_id == scope.run_id,
                RunModel.execution_mode == scope.execution_mode.value,
            )
            .join(
                TaskTenantBindingModel,
                and_(
                    TaskTenantBindingModel.tenant_id == RunModel.tenant_id,
                    TaskTenantBindingModel.task_id == RunModel.task_id,
                    TaskTenantBindingModel.principal_id == scope.identity.principal_id,
                ),
            )
        )
        if lock:
            # The principal join is read-only.  Lock only the authoritative Run;
            # otherwise PostgreSQL also requires UPDATE on task_tenant_bindings.
            statement = statement.with_for_update(of=RunModel)
        run = await session.scalar(statement)
        if run is None:
            raise ConflictError("Data Runtime scope does not match authoritative Run")
        return run

    @staticmethod
    def _require_record_scope(scope: InvocationScope, record) -> None:
        if (
            record.owner_id != scope.owner_id
            or record.tenant_id != scope.identity.tenant_id
            or record.task_id != scope.identity.task_id
            or record.run_id != scope.run_id
        ):
            raise ConflictError("Data Runtime record does not match invocation scope")

    @staticmethod
    async def _require_exact_member_artifacts(session, members) -> None:
        for member in members:
            artifact = await session.scalar(
                select(ArtifactVersionModel).where(
                    ArtifactVersionModel.owner_id == member.owner_id,
                    ArtifactVersionModel.tenant_id == member.tenant_id,
                    ArtifactVersionModel.task_id == member.task_id,
                    ArtifactVersionModel.artifact_id == member.artifact.artifact_id,
                    ArtifactVersionModel.version_id == member.artifact.version_id,
                )
            )
            if artifact is None or artifact.sha256 != member.artifact.sha256:
                raise ConflictError("Data Package member Artifact version is not exact")

    @staticmethod
    async def _require_operation_artifacts(
        session,
        operation: CriticalOperationRecord,
    ) -> None:
        if (
            operation.tool_name == "data_derive"
            and operation.description_artifact is None
        ):
            raise ConflictError("Data Transform requires an exact DESCRIPTION Artifact")
        for label, reference in (
            ("CODE", operation.code_artifact),
            ("DESCRIPTION", operation.description_artifact),
        ):
            if reference is None:
                continue
            artifact = await session.scalar(
                select(ArtifactVersionModel).where(
                    ArtifactVersionModel.owner_id == operation.owner_id,
                    ArtifactVersionModel.tenant_id == operation.tenant_id,
                    ArtifactVersionModel.task_id == operation.task_id,
                    ArtifactVersionModel.artifact_id == reference.artifact_id,
                    ArtifactVersionModel.version_id == reference.version_id,
                )
            )
            if artifact is None or artifact.sha256 != reference.sha256:
                raise ConflictError(f"Operation {label} Artifact version is not exact")

    @staticmethod
    async def _require_repair_parent(session, scope, operation) -> None:
        parent_id = operation.repair_parent_operation_id
        if parent_id is None:
            return
        parent = await session.scalar(
            select(CriticalOperationModel).where(
                CriticalOperationModel.owner_id == operation.owner_id,
                CriticalOperationModel.tenant_id == operation.tenant_id,
                CriticalOperationModel.task_id == operation.task_id,
                CriticalOperationModel.run_id == operation.run_id,
                CriticalOperationModel.run_id == scope.run_id,
                CriticalOperationModel.operation_id == parent_id,
                CriticalOperationModel.status.in_(
                    (
                        CriticalOperationStatus.FAILED.value,
                        CriticalOperationStatus.SUCCEEDED.value,
                    )
                ),
                CriticalOperationModel.operation_kind == CriticalOperationKind.DATA_TRANSFORM.value,
                CriticalOperationModel.tool_name == "data_derive",
                CriticalOperationModel.code_artifact_id.is_not(None),
                CriticalOperationModel.code_artifact_version_id.is_not(None),
                CriticalOperationModel.code_artifact_sha256.is_not(None),
                CriticalOperationModel.description_artifact_id.is_not(None),
                CriticalOperationModel.description_artifact_version_id.is_not(None),
                CriticalOperationModel.description_artifact_sha256.is_not(None),
            )
        )
        if parent is None:
            raise ConflictError("Repair parent Operation is not an exact repairable transform")
        detail_payload = dict(parent.operation_detail_json or {})
        if detail_payload.get("detail_kind") != "data_transform":
            raise ConflictError("Repair parent Operation is not an exact repairable transform")
        source = operation.detail.ifc_source
        if (parent.source_ifc_model_id, parent.source_ifc_revision_id) != (
            source.model_id if source else None, source.revision_id if source else None,
        ):
            raise ConflictError("Repair must retain its original exact IFC source")

    async def _require_ifc_read_source(self, session, scope, operation, *, publication=False) -> None:
        detail = operation.detail
        source = detail.ifc_source if isinstance(detail, DataTransformOperationDetail) else None
        if source is None or (publication and operation.repair_parent_operation_id is None):
            return
        ifc_scope = IfcRunIdentity(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id, run_id=scope.run_id)
        repair = operation.repair_parent_operation_id is not None
        row = await self._revision_row(session, ifc_scope, current=repair,
            revision_id=None if repair else source.revision_id, lock_model=repair)
        actual = (IfcReadSourceRef(model_id=row[0].ifc_model_id,
            revision_id=row[1].revision_id, revision_no=row[1].revision_no) if row else None)
        if repair and actual != source:
            raise IfcSourceChangedError(source, actual)
        if actual != source:
            raise ConflictError("The IFC read source is not an exact authorized revision")

    @staticmethod
    async def _require_transform_inputs(session, scope, operation) -> None:
        detail = operation.detail
        if isinstance(detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail)):
            bindings = (detail.input_binding,)
        elif operation.operation_kind is not CriticalOperationKind.DATA_TRANSFORM:
            return
        elif not isinstance(detail, (DataTransformOperationDetail, CadMeasurementOperationDetail)):
            raise ConflictError("Data Transform detail is missing")
        else:
            bindings = detail.inputs
        for binding in bindings:
            statement = (
                select(DataPackageMemberModel)
                .join(
                    DataPackageModel,
                    DataPackageModel.package_id == DataPackageMemberModel.package_id,
                )
                .join(
                    DataCatalogEntryModel,
                    DataCatalogEntryModel.package_id == DataPackageModel.package_id,
                )
                .where(
                    DataPackageModel.owner_id == operation.owner_id,
                    DataPackageModel.task_id == operation.task_id,
                    DataPackageModel.package_id == binding.package_id,
                    DataPackageMemberModel.member_id == binding.member_id,
                    DataPackageMemberModel.member_kind == PackageMemberKind.PAYLOAD.value,
                    DataCatalogEntryModel.run_id == scope.run_id,
                    DataCatalogEntryModel.resource_kind == CatalogResourceKind.PACKAGE.value,
                )
            )
            if getattr(scope, "catalog_frontier", None) is not None:
                statement = statement.where(
                    DataCatalogEntryModel.catalog_revision <= scope.catalog_frontier
                )
            member = await session.scalar(statement)
            if member is None or _payload_name_model(member) != binding.payload_name:
                raise ConflictError("Data Transform input binding is not exact and visible")

    async def _register_product_report(self, session, scope, operation, report) -> None:
        """Append the complete report using the IFC owner's existing transaction."""
        from CAD2BIM_backend.infrastructure.postgres.repository_artifact import register_artifact_in_transaction
        from CAD2BIM_backend.domain.data_runtime import ProductApplyReportPublication, ProductEditReportPublication

        expected_report = ProductEditReportPublication if operation.tool_name == "ifc_products_edit" else ProductApplyReportPublication
        if not isinstance(report, expected_report):
            raise ConflictError("product report does not match its Operation tool")

        if (operation.tool_name not in {"ifc_products_apply", "ifc_products_edit"}
                or report.package.request_fingerprint != operation.request_fingerprint
                or report.package.created_by_run_id != scope.run_id
                or report.package.owner_id != scope.owner_id
                or report.package.tenant_id != scope.tenant_id
                or report.package.task_id != scope.task_id):
            raise ConflictError("product report changed its Operation or Scope")
        for artifact in report.artifacts:
            await register_artifact_in_transaction(
                session, artifact, tenant_id=scope.tenant_id,
                request_value=artifact.model_dump(mode="json"),
                lineage={"operation_id": operation.operation_id},
            )
        session.add(self._package_model(report.package))
        await session.flush()
        session.add_all(self._member_model(member) for member in report.members)
        await session.flush()
        await append_catalog_entry(
            session, owner_id=scope.owner_id, tenant_id=scope.tenant_id,
            task_id=scope.task_id, run_id=scope.run_id, kind=CatalogResourceKind.PACKAGE,
            catalog_revision=await next_catalog_revision(session, scope.run_id),
            package_id=report.package.package_id,
        )
        detail_type = IfcProductsEditOperationDetail if operation.tool_name == "ifc_products_edit" else IfcProductsApplyOperationDetail
        detail = detail_type.model_validate(operation.operation_detail_json)
        operation.operation_detail_json = detail.model_copy(update={"result": report.summary}).persisted_payload()
        operation.result_package_id = report.package.package_id

    @staticmethod
    def _package_model(record: DataPackageRecord) -> DataPackageModel:
        return DataPackageModel(
            package_id=record.package_id,
            owner_id=record.owner_id,
            tenant_id=record.tenant_id,
            task_id=record.task_id,
            created_by_run_id=record.created_by_run_id,
            name=record.name,
            package_kind=record.package_kind.value,
            source_filename=record.source_filename,
            description_title=record.description_title,
            summary=record.summary,
            section_titles_json=list(record.section_titles),
            idempotency_key=record.idempotency_key,
            request_fingerprint=record.request_fingerprint,
            created_at=record.created_at,
        )

    @staticmethod
    def _member_model(record: DataPackageMemberRecord) -> DataPackageMemberModel:
        return DataPackageMemberModel(
            member_id=record.member_id,
            owner_id=record.owner_id,
            tenant_id=record.tenant_id,
            task_id=record.task_id,
            package_id=record.package_id,
            member_name=record.member_name,
            member_kind=record.member_kind.value,
            artifact_id=record.artifact.artifact_id,
            artifact_version_id=record.artifact.version_id,
            artifact_sha256=record.artifact.sha256,
            media_type=record.media_type,
            row_count=record.row_count,
            logical_payload_name=record.logical_payload_name,
            payload_description=record.payload_description,
            logical_key_fields_json=(
                list(record.logical_key_fields) if record.logical_key_fields else None
            ),
            arrow_schema_json=(
                [column.model_dump(mode="json") for column in record.arrow_schema]
                if record.arrow_schema
                else None
            ),
            logical_byte_count=record.logical_byte_count,
            json_kind=record.json_kind.value if record.json_kind is not None else None,
            created_at=record.created_at,
        )

    @staticmethod
    def _operation_model(record: CriticalOperationRecord) -> CriticalOperationModel:
        source = record.detail.ifc_source if isinstance(record.detail, DataTransformOperationDetail) else None
        code = record.code_artifact
        description = record.description_artifact
        inspection_request = (
            record.detail.request_artifact
            if isinstance(record.detail, GeometryInspectionOperationDetail) else None
        )
        inspection_result = record.result_inspection_artifact
        inspection_detail = record.detail if isinstance(record.detail, GeometryInspectionOperationDetail) else None
        return CriticalOperationModel(
            operation_id=record.operation_id,
            owner_id=record.owner_id,
            tenant_id=record.tenant_id,
            task_id=record.task_id,
            run_id=record.run_id,
            operation_kind=record.operation_kind.value,
            initiator=record.initiator.value,
            tool_name=record.tool_name,
            intent_summary=record.intent_summary,
            source_filename=record.source_filename,
            idempotency_key=record.idempotency_key,
            request_fingerprint=record.request_fingerprint,
            status=record.status.value,
            result_package_id=record.result_package_id,
            error_code=record.error_code,
            error_message=record.error_message,
            error_phase=record.error_phase.value if record.error_phase is not None else None,
            code_artifact_id=code.artifact_id if code is not None else None,
            code_artifact_version_id=code.version_id if code is not None else None,
            code_artifact_sha256=code.sha256 if code is not None else None,
            inspection_request_artifact_id=None if inspection_request is None else inspection_request.artifact_id,
            inspection_request_artifact_version_id=None if inspection_request is None else inspection_request.version_id,
            inspection_request_artifact_sha256=None if inspection_request is None else inspection_request.sha256,
            inspection_result_artifact_id=None if inspection_result is None else inspection_result.artifact_id,
            inspection_result_artifact_version_id=None if inspection_result is None else inspection_result.version_id,
            inspection_result_artifact_sha256=None if inspection_result is None else inspection_result.sha256,
            inspection_source_geometry_version_id=(
                inspection_detail.source_version_id
                if inspection_detail is not None and inspection_detail.source_type == "published"
                and inspection_detail.source_kind == "geometry_sample" else None
            ),
            inspection_source_family_version_id=(
                inspection_detail.source_version_id
                if inspection_detail is not None and inspection_detail.source_type == "published"
                and inspection_detail.source_kind == "family_realization" else None
            ),
            description_artifact_id=(description.artifact_id if description is not None else None),
            description_artifact_version_id=(
                description.version_id if description is not None else None
            ),
            description_artifact_sha256=(description.sha256 if description is not None else None),
            repair_parent_operation_id=record.repair_parent_operation_id,
            source_ifc_model_id=source.model_id if source else None,
            source_ifc_revision_id=source.revision_id if source else None,
            base_ifc_revision_id=record.base_ifc_revision_id,
            expected_ifc_write_epoch=record.expected_ifc_write_epoch,
            target_geometry_resource_id=record.target_geometry_resource_id,
            result_geometry_resource_version_id=(record.result_geometry_resource_version_id),
            target_product_family_id=record.target_product_family_id,
            base_product_family_version_id=record.base_product_family_version_id,
            result_product_family_version_id=record.result_product_family_version_id,
            operation_detail_json=_operation_detail_json(record),
            started_at=record.started_at,
            finished_at=record.finished_at,
            created_at=record.finished_at or record.started_at,
        )

    @staticmethod
    def _operation_input_models(record: CriticalOperationRecord):
        detail = record.detail
        if isinstance(detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail)):
            binding = detail.input_binding
            bindings = (TransformInputBindingRecord(
                ordinal=1, input_name="products", package_id=binding.package_id,
                member_id=binding.member_id, payload_name=binding.payload_name,
            ),)
        elif isinstance(detail, (DataTransformOperationDetail, CadMeasurementOperationDetail)):
            bindings = detail.inputs
        else:
            return ()
        return tuple(
            CriticalOperationInputModel(
                input_binding_id=f"operation_input_{uuid4().hex}",
                owner_id=record.owner_id,
                tenant_id=record.tenant_id,
                task_id=record.task_id,
                operation_id=record.operation_id,
                ordinal=binding.ordinal,
                input_name=binding.input_name,
                package_id=binding.package_id,
                member_id=binding.member_id,
                payload_name=binding.payload_name,
                created_at=record.finished_at or record.started_at,
            )
            for binding in bindings
        )

    @staticmethod
    def _assert_operation_replay(model, requested) -> None:
        if model.request_fingerprint != requested.request_fingerprint:
            raise IdempotencyConflictError(
                "operation idempotency key was reused with different input"
            )

    @staticmethod
    def _assert_package_replay(model, requested) -> None:
        if model.request_fingerprint != requested.request_fingerprint:
            raise IdempotencyConflictError(
                "package idempotency key was reused with different input"
            )

    async def _published_import_result(self, session, operation_model):
        published = await self._published_package_result(session, operation_model)
        return PublishedImportResult(
            package=published.package,
            operation=published.operation,
        )

    async def _published_package_result(self, session, operation_model):
        if operation_model.result_package_id is None:
            raise ConflictError("successful operation has no result package")
        package = await session.get(DataPackageModel, operation_model.result_package_id)
        if package is None:
            raise ConflictError("operation result package is missing")
        return PublishedPackageResult(
            package=await self._published_package(session, package, run_id=operation_model.run_id),
            operation=await self._require_published_operation(session, operation_model),
        )

    async def _published_package(
        self,
        session,
        package_model,
        *,
        run_id=None,
        frontier=None,
    ):
        members = (
            await session.scalars(
                select(DataPackageMemberModel)
                .where(DataPackageMemberModel.package_id == package_model.package_id)
                .order_by(DataPackageMemberModel.member_name)
            )
        ).all()
        statement = select(DataCatalogEntryModel).where(
            DataCatalogEntryModel.package_id == package_model.package_id
        )
        if run_id is not None:
            statement = statement.where(DataCatalogEntryModel.run_id == run_id)
        if frontier is not None:
            statement = statement.where(DataCatalogEntryModel.catalog_revision <= frontier)
        entry = await session.scalar(statement)
        if entry is None:
            raise ConflictError("package Catalog entry is missing")
        return PublishedDataPackage(
            package=self._package_record(package_model),
            members=tuple(self._member_record(item) for item in members),
            catalog_entry=self._catalog_record(entry),
        )

    async def _published_operation(
        self,
        session,
        operation_model,
        *,
        run_id=None,
        frontier=None,
        summary_only=False,
    ):
        statement = select(DataCatalogEntryModel).where(
            DataCatalogEntryModel.operation_id == operation_model.operation_id
        )
        if run_id is not None:
            statement = statement.where(DataCatalogEntryModel.run_id == run_id)
        if frontier is not None:
            statement = statement.where(DataCatalogEntryModel.catalog_revision <= frontier)
        entry = await session.scalar(statement)
        if entry is None:
            return None
        if summary_only:
            return PublishedOperationCatalogSummary(
                operation=CriticalOperationCatalogSummary(**{key: getattr(operation_model, key) for key in (
                    "operation_id", "run_id", "operation_kind", "initiator", "tool_name", "intent_summary",
                    "status", "started_at", "finished_at", "error_code")}),
                catalog_entry=self._catalog_record(entry),
            )
        return PublishedCriticalOperation(
            operation=await self._operation_record(session, operation_model),
            catalog_entry=self._catalog_record(entry),
        )

    async def _require_published_operation(self, session, operation_model):
        published = await self._published_operation(session, operation_model)
        if published is None:
            raise ConflictError("operation Catalog entry is missing")
        return published

    @staticmethod
    def _package_record(model):
        return DataPackageRecord(
            package_id=model.package_id,
            owner_id=model.owner_id,
            tenant_id=model.tenant_id,
            task_id=model.task_id,
            created_by_run_id=model.created_by_run_id,
            name=model.name,
            package_kind=model.package_kind,
            source_filename=model.source_filename,
            description_title=model.description_title,
            summary=model.summary,
            section_titles=tuple(model.section_titles_json or ()),
            idempotency_key=model.idempotency_key,
            request_fingerprint=model.request_fingerprint,
            created_at=as_utc(model.created_at),
        )

    @staticmethod
    def _member_record(model):
        return DataPackageMemberRecord(
            member_id=model.member_id,
            owner_id=model.owner_id,
            tenant_id=model.tenant_id,
            task_id=model.task_id,
            package_id=model.package_id,
            member_name=model.member_name,
            member_kind=PackageMemberKind(model.member_kind),
            artifact=ArtifactVersionRef(
                artifact_id=model.artifact_id,
                version_id=model.artifact_version_id,
                sha256=model.artifact_sha256,
            ),
            media_type=model.media_type,
            row_count=model.row_count,
            logical_payload_name=model.logical_payload_name,
            payload_description=model.payload_description,
            logical_key_fields=tuple(model.logical_key_fields_json or ()),
            arrow_schema=tuple(
                ArrowColumnRecord.model_validate(column)
                for column in (model.arrow_schema_json or ())
            ),
            logical_byte_count=model.logical_byte_count,
            json_kind=(JsonPayloadKind(model.json_kind) if model.json_kind is not None else None),
            created_at=as_utc(model.created_at),
        )

    @staticmethod
    async def _operation_record(session, model):
        detail_payload = dict(model.operation_detail_json or {})
        operation_kind = CriticalOperationKind(model.operation_kind)
        if operation_kind is CriticalOperationKind.DATA_TRANSFORM:
            if model.source_ifc_model_id is not None:
                revision_no = await session.scalar(select(IfcModelRevisionModel.revision_no).where(
                    IfcModelRevisionModel.owner_id == model.owner_id,
                    IfcModelRevisionModel.task_id == model.task_id,
                    IfcModelRevisionModel.run_id == model.run_id,
                    IfcModelRevisionModel.ifc_model_id == model.source_ifc_model_id,
                    IfcModelRevisionModel.revision_id == model.source_ifc_revision_id,
                ))
                if revision_no is None:
                    raise ConflictError("The Operation IFC read source is unavailable")
                detail_payload["ifc_source"] = IfcReadSourceRef(model_id=model.source_ifc_model_id,
                    revision_id=model.source_ifc_revision_id, revision_no=revision_no).model_dump(mode="json")
            input_models = (
                await session.scalars(
                    select(CriticalOperationInputModel)
                    .where(CriticalOperationInputModel.operation_id == model.operation_id)
                    .order_by(CriticalOperationInputModel.ordinal)
                )
            ).all()
            detail_payload["inputs"] = [
                TransformInputBindingRecord(
                    ordinal=item.ordinal,
                    input_name=item.input_name,
                    package_id=item.package_id,
                    member_id=item.member_id,
                    payload_name=item.payload_name,
                ).model_dump(mode="json")
                for item in input_models
            ]
            detail_type = (
                CadMeasurementOperationDetail
                if detail_payload.get("engine") == "cad_measurement"
                else DataTransformOperationDetail
            )
            detail = detail_type.model_validate(detail_payload)
        elif operation_kind is CriticalOperationKind.SOURCE_INGESTION:
            detail = (
                SourceIngestionOperationDetail.model_validate(detail_payload)
                if detail_payload
                else None
            )
        elif operation_kind is CriticalOperationKind.IFC_MODEL_WRITE:
            detail_kind = detail_payload.get("detail_kind")
            if detail_kind == "ifc_model_configure_geometry_basis":
                detail = IfcGeometryBasisConfigureOperationDetail.model_validate(detail_payload)
            elif detail_kind == "ifc_project_update":
                detail = IfcProjectUpdateOperationDetail.model_validate(detail_payload)
            elif detail_kind == "ifc_trusted_baseline_load":
                detail = IfcTrustedBaselineLoadOperationDetail.model_validate(detail_payload)
            elif detail_kind == "ifc_spatial_structure_apply":
                detail = IfcSpatialStructureApplyOperationDetail.model_validate(detail_payload)
            elif detail_kind == "ifc_products_apply":
                detail = IfcProductsApplyOperationDetail.model_validate(detail_payload)
            elif detail_kind == "ifc_products_edit":
                detail = IfcProductsEditOperationDetail.model_validate(detail_payload)
            else:
                detail = IfcModelInitializeOperationDetail.model_validate(detail_payload)
        elif operation_kind is CriticalOperationKind.AUTHORING_PUBLISH:
            detail = AuthoringPublishOperationDetail.model_validate(detail_payload)
        elif operation_kind in {CriticalOperationKind.GEOMETRY_RESOURCE_WRITE, CriticalOperationKind.PRODUCT_FAMILY_WRITE} and detail_payload.get("detail_kind") == "authoring_draft_write":
            detail = AuthoringDraftWriteOperationDetail.model_validate(detail_payload)
        elif operation_kind is CriticalOperationKind.GEOMETRY_RESOURCE_WRITE:
            raise ConflictError("retired executing Geometry Write is not a current Operation")
        elif operation_kind is CriticalOperationKind.PRODUCT_FAMILY_WRITE:
            draft_payload = detail_payload.get("version_draft")
            contract_payload = draft_payload.get("family_contract") if isinstance(draft_payload, dict) else None
            if (
                detail_payload.get("mode") != "system_copy"
                or not isinstance(contract_payload, dict)
                or not isinstance(contract_payload.get("standard_model"), dict)
            ):
                raise ProductFamilyCatalogReferenceError(
                    "ProductFamily Operation is unavailable under the current contract"
                )
            detail = ProductFamilyWriteOperationDetail.model_validate(detail_payload)
        elif operation_kind is CriticalOperationKind.GEOMETRY_INSPECTION:
            detail = GeometryInspectionOperationDetail.model_validate(detail_payload)
            if detail.request_artifact != ArtifactVersionRef(
                artifact_id=model.inspection_request_artifact_id,
                version_id=model.inspection_request_artifact_version_id,
                sha256=model.inspection_request_artifact_sha256,
            ):
                raise ConflictError("geometry inspection request Artifact dependency changed")
            if detail.source_type == "draft":
                if model.inspection_source_geometry_version_id is not None or model.inspection_source_family_version_id is not None:
                    raise ConflictError("draft inspection cannot bind a formal source version")
            elif (
                (detail.source_kind == "geometry_sample" and (
                    model.inspection_source_geometry_version_id != detail.source_version_id
                    or model.inspection_source_family_version_id is not None
                ))
                or (detail.source_kind == "family_realization" and (
                    model.inspection_source_family_version_id != detail.source_version_id
                    or model.inspection_source_geometry_version_id is not None
                ))
            ):
                raise ConflictError("geometry inspection source version dependency changed")
        else:  # pragma: no cover - CriticalOperationKind is closed
            raise ValueError("unsupported critical Operation kind")
        result_ifc_revision_id = None
        if operation_kind is CriticalOperationKind.IFC_MODEL_WRITE:
            result_ifc_revision_id = await session.scalar(
                select(IfcModelRevisionModel.revision_id).where(
                    IfcModelRevisionModel.owner_id == model.owner_id,
                    IfcModelRevisionModel.task_id == model.task_id,
                    IfcModelRevisionModel.run_id == model.run_id,
                    IfcModelRevisionModel.operation_id == model.operation_id,
                )
            )
        code_artifact = None
        if model.code_artifact_id is not None:
            code_artifact = ArtifactVersionRef(
                artifact_id=model.code_artifact_id,
                version_id=model.code_artifact_version_id,
                sha256=model.code_artifact_sha256,
            )
        description_artifact = None
        if model.description_artifact_id is not None:
            description_artifact = ArtifactVersionRef(
                artifact_id=model.description_artifact_id,
                version_id=model.description_artifact_version_id,
                sha256=model.description_artifact_sha256,
            )
        return CriticalOperationRecord(
            operation_id=model.operation_id,
            owner_id=model.owner_id,
            tenant_id=model.tenant_id,
            task_id=model.task_id,
            run_id=model.run_id,
            operation_kind=operation_kind,
            initiator=CriticalOperationInitiator(model.initiator),
            tool_name=model.tool_name,
            intent_summary=model.intent_summary,
            source_filename=model.source_filename,
            idempotency_key=model.idempotency_key,
            request_fingerprint=model.request_fingerprint,
            status=CriticalOperationStatus(model.status),
            result_package_id=model.result_package_id,
            error_code=model.error_code,
            error_message=model.error_message,
            error_phase=(
                CriticalOperationErrorPhase(model.error_phase)
                if model.error_phase is not None
                else None
            ),
            code_artifact=code_artifact,
            description_artifact=description_artifact,
            repair_parent_operation_id=model.repair_parent_operation_id,
            detail=detail,
            base_ifc_revision_id=model.base_ifc_revision_id,
            expected_ifc_write_epoch=model.expected_ifc_write_epoch,
            result_ifc_revision_id=result_ifc_revision_id,
            target_geometry_resource_id=model.target_geometry_resource_id,
            result_geometry_resource_version_id=(model.result_geometry_resource_version_id),
            target_product_family_id=model.target_product_family_id,
            base_product_family_version_id=model.base_product_family_version_id,
            result_product_family_version_id=model.result_product_family_version_id,
            result_inspection_artifact=(None if model.inspection_result_artifact_id is None else ArtifactVersionRef(
                artifact_id=model.inspection_result_artifact_id,
                version_id=model.inspection_result_artifact_version_id,
                sha256=model.inspection_result_artifact_sha256,
            )),
            started_at=as_utc(model.started_at),
            finished_at=(as_utc(model.finished_at) if model.finished_at is not None else None),
        )

    @staticmethod
    def _catalog_record(model):
        return DataCatalogEntry(
            catalog_entry_id=model.catalog_entry_id,
            owner_id=model.owner_id,
            tenant_id=model.tenant_id,
            task_id=model.task_id,
            run_id=model.run_id,
            resource_kind=CatalogResourceKind(model.resource_kind),
            alias=model.alias,
            ordinal=model.ordinal,
            catalog_revision=model.catalog_revision,
            package_id=model.package_id,
            operation_id=model.operation_id,
            created_at=as_utc(model.created_at),
        )


async def append_catalog_entry(
    session,
    *,
    owner_id,
    tenant_id,
    task_id,
    run_id,
    kind,
    catalog_revision,
    package_id=None,
    operation_id=None,
):
    """Append one Run-scoped Catalog binding while the caller holds the Run lock."""

    maximum = await session.scalar(
        select(func.max(DataCatalogEntryModel.ordinal)).where(
            DataCatalogEntryModel.run_id == run_id,
            DataCatalogEntryModel.resource_kind == kind.value,
        )
    )
    ordinal = int(maximum or 0) + 1
    prefix = "data" if kind is CatalogResourceKind.PACKAGE else "op"
    model = DataCatalogEntryModel(
        catalog_entry_id=f"catalog_{uuid4().hex}",
        owner_id=owner_id,
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        resource_kind=kind.value,
        alias=f"{prefix}_{ordinal:03d}",
        ordinal=ordinal,
        catalog_revision=catalog_revision,
        package_id=package_id,
        operation_id=operation_id,
    )
    session.add(model)
    return model


async def next_catalog_revision(session, run_id: str) -> int:
    """Allocate the next Run Catalog frontier while the caller holds the Run lock."""

    maximum = await session.scalar(
        select(func.max(DataCatalogEntryModel.catalog_revision)).where(
            DataCatalogEntryModel.run_id == run_id
        )
    )
    return int(maximum or 0) + 1


__all__ = [
    "PostgresDataRuntimeRepositoryMixin",
    "append_catalog_entry",
    "next_catalog_revision",
]


def _operation_detail_json(record: CriticalOperationRecord) -> dict:
    detail = record.detail
    if isinstance(detail, (DataTransformOperationDetail, CadMeasurementOperationDetail)):
        # Exact input bindings live in the normalized append-only relation so
        # they remain enforceable by tenant/task/member foreign keys.
        return detail.model_dump(mode="json", exclude={"inputs", "ifc_source"})
    if isinstance(detail, SourceIngestionOperationDetail):
        return detail.model_dump(mode="json")
    if isinstance(detail, IfcModelInitializeOperationDetail):
        return detail.model_dump(mode="json")
    if isinstance(detail, IfcGeometryBasisConfigureOperationDetail):
        return detail.persisted_payload()
    if isinstance(detail, IfcProjectUpdateOperationDetail):
        return detail.persisted_payload()
    if isinstance(detail, IfcSpatialStructureApplyOperationDetail):
        return detail.persisted_payload()
    if isinstance(detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail)):
        return detail.persisted_payload()
    if isinstance(
        detail,
        (AuthoringDraftWriteOperationDetail, AuthoringPublishOperationDetail),
    ):
        return detail.model_dump(mode="json")
    if isinstance(detail, ProductFamilyWriteOperationDetail):
        if detail.mode != "system_copy":
            raise ValueError("retired executing Family Write cannot be serialized")
        standard = detail.version_draft.family_contract.standard_model
        if standard is None:
            raise ValueError("New Family Operation serialization requires a formal standard model capability")
        return detail.model_dump(mode="json")
    if isinstance(detail, GeometryInspectionOperationDetail):
        return detail.model_dump(mode="json")
    if record.operation_kind is CriticalOperationKind.SOURCE_INGESTION:
        return SourceIngestionOperationDetail(
            filename=record.source_filename,
        ).model_dump(mode="json")
    raise ValueError("critical Operation detail is missing")


def _payload_name_model(member: DataPackageMemberModel) -> str | None:
    if member.logical_payload_name is not None:
        return member.logical_payload_name
    parts = PurePosixPath(member.member_name).parts
    if member.member_kind == PackageMemberKind.PAYLOAD.value and len(parts) >= 3:
        if parts[0] == "payloads":
            return parts[1]
    return None
