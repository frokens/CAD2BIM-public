"""Concurrency-safe in-memory Data Runtime persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from json import loads
from pathlib import PurePosixPath
from secrets import compare_digest, token_urlsafe
from typing import Mapping
from uuid import uuid4

from CAD2BIM_backend.domain.control_plane import (
    ConflictError,
    ExecutionMode,
    ExecutionProtocolStatus,
    IdempotencyConflictError,
    RunRecord,
    RunStatus,
    TaskRecord,
    TaskStatus,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.data_runtime import (
    CatalogEntryPage,
    CatalogResourceKind,
    CadMeasurementOperationDetail,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataCatalogEntry,
    DataPackageMemberRecord,
    DataPackageRecord,
    DataTransformCandidate,
    DataTransformExecutionClaim,
    DataTransformOperationState,
    DataTransformOperationDetail,
    DirectSessionBinding,
    OpenDirectSessionRequest,
    OpenedDirectSession,
    PublishedCriticalOperation,
    PublishedOperationCatalogSummary,
    CriticalOperationCatalogSummary,
    PublishedDataPackage,
    PublishedImportResult,
    PublishedPackageResult,
    validate_package_publication,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.memory.state import MemoryRepositoryBase, _now
from CAD2BIM_backend.infrastructure.data_transform.executor_identity import (
    data_transform_executor_stopped,
)


class DataRuntimeRepositoryMixin(MemoryRepositoryBase):
    async def open_direct_session(
        self,
        request: OpenDirectSessionRequest,
    ) -> OpenedDirectSession:
        key = (
            request.tenant_id,
            request.configured_subject,
            request.normalized_session_name,
        )
        async with self._lock:
            existing = self._state.direct_sessions.get(key)
            if existing is not None:
                if (
                    existing.owner_id != request.owner_id
                    or existing.principal_id != request.principal_id
                ):
                    raise ConflictError("Direct session identity binding changed")
                return OpenedDirectSession(binding=existing, resumed=True)

            now = _now()
            suffix = uuid4().hex
            task_id = f"task_{suffix}"
            run_id = f"run_{suffix}"
            ordinal = 1 + max(
                (
                    item.ordinal
                    for (item_tenant, item_subject, _), item in self._state.direct_sessions.items()
                    if item_tenant == request.tenant_id
                    and item_subject == request.configured_subject
                ),
                default=0,
            )
            binding = DirectSessionBinding(
                session_id=f"direct_{uuid4().hex}",
                direct_session_alias=f"direct_{ordinal:03d}",
                ordinal=ordinal,
                configured_subject=request.configured_subject,
                session_name=request.session_name,
                normalized_session_name=request.normalized_session_name,
                owner_id=request.owner_id,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                task_id=task_id,
                run_id=run_id,
                created_at=now,
            )
            task = TaskRecord(
                task_id=task_id,
                owner_id=request.owner_id,
                tenant_id=request.tenant_id,
                title=request.session_name,
                status=TaskStatus.RUNNING,
                active_module="data_runtime",
                idempotency_key=f"direct-session:{binding.session_id}",
                created_at=now,
                updated_at=now,
            )
            run = RunRecord(
                run_id=run_id,
                task_id=task_id,
                owner_id=request.owner_id,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                module="data_runtime",
                stage_key="direct",
                idempotency_key=f"direct-session:{binding.session_id}",
                thread_id=f"thread_{run_id}",
                execution_mode=ExecutionMode.DIRECT_MCP,
                execution_protocol_status=ExecutionProtocolStatus.NOT_APPLICABLE,
                execution_protocol=None,
                execution_protocol_quarantine_reason=None,
                status=RunStatus.RUNNING,
                created_at=now,
                updated_at=now,
                started_at=now,
            )
            self._tasks[task_id] = task
            self._runs[run_id] = run
            self._events[run_id] = []
            self._state.direct_sessions[key] = binding
            return OpenedDirectSession(binding=binding, resumed=False)

    async def get_direct_session_by_alias(
        self,
        *,
        tenant_id: str,
        configured_subject: str,
        session_alias: str,
    ) -> OpenedDirectSession | None:
        async with self._lock:
            binding = next(
                (
                    item
                    for (item_tenant, item_subject, _), item in self._state.direct_sessions.items()
                    if item_tenant == tenant_id
                    and item_subject == configured_subject
                    and item.direct_session_alias == session_alias
                ),
                None,
            )
            if binding is None:
                return None
            return OpenedDirectSession(binding=binding, resumed=True)

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
        async with self._lock:
            self._require_run_scope(scope)
            operation_id = self._state.data_transform_request_replays.get(
                (scope.run_id, request_identity)
            )
            state = self._state.data_transform_operations.get(operation_id) if operation_id else None
            return state if state and state.operation.tool_name == "data_derive" else None

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
        async with self._lock:
            self._require_run_scope(scope)
            existing_id = self._state.data_transform_request_replays.get(
                (scope.run_id, request_identity)
            )
            if existing_id is not None:
                existing = self._state.data_transform_operations[existing_id]
                if existing.operation.tool_name != "data_derive":
                    raise ConflictError("The original producer is retired; create a new data derivation")
                if existing.request_digest != request_digest or existing.request_payload != declared:
                    raise IdempotencyConflictError(
                        "Data Transform request identity was reused with different input"
                    )
                return existing
            if (
                prepared.owner_id != scope.owner_id
                or prepared.tenant_id != scope.identity.tenant_id
                or prepared.task_id != scope.identity.task_id
                or prepared.run_id != scope.run_id
                or prepared.operation_id in self._state.critical_operations
                or (prepared.run_id, prepared.idempotency_key)
                in self._state.critical_operation_replays
            ):
                raise ConflictError("Data Transform admission identity conflicts")
            self._require_operation_artifacts(prepared)
            self._require_repair_parent(scope, prepared)
            if prepared.detail.ifc_source is not None:
                raise ConflictError("This in-memory repository has no authoritative IFC revision reader")
            self._require_transform_inputs(scope, prepared)
            state = DataTransformOperationState(
                operation=prepared,
                request_identity=request_identity,
                request_digest=request_digest,
                request_payload=declared,
                resolved_command=resolved,
                resolved_parent_operation_alias=resolved_parent_operation_alias,
                deadline_at=deadline_at,
                execution_generation=0,
            )
            self._state.critical_operations[prepared.operation_id] = prepared
            self._state.critical_operation_replays[
                (prepared.run_id, prepared.idempotency_key)
            ] = prepared.operation_id
            self._state.data_transform_operations[prepared.operation_id] = state
            self._state.data_transform_request_replays[
                (prepared.run_id, request_identity)
            ] = prepared.operation_id
            return state

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
        async with self._lock:
            self._require_run_scope(scope)
            state = self._transform_state(scope, operation_id)
            if (
                state is None
                or state.operation.status is not CriticalOperationStatus.PREPARED
                or state.execution_owner is not None
                or state.deadline_at <= _now()
            ):
                return None
            token = token_urlsafe(32)
            claim = DataTransformExecutionClaim(
                operation_id=operation_id,
                owner=owner,
                generation=state.execution_generation + 1,
                token=token,
            )
            self._state.data_transform_operations[operation_id] = state.model_copy(
                update={
                    "execution_owner": owner,
                    "execution_generation": claim.generation,
                    "execution_lease_expires_at": _now() + timedelta(seconds=lease_seconds),
                    "execution_token_sha256": sha256(token.encode("utf-8")).hexdigest(),
                }
            )
            return claim

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
        async with self._lock:
            self._require_run_scope(scope)
            state = self._transform_state(scope, operation_id)
            if (
                state is None
                or state.operation.status is not CriticalOperationStatus.PREPARED
                or state.execution_owner != expected_owner
                or state.execution_generation != expected_generation
                or not data_transform_executor_stopped(expected_owner)
                or (state.candidate is None and state.deadline_at <= _now())
            ):
                return None
            token = token_urlsafe(32)
            claim = DataTransformExecutionClaim(
                operation_id=operation_id,
                owner=owner,
                generation=state.execution_generation + 1,
                token=token,
            )
            self._state.data_transform_operations[operation_id] = state.model_copy(
                update={
                    "execution_owner": owner,
                    "execution_generation": claim.generation,
                    "execution_lease_expires_at": _now() + timedelta(seconds=lease_seconds),
                    "execution_token_sha256": sha256(token.encode("utf-8")).hexdigest(),
                }
            )
            return claim

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
        async with self._lock:
            self._require_run_scope(scope)
            state = self._transform_state(scope, operation_id)
            if state is None or state.operation.status is not CriticalOperationStatus.PREPARED:
                return None
            if (
                state.candidate is not None
                or state.deadline_at > _now()
                or state.execution_owner != expected_owner
                or state.execution_generation != expected_generation
                or (
                    expected_owner is None
                    and (expected_generation != 0 or state.execution_token_sha256 is not None)
                )
                or (
                    expected_owner is not None
                    and not data_transform_executor_stopped(expected_owner)
                )
            ):
                return None
            self._assert_transform_terminal_matches(state.operation, failed_operation)
            entry = self._next_catalog_entry(
                state.operation.owner_id,
                state.operation.tenant_id,
                state.operation.task_id,
                state.operation.run_id,
                CatalogResourceKind.OPERATION,
                catalog_revision=self._next_catalog_revision(state.operation.run_id),
                operation_id=operation_id,
            )
            settled = state.model_copy(update={"operation": failed_operation})
            self._state.critical_operations[operation_id] = failed_operation
            self._state.data_transform_operations[operation_id] = settled
            self._state.data_catalog_entries.setdefault(state.operation.run_id, []).append(entry)
            return settled

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
        async with self._lock:
            self._require_run_scope(scope)
            state = self._transform_state(scope, operation_id)
            if state is None or state.operation.status is not CriticalOperationStatus.PREPARED:
                raise ConflictError("Data Transform Operation is not prepared")
            self._require_transform_claim(state, execution_claim)
            proposed = CriticalOperationRecord.model_validate(
                state.operation.model_dump(mode="python") | {
                    "status": CriticalOperationStatus.SUCCEEDED,
                    "result_package_id": package.package_id,
                    "finished_at": _now(),
                }
            )
            validate_package_publication(package, member_tuple, proposed)
            self._require_exact_member_artifacts(member_tuple)
            candidate = DataTransformCandidate(package=package, members=member_tuple)
            if state.candidate is not None and state.candidate != candidate:
                raise ConflictError("Data Transform candidate changed for the same Operation")
            updated = state.model_copy(update={"candidate": candidate})
            self._state.data_transform_operations[operation_id] = updated
            return updated

    async def get_result_for_operation(
        self, scope: InvocationScope, operation_id: str
    ) -> PublishedPackageResult | None:
        async with self._lock:
            self._require_run_scope(scope)
            state = self._transform_state(scope, operation_id)
            if state is None or state.operation.status is not CriticalOperationStatus.SUCCEEDED:
                return None
            return self._published_package_result(state.operation)

    async def get_input_package_for_operation(
        self, scope: InvocationScope, operation_id: str, package_id: str
    ) -> PublishedDataPackage | None:
        async with self._lock:
            self._require_run_scope(scope)
            state = self._transform_state(scope, operation_id)
            if state is None or not isinstance(state.operation.detail, DataTransformOperationDetail):
                return None
            if not any(item.package_id == package_id for item in state.operation.detail.inputs):
                return None
            package = self._state.data_packages.get(package_id)
            if package is None or package.owner_id != scope.owner_id or package.task_id != scope.identity.task_id:
                return None
            entry = next(
                (
                    item for item in self._state.data_catalog_entries.get(scope.run_id, ())
                    if item.package_id == package_id
                ), None
            )
            if entry is None:
                return None
            return PublishedDataPackage(
                package=package,
                members=self._state.data_package_members[package_id],
                catalog_entry=entry,
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
        async with self._lock:
            self._require_run_scope(scope)
            self._require_exact_member_artifacts(member_tuple)
            self._require_operation_artifacts(operation)
            self._require_repair_parent(scope, operation)
            self._require_transform_inputs(scope, operation)
            if (
                operation.run_id != package.created_by_run_id
                or operation.owner_id != package.owner_id
                or operation.tenant_id != package.tenant_id
                or operation.task_id != package.task_id
            ):
                raise ValueError("package and operation must belong to the same Run")
            if (
                package.owner_id != scope.owner_id
                or package.tenant_id != scope.identity.tenant_id
                or package.task_id != scope.identity.task_id
                or package.created_by_run_id != scope.run_id
            ):
                raise ConflictError("Data Runtime publication does not match scope")
            replay = self._state.critical_operation_replays.get(
                (operation.run_id, operation.idempotency_key)
            )
            if replay is not None:
                existing = self._state.critical_operations[replay]
                if existing.request_fingerprint != operation.request_fingerprint:
                    raise IdempotencyConflictError(
                        "operation idempotency key was reused with different input"
                    )
                return self._published_package_result(existing)
            fingerprint_replay = next(
                (
                    item
                    for item in self._state.critical_operations.values()
                    if item.run_id == operation.run_id
                    and item.tool_name == operation.tool_name
                    and item.request_fingerprint == operation.request_fingerprint
                ),
                None,
            )
            if fingerprint_replay is not None:
                return self._published_package_result(fingerprint_replay)

            package_replay = self._state.data_package_replays.get(
                (package.task_id, package.idempotency_key)
            )
            if package_replay is not None:
                existing = self._state.data_packages[package_replay]
                if existing.request_fingerprint != package.request_fingerprint:
                    raise IdempotencyConflictError(
                        "package idempotency key was reused with different input"
                    )
                raise ConflictError("package exists without the requested operation")

            package_entry = self._next_catalog_entry(
                package.owner_id,
                package.tenant_id,
                package.task_id,
                package.created_by_run_id,
                CatalogResourceKind.PACKAGE,
                catalog_revision=self._next_catalog_revision(operation.run_id),
                package_id=package.package_id,
            )
            operation_entry = self._next_catalog_entry(
                operation.owner_id,
                operation.tenant_id,
                operation.task_id,
                operation.run_id,
                CatalogResourceKind.OPERATION,
                catalog_revision=package_entry.catalog_revision,
                operation_id=operation.operation_id,
            )
            self._state.data_packages[package.package_id] = package
            self._state.data_package_members[package.package_id] = member_tuple
            self._state.critical_operations[operation.operation_id] = operation
            self._state.data_package_replays[(package.task_id, package.idempotency_key)] = (
                package.package_id
            )
            self._state.critical_operation_replays[
                (operation.run_id, operation.idempotency_key)
            ] = operation.operation_id
            entries = self._state.data_catalog_entries.setdefault(operation.run_id, [])
            entries.extend((package_entry, operation_entry))
            return PublishedPackageResult(
                package=PublishedDataPackage(
                    package=package,
                    members=member_tuple,
                    catalog_entry=package_entry,
                ),
                operation=PublishedCriticalOperation(
                    operation=operation,
                    catalog_entry=operation_entry,
                ),
            )

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
        async with self._lock:
            self._require_run_scope(scope)
            self._require_operation_artifacts(operation)
            self._require_repair_parent(scope, operation)
            self._require_transform_inputs(scope, operation)
            if (
                operation.owner_id != scope.owner_id
                or operation.tenant_id != scope.identity.tenant_id
                or operation.task_id != scope.identity.task_id
                or operation.run_id != scope.run_id
            ):
                raise ConflictError("Data Runtime operation does not match scope")
            replay = self._state.critical_operation_replays.get(
                (operation.run_id, operation.idempotency_key)
            )
            if replay is not None:
                existing = self._state.critical_operations[replay]
                if existing.request_fingerprint != operation.request_fingerprint:
                    raise IdempotencyConflictError(
                        "operation idempotency key was reused with different input"
                    )
                published = self._published_operation(existing)
                if published is None:
                    raise ConflictError("operation Catalog entry is missing")
                return published
            fingerprint_replay = next(
                (
                    item
                    for item in self._state.critical_operations.values()
                    if item.run_id == operation.run_id
                    and item.tool_name == operation.tool_name
                    and item.request_fingerprint == operation.request_fingerprint
                ),
                None,
            )
            if fingerprint_replay is not None:
                published = self._published_operation(fingerprint_replay)
                if published is None:
                    raise ConflictError("operation Catalog entry is missing")
                return published
            entry = self._next_catalog_entry(
                operation.owner_id,
                operation.tenant_id,
                operation.task_id,
                operation.run_id,
                CatalogResourceKind.OPERATION,
                catalog_revision=self._next_catalog_revision(operation.run_id),
                operation_id=operation.operation_id,
            )
            self._state.critical_operations[operation.operation_id] = operation
            self._state.critical_operation_replays[
                (operation.run_id, operation.idempotency_key)
            ] = operation.operation_id
            self._state.data_catalog_entries.setdefault(operation.run_id, []).append(entry)
            return PublishedCriticalOperation(operation=operation, catalog_entry=entry)

    async def _publish_transform_result(
        self,
        scope: InvocationScope,
        package: DataPackageRecord,
        members: tuple[DataPackageMemberRecord, ...],
        operation: CriticalOperationRecord,
        claim: DataTransformExecutionClaim,
    ) -> PublishedPackageResult:
        async with self._lock:
            self._require_run_scope(scope)
            state = self._transform_state(scope, operation.operation_id)
            if state is None:
                raise ConflictError("Data Transform Operation was not admitted")
            self._require_transform_claim(state, claim)
            if state.operation.status is CriticalOperationStatus.SUCCEEDED:
                self._assert_transform_terminal_matches(state.operation, operation)
                return self._published_package_result(state.operation)
            if state.operation.status is not CriticalOperationStatus.PREPARED:
                raise ConflictError("Data Transform Operation is already terminal")
            self._assert_transform_terminal_matches(state.operation, operation)
            if state.candidate != DataTransformCandidate(package=package, members=members):
                raise ConflictError("Data Transform candidate is not the admitted exact output")
            self._require_exact_member_artifacts(members)
            if package.package_id in self._state.data_packages or (
                package.task_id, package.idempotency_key
            ) in self._state.data_package_replays:
                raise ConflictError("Data Transform candidate Package identity already exists")
            package_entry = self._next_catalog_entry(
                package.owner_id,
                package.tenant_id,
                package.task_id,
                package.created_by_run_id,
                CatalogResourceKind.PACKAGE,
                catalog_revision=self._next_catalog_revision(operation.run_id),
                package_id=package.package_id,
            )
            operation_entry = self._next_catalog_entry(
                operation.owner_id,
                operation.tenant_id,
                operation.task_id,
                operation.run_id,
                CatalogResourceKind.OPERATION,
                catalog_revision=package_entry.catalog_revision,
                operation_id=operation.operation_id,
            )
            self._state.data_packages[package.package_id] = package
            self._state.data_package_members[package.package_id] = members
            self._state.critical_operations[operation.operation_id] = operation
            self._state.data_transform_operations[operation.operation_id] = state.model_copy(
                update={"operation": operation}
            )
            self._state.data_package_replays[
                (package.task_id, package.idempotency_key)
            ] = package.package_id
            self._state.data_catalog_entries.setdefault(operation.run_id, []).extend(
                (package_entry, operation_entry)
            )
            return PublishedPackageResult(
                package=PublishedDataPackage(
                    package=package, members=members, catalog_entry=package_entry
                ),
                operation=PublishedCriticalOperation(
                    operation=operation, catalog_entry=operation_entry
                ),
            )

    async def _fail_transform_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        claim: DataTransformExecutionClaim,
    ) -> PublishedCriticalOperation:
        async with self._lock:
            self._require_run_scope(scope)
            state = self._transform_state(scope, operation.operation_id)
            if state is None:
                raise ConflictError("Data Transform Operation was not admitted")
            self._require_transform_claim(state, claim)
            if state.operation.status is CriticalOperationStatus.FAILED:
                self._assert_transform_terminal_matches(state.operation, operation)
                published = self._published_operation(state.operation)
                if published is None:
                    raise ConflictError("Operation Catalog entry is missing")
                return published
            if state.operation.status is not CriticalOperationStatus.PREPARED:
                raise ConflictError("Data Transform Operation is already terminal")
            if state.candidate is not None:
                raise ConflictError("Data Transform candidate must be reconciled before failure")
            self._assert_transform_terminal_matches(state.operation, operation)
            entry = self._next_catalog_entry(
                operation.owner_id,
                operation.tenant_id,
                operation.task_id,
                operation.run_id,
                CatalogResourceKind.OPERATION,
                catalog_revision=self._next_catalog_revision(operation.run_id),
                operation_id=operation.operation_id,
            )
            self._state.critical_operations[operation.operation_id] = operation
            self._state.data_transform_operations[operation.operation_id] = state.model_copy(
                update={"operation": operation}
            )
            self._state.data_catalog_entries.setdefault(operation.run_id, []).append(entry)
            return PublishedCriticalOperation(operation=operation, catalog_entry=entry)

    def _transform_state(
        self, scope: InvocationScope, operation_id: str
    ) -> DataTransformOperationState | None:
        state = self._state.data_transform_operations.get(operation_id)
        if state is None or (
            state.operation.owner_id != scope.owner_id
            or state.operation.tenant_id != scope.identity.tenant_id
            or state.operation.task_id != scope.identity.task_id
            or state.operation.run_id != scope.run_id
            or state.operation.tool_name != "data_derive"
        ):
            return None
        return state

    @staticmethod
    def _require_transform_claim(
        state: DataTransformOperationState, claim: DataTransformExecutionClaim
    ) -> None:
        if (
            state.operation.operation_id != claim.operation_id
            or state.execution_owner != claim.owner
            or state.execution_generation != claim.generation
            or state.execution_token_sha256 is None
            or not compare_digest(
                state.execution_token_sha256,
                sha256(claim.token.encode("utf-8")).hexdigest(),
            )
        ):
            raise ConflictError("Data Transform execution claim is stale")

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

    async def get_package(
        self,
        scope: InvocationScope,
        package_id: str,
    ) -> PublishedDataPackage | None:
        async with self._lock:
            self._require_run_scope(scope)
            package = self._state.data_packages.get(package_id)
            if (
                package is None
                or package.owner_id != scope.owner_id
                or package.task_id != scope.identity.task_id
            ):
                return None
            entry = next(
                (
                    item
                    for item in self._state.data_catalog_entries.get(scope.run_id, ())
                    if item.package_id == package_id
                ),
                None,
            )
            if entry is None or (
                scope.catalog_frontier is not None
                and entry.catalog_revision > scope.catalog_frontier
            ):
                return None
            return PublishedDataPackage(
                package=package,
                members=self._state.data_package_members[package_id],
                catalog_entry=entry,
            )

    async def list_catalog(
        self,
        scope: InvocationScope,
        resource_kind: CatalogResourceKind | None = None,
    ) -> tuple[DataCatalogEntry, ...]:
        async with self._lock:
            run = self._require_run_scope(scope)
            entries = self._state.data_catalog_entries.get(run.run_id, ())
            return tuple(
                item
                for item in entries
                if (resource_kind is None or item.resource_kind is resource_kind)
                and (
                    scope.catalog_frontier is None
                    or item.catalog_revision <= scope.catalog_frontier
                )
            )

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
        async with self._lock:
            run = self._require_run_scope(scope)
            matching = sorted(
                (
                    item
                    for item in self._state.data_catalog_entries.get(run.run_id, ())
                    if item.resource_kind is resource_kind
                    and (
                        operation_kind is None
                        or (
                            item.operation_id is not None
                            and self._state.critical_operations[item.operation_id].operation_kind
                            is operation_kind
                        )
                    )
                    and (
                        operation_tool_names is None
                        or (
                            item.operation_id is not None
                            and self._state.critical_operations[item.operation_id].tool_name
                            in operation_tool_names
                        )
                    )
                    and (
                        operation_statuses is None
                        or (
                            item.operation_id is not None
                            and self._state.critical_operations[item.operation_id].status
                            in operation_statuses
                        )
                    )
                    and (
                        not operation_requires_code_artifact
                        or (
                            item.operation_id is not None
                            and self._state.critical_operations[item.operation_id].code_artifact
                            is not None
                        )
                    )
                    and (before_sequence is None or item.ordinal < before_sequence)
                    and (
                        scope.catalog_frontier is None
                        or item.catalog_revision <= scope.catalog_frontier
                    )
                ),
                key=lambda item: item.ordinal,
                reverse=True,
            )
            entries = tuple(matching[:page_size])
            complete = len(matching) <= page_size
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
        async with self._lock:
            run = self._require_run_scope(scope)
            return next(
                (
                    item
                    for item in self._state.data_catalog_entries.get(run.run_id, ())
                    if item.resource_kind is resource_kind
                    and item.alias == alias
                    and (
                        scope.catalog_frontier is None
                        or item.catalog_revision <= scope.catalog_frontier
                    )
                ),
                None,
            )

    async def get_operation_for_result_package(self, scope, package_id):
        async with self._lock:
            run = self._require_run_scope(scope)
            operation = next((item for item in self._state.critical_operations.values()
                if item.owner_id == scope.owner_id and item.tenant_id == scope.identity.tenant_id
                and item.task_id == scope.identity.task_id and item.run_id == run.run_id
                and item.result_package_id == package_id
                and item.status in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}), None)
            if operation is None:
                return None
            published = self._published_operation(operation, run_id=run.run_id)
            if published is None or (scope.catalog_frontier is not None
                and published.catalog_entry.catalog_revision > scope.catalog_frontier):
                return None
            return published

    async def get_operation_by_locator(self, scope, *, origin_run_id, operation_id):
        if origin_run_id != scope.run_id and scope.catalog_frontier is not None:
            return None
        return await self.get_operation(scope.model_copy(update={"run_id": origin_run_id}), operation_id)

    async def get_operation_summary(self, scope, operation_id):
        published = await self.get_operation(scope, operation_id)
        if published is None:
            return None
        record = published.operation
        return PublishedOperationCatalogSummary(catalog_entry=published.catalog_entry,
            operation=CriticalOperationCatalogSummary(**{key: getattr(record, key) for key in (
                "operation_id", "run_id", "operation_kind", "initiator", "tool_name", "intent_summary",
                "status", "started_at", "finished_at", "error_code")}))

    async def get_operation(
        self,
        scope: InvocationScope,
        operation_id: str,
    ) -> PublishedCriticalOperation | None:
        async with self._lock:
            run = self._require_run_scope(scope)
            operation = self._state.critical_operations.get(operation_id)
            if operation is None:
                return None
            published = self._published_operation(operation, run_id=run.run_id)
            if published is None:
                return None
            if (
                scope.catalog_frontier is not None
                and published.catalog_entry.catalog_revision > scope.catalog_frontier
            ):
                return None
            return published

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
        async with self._lock:
            run = self._require_run_scope(scope)
            matches = tuple(
                item
                for item in self._state.critical_operations.values()
                if item.run_id == run.run_id
                and item.tool_name in names
                and item.request_fingerprint == request_fingerprint
            )
            if len(matches) > 1:
                raise ConflictError("Operation replay identity is ambiguous")
            operation = matches[0] if matches else None
            if operation is None:
                return None
            # Technical response-loss reconcile is scoped to the originating Run
            # and deliberately bypasses a historical public Catalog frontier.
            return self._published_operation(operation, run_id=run.run_id)

    def _require_run_scope(self, scope: InvocationScope) -> RunRecord:
        run = self._require_run(scope.owner_id, scope.run_id)
        if (
            run.tenant_id != scope.identity.tenant_id
            or run.principal_id != scope.identity.principal_id
            or run.task_id != scope.identity.task_id
        ):
            raise ConflictError("Data Runtime resource is outside the Run Task")
        if run.execution_mode is not scope.execution_mode:
            raise ConflictError("Data Runtime scope execution mode does not match Run")
        return run

    def _require_exact_member_artifacts(
        self,
        members: Sequence[DataPackageMemberRecord],
    ) -> None:
        for member in members:
            artifact = self._artifacts.get(member.artifact.artifact_id)
            if (
                artifact is None
                or artifact.owner_id != member.owner_id
                or artifact.task_id != member.task_id
                or artifact.version_id != member.artifact.version_id
                or artifact.sha256 != member.artifact.sha256
            ):
                raise ConflictError("Data Package member Artifact version is not exact")

    def _require_operation_artifacts(self, operation: CriticalOperationRecord) -> None:
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
            artifact = self._artifacts.get(reference.artifact_id)
            if (
                artifact is None
                or artifact.owner_id != operation.owner_id
                or artifact.task_id != operation.task_id
                or artifact.version_id != reference.version_id
                or artifact.sha256 != reference.sha256
            ):
                raise ConflictError(f"Operation {label} Artifact version is not exact")

    def _require_repair_parent(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
    ) -> None:
        parent_id = operation.repair_parent_operation_id
        if parent_id is None:
            return
        parent = self._state.critical_operations.get(parent_id)
        if (
            parent is None
            or parent.owner_id != operation.owner_id
            or parent.tenant_id != operation.tenant_id
            or parent.task_id != operation.task_id
            or parent.run_id != operation.run_id
            or parent.run_id != scope.run_id
            or parent.status
            not in {
                CriticalOperationStatus.FAILED,
                CriticalOperationStatus.SUCCEEDED,
            }
            or parent.operation_kind is not CriticalOperationKind.DATA_TRANSFORM
            or parent.tool_name != "data_derive"
            or not isinstance(parent.detail, DataTransformOperationDetail)
            or parent.code_artifact is None
            or parent.description_artifact is None
        ):
            raise ConflictError("Repair parent Operation is not an exact repairable transform")
        if operation.detail.ifc_source != parent.detail.ifc_source:
            raise ConflictError("Repair must retain its original exact IFC source")

    def _require_transform_inputs(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
    ) -> None:
        if operation.operation_kind is not CriticalOperationKind.DATA_TRANSFORM:
            return
        detail = operation.detail
        if not isinstance(
            detail,
            (DataTransformOperationDetail, CadMeasurementOperationDetail),
        ):
            raise ConflictError("Data Transform detail is missing")
        entries = self._state.data_catalog_entries.get(scope.run_id, ())
        for binding in detail.inputs:
            package = self._state.data_packages.get(binding.package_id)
            members = self._state.data_package_members.get(binding.package_id, ())
            member = next((item for item in members if item.member_id == binding.member_id), None)
            visible = next(
                (
                    item
                    for item in entries
                    if item.package_id == binding.package_id
                    and (
                        scope.catalog_frontier is None
                        or item.catalog_revision <= scope.catalog_frontier
                    )
                ),
                None,
            )
            if (
                package is None
                or member is None
                or visible is None
                or package.task_id != operation.task_id
                or member.member_kind.value != "payload"
                or _payload_name(member) != binding.payload_name
            ):
                raise ConflictError("Data Transform input binding is not exact and visible")

    def _next_catalog_entry(
        self,
        owner_id: str,
        tenant_id: str,
        task_id: str,
        run_id: str,
        kind: CatalogResourceKind,
        *,
        catalog_revision: int,
        package_id: str | None = None,
        operation_id: str | None = None,
    ) -> DataCatalogEntry:
        entries = self._state.data_catalog_entries.get(run_id, ())
        ordinal = 1 + max(
            (item.ordinal for item in entries if item.resource_kind is kind),
            default=0,
        )
        prefix = "data" if kind is CatalogResourceKind.PACKAGE else "op"
        return DataCatalogEntry(
            catalog_entry_id=f"catalog_{uuid4().hex}",
            owner_id=owner_id,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            resource_kind=kind,
            alias=f"{prefix}_{ordinal:03d}",
            ordinal=ordinal,
            catalog_revision=catalog_revision,
            package_id=package_id,
            operation_id=operation_id,
            created_at=_now(),
        )

    def _next_catalog_revision(self, run_id: str) -> int:
        return 1 + max(
            (item.catalog_revision for item in self._state.data_catalog_entries.get(run_id, ())),
            default=0,
        )

    def _published_operation(
        self,
        operation: CriticalOperationRecord,
        *,
        run_id: str | None = None,
    ) -> PublishedCriticalOperation | None:
        target_run = run_id or operation.run_id
        entry = next(
            (
                item
                for item in self._state.data_catalog_entries.get(target_run, ())
                if item.operation_id == operation.operation_id
            ),
            None,
        )
        if entry is None:
            return None
        return PublishedCriticalOperation(operation=operation, catalog_entry=entry)

    def _published_import_result(
        self,
        operation: CriticalOperationRecord,
    ) -> PublishedImportResult:
        published = self._published_package_result(operation)
        return PublishedImportResult(
            package=published.package,
            operation=published.operation,
        )

    def _published_package_result(
        self,
        operation: CriticalOperationRecord,
    ) -> PublishedPackageResult:
        if operation.result_package_id is None:
            raise ConflictError("successful operation has no result package")
        package = self._state.data_packages[operation.result_package_id]
        package_entry = next(
            item
            for item in self._state.data_catalog_entries[operation.run_id]
            if item.package_id == package.package_id
        )
        published_operation = self._published_operation(operation)
        if published_operation is None:
            raise ConflictError("operation Catalog entry is missing")
        return PublishedPackageResult(
            package=PublishedDataPackage(
                package=package,
                members=self._state.data_package_members[package.package_id],
                catalog_entry=package_entry,
            ),
            operation=published_operation,
        )


__all__ = ["DataRuntimeRepositoryMixin"]


def _payload_name(member: DataPackageMemberRecord) -> str | None:
    if member.logical_payload_name is not None:
        return member.logical_payload_name
    parts = PurePosixPath(member.member_name).parts
    if member.member_kind.value == "payload" and len(parts) >= 3 and parts[0] == "payloads":
        return parts[1]
    return None
