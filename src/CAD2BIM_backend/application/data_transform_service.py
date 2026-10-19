"""M2a orchestration for one immutable DuckDB SQL transformation.

The service is entry-neutral: callers provide a server-resolved invocation
scope and a business command.  Direct session aliases, authentication fields,
filesystem paths, and provider DTOs do not cross this boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import AsyncIterator, Awaitable, Literal, Mapping, TypeAlias, TypeVar

import jsonschema
import pyarrow as pa
from pydantic import ValidationError
import pyarrow.parquet as pq

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcRevision, IfcModelFrozenError, IfcModelIntegrityError,
    IfcModelNotInitializedError, IfcRunIdentity,
)
from CAD2BIM_backend.domain.runtime_execution import DATA_PYTHON_EXECUTION, DATA_SQL_EXECUTION
from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactJsonPayload,
    ExactParquetPayload,
    ExactPayloadInput,
    ExactPayloadResolutionError,
    ExactPayloadResolver,
    ExactTransformPayload,
    exact_payload_arrow_inputs,
    exact_payload_members,
)
from CAD2BIM_backend.application.package_synopsis import (
    PackageSynopsis,
    PackageSynopsisCompiler,
    PackageSynopsisError,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest, stable_id
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, IdempotencyConflictError
from CAD2BIM_backend.domain.data_runtime import (
    ArrowColumnRecord,
    ArtifactVersionRef,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataPackageKind,
    DataPackageMemberRecord,
    DataPackageRecord,
    DataTransformExecutionClaim,
    DataTransformOperationDetail,
    DataTransformOperationState,
    IfcReadSourceRef,
    IfcSourceChangedError,
    JsonPayloadKind,
    MAX_ARROW_TYPE_UTF8_BYTES,
    PackageMemberKind,
    PublishedCriticalOperation,
    PublishedDataPackage,
    PublishedPackageResult,
    TransformInputBindingRecord,
    TransformOutputRecord,
    TransformParameterKind,
    TransformParameterRecord,
    validate_arrow_type_string,
)
from CAD2BIM_backend.domain.data_transform import (
    MAX_TRANSFORM_CODE_BYTES,
    DataRepairTransformCommand,
    DataTransformCommand,
    DataTransformWorkerError,
    DataTransformWorkerRequest,
    JsonOutputDeclaration,
    JsonOutputInput,
    JsonSchemaSet,
    TransformOutputsSet,
    parse_data_schema_change,
    PythonArrowExecution,
    PythonArrowWorkerRequest,
    truncate_transform_error_message,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.text_source import SourceEdit, TextChange, TextSet
from CAD2BIM_backend.ports.source_patch import SourcePatchPort, SourcePatchPortError
from CAD2BIM_backend.infrastructure.data_transform.executor_identity import (
    current_data_transform_executor_owner,
)
from CAD2BIM_backend.ports.data_runtime import DataRuntimeRepository
from CAD2BIM_backend.ports.data_transform import (
    ArrowTransformInput,
    ArrowTransformOutput,
    DataTransformWorkerPort,
    PythonArrowDataTransformWorkerPort,
    IfcPythonArrowDataTransformWorkerPort,
)
from CAD2BIM_backend.ports.ifc_query_worker import MAX_IFC_INPUT_BYTES
from CAD2BIM_backend.ports.storage import StorageError


_TOOL_NAME = "data_derive"
_PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
_JSON_MEDIA_TYPE = "application/json"
_JSON_SCHEMA_MEDIA_TYPE = "application/schema+json"
_MARKDOWN_MEDIA_TYPE = "text/markdown"
_SQL_MEDIA_TYPE = "application/sql"
_PYTHON_MEDIA_TYPE = "text/x-python"
_MANIFEST_MEDIA_TYPE = "application/json"
_SQL_CODE_MEMBER = "code/transformation.sql"
_PYTHON_CODE_MEMBER = "code/transformation.py"
_DESCRIPTION_MEMBER = "description.md"
_MANIFEST_MEMBER = "manifest.json"
_OUTPUT_VALIDATION_CLEANUPS: set[asyncio.Task[None]] = set()
_ACTIVE_TRANSFORM_TASKS: set[asyncio.Task[DataTransformApplicationResult]] = set()
_TOTAL_TIMEOUT_SECONDS = DATA_SQL_EXECUTION.total_seconds
_PYTHON_TOTAL_TIMEOUT_SECONDS = DATA_PYTHON_EXECUTION.total_seconds
# Execution stops before both reserves: the Worker may need its own process
# cleanup, then the Application still needs time to publish/reconcile a terminal
# failed Operation.  Neither reserve extends the engine-specific request deadline.
_WORKER_CLEANUP_RESERVE_SECONDS = DATA_SQL_EXECUTION.cleanup_seconds
_PYTHON_WORKER_CLEANUP_RESERVE_SECONDS = DATA_PYTHON_EXECUTION.cleanup_seconds
_TERMINAL_PUBLICATION_RESERVE_SECONDS = DATA_SQL_EXECUTION.terminal_seconds
_PUBLICATION_RECONCILE_RESERVE_SECONDS = 0.25
_INPUT_ROW_LIMIT = 4_000_000
_INPUT_PARQUET_BYTES = 256 * 1024 * 1024
_OUTPUT_ROW_LIMIT = 2_000_000
_OUTPUT_ARROW_BYTES = 512 * 1024 * 1024
_OUTPUT_PARQUET_BYTES = 100 * 1024 * 1024
_OUTPUT_AGGREGATE_PARQUET_BYTES = 256 * 1024 * 1024
_OUTPUT_COLUMN_LIMIT = 512
_JSON_PAYLOAD_BYTES = 100 * 1024 * 1024
_JSON_MAX_DEPTH = 64

TransformPhase = Literal[
    "preflight",
    "code_validation",
    "execution",
    "output_validation",
    "publication",
]
_T = TypeVar("_T")


class DataTransformApplicationError(RuntimeError):
    """A bounded M2a failure that an entry adapter can project to ToolError."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: TransformPhase,
        retryable: bool = False,
        operation_alias: str | None = None,
        diagnostic: Mapping[str, object] | None = None,
    ) -> None:
        bounded = truncate_transform_error_message(message)
        super().__init__(bounded)
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.operation_alias = operation_alias
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


@dataclass(frozen=True, slots=True)
class DataTransformApplicationResult:
    published: PublishedPackageResult
    output_members: tuple[DataPackageMemberRecord, ...]
    repair_parent_operation_alias: str | None = None


@dataclass(frozen=True, slots=True)
class _ValidatedParquetOutput:
    worker_output: ArrowTransformOutput
    table: pa.Table
    path: Path
    parquet_bytes: int
    persisted_schema: pa.Schema


@dataclass(frozen=True, slots=True)
class _ValidatedJsonOutput:
    worker_output: ArrowTransformOutput
    content: bytes
    schema_content: bytes


_ValidatedOutput: TypeAlias = _ValidatedParquetOutput | _ValidatedJsonOutput


@dataclass(frozen=True, slots=True)
class _ResolvedInput:
    request_name: str
    requested_package_alias: str
    payload: ExactTransformPayload
    package_id: str


@dataclass(frozen=True, slots=True)
class _ExecutionIdentity:
    tool_name: Literal["data_derive"]
    fingerprint: str
    request_identity: str
    request_payload: Mapping[str, object]
    repair_parent_operation_id: str | None = None
    repair_parent_operation_alias: str | None = None
    description_artifact: ArtifactRecord | None = None


class DataTransformService:
    """Resolve exact inputs, supervise SQL, validate output, and publish atomically."""

    def __init__(
        self,
        repository: DataRuntimeRepository,
        artifacts: ArtifactService,
        payloads: ExactPayloadResolver,
        worker: DataTransformWorkerPort,
        synopsis_compiler: PackageSynopsisCompiler | None = None,
        *,
        python_worker: PythonArrowDataTransformWorkerPort | None = None,
        source_patch: SourcePatchPort | None = None,
        ifc_revisions: IfcRevisionReader | None = None,
        ifc_worker: IfcPythonArrowDataTransformWorkerPort | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._payloads = payloads
        self._worker = worker
        self._python_worker = python_worker
        self._source_patch = source_patch
        self._ifc_revisions = ifc_revisions
        self._ifc_worker = ifc_worker
        self._synopsis = synopsis_compiler or PackageSynopsisCompiler()

    async def resume_by_identity(
        self,
        scope: InvocationScope,
        request_identity: str,
        request_payload: Mapping[str, object],
        *,
        deadline: float,
        resume_unclaimed: bool = True,
    ) -> DataTransformApplicationResult | None:
        """Read an admitted action before resolving any current catalog alias."""
        if not request_identity or not request_identity.strip():
            raise DataTransformApplicationError(
                "INVALID_ARGUMENT", "A stable server action identity is required", phase="preflight"
            )
        state = await _await_before_deadline(
            self._repository.get_data_transform_operation_by_identity(scope, request_identity),
            deadline,
            phase="preflight",
        )
        if state is None:
            return None
        if state.request_digest != sha256_digest(request_payload):
            raise DataTransformApplicationError(
                "TRANSFORM_REQUEST_IDENTITY_CONFLICT",
                "The action identity is already bound to a different transformation request",
                phase="preflight",
            )
        if (
            resume_unclaimed
            and state.operation.status is CriticalOperationStatus.PREPARED
            and state.candidate is None
            and state.deadline_at <= _now()
        ):
            failed_operation = state.operation.model_copy(
                update={
                    "status": CriticalOperationStatus.FAILED,
                    "error_code": "TRANSFORM_TIMEOUT",
                    "error_message": (
                        "The admitted transformation did not finish before its fixed deadline"
                    ),
                    "error_phase": CriticalOperationErrorPhase.EXECUTION,
                    "finished_at": _now(),
                }
            )
            expired = await _await_before_deadline(
                self._repository.expire_data_transform_operation(
                    scope,
                    state.operation.operation_id,
                    expected_owner=state.execution_owner,
                    expected_generation=state.execution_generation,
                    failed_operation=failed_operation,
                ),
                deadline,
                phase="publication",
            )
            if expired is None:
                refreshed = await _await_before_deadline(
                    self._repository.get_data_transform_operation_by_identity(
                        scope, request_identity
                    ),
                    deadline,
                    phase="publication",
                )
                if refreshed is None or refreshed.request_digest != state.request_digest:
                    raise DataTransformApplicationError(
                        "OPERATION_INTEGRITY_ERROR",
                        "The admitted transformation changed during expiry recovery",
                        phase="publication",
                    )
                state = refreshed
                if (
                    state.operation.status is CriticalOperationStatus.PREPARED
                    and state.candidate is None
                ):
                    raise DataTransformApplicationError(
                        "TRANSFORM_RESULT_UNCERTAIN",
                        "The original executor may still be running",
                        phase="execution",
                        retryable=True,
                    )
            else:
                state = expired
        if (
            resume_unclaimed
            and state.operation.status is CriticalOperationStatus.PREPARED
            and state.execution_owner is None
        ):
            return await self._run_detached(
                self._resume_unclaimed(scope, state, deadline=deadline)
            )
        if (
            resume_unclaimed
            and state.operation.status is CriticalOperationStatus.PREPARED
            and state.execution_owner is not None
        ):
            recovered_claim = await _await_before_deadline(
                self._repository.reclaim_data_transform_execution(
                    scope,
                    state.operation.operation_id,
                    expected_owner=state.execution_owner,
                    expected_generation=state.execution_generation,
                    owner=current_data_transform_executor_owner(),
                    lease_seconds=max(
                        30.0, (state.deadline_at - _now()).total_seconds() + 30.0
                    ),
                ),
                deadline,
                phase="execution",
            )
            if recovered_claim is not None:
                return await self._run_detached(
                    self._resume_reclaimed(scope, state, recovered_claim)
                )
        while state.operation.status is CriticalOperationStatus.PREPARED:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise DataTransformApplicationError(
                    "TRANSFORM_RESULT_UNCERTAIN",
                    "The original transformation is still admitted; retry with the same action identity",
                    phase="execution",
                    retryable=True,
                )
            await asyncio.sleep(min(0.1, remaining))
            refreshed = await _await_before_deadline(
                self._repository.get_data_transform_operation_by_identity(
                    scope, request_identity
                ),
                deadline,
                phase="execution",
            )
            if refreshed is None or refreshed.request_digest != state.request_digest:
                raise DataTransformApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "The admitted transformation changed during result recovery",
                    phase="publication",
                )
            state = refreshed
        operation = state.operation
        if operation.status is CriticalOperationStatus.FAILED:
            published = await _await_before_deadline(
                self._repository.get_operation_replay(
                    scope,
                    (operation.tool_name,),
                    operation.request_fingerprint,
                ),
                deadline,
                phase="publication",
            )
            if published is None or published.operation.operation_id != operation.operation_id:
                raise DataTransformApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "The admitted failure has no exact Catalog record",
                    phase="publication",
                )
            return await self._resolve_replay(scope, published, deadline)
        if operation.status is CriticalOperationStatus.SUCCEEDED:
            published = await _await_before_deadline(
                self._repository.get_result_for_operation(scope, operation.operation_id),
                deadline,
                phase="publication",
            )
            if published is None or not isinstance(operation.detail, DataTransformOperationDetail):
                raise DataTransformApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "The admitted transformation has no exact committed result",
                    phase="publication",
                )
            return DataTransformApplicationResult(
                published=published,
                output_members=_ordered_output_members(
                    published.package.members, operation.detail.outputs
                ),
                repair_parent_operation_alias=state.resolved_parent_operation_alias,
            )
        raise DataTransformApplicationError(
            "OPERATION_INTEGRITY_ERROR",
            "The original transformation has an unsupported outcome",
            phase="publication",
        )

    async def _resume_unclaimed(
        self,
        scope: InvocationScope,
        state: DataTransformOperationState,
        *,
        deadline: float,
        recovery_claim: DataTransformExecutionClaim | None = None,
    ) -> DataTransformApplicationResult:
        """Start an admitted action whose domain executor was never claimed."""
        try:
            command = DataTransformCommand.model_validate(state.resolved_command)
            detail = state.operation.detail
            if not isinstance(detail, DataTransformOperationDetail):
                raise ValueError("Operation detail is not a Data Transform")
            synopsis = self._compile_synopsis(command.output_package.description_markdown)
            resolved = await self._resolved_inputs_for_operation(
                scope, state.operation.operation_id, command, detail, deadline
            )
        except (ValueError, TypeError) as exc:
            raise DataTransformApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The admitted transformation cannot reconstruct its exact request",
                phase="preflight",
            ) from exc
        identity = _ExecutionIdentity(
            tool_name=_TOOL_NAME,
            fingerprint=state.operation.request_fingerprint,
            request_identity=state.request_identity,
            request_payload=state.request_payload,
            repair_parent_operation_id=state.operation.repair_parent_operation_id,
            repair_parent_operation_alias=state.resolved_parent_operation_alias,
        )
        execution_deadline = monotonic() + max(
            0.0, (state.deadline_at - _now()).total_seconds()
        )
        return await self._execute_transform(
            scope=scope,
            command=command,
            synopsis=synopsis,
            resolved=resolved,
            identity=identity,
            deadline=execution_deadline,
            admitted_state=state,
            recovery_claim=recovery_claim,
            ifc_source=detail.ifc_source,
        )

    async def _resume_reclaimed(
        self,
        scope: InvocationScope,
        stale_state: DataTransformOperationState,
        claim: DataTransformExecutionClaim,
    ) -> DataTransformApplicationResult:
        state = await self._repository.get_data_transform_operation_by_identity(
            scope, stale_state.request_identity
        )
        if (
            state is None
            or state.operation.operation_id != claim.operation_id
            or state.request_digest != stale_state.request_digest
        ):
            raise DataTransformApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The reclaimed transform no longer matches its admitted request",
                phase="publication",
            )
        if state.candidate is None:
            return await self._resume_unclaimed(
                scope,
                state,
                deadline=monotonic() + _total_timeout_seconds(
                    DataTransformCommand.model_validate(state.resolved_command)
                ),
                recovery_claim=claim,
            )
        candidate = state.candidate
        publication_deadline = monotonic() + _total_timeout_seconds(
            DataTransformCommand.model_validate(state.resolved_command)
        )
        await self._verify_candidate_artifacts(
            scope, candidate.members, publication_deadline
        )
        operation = CriticalOperationRecord.model_validate(
            state.operation.model_dump(mode="python")
            | {
                "status": CriticalOperationStatus.SUCCEEDED,
                "result_package_id": candidate.package.package_id,
                "finished_at": _now(),
            }
        )
        try:
            published = await _await_publication_io(
                self._repository.publish_package_result(
                    scope,
                    candidate.package,
                    candidate.members,
                    operation,
                    execution_claim=claim,
                ),
                publication_deadline,
            )
        except IfcSourceChangedError as exc:
            saved = state.operation
            detail = saved.detail
            assert isinstance(detail, DataTransformOperationDetail)
            assert saved.code_artifact is not None and saved.description_artifact is not None
            command = DataTransformCommand.model_validate(state.resolved_command)
            error = _ifc_source_changed(exc, phase="publication")
            return await self._record_failure_or_resolve_replay(
                scope=scope, operation_id=saved.operation_id,
                fingerprint=saved.request_fingerprint,
                synopsis=self._compile_synopsis(command.output_package.description_markdown),
                code_artifact=saved.code_artifact,
                description_artifact=saved.description_artifact,
                detail=detail, tool_name=_TOOL_NAME,
                repair_parent_operation_id=saved.repair_parent_operation_id,
                started_at=saved.started_at, code=error.code, message=str(error),
                phase="publication", deadline=publication_deadline, execution_claim=claim,
                request_identity=state.request_identity, request_payload=state.request_payload,
            )
        except Exception as exc:
            committed = await self._repository.get_result_for_operation(
                scope, state.operation.operation_id
            )
            if committed is None:
                raise DataTransformApplicationError(
                    "TRANSFORM_PUBLICATION_UNCERTAIN",
                    "The complete candidate could not be reconciled",
                    phase="publication",
                    retryable=True,
                ) from exc
            published = committed
        detail = state.operation.detail
        if not isinstance(detail, DataTransformOperationDetail):
            raise DataTransformApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The recovered transform has no output declaration",
                phase="publication",
            )
        return DataTransformApplicationResult(
            published=published,
            output_members=_ordered_output_members(published.package.members, detail.outputs),
            repair_parent_operation_alias=state.resolved_parent_operation_alias,
        )

    async def _verify_candidate_artifacts(
        self,
        scope: InvocationScope,
        members: tuple[DataPackageMemberRecord, ...],
        deadline: float,
    ) -> None:
        seen: set[tuple[str, str]] = set()
        for member in members:
            reference = member.artifact
            key = (reference.artifact_id, reference.version_id)
            if key in seen:
                continue
            seen.add(key)
            try:
                stream = await _await_before_deadline(
                    self._artifacts.stream_version(
                        scope.owner_id, reference.artifact_id, reference.version_id
                    ),
                    deadline,
                    phase="publication",
                )
                record = stream.artifact
                if (
                    record.owner_id != scope.owner_id
                    or record.task_id != scope.identity.task_id
                    or record.artifact_id != reference.artifact_id
                    or record.version_id != reference.version_id
                    or record.sha256 != reference.sha256
                ):
                    raise ValueError("candidate Artifact identity changed")
                digest = hashlib.sha256()
                size = 0
                async with asyncio.timeout(max(0.0, deadline - monotonic())):
                    async for chunk in stream.chunks:
                        size += len(chunk)
                        if size > record.size_bytes:
                            raise ValueError("candidate Artifact size exceeded its exact record")
                        digest.update(chunk)
                if size != record.size_bytes or digest.hexdigest() != reference.sha256:
                    raise ValueError("candidate Artifact bytes changed")
            except (asyncio.CancelledError, DataTransformApplicationError):
                raise
            except TimeoutError as exc:
                raise DataTransformApplicationError(
                    "TRANSFORM_PUBLICATION_UNCERTAIN",
                    "The exact candidate Artifact read did not finish before the deadline",
                    phase="publication",
                    retryable=True,
                ) from exc
            except Exception as exc:
                raise DataTransformApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "A complete transform candidate has an unreadable exact Artifact",
                    phase="publication",
                ) from exc

    async def _resolved_inputs_for_operation(
        self,
        scope: InvocationScope,
        operation_id: str,
        command: DataTransformCommand,
        detail: DataTransformOperationDetail,
        deadline: float,
    ) -> tuple[_ResolvedInput, ...]:
        if len(command.inputs) != len(detail.inputs):
            raise ValueError("The admitted transform input declaration changed")
        result: list[_ResolvedInput] = []
        for declared, fixed in zip(command.inputs, detail.inputs, strict=True):
            if declared.input_name != fixed.input_name or declared.payload_name != fixed.payload_name:
                raise ValueError("The admitted transform input binding changed")
            package = await _await_before_deadline(
                self._repository.get_input_package_for_operation(
                    scope, operation_id, fixed.package_id
                ),
                deadline,
                phase="preflight",
            )
            if package is None:
                raise ValueError("The admitted exact input Package is unavailable")
            candidates = [
                payload
                for payload in self._payloads.transform_payloads(package).values()
                if payload.member.member_id == fixed.member_id
            ]
            if len(candidates) != 1:
                raise ValueError("The admitted exact input Payload is unavailable")
            payload = candidates[0]
            schema_member_id = (
                payload.schema_member.member_id
                if isinstance(payload, ExactJsonPayload)
                else None
            )
            _, verified = self._payloads.payload_from_exact_package(
                package,
                package_id=fixed.package_id,
                payload_member_id=fixed.member_id,
                schema_member_id=schema_member_id,
            )
            result.append(
                _ResolvedInput(
                    request_name=declared.input_name,
                    requested_package_alias=declared.package_alias,
                    payload=verified,
                    package_id=fixed.package_id,
                )
            )
        return tuple(result)

    async def _result_for_existing_state(
        self,
        scope: InvocationScope,
        state: DataTransformOperationState,
        request_payload: Mapping[str, object],
        deadline: float,
    ) -> DataTransformApplicationResult:
        operation = state.operation
        replay = await self.resume_by_identity(
            scope,
            state.request_identity,
            request_payload,
            deadline=deadline,
            resume_unclaimed=False,
        )
        if replay is not None:
            return replay
        raise DataTransformApplicationError(
            "OPERATION_INTEGRITY_ERROR",
            f"The admitted transform {operation.operation_id} could not be read",
            phase="publication",
        )

    async def _run_detached(
        self, work: Awaitable[DataTransformApplicationResult]
    ) -> DataTransformApplicationResult:
        """Keep an admitted domain execution alive when its waiter disconnects."""
        task = asyncio.create_task(work)
        _ACTIVE_TRANSFORM_TASKS.add(task)

        def completed(done: asyncio.Task[DataTransformApplicationResult]) -> None:
            _ACTIVE_TRANSFORM_TASKS.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def transform(
        self,
        scope: InvocationScope,
        command: DataTransformCommand,
        *,
        request_identity: str,
        deadline: float | None = None,
    ) -> DataTransformApplicationResult:
        """Execute one new transform using its caller-declared immutable contract."""
        if not isinstance(command, DataTransformCommand):
            raise DataTransformApplicationError(
                "INVALID_ARGUMENT",
                "The transform command is invalid",
                phase="preflight",
            )
        total_timeout = _total_timeout_seconds(command)
        deadline = monotonic() + total_timeout if deadline is None else deadline
        execution_deadline = monotonic() + total_timeout
        _check_deadline(deadline, "preflight")
        request_payload = _create_request_payload(command)
        existing = await self.resume_by_identity(
            scope, request_identity, request_payload, deadline=deadline
        )
        if existing is not None:
            return existing
        ifc_source = None
        if command.ifc_source is not None:
            exact = await self._pin_ifc(scope, deadline)
            ifc_source = IfcReadSourceRef(
                model_id=exact.model_id,
                revision_id=exact.revision_id,
                revision_no=exact.revision_no,
            )
        synopsis = self._compile_synopsis(command.output_package.description_markdown)
        _preflight_json_schemas(command)
        resolved = await self._resolve_inputs(scope, command, deadline)
        fingerprint = _action_fingerprint(
            scope, request_identity,
            _request_fingerprint(scope, command, resolved, ifc_source=ifc_source),
        )
        return await _await_before_deadline(
            self._run_detached(
                self._execute_transform(
                    scope=scope,
                    command=command,
                    synopsis=synopsis,
                    resolved=resolved,
                    identity=_ExecutionIdentity(
                        tool_name=_TOOL_NAME,
                        fingerprint=fingerprint,
                        request_identity=request_identity,
                        request_payload=request_payload,
                    ),
                    deadline=execution_deadline,
                    ifc_source=ifc_source,
                )
            ),
            deadline,
            phase="execution",
        )

    async def repair_transform(
        self,
        scope: InvocationScope,
        parent_operation: PublishedCriticalOperation,
        catalog_packages: Mapping[str, PublishedDataPackage],
        command: DataRepairTransformCommand,
        *,
        request_identity: str,
        request_payload: Mapping[str, object],
        repair_parent_operation_alias: str | None = None,
        deadline: float | None = None,
    ) -> DataTransformApplicationResult:
        """Patch one exact terminal transform attempt and execute its inherited contract."""

        if not isinstance(command, DataRepairTransformCommand):
            raise DataTransformApplicationError(
                "INVALID_ARGUMENT",
                "The transform repair command is invalid",
                phase="preflight",
            )
        existing = await self.resume_by_identity(
            scope,
            request_identity,
            request_payload,
            deadline=deadline or monotonic() + _PYTHON_TOTAL_TIMEOUT_SECONDS,
        )
        if existing is not None:
            return existing
        parent = self._validate_repair_parent(scope, parent_operation, command)
        detail = parent.detail
        assert isinstance(detail, DataTransformOperationDetail)
        assert parent.code_artifact is not None
        assert parent.description_artifact is not None
        total_timeout = (
            _PYTHON_TOTAL_TIMEOUT_SECONDS
            if detail.engine == "python_arrow"
            else _TOTAL_TIMEOUT_SECONDS
        )
        deadline = monotonic() + total_timeout if deadline is None else deadline
        execution_deadline = monotonic() + total_timeout
        _check_deadline(deadline, "preflight")

        source, _source_record = await self._read_exact_text_artifact(
            scope,
            parent.code_artifact,
            expected_media_type=(
                _PYTHON_MEDIA_TYPE if detail.engine == "python_arrow" else _SQL_MEDIA_TYPE
            ),
            incomplete_message="The parent Operation has no readable exact transformation source",
            maximum_bytes=MAX_TRANSFORM_CODE_BYTES,
            deadline=deadline,
        )
        description, description_record = await self._read_exact_text_artifact(
            scope,
            parent.description_artifact,
            expected_media_type=_MARKDOWN_MEDIA_TYPE,
            incomplete_message="The parent Operation has no readable exact Package description",
            deadline=deadline,
        )
        inherited = self._rebuild_repair_command(
            scope=scope,
            detail=detail,
            catalog_packages=catalog_packages,
            source=source,
            description=description,
        )
        base = inherited
        inherited = self._apply_repair_changes(base, command)
        if inherited == base:
            raise DataTransformApplicationError(
                "INVALID_ARGUMENT", "Repair must change source, outputs or Package metadata", phase="preflight"
            )
        if inherited.output_package.description_markdown != description:
            description_record = None
        synopsis = self._compile_synopsis(inherited.output_package.description_markdown)
        _preflight_json_schemas(inherited)
        resolved = await self._resolve_inputs(scope, inherited, deadline)
        _assert_repair_inputs_exact(detail, resolved)
        content_fingerprint = _repair_request_fingerprint(
            scope=scope,
            parent=parent,
            command=inherited,
            resolved=resolved,
            description_artifact=parent.description_artifact,
            ifc_source=detail.ifc_source,
        )
        fingerprint = _action_fingerprint(scope, request_identity, content_fingerprint)
        return await _await_before_deadline(
            self._run_detached(
                self._execute_transform(
                    scope=scope,
                    command=inherited,
                    synopsis=synopsis,
                    resolved=resolved,
                    identity=_ExecutionIdentity(
                        tool_name=_TOOL_NAME,
                        fingerprint=fingerprint,
                        request_identity=request_identity,
                        request_payload=request_payload,
                        repair_parent_operation_id=parent.operation_id,
                        repair_parent_operation_alias=repair_parent_operation_alias,
                        description_artifact=description_record,
                    ),
                    deadline=execution_deadline,
                    ifc_source=detail.ifc_source,
                )
            ),
            deadline,
            phase="execution",
        )

    def _apply_text_edits(self, source: str, edits: tuple[SourceEdit, ...], *, field: str, limit: int) -> str:
        if self._source_patch is None:
            raise DataTransformApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE", "The exact source patch service is unavailable",
                phase="preflight", retryable=True,
            )
        try:
            return self._source_patch.apply_text(source, edits, maximum_source_bytes=limit)
        except SourcePatchPortError as exc:
            code = _repair_patch_error_code(exc.failure)
            location = (f"{field}[{exc.edit_index}]" if field == "edits" else f"{field}.edits[{exc.edit_index}]") if exc.edit_index is not None else field
            raise DataTransformApplicationError(
                code, f"{location}: {exc}", phase="preflight", retryable=exc.failure == "engine_failed",
            ) from None

    def _apply_repair_changes(self, base: DataTransformCommand, changes: DataRepairTransformCommand) -> DataTransformCommand:
        payload = base.model_dump(mode="python")
        if changes.edits is not None:
            payload["execution"]["code"] = self._apply_text_edits(
                base.execution.code, changes.edits, field="edits", limit=MAX_TRANSFORM_CODE_BYTES,
            )
        package = changes.output_package
        if package is not None:
            if package.name is not None:
                payload["output_package"]["name"] = package.name
            change = package.description_markdown
            if change is not None:
                payload["output_package"]["description_markdown"] = (
                    change.value if isinstance(change, TextSet) else self._apply_text_edits(
                        base.output_package.description_markdown, change.edits,
                        field="output_package.description_markdown", limit=65_536,
                    )
                )
        if changes.outputs is not None:
            old_outputs = {item.payload_name: item for item in base.outputs}
            values = changes.outputs.value if isinstance(changes.outputs, TransformOutputsSet) else changes.outputs.set
            resolved = {} if isinstance(changes.outputs, TransformOutputsSet) else {
                name: item.model_dump(mode="python") for name, item in old_outputs.items()
            }
            if not isinstance(changes.outputs, TransformOutputsSet):
                for name in changes.outputs.remove:
                    if name not in old_outputs:
                        raise DataTransformApplicationError("INVALID_ARGUMENT", f"outputs.remove: unknown payload_name {name}", phase="preflight")
                    del resolved[name]
            for item in values:
                value = item.model_dump(mode="python")
                if isinstance(item, JsonOutputInput):
                    schema_change = parse_data_schema_change(item.schema_json)
                    if isinstance(schema_change, JsonSchemaSet):
                        value["schema_json"] = canonical_json(schema_change.value)
                    else:
                        old = old_outputs.get(item.payload_name)
                        if not isinstance(old, JsonOutputDeclaration):
                            raise DataTransformApplicationError(
                                "INVALID_ARGUMENT", f"outputs.{item.payload_name}.schema_json: edit requires an existing JSON output", phase="preflight"
                            )
                        value["schema_json"] = self._apply_text_edits(
                            old.schema_json, schema_change.edits, field=f"outputs.{item.payload_name}.schema_json", limit=65_536,
                        )
                if item.payload_name in resolved and isinstance(changes.outputs, TransformOutputsSet):
                    raise DataTransformApplicationError("INVALID_ARGUMENT", f"outputs: duplicate payload_name {item.payload_name}", phase="preflight")
                resolved[item.payload_name] = value
            payload["outputs"] = tuple(resolved.values())
        try:
            return DataTransformCommand.model_validate(payload)
        except ValidationError as exc:
            error = exc.errors(include_input=False)[0]
            location = ".".join(str(part) for part in error["loc"])
            raise DataTransformApplicationError("INVALID_ARGUMENT", f"{location}: {error['msg']}", phase="preflight") from exc

    async def _execute_transform(
        self,
        *,
        scope: InvocationScope,
        command: DataTransformCommand,
        synopsis: PackageSynopsis,
        resolved: tuple[_ResolvedInput, ...],
        identity: _ExecutionIdentity,
        deadline: float,
        admitted_state: DataTransformOperationState | None = None,
        recovery_claim: DataTransformExecutionClaim | None = None,
        ifc_source: IfcReadSourceRef | None = None,
    ) -> DataTransformApplicationResult:
        """Shared Worker, replay, terminal Operation, and Package publication path."""

        fingerprint = identity.fingerprint
        detail = _operation_detail(command, resolved, ifc_source=ifc_source)
        operation_id = stable_id("operation", {"run_id": scope.run_id, "fingerprint": fingerprint})
        if admitted_state is None:
            description_artifact = identity.description_artifact
            if description_artifact is None:
                description_artifact = await _await_before_deadline(
                    self._upload_bytes(
                        scope,
                        filename=_DESCRIPTION_MEMBER,
                        media_type=_MARKDOWN_MEDIA_TYPE,
                        content=command.output_package.description_markdown.encode("utf-8"),
                        logical_name=_DESCRIPTION_MEMBER,
                        fingerprint=fingerprint,
                        kind="data_package_member",
                    ),
                    deadline,
                    phase="preflight",
                )
            code_artifact = await _await_before_deadline(
                self._upload_bytes(
                    scope,
                    filename=_code_filename(command),
                    media_type=_code_media_type(command),
                    content=command.execution.code.encode("utf-8"),
                    logical_name=_code_member_name(command),
                    fingerprint=fingerprint,
                    kind="data_transform_code",
                ),
                deadline,
                phase="preflight",
            )
            started_at = _now()
            prepared = CriticalOperationRecord(
                operation_id=operation_id,
                owner_id=scope.owner_id,
                tenant_id=scope.identity.tenant_id,
                task_id=scope.identity.task_id or "",
                run_id=scope.run_id,
                operation_kind=CriticalOperationKind.DATA_TRANSFORM,
                tool_name=identity.tool_name,
                intent_summary=_intent_summary(synopsis),
                idempotency_key=_operation_idempotency_key(fingerprint),
                request_fingerprint=fingerprint,
                status=CriticalOperationStatus.PREPARED,
                code_artifact=_artifact_ref(code_artifact),
                description_artifact=_artifact_ref(description_artifact),
                repair_parent_operation_id=identity.repair_parent_operation_id,
                detail=detail,
                started_at=started_at,
            )
            try:
                state = await _await_before_deadline(
                    self._repository.prepare_data_transform_operation(
                        scope,
                        prepared,
                        request_identity=identity.request_identity,
                        request_digest=sha256_digest(identity.request_payload),
                        request_payload=identity.request_payload,
                        resolved_command=command.model_dump(mode="json"),
                        resolved_parent_operation_alias=identity.repair_parent_operation_alias,
                        deadline_at=started_at
                        + timedelta(seconds=max(0.0, deadline - monotonic())),
                    ),
                    deadline,
                    phase="preflight",
                )
            except IdempotencyConflictError:
                raise DataTransformApplicationError(
                    "TRANSFORM_REQUEST_IDENTITY_CONFLICT",
                    "The action identity is already bound to a different transformation request",
                    phase="preflight",
                ) from None
            except IfcSourceChangedError as exc:
                raise _ifc_source_changed(exc, phase="preflight") from None
            if state.operation.operation_id != operation_id:
                return await self._result_for_existing_state(
                    scope, state, identity.request_payload, deadline
                )
        else:
            state = admitted_state
            if state.operation.operation_id != operation_id or state.operation.detail != detail:
                raise DataTransformApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "The admitted transform no longer matches its exact input binding",
                    phase="preflight",
                )
            code_ref = state.operation.code_artifact
            description_ref = state.operation.description_artifact
            if code_ref is None or description_ref is None:
                raise DataTransformApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "The admitted transform has no exact source or description",
                    phase="preflight",
                )
            exact_code, code_artifact = await self._read_exact_text_artifact(
                scope,
                code_ref,
                expected_media_type=_code_media_type(command),
                incomplete_message="The admitted transform code is unavailable",
                deadline=deadline,
                maximum_bytes=MAX_TRANSFORM_CODE_BYTES,
            )
            exact_description, description_artifact = await self._read_exact_text_artifact(
                scope,
                description_ref,
                expected_media_type=_MARKDOWN_MEDIA_TYPE,
                incomplete_message="The admitted transform description is unavailable",
                deadline=deadline,
            )
            if (
                exact_code != command.execution.code
                or exact_description != command.output_package.description_markdown
            ):
                raise DataTransformApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "The admitted transform source changed",
                    phase="preflight",
                )
            started_at = state.operation.started_at
        execution_claim = recovery_claim
        if execution_claim is None:
            execution_claim = await _await_before_deadline(
                self._repository.claim_data_transform_execution(
                    scope,
                    operation_id,
                    owner=current_data_transform_executor_owner(),
                    lease_seconds=max(1.0, deadline - monotonic() + 30.0),
                ),
                deadline,
                phase="preflight",
            )
        if execution_claim is None:
            return await self._result_for_existing_state(
                scope, state, identity.request_payload, deadline
            )

        materialized: dict[str, Path] = {}
        try:
            materialized = await _await_before_deadline(
                self._payloads.materialize_members(
                    _resolved_members(resolved),
                    check_budget=lambda: _check_deadline(deadline, "preflight"),
                    max_total_bytes=_INPUT_PARQUET_BYTES,
                ),
                deadline,
                phase="preflight",
            )
            worker_inputs = await _await_before_deadline(
                asyncio.to_thread(_all_arrow_inputs, resolved, materialized, command),
                deadline,
                phase="preflight",
            )
            cleanup_reserve = (
                _PYTHON_WORKER_CLEANUP_RESERVE_SECONDS
                if _is_python(command)
                else _WORKER_CLEANUP_RESERVE_SECONDS
            )
            worker_seconds = (
                deadline - monotonic() - cleanup_reserve - _TERMINAL_PUBLICATION_RESERVE_SECONDS
            )
            if worker_seconds <= 0:
                raise DataTransformApplicationError(
                    "TRANSFORM_TIMEOUT",
                    "The request deadline was reached before Worker execution could start",
                    phase="preflight",
                )
            worker_task = asyncio.create_task(
                self._execute_worker(scope, command, worker_inputs, ifc_source, deadline)
            )
            try:
                try:
                    done, _pending = await asyncio.wait({worker_task}, timeout=worker_seconds)
                except asyncio.CancelledError:
                    worker_task.cancel()
                    await _cancelled_worker_failure(worker_task)
                    raise
                if not done:
                    worker_task.cancel()
                    cancellation_failure = await _cancelled_worker_failure(worker_task)
                    if cancellation_failure is None:
                        cancellation_failure = DataTransformWorkerError(
                            "TRANSFORM_TIMEOUT",
                            (
                                "The request deadline was reached before Worker execution was "
                                "confirmed"
                            ),
                            phase="preflight",
                            execution_started=False,
                        )
                    raise cancellation_failure
                try:
                    outputs = worker_task.result()
                except (asyncio.CancelledError, TimeoutError):
                    raise DataTransformWorkerError(
                        "TRANSFORM_TIMEOUT",
                        "Worker execution ended without a confirmed started lifecycle state",
                        phase="preflight",
                        execution_started=False,
                    ) from None
            except DataTransformWorkerError as exc:
                code_validation_failure = not exc.execution_started and _is_code_validation_failure(
                    exc.code
                )
                return await self._record_failure_or_resolve_replay(
                    scope=scope,
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                    synopsis=synopsis,
                    code_artifact=code_artifact,
                    description_artifact=description_artifact,
                    detail=detail,
                    tool_name=identity.tool_name,
                    repair_parent_operation_id=identity.repair_parent_operation_id,
                    started_at=started_at,
                    code=exc.code,
                    # Preserve the safe Worker cause in the original failure
                    # text so the first response and saved replays agree.
                    message=(
                        str(exc) + (f"; diagnostic={canonical_json(exc.diagnostic)}"
                                    if exc.diagnostic else "")
                    ),
                    phase="code_validation" if code_validation_failure else exc.phase,
                    deadline=deadline,
                    execution_claim=execution_claim,
                    request_identity=identity.request_identity,
                    request_payload=identity.request_payload,
                )

            try:
                output_validation_deadline = deadline - _TERMINAL_PUBLICATION_RESERVE_SECONDS
                validated_outputs = await _await_before_deadline(
                    _validated_outputs(
                        outputs,
                        command,
                        output_validation_deadline,
                    ),
                    output_validation_deadline,
                    phase="output_validation",
                )
            except asyncio.CancelledError:
                raise
            except DataTransformApplicationError as exc:
                return await self._record_failure_or_resolve_replay(
                    scope=scope,
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                    synopsis=synopsis,
                    code_artifact=code_artifact,
                    description_artifact=description_artifact,
                    detail=detail,
                    tool_name=identity.tool_name,
                    repair_parent_operation_id=identity.repair_parent_operation_id,
                    started_at=started_at,
                    code=exc.code,
                    message=str(exc),
                    phase="output_validation",
                    deadline=deadline,
                    execution_claim=execution_claim,
                    request_identity=identity.request_identity,
                    request_payload=identity.request_payload,
                )

            try:
                return await self._publish_success(
                    scope=scope,
                    command=command,
                    synopsis=synopsis,
                    resolved=resolved,
                    detail=detail,
                    fingerprint=fingerprint,
                    operation_id=operation_id,
                    code_artifact=code_artifact,
                    description_artifact=description_artifact,
                    tool_name=identity.tool_name,
                    repair_parent_operation_id=identity.repair_parent_operation_id,
                    started_at=started_at,
                    validated_outputs=validated_outputs,
                    deadline=deadline,
                    execution_claim=execution_claim,
                    request_identity=identity.request_identity,
                    request_payload=identity.request_payload,
                    repair_parent_operation_alias=identity.repair_parent_operation_alias,
                )
            finally:
                for item in validated_outputs:
                    if isinstance(item, _ValidatedParquetOutput):
                        with suppress(OSError):
                            item.path.unlink(missing_ok=True)
        except ExactPayloadResolutionError as exc:
            raise DataTransformApplicationError(exc.code, str(exc), phase="preflight") from None
        finally:
            self._payloads.remove_paths(materialized.values())

    def _validate_repair_parent(
        self,
        scope: InvocationScope,
        published: PublishedCriticalOperation,
        command: DataRepairTransformCommand,
    ) -> CriticalOperationRecord:
        operation = published.operation
        entry = published.catalog_entry
        same_scope = (
            operation.owner_id == scope.owner_id
            and operation.tenant_id == scope.identity.tenant_id
            and operation.task_id == (scope.identity.task_id or "")
            and operation.run_id == scope.run_id
            and entry.owner_id == scope.owner_id
            and entry.tenant_id == scope.identity.tenant_id
            and entry.task_id == (scope.identity.task_id or "")
            and entry.run_id == scope.run_id
            and entry.operation_id == operation.operation_id
        )
        if (
            not same_scope
            or command.parent_operation_id != operation.operation_id
            or operation.status
            not in {
                CriticalOperationStatus.FAILED,
                CriticalOperationStatus.SUCCEEDED,
            }
            or operation.operation_kind is not CriticalOperationKind.DATA_TRANSFORM
            or operation.tool_name != _TOOL_NAME
        ):
            raise DataTransformApplicationError(
                "TRANSFORM_REPAIR_PARENT_INVALID",
                "The selected parent is not a visible terminal Data Transform Operation",
                phase="preflight",
            )
        if (
            not isinstance(operation.detail, DataTransformOperationDetail)
            or operation.code_artifact is None
            or operation.description_artifact is None
        ):
            raise DataTransformApplicationError(
                "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
                "The parent Operation does not contain its complete exact replay contract",
                phase="preflight",
            )
        return operation

    async def _read_exact_text_artifact(
        self,
        scope: InvocationScope,
        reference: ArtifactVersionRef,
        *,
        expected_media_type: str,
        incomplete_message: str,
        deadline: float,
        maximum_bytes: int = 65_536,
    ) -> tuple[str, ArtifactRecord]:
        try:
            stream = await _await_before_deadline(
                self._artifacts.stream_version(
                    scope.owner_id,
                    reference.artifact_id,
                    reference.version_id,
                ),
                deadline,
                phase="preflight",
            )
            content = await _await_before_deadline(
                _read_bounded_bytes(stream.chunks, maximum_bytes),
                deadline,
                phase="preflight",
            )
            record = stream.artifact
            if (
                record.owner_id != scope.owner_id
                or record.task_id != (scope.identity.task_id or "")
                or record.artifact_id != reference.artifact_id
                or record.version_id != reference.version_id
                or record.sha256 != reference.sha256
                or record.content_type != expected_media_type
                or hashlib.sha256(content).hexdigest() != reference.sha256
            ):
                raise ValueError("exact Artifact metadata mismatch")
            return content.decode("utf-8"), record
        except (DataTransformApplicationError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise DataTransformApplicationError(
                "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
                incomplete_message,
                phase="preflight",
            ) from exc

    def _rebuild_repair_command(
        self,
        *,
        scope: InvocationScope,
        detail: DataTransformOperationDetail,
        catalog_packages: Mapping[str, PublishedDataPackage],
        source: str,
        description: str,
    ) -> DataTransformCommand:
        package_by_id: dict[str, tuple[str, PublishedDataPackage]] = {}
        for alias, published in catalog_packages.items():
            package = published.package
            entry = published.catalog_entry
            if (
                alias != entry.alias
                or entry.package_id != package.package_id
                or entry.owner_id != scope.owner_id
                or entry.tenant_id != scope.identity.tenant_id
                or entry.task_id != (scope.identity.task_id or "")
                or entry.run_id != scope.run_id
            ):
                continue
            if package.package_id in package_by_id:
                raise DataTransformApplicationError(
                    "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
                    "The visible Package catalog contains an ambiguous exact input binding",
                    phase="preflight",
                )
            package_by_id[package.package_id] = (alias, published)

        inputs: list[dict[str, object]] = []
        for binding in detail.inputs:
            selected = package_by_id.get(binding.package_id)
            if selected is None:
                raise DataTransformApplicationError(
                    "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
                    "An exact input Package from the parent Operation is no longer visible",
                    phase="preflight",
                )
            alias, published = selected
            try:
                payload = self._payloads.transform_payloads(published).get(binding.payload_name)
            except ExactPayloadResolutionError as exc:
                raise DataTransformApplicationError(
                    "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
                    "An exact input Package from the parent Operation is invalid",
                    phase="preflight",
                ) from exc
            if payload is None or payload.member.member_id != binding.member_id:
                raise DataTransformApplicationError(
                    "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
                    "An exact input Payload from the parent Operation is incomplete",
                    phase="preflight",
                )
            inputs.append(
                {
                    "input_name": binding.input_name,
                    "package_alias": alias,
                    "payload_name": binding.payload_name,
                }
            )

        outputs = [
            item.model_dump(
                mode="json", exclude_none=True,
                exclude=(
                    {"logical_key_fields"}
                    if item.format == "json" else {"json_kind", "schema_json"}
                ),
            )
            for item in detail.outputs
        ]
        if any(item["format"] == "json" and "schema_json" not in item for item in outputs):
            raise DataTransformApplicationError(
                "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
                "A JSON output declaration is missing its exact JSON Schema",
                phase="preflight",
            )
        try:
            return DataTransformCommand.model_validate(
                {
                    "inputs": inputs,
                    "ifc_source": "current" if detail.ifc_source is not None else None,
                    "parameters": [item.model_dump(mode="json") for item in detail.parameters],
                    "inline_tables": [
                        item.model_dump(mode="json") for item in detail.inline_tables
                    ],
                    "execution": {"engine": detail.engine, "code": source},
                    "outputs": outputs,
                    "output_package": {
                        "name": detail.output_package_name,
                        "description_markdown": description,
                    },
                }
            )
        except Exception as exc:
            raise DataTransformApplicationError(
                "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
                "The failed Operation does not contain a valid complete replay contract",
                phase="preflight",
            ) from exc

    async def _pin_ifc(
        self,
        scope: InvocationScope,
        deadline: float,
        source: IfcReadSourceRef | None = None,
    ) -> ExactIfcRevision:
        if self._ifc_revisions is None or self._ifc_worker is None:
            raise DataTransformApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE",
                "The IFC derivation reader and Worker are not configured",
                phase="preflight", retryable=True,
            )
        identity = IfcRunIdentity(
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            run_id=scope.run_id,
        )
        try:
            read = (
                self._ifc_revisions.pin_current(identity)
                if source is None
                else self._ifc_revisions.pin_exact(identity, revision_id=source.revision_id)
            )
            return await _await_before_deadline(read, deadline, phase="preflight")
        except IfcModelNotInitializedError as exc:
            raise DataTransformApplicationError(
                "IFC_MODEL_NOT_INITIALIZED", str(exc), phase="preflight",
            ) from None
        except IfcModelFrozenError as exc:
            raise DataTransformApplicationError(
                "IFC_MODEL_FROZEN", str(exc), phase="preflight",
            ) from None
        except IfcModelIntegrityError as exc:
            raise DataTransformApplicationError(
                "IFC_MODEL_INTEGRITY_FAILED", str(exc), phase="preflight",
            ) from None

    async def _execute_worker(
        self,
        scope: InvocationScope,
        command: DataTransformCommand,
        inputs: tuple[ArrowTransformInput, ...],
        ifc_source: IfcReadSourceRef | None,
        deadline: float,
    ) -> tuple[ArrowTransformOutput, ...]:
        try:
            if ifc_source is None:
                worker, request = self._worker_call(command)
                return await worker.execute(request, inputs, deadline=deadline)
            exact = await self._pin_ifc(scope, deadline, ifc_source)
            assert self._ifc_revisions is not None and self._ifc_worker is not None
            source = await self._ifc_revisions.open_exact(exact)
            if source.size_bytes > MAX_IFC_INPUT_BYTES:
                raise DataTransformApplicationError(
                    "IFC_INPUT_BUDGET_EXCEEDED", "The fixed IFC exceeds the Worker input budget",
                    phase="preflight",
                )
            chunks = []
            async for chunk in source.iter_chunks():
                _check_deadline(deadline, "preflight")
                chunks.append(chunk)
            payload = b"".join(chunks)
            chunks.clear()
            return await self._ifc_worker.execute_transform(
                PythonArrowWorkerRequest.from_command(command), inputs,
                ifc_bytes=payload, expected_ifc_sha256=source.sha256,
                deadline=deadline,
            )
        except (IfcModelIntegrityError, IfcModelFrozenError, StorageError, OSError) as exc:
            raise DataTransformWorkerError(
                "IFC_MODEL_INTEGRITY_FAILED", str(exc), phase="preflight",
                execution_started=False,
            ) from None
        except DataTransformApplicationError as exc:
            raise DataTransformWorkerError(
                exc.code, str(exc), phase=exc.phase, execution_started=False,
                diagnostic=exc.diagnostic, retryable=exc.retryable,
            ) from None

    def _worker_call(
        self,
        command: DataTransformCommand,
    ) -> tuple[
        DataTransformWorkerPort | PythonArrowDataTransformWorkerPort,
        DataTransformWorkerRequest | PythonArrowWorkerRequest,
    ]:
        if not _is_python(command):
            return self._worker, DataTransformWorkerRequest.from_command(command)
        if self._python_worker is None:
            raise DataTransformApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE",
                "The Python/Arrow transform Worker is not configured",
                phase="preflight",
                retryable=True,
            )
        return self._python_worker, PythonArrowWorkerRequest.from_command(command)

    def _compile_synopsis(self, markdown: str) -> PackageSynopsis:
        try:
            return self._synopsis.compile(markdown)
        except PackageSynopsisError as exc:
            raise DataTransformApplicationError(
                "PACKAGE_DESCRIPTION_INVALID", str(exc), phase="preflight"
            ) from None

    async def _resolve_inputs(
        self,
        scope: InvocationScope,
        command: DataTransformCommand,
        deadline: float,
    ) -> tuple[_ResolvedInput, ...]:
        result: list[_ResolvedInput] = []
        seen_members: set[tuple[str, str]] = set()
        package_cache: dict[
            str,
            tuple[PublishedDataPackage, Mapping[str, ExactTransformPayload]],
        ] = {}
        total_rows = sum(len(table.rows) for table in command.inline_tables)
        for item in command.inputs:
            _check_deadline(deadline, "preflight")
            try:
                cached = package_cache.get(item.package_alias)
                if cached is None:
                    package = await _await_before_deadline(
                        self._payloads.package_by_alias(scope, item.package_alias),
                        deadline,
                        phase="preflight",
                    )
                    payloads = self._payloads.transform_payloads(package)
                    package_cache[item.package_alias] = (package, payloads)
                else:
                    package, payloads = cached
                payload = payloads.get(item.payload_name)
            except ExactPayloadResolutionError as exc:
                raise DataTransformApplicationError(exc.code, str(exc), phase="preflight") from None
            if payload is None:
                raise DataTransformApplicationError(
                    "PAYLOAD_NOT_FOUND",
                    f"Payload {item.payload_name!r} was not found in {item.package_alias}",
                    phase="preflight",
                )
            exact = (payload.member.artifact.version_id, payload.member.member_id)
            if exact in seen_members:
                raise DataTransformApplicationError(
                    "INVALID_ARGUMENT",
                    "The same exact Payload cannot be bound more than once",
                    phase="preflight",
                )
            seen_members.add(exact)
            if isinstance(payload, ExactParquetPayload):
                total_rows += int(payload.member.row_count or 0)
            result.append(
                _ResolvedInput(
                    request_name=item.input_name,
                    requested_package_alias=item.package_alias,
                    payload=payload,
                    package_id=package.package.package_id,
                )
            )
        if total_rows > _INPUT_ROW_LIMIT:
            raise DataTransformApplicationError(
                "TRANSFORM_INPUT_BUDGET_EXCEEDED",
                "Exact and inline input rows exceed the 4,000,000-row transform budget",
                phase="preflight",
            )
        return tuple(result)

    async def _publish_success(
        self,
        *,
        scope: InvocationScope,
        command: DataTransformCommand,
        synopsis: PackageSynopsis,
        resolved: tuple[_ResolvedInput, ...],
        detail: DataTransformOperationDetail,
        fingerprint: str,
        operation_id: str,
        code_artifact: ArtifactRecord,
        description_artifact: ArtifactRecord,
        tool_name: Literal["data_derive"],
        repair_parent_operation_id: str | None,
        started_at: datetime,
        validated_outputs: tuple[_ValidatedOutput, ...],
        deadline: float,
        execution_claim: DataTransformExecutionClaim,
        request_identity: str,
        request_payload: Mapping[str, object],
        repair_parent_operation_alias: str | None,
    ) -> DataTransformApplicationResult:
        _check_deadline(deadline, "publication")
        payload_artifacts: dict[str, ArtifactRecord] = {}
        schema_artifacts: dict[str, ArtifactRecord] = {}
        for declaration, validated in zip(command.outputs, validated_outputs, strict=True):
            payload_member_name = _payload_member_name(declaration)
            if isinstance(validated, _ValidatedParquetOutput):
                payload_artifacts[declaration.payload_name] = await _await_publication_io(
                    self._upload_path(
                        scope,
                        filename=f"{declaration.payload_name}.parquet",
                        media_type=_PARQUET_MEDIA_TYPE,
                        path=validated.path,
                        logical_name=payload_member_name,
                        fingerprint=fingerprint,
                        kind="data_package_member",
                        content_addressed=True,
                    ),
                    deadline,
                )
            else:
                payload_artifacts[declaration.payload_name] = await _await_publication_io(
                    self._upload_bytes(
                        scope,
                        filename=f"{declaration.payload_name}.json",
                        media_type=_JSON_MEDIA_TYPE,
                        content=validated.content,
                        logical_name=payload_member_name,
                        fingerprint=fingerprint,
                        kind="data_package_member",
                        content_addressed=True,
                    ),
                    deadline,
                )
                schema_member_name = _schema_member_name(declaration.payload_name)
                schema_artifacts[declaration.payload_name] = await _await_publication_io(
                    self._upload_bytes(
                        scope,
                        filename=f"{declaration.payload_name}.schema.json",
                        media_type=_JSON_SCHEMA_MEDIA_TYPE,
                        content=validated.schema_content,
                        logical_name=schema_member_name,
                        fingerprint=fingerprint,
                        kind="data_package_member",
                        content_addressed=True,
                    ),
                    deadline,
                )
        created_at = code_artifact.created_at
        package_id = stable_id("data_package", {"run_id": scope.run_id, "fingerprint": fingerprint})
        manifest = _derived_manifest(
            command=command,
            synopsis=synopsis,
            resolved=resolved,
            operation_id=operation_id,
            package_id=package_id,
            created_at=created_at,
            code_artifact=code_artifact,
            description_artifact=description_artifact,
            payload_artifacts=payload_artifacts,
            schema_artifacts=schema_artifacts,
            validated_outputs=validated_outputs,
            ifc_source=detail.ifc_source,
        )
        manifest_artifact = await _await_publication_io(
            self._upload_bytes(
                scope,
                filename=_MANIFEST_MEMBER,
                media_type=_MANIFEST_MEDIA_TYPE,
                content=(canonical_json(manifest) + "\n").encode("utf-8"),
                logical_name=_MANIFEST_MEMBER,
                fingerprint=fingerprint,
                kind="data_package_manifest",
                content_addressed=True,
            ),
            deadline,
        )

        package = DataPackageRecord(
            package_id=package_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            created_by_run_id=scope.run_id,
            name=command.output_package.name,
            package_kind=DataPackageKind.DERIVED,
            source_filename=None,
            description_title=synopsis.description_title,
            summary=synopsis.summary,
            section_titles=synopsis.section_titles,
            idempotency_key=f"data-transform-package:{fingerprint}",
            request_fingerprint=fingerprint,
            created_at=created_at,
        )
        common = {
            "owner_id": scope.owner_id,
            "tenant_id": scope.identity.tenant_id,
            "task_id": scope.identity.task_id or "",
            "package_id": package_id,
            "created_at": created_at,
        }
        output_members = tuple(
            _output_member(
                package_id=package_id,
                declaration=declaration,
                validated=validated,
                artifact=payload_artifacts[declaration.payload_name],
                common=common,
            )
            for declaration, validated in zip(command.outputs, validated_outputs, strict=True)
        )
        schema_members = tuple(
            DataPackageMemberRecord(
                member_id=_member_id(package_id, _schema_member_name(declaration.payload_name)),
                member_name=_schema_member_name(declaration.payload_name),
                member_kind=PackageMemberKind.SCHEMA,
                artifact=_artifact_ref(schema_artifacts[declaration.payload_name]),
                media_type=_JSON_SCHEMA_MEDIA_TYPE,
                **common,
            )
            for declaration in command.outputs
            if isinstance(declaration, JsonOutputDeclaration)
        )
        members = (
            *output_members,
            *schema_members,
            DataPackageMemberRecord(
                member_id=_member_id(package_id, _code_member_name(command)),
                member_name=_code_member_name(command),
                member_kind=PackageMemberKind.CODE,
                artifact=_artifact_ref(code_artifact),
                media_type=_code_media_type(command),
                **common,
            ),
            DataPackageMemberRecord(
                member_id=_member_id(package_id, _DESCRIPTION_MEMBER),
                member_name=_DESCRIPTION_MEMBER,
                member_kind=PackageMemberKind.DESCRIPTION,
                artifact=_artifact_ref(description_artifact),
                media_type=_MARKDOWN_MEDIA_TYPE,
                **common,
            ),
            DataPackageMemberRecord(
                member_id=_member_id(package_id, _MANIFEST_MEMBER),
                member_name=_MANIFEST_MEMBER,
                member_kind=PackageMemberKind.MANIFEST,
                artifact=_artifact_ref(manifest_artifact),
                media_type=_MANIFEST_MEDIA_TYPE,
                **common,
            ),
        )
        operation = CriticalOperationRecord(
            operation_id=operation_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            run_id=scope.run_id,
            operation_kind=CriticalOperationKind.DATA_TRANSFORM,
            tool_name=tool_name,
            intent_summary=_intent_summary(synopsis),
            source_filename=None,
            idempotency_key=_operation_idempotency_key(fingerprint),
            request_fingerprint=fingerprint,
            status=CriticalOperationStatus.SUCCEEDED,
            result_package_id=package_id,
            code_artifact=_artifact_ref(code_artifact),
            description_artifact=_artifact_ref(description_artifact),
            repair_parent_operation_id=repair_parent_operation_id,
            detail=detail,
            started_at=started_at,
            finished_at=_now(),
        )
        try:
            _check_deadline(deadline, "publication")
            await _await_publication_io(
                self._repository.stage_data_transform_candidate(
                    scope,
                    operation_id,
                    package,
                    members,
                    execution_claim=execution_claim,
                ),
                deadline,
            )
            published = await _await_publication_io(
                self._repository.publish_package_result(
                    scope,
                    package,
                    members,
                    operation,
                    execution_claim=execution_claim,
                ),
                deadline,
            )
        except IfcSourceChangedError as exc:
            error = _ifc_source_changed(exc, phase="publication")
            return await self._record_failure_or_resolve_replay(
                scope=scope, operation_id=operation_id, fingerprint=fingerprint,
                synopsis=synopsis, code_artifact=code_artifact,
                description_artifact=description_artifact, detail=detail,
                tool_name=tool_name, repair_parent_operation_id=repair_parent_operation_id,
                started_at=started_at, code=error.code, message=str(error), phase="publication",
                deadline=deadline, execution_claim=execution_claim,
                request_identity=request_identity, request_payload=request_payload,
            )
        except asyncio.CancelledError:
            reconcile_task = asyncio.create_task(
                self._try_reconcile(
                    scope,
                    request_identity,
                    request_payload,
                    deadline,
                )
            )
            with suppress(Exception):
                await asyncio.shield(reconcile_task)
            raise
        except Exception as exc:
            replay = await self._try_reconcile(
                scope,
                request_identity,
                request_payload,
                deadline,
            )
            if replay is not None:
                return replay
            raise DataTransformApplicationError(
                "TRANSFORM_PUBLICATION_UNCERTAIN",
                "The Package publication could not be reconciled",
                phase="publication",
                retryable=True,
            ) from exc
        return DataTransformApplicationResult(
            published=published,
            output_members=_ordered_output_members(
                published.package.members,
                detail.outputs,
            ),
            repair_parent_operation_alias=repair_parent_operation_alias,
        )

    async def _try_reconcile(
        self,
        scope: InvocationScope,
        request_identity: str,
        request_payload: Mapping[str, object],
        deadline: float,
    ) -> DataTransformApplicationResult | None:
        remaining = max(0.0, min(_PUBLICATION_RECONCILE_RESERVE_SECONDS, deadline - monotonic()))
        if remaining == 0:
            return None
        try:
            async with asyncio.timeout(remaining):
                state = await self._repository.get_data_transform_operation_by_identity(
                    scope, request_identity
                )
                if (
                    state is None
                    or state.request_digest != sha256_digest(request_payload)
                    or state.operation.status is not CriticalOperationStatus.SUCCEEDED
                ):
                    return None
                published = await self._repository.get_result_for_operation(
                    scope, state.operation.operation_id
                )
                detail = state.operation.detail
                if published is None or not isinstance(detail, DataTransformOperationDetail):
                    return None
                return DataTransformApplicationResult(
                    published=published,
                    output_members=_ordered_output_members(
                        published.package.members, detail.outputs
                    ),
                    repair_parent_operation_alias=state.resolved_parent_operation_alias,
                )
        except Exception:
            return None

    async def _resolve_replay(
        self,
        scope: InvocationScope,
        replay: PublishedCriticalOperation,
        deadline: float,
    ) -> DataTransformApplicationResult:
        operation = replay.operation
        if operation.status is CriticalOperationStatus.FAILED:
            phase = (
                operation.error_phase.value if operation.error_phase is not None else "execution"
            )
            if operation.error_phase is CriticalOperationErrorPhase.COMMIT_FENCE:
                phase = "publication"
            raise DataTransformApplicationError(
                operation.error_code or "TRANSFORM_WORKER_FAILED",
                operation.error_message or "The previous transformation failed",
                phase=phase,
                operation_alias=replay.catalog_entry.alias,
            )
        package = await _await_before_deadline(
            self._repository.get_package(scope, operation.result_package_id or ""),
            deadline,
            phase="publication",
        )
        if package is None:
            raise DataTransformApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The successful transform has no visible exact result Package",
                phase="publication",
            )
        detail = operation.detail
        if not isinstance(detail, DataTransformOperationDetail):
            raise DataTransformApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The successful transform has no exact output declaration",
                phase="publication",
            )
        output_members = _ordered_output_members(package.members, detail.outputs)
        return DataTransformApplicationResult(
            published=PublishedPackageResult(package=package, operation=replay),
            output_members=output_members,
        )

    async def _record_failure_or_resolve_replay(
        self,
        *,
        scope: InvocationScope,
        operation_id: str,
        fingerprint: str,
        synopsis: PackageSynopsis,
        code_artifact: ArtifactRecord,
        description_artifact: ArtifactRecord,
        detail: DataTransformOperationDetail,
        tool_name: Literal["data_derive"],
        repair_parent_operation_id: str | None,
        started_at: datetime,
        code: str,
        message: str,
        phase: str,
        deadline: float,
        execution_claim: DataTransformExecutionClaim,
        request_identity: str,
        request_payload: Mapping[str, object],
    ) -> DataTransformApplicationResult:
        persisted_phase = {
            "code_validation": CriticalOperationErrorPhase.CODE_VALIDATION,
            "output_validation": CriticalOperationErrorPhase.OUTPUT_VALIDATION,
            "publication": CriticalOperationErrorPhase.COMMIT_FENCE,
        }.get(phase, CriticalOperationErrorPhase.EXECUTION)
        operation = CriticalOperationRecord(
            operation_id=operation_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            run_id=scope.run_id,
            operation_kind=CriticalOperationKind.DATA_TRANSFORM,
            tool_name=tool_name,
            intent_summary=_intent_summary(synopsis),
            source_filename=None,
            idempotency_key=_operation_idempotency_key(fingerprint),
            request_fingerprint=fingerprint,
            status=CriticalOperationStatus.FAILED,
            error_code=code,
            error_message=truncate_transform_error_message(message),
            error_phase=persisted_phase,
            code_artifact=_artifact_ref(code_artifact),
            description_artifact=_artifact_ref(description_artifact),
            repair_parent_operation_id=repair_parent_operation_id,
            detail=detail,
            started_at=started_at,
            finished_at=_now(),
        )
        try:
            published = await _await_publication_io(
                self._repository.record_failed_operation(
                    scope, operation, execution_claim=execution_claim
                ),
                deadline,
            )
        except asyncio.CancelledError:
            reconcile_task = asyncio.create_task(
                self._try_reconcile(
                    scope,
                    request_identity,
                    request_payload,
                    deadline,
                )
            )
            with suppress(Exception):
                await asyncio.shield(reconcile_task)
            raise
        except Exception as exc:
            replay = await self._try_reconcile(
                scope,
                request_identity,
                request_payload,
                deadline,
            )
            if replay is not None:
                return replay
            raise DataTransformApplicationError(
                "TRANSFORM_PUBLICATION_UNCERTAIN",
                "The failed Operation could not be reconciled",
                phase="publication",
                retryable=True,
            ) from exc
        # The repository owns first-terminal-wins.  Always project the exact
        # persisted winner: it may be a concurrent success or a different
        # concurrent failure, never the losing caller's local outcome.
        return await self._resolve_replay(scope, published, deadline)

    async def _upload_path(
        self,
        scope: InvocationScope,
        *,
        filename: str,
        media_type: str,
        path: Path,
        logical_name: str,
        fingerprint: str,
        kind: str,
        content_addressed: bool = False,
    ) -> ArtifactRecord:
        try:
            idempotency_key = f"data-transform:{fingerprint}:{logical_name}"
            if content_addressed:
                content_sha256 = await asyncio.to_thread(_file_sha256, path)
                idempotency_key = _generated_artifact_idempotency_key(
                    fingerprint,
                    logical_name,
                    content_sha256,
                )
            return await self._artifacts.upload(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename=filename,
                content_type=media_type,
                chunks=_file_chunks(path),
                idempotency_key=idempotency_key,
                kind=kind,
                run_id=scope.run_id,
                version_metadata={"logical_name": logical_name},
            )
        except Exception as exc:
            raise DataTransformApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE",
                "An exact transformation Artifact could not be stored",
                phase="publication",
                retryable=True,
            ) from exc

    async def _upload_bytes(
        self,
        scope: InvocationScope,
        *,
        filename: str,
        media_type: str,
        content: bytes,
        logical_name: str,
        fingerprint: str,
        kind: str,
        content_addressed: bool = False,
    ) -> ArtifactRecord:
        try:
            idempotency_key = f"data-transform:{fingerprint}:{logical_name}"
            if content_addressed:
                idempotency_key = _generated_artifact_idempotency_key(
                    fingerprint,
                    logical_name,
                    hashlib.sha256(content).hexdigest(),
                )
            return await self._artifacts.upload_bytes(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename=filename,
                content_type=media_type,
                content=content,
                idempotency_key=idempotency_key,
                kind=kind,
                run_id=scope.run_id,
                version_metadata={"logical_name": logical_name},
            )
        except Exception as exc:
            raise DataTransformApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE",
                "An exact transformation Artifact could not be stored",
                phase=(
                    "preflight"
                    if logical_name in {_SQL_CODE_MEMBER, _PYTHON_CODE_MEMBER}
                    else "publication"
                ),
                retryable=True,
            ) from exc


async def _read_bounded_bytes(chunks: AsyncIterator[bytes], limit: int) -> bytes:
    content = bytearray()
    async for chunk in chunks:
        content.extend(chunk)
        if len(content) > limit:
            raise ValueError("exact text Artifact exceeds its contract budget")
    return bytes(content)


def _repair_patch_error_code(failure: str) -> str:
    codes = {
        "match_not_found": "TRANSFORM_REPAIR_MATCH_NOT_FOUND",
        "match_not_unique": "TRANSFORM_REPAIR_MATCH_NOT_UNIQUE",
        "overlap": "TRANSFORM_REPAIR_OVERLAP",
        "budget_exceeded": "TRANSFORM_REPAIR_BUDGET_EXCEEDED",
        "engine_failed": "TRANSFORM_SERVICE_UNAVAILABLE",
    }
    try:
        return codes[failure]
    except KeyError as exc:
        raise ValueError("patch engine failure must be handled before public projection") from exc


def _assert_repair_inputs_exact(
    inherited: DataTransformOperationDetail,
    resolved: tuple[_ResolvedInput, ...],
) -> None:
    actual = tuple(
        (
            item.request_name,
            item.package_id,
            item.payload.member.member_id,
            item.payload.payload_name,
        )
        for item in resolved
    )
    expected = tuple(
        (item.input_name, item.package_id, item.member_id, item.payload_name)
        for item in inherited.inputs
    )
    if actual != expected:
        raise DataTransformApplicationError(
            "TRANSFORM_REPAIR_SOURCE_INCOMPLETE",
            "The visible input bindings do not match the parent Operation's exact contract",
            phase="preflight",
        )


def _ifc_source_changed(
    error: IfcSourceChangedError, *, phase: TransformPhase,
) -> DataTransformApplicationError:
    diagnostic = error.diagnostic
    return DataTransformApplicationError(
        error.code,
        "IFC source changed from revision "
        f"{diagnostic['expected_revision_no']} to {diagnostic['actual_revision_no']}. "
        "No Package was published. Create a new derivation from CURRENT.",
        phase=phase, diagnostic=diagnostic,
    )


def _repair_request_fingerprint(
    *,
    scope: InvocationScope,
    parent: CriticalOperationRecord,
    command: DataTransformCommand,
    resolved: tuple[_ResolvedInput, ...],
    description_artifact: ArtifactVersionRef,
    ifc_source: IfcReadSourceRef | None = None,
) -> str:
    return sha256_digest(
        {
            "run_id": scope.run_id,
            "repair_parent_operation_id": parent.operation_id,
            "repair_parent_request_fingerprint": parent.request_fingerprint,
            "repaired_transform_fingerprint": _request_fingerprint(
                scope, command, resolved, ifc_source=ifc_source,
            ),
            "description_artifact": description_artifact.model_dump(mode="json"),
        }
    )


def _create_request_payload(command: DataTransformCommand) -> dict[str, object]:
    return {
        "mode": "create",
        "command": command.model_dump(mode="json"),
    }


def _action_fingerprint(
    scope: InvocationScope, request_identity: str, content_fingerprint: str
) -> str:
    return sha256_digest(
        {
            "run_id": scope.run_id,
            "request_identity": request_identity,
            "content_fingerprint": content_fingerprint,
        }
    )


def _operation_idempotency_key(fingerprint: str) -> str:
    return f"data-transform:{fingerprint}"


def _is_code_validation_failure(code: str) -> bool:
    return code in {
        "TRANSFORM_PYTHON_INVALID",
        "TRANSFORM_SQL_INVALID",
        "TRANSFORM_SQL_NOT_ALLOWED",
    }


def _is_python(command: DataTransformCommand) -> bool:
    return isinstance(command.execution, PythonArrowExecution)


def _total_timeout_seconds(command: DataTransformCommand) -> float:
    return _PYTHON_TOTAL_TIMEOUT_SECONDS if _is_python(command) else _TOTAL_TIMEOUT_SECONDS


def _code_member_name(command: DataTransformCommand) -> str:
    return _PYTHON_CODE_MEMBER if _is_python(command) else _SQL_CODE_MEMBER


def _code_filename(command: DataTransformCommand) -> str:
    return "transformation.py" if _is_python(command) else "transformation.sql"


def _code_media_type(command: DataTransformCommand) -> str:
    return _PYTHON_MEDIA_TYPE if _is_python(command) else _SQL_MEDIA_TYPE


def _payload_member_name(declaration: object) -> str:
    payload_name = str(getattr(declaration, "payload_name"))
    if getattr(declaration, "format") == "json":
        return f"payloads/{payload_name}.json"
    return f"payloads/{payload_name}/part-00000.parquet"


def _schema_member_name(payload_name: str) -> str:
    return f"schemas/{payload_name}.schema.json"


def _output_member(
    *,
    package_id: str,
    declaration: object,
    validated: _ValidatedOutput,
    artifact: ArtifactRecord,
    common: Mapping[str, object],
) -> DataPackageMemberRecord:
    member_name = _payload_member_name(declaration)
    payload_name = str(getattr(declaration, "payload_name"))
    description = str(getattr(declaration, "description"))
    if isinstance(validated, _ValidatedParquetOutput):
        return DataPackageMemberRecord(
            member_id=_member_id(package_id, member_name),
            member_name=member_name,
            member_kind=PackageMemberKind.PAYLOAD,
            artifact=_artifact_ref(artifact),
            media_type=_PARQUET_MEDIA_TYPE,
            row_count=validated.worker_output.receipt.row_count,
            logical_payload_name=payload_name,
            payload_description=description,
            logical_key_fields=getattr(declaration, "logical_key_fields"),
            arrow_schema=_arrow_schema(validated.persisted_schema),
            logical_byte_count=validated.worker_output.receipt.arrow_nbytes,
            **common,
        )
    return DataPackageMemberRecord(
        member_id=_member_id(package_id, member_name),
        member_name=member_name,
        member_kind=PackageMemberKind.PAYLOAD,
        artifact=_artifact_ref(artifact),
        media_type=_JSON_MEDIA_TYPE,
        logical_payload_name=payload_name,
        payload_description=description,
        logical_byte_count=len(validated.content),
        json_kind=JsonPayloadKind(str(getattr(declaration, "json_kind"))),
        **common,
    )


def _request_fingerprint(
    scope: InvocationScope,
    command: DataTransformCommand,
    resolved: tuple[_ResolvedInput, ...],
    *, ifc_source: IfcReadSourceRef | None = None,
) -> str:
    inputs: list[dict[str, object]] = []
    for item in resolved:
        exact_input: dict[str, object] = {
            "input_name": item.request_name,
            "package_id": item.package_id,
            "member_id": item.payload.member.member_id,
            "artifact": item.payload.member.artifact.model_dump(mode="json"),
        }
        if isinstance(item.payload, ExactJsonPayload):
            exact_input["schema_member"] = {
                "member_id": item.payload.schema_member.member_id,
                "artifact": item.payload.schema_member.artifact.model_dump(mode="json"),
            }
        inputs.append(exact_input)
    fingerprint_value: dict[str, object] = {
        "run_id": scope.run_id,
        "inputs": inputs,
        "parameters": [item.model_dump(mode="json") for item in command.parameters],
        "inline_tables": [item.model_dump(mode="json") for item in command.inline_tables],
        "execution": command.execution.model_dump(mode="json"),
        "outputs": [item.model_dump(mode="json") for item in command.outputs],
        "output_package": command.output_package.model_dump(mode="json"),
    }
    if ifc_source is not None:
        fingerprint_value["ifc_source"] = ifc_source.model_dump(mode="json")
    return sha256_digest(fingerprint_value)


def _generated_artifact_idempotency_key(
    fingerprint: str,
    logical_name: str,
    content_sha256: str,
) -> str:
    identity = sha256_digest(
        {
            "request_fingerprint": fingerprint,
            "logical_name": logical_name,
            "content_sha256": content_sha256,
        }
    )
    return f"data-transform-generated:{identity}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _operation_detail(
    command: DataTransformCommand,
    resolved: tuple[_ResolvedInput, ...],
    *, ifc_source: IfcReadSourceRef | None = None,
) -> DataTransformOperationDetail:
    return DataTransformOperationDetail(
        engine=command.execution.engine,
        ifc_source=ifc_source,
        inputs=tuple(
            TransformInputBindingRecord(
                ordinal=index,
                input_name=item.request_name,
                package_id=item.package_id,
                member_id=item.payload.member.member_id,
                payload_name=item.payload.payload_name,
            )
            for index, item in enumerate(resolved, 1)
        ),
        parameters=tuple(
            TransformParameterRecord(
                name=item.name,
                kind=TransformParameterKind(item.kind),
                value=item.value,
            )
            for item in command.parameters
        ),
        inline_tables=command.inline_tables,
        outputs=tuple(
            TransformOutputRecord(
                payload_name=item.payload_name,
                format=item.format,
                description=item.description,
                logical_key_fields=(item.logical_key_fields if item.format == "parquet" else ()),
                json_kind=(JsonPayloadKind(item.json_kind) if item.format == "json" else None),
                schema_json=(item.schema_json if item.format == "json" else None),
            )
            for item in command.outputs
        ),
        output_package_name=command.output_package.name,
    )


def _arrow_inputs(
    resolved: tuple[_ResolvedInput, ...],
    materialized: Mapping[str, Path],
) -> tuple[ArrowTransformInput, ...]:
    try:
        return exact_payload_arrow_inputs(
            tuple(
                ExactPayloadInput(input_name=item.request_name, payload=item.payload)
                for item in resolved
            ),
            materialized,
        )
    except ExactPayloadResolutionError as exc:
        raise DataTransformApplicationError(exc.code, str(exc), phase="preflight") from None


def _all_arrow_inputs(
    resolved: tuple[_ResolvedInput, ...],
    materialized: Mapping[str, Path],
    command: DataTransformCommand,
) -> tuple[ArrowTransformInput, ...]:
    return _arrow_inputs(resolved, materialized) + _inline_arrow_inputs(command)


def _inline_arrow_inputs(command: DataTransformCommand) -> tuple[ArrowTransformInput, ...]:
    arrow_types = {
        "string": pa.string(),
        "integer": pa.int64(),
        "number": pa.float64(),
        "boolean": pa.bool_(),
    }
    result: list[ArrowTransformInput] = []
    try:
        for inline in command.inline_tables:
            arrays = []
            fields = []
            for index, column in enumerate(inline.columns):
                values = [
                    None if row.values[index].kind == "null" else row.values[index].value
                    for row in inline.rows
                ]
                arrow_type = arrow_types[column.type]
                arrays.append(pa.array(values, type=arrow_type))
                fields.append(pa.field(column.name, arrow_type, nullable=column.nullable))
            table = pa.Table.from_arrays(arrays, schema=pa.schema(fields))
            result.append(
                ArrowTransformInput(
                    input_name=inline.input_name,
                    reader=pa.RecordBatchReader.from_batches(
                        table.schema,
                        table.to_batches(max_chunksize=65_536),
                    ),
                    input_format="inline",
                    row_count=table.num_rows,
                )
            )
    except (pa.ArrowException, ValueError, TypeError, OverflowError) as exc:
        raise DataTransformApplicationError(
            "TRANSFORM_INLINE_TABLE_INVALID",
            "An inline table cannot be represented by its declared Arrow types",
            phase="preflight",
        ) from exc
    return tuple(result)


def _resolved_members(
    resolved: tuple[_ResolvedInput, ...],
) -> tuple[DataPackageMemberRecord, ...]:
    return exact_payload_members(
        tuple(
            ExactPayloadInput(input_name=item.request_name, payload=item.payload)
            for item in resolved
        )
    )


async def _validated_outputs(
    outputs: tuple[ArrowTransformOutput, ...],
    command: DataTransformCommand,
    deadline: float,
) -> tuple[_ValidatedOutput, ...]:
    if len(outputs) != len(command.outputs) or any(
        output.payload_name != declaration.payload_name
        for output, declaration in zip(outputs, command.outputs, strict=False)
    ):
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "The Worker outputs do not match the declared Payload order",
            phase="output_validation",
        )
    validated: list[_ValidatedOutput] = []
    total_rows = 0
    total_arrow_bytes = 0
    total_parquet_bytes = 0
    try:
        for output, declaration in zip(outputs, command.outputs, strict=True):
            if declaration.format == "parquet":
                item: _ValidatedOutput = await _validated_parquet_output(
                    output,
                    declaration.logical_key_fields,
                    deadline,
                )
                artifact_bytes = item.parquet_bytes
            else:
                assert isinstance(declaration, JsonOutputDeclaration)
                item = await _validated_json_output(output, declaration, deadline)
                artifact_bytes = len(item.content) + len(item.schema_content)
            validated.append(item)
            total_rows += item.worker_output.receipt.row_count
            total_arrow_bytes += item.worker_output.receipt.arrow_nbytes
            total_parquet_bytes += artifact_bytes
            if total_rows > _OUTPUT_ROW_LIMIT or total_arrow_bytes > _OUTPUT_ARROW_BYTES:
                raise DataTransformApplicationError(
                    "TRANSFORM_OUTPUT_BUDGET_EXCEEDED",
                    "The aggregate Worker outputs exceed the row or Arrow budget",
                    phase="output_validation",
                )
            if total_parquet_bytes > _OUTPUT_AGGREGATE_PARQUET_BYTES:
                raise DataTransformApplicationError(
                    (
                        "JSON_OUTPUT_BUDGET_EXCEEDED"
                        if any(item.format == "json" for item in command.outputs)
                        else "TRANSFORM_OUTPUT_BUDGET_EXCEEDED"
                    ),
                    "The aggregate declared output Artifacts exceed the 256 MiB budget",
                    phase="output_validation",
                )
        return tuple(validated)
    except BaseException:
        for item in validated:
            if isinstance(item, _ValidatedParquetOutput):
                with suppress(OSError):
                    item.path.unlink(missing_ok=True)
        raise


async def _validated_json_output(
    output: ArrowTransformOutput,
    declaration: JsonOutputDeclaration,
    deadline: float,
) -> _ValidatedJsonOutput:
    return await _await_before_deadline(
        asyncio.to_thread(_validated_json_output_sync, output, declaration),
        deadline,
        phase="output_validation",
    )


def _validated_json_output_sync(
    output: ArrowTransformOutput,
    declaration: JsonOutputDeclaration,
) -> _ValidatedJsonOutput:
    table = _validated_worker_arrow_table(output)
    value = _json_value_from_worker_table(table, declaration.json_kind)
    if _json_depth(value) > _JSON_MAX_DEPTH:
        raise DataTransformApplicationError(
            "JSON_OUTPUT_BUDGET_EXCEEDED",
            "A JSON output exceeds the 64-level nesting budget",
            phase="output_validation",
        )
    try:
        content = canonical_json(value).encode("utf-8")
    except ValueError as exc:
        raise DataTransformApplicationError(
            "JSON_INSTANCE_INVALID",
            "The Worker JSON value is outside the supported canonical JSON domain",
            phase="output_validation",
        ) from exc
    if len(content) > _JSON_PAYLOAD_BYTES:
        raise DataTransformApplicationError(
            "JSON_OUTPUT_BUDGET_EXCEEDED",
            "A JSON output exceeds the 100 MiB per-Payload budget",
            phase="output_validation",
        )
    schema = _parse_json_schema(declaration.schema_json, declaration.json_kind)
    _validate_json_instance(schema, value)
    return _ValidatedJsonOutput(
        worker_output=output,
        content=content,
        schema_content=declaration.schema_json.encode("utf-8"),
    )


async def _validated_parquet_output(
    output: ArrowTransformOutput,
    logical_key_fields: tuple[str, ...],
    deadline: float,
) -> _ValidatedParquetOutput:
    _check_deadline(deadline, "output_validation")
    table = await _await_before_deadline(
        asyncio.to_thread(
            _validated_arrow_table,
            output,
            logical_key_fields,
        ),
        deadline,
        phase="output_validation",
    )
    fd: int | None = None
    raw_path: str | None = None
    keep_output = False
    try:
        fd, raw_path = tempfile.mkstemp(
            prefix="cad2bim-transform-output-",
            suffix=".parquet",
        )
        # PyArrow owns the output file; close the unused exclusive descriptor first.
        os.close(fd)
        fd = None
        Path(raw_path).unlink(missing_ok=True)
        write_task = asyncio.create_task(
            asyncio.to_thread(pq.write_table, table, raw_path, compression="zstd")
        )
        try:
            remaining = max(0.0, deadline - monotonic())
            async with asyncio.timeout(remaining):
                await asyncio.shield(write_task)
        except TimeoutError:
            _transfer_output_cleanup(write_task, Path(raw_path))
            raw_path = None
            raise DataTransformApplicationError(
                "TRANSFORM_TIMEOUT",
                "The transform request deadline was reached during output validation",
                phase="output_validation",
            ) from None
        except asyncio.CancelledError:
            _transfer_output_cleanup(write_task, Path(raw_path))
            raw_path = None
            raise
        except (pa.ArrowException, ValueError, TypeError, NotImplementedError) as exc:
            raise DataTransformApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "The Worker result cannot be serialized as the declared Parquet output",
                phase="output_validation",
            ) from exc
        path = Path(raw_path)
        parquet_bytes = path.stat().st_size
        if parquet_bytes > _OUTPUT_PARQUET_BYTES:
            raise DataTransformApplicationError(
                "TRANSFORM_OUTPUT_BUDGET_EXCEEDED",
                "One output Parquet exceeds the 100 MiB M2b per-Payload budget",
                phase="output_validation",
            )
        try:
            persisted_schema = pq.read_schema(path)
        except (OSError, pa.ArrowException) as exc:
            raise DataTransformApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "The serialized Parquet output has no readable schema",
                phase="output_validation",
            ) from exc
        _check_deadline(deadline, "output_validation")
        keep_output = True
        return _ValidatedParquetOutput(
            worker_output=output,
            table=table,
            path=path,
            parquet_bytes=parquet_bytes,
            persisted_schema=persisted_schema,
        )
    except OSError as exc:
        raise DataTransformApplicationError(
            "TRANSFORM_RESOURCE_LIMIT",
            "Output scratch storage could not create, write, or inspect the Parquet result",
            phase="output_validation",
        ) from exc
    finally:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
        if raw_path is not None and not keep_output:
            with suppress(OSError):
                Path(raw_path).unlink(missing_ok=True)


def _validated_arrow_table(
    output: ArrowTransformOutput,
    logical_key_fields: tuple[str, ...],
) -> pa.Table:
    table = _validated_worker_arrow_table(output)
    keys = logical_key_fields
    if any(key not in table.column_names for key in keys):
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "A declared logical key field is absent from the result",
            phase="output_validation",
        )
    if any(table.column(key).null_count for key in keys):
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "Logical key fields must not contain null values",
            phase="output_validation",
        )
    if table.num_rows:
        try:
            unique_count = table.select(keys).group_by(list(keys)).aggregate([]).num_rows
        except (pa.ArrowException, NotImplementedError) as exc:
            raise DataTransformApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "Logical key fields cannot be deterministically validated",
                phase="output_validation",
            ) from exc
        if unique_count != table.num_rows:
            raise DataTransformApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "Logical key field combinations must be unique",
                phase="output_validation",
            )
    return table


def _validated_worker_arrow_table(output: ArrowTransformOutput) -> pa.Table:
    try:
        table = pa.Table.from_batches(output.batches, schema=output.schema)
    except (pa.ArrowException, ValueError) as exc:
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "The Worker result is not a valid Arrow table",
            phase="output_validation",
        ) from exc
    receipt = output.receipt
    if (
        not 1 <= table.num_columns <= _OUTPUT_COLUMN_LIMIT
        or len(set(table.column_names)) != table.num_columns
    ):
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "The Worker result must contain 1..512 uniquely named columns",
            phase="output_validation",
        )
    _validate_arrow_field_names(table.schema)
    if table.num_rows != receipt.row_count or table.nbytes != receipt.arrow_nbytes:
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "The Worker output receipt does not match its Arrow batches",
            phase="output_validation",
        )
    if table.num_rows > _OUTPUT_ROW_LIMIT or table.nbytes > _OUTPUT_ARROW_BYTES:
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_BUDGET_EXCEEDED",
            "The Worker output exceeds the M2b row or Arrow budget",
            phase="output_validation",
        )
    return table


def _transfer_output_cleanup(write_task: asyncio.Task[None], path: Path) -> None:
    cleanup = asyncio.create_task(_finish_output_cleanup(write_task, path))
    _OUTPUT_VALIDATION_CLEANUPS.add(cleanup)
    cleanup.add_done_callback(_OUTPUT_VALIDATION_CLEANUPS.discard)


async def _finish_output_cleanup(write_task: asyncio.Task[None], path: Path) -> None:
    try:
        await write_task
    except BaseException:
        pass
    finally:
        with suppress(OSError):
            path.unlink(missing_ok=True)


def _validate_arrow_field_names(schema: pa.Schema) -> None:
    """Validate every top-level and nested Arrow field before publication."""

    pending = list(schema)
    while pending:
        field = pending.pop()
        name = field.name
        if not name.strip() or "\x00" in name or len(name) > 255:
            raise DataTransformApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "Arrow field names must be 1..255 non-blank characters without NUL",
                phase="output_validation",
            )
        field_type = field.type
        try:
            validate_arrow_type_string(str(field_type))
        except ValueError:
            raise DataTransformApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                (
                    "Arrow field types must have a serializable description of at most "
                    f"{MAX_ARROW_TYPE_UTF8_BYTES} UTF-8 bytes"
                ),
                phase="output_validation",
            ) from None
        # Arrow nested types expose child fields through num_fields/field().
        # Actual Parquet type support remains checked by pq.write_table below.
        for index in range(field_type.num_fields):
            pending.append(field_type.field(index))


def _preflight_json_schemas(command: DataTransformCommand) -> None:
    for declaration in command.outputs:
        if isinstance(declaration, JsonOutputDeclaration):
            _parse_json_schema(declaration.schema_json, declaration.json_kind)


def _parse_json_schema(schema_json: str, json_kind: str) -> object:
    try:
        schema = _load_json(schema_json)
    except (UnicodeError, ValueError) as exc:
        raise DataTransformApplicationError(
            "JSON_SCHEMA_INVALID",
            "schema_json must contain one valid JSON Schema document",
            phase="preflight",
        ) from exc
    if _json_depth(schema) > _JSON_MAX_DEPTH:
        raise DataTransformApplicationError(
            "JSON_SCHEMA_INVALID",
            "JSON Schema nesting exceeds the 64-level product limit",
            phase="preflight",
        )
    if (
        isinstance(schema, dict)
        and schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
        != "https://json-schema.org/draft/2020-12/schema"
    ):
        raise DataTransformApplicationError(
            "JSON_SCHEMA_INVALID",
            "schema_json must use JSON Schema Draft 2020-12",
            phase="preflight",
        )
    if json_kind == "records" and (
        not isinstance(schema, dict) or schema.get("type") != "array" or "items" not in schema
    ):
        raise DataTransformApplicationError(
            "JSON_SCHEMA_INVALID",
            "A records JSON Schema must declare an array root with an items contract",
            phase="preflight",
        )
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        _validate_local_schema_references(schema)
    except (jsonschema.SchemaError, ValueError, RecursionError) as exc:
        raise DataTransformApplicationError(
            "JSON_SCHEMA_INVALID",
            "schema_json is not a safe Draft 2020-12 schema with resolvable local references",
            phase="preflight",
        ) from exc
    return schema


def _validate_local_schema_references(schema: object) -> None:
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef"}:
                    if not isinstance(value, str) or not (
                        value == "#" or value.startswith("#/$defs/")
                    ):
                        raise ValueError("JSON Schema references must stay in the local $defs tree")
                    _resolve_local_schema_pointer(schema, value)
                pending.append(value)
        elif isinstance(node, list):
            pending.extend(node)


def _resolve_local_schema_pointer(schema: object, reference: str) -> None:
    if reference == "#":
        return
    current = schema
    for raw_token in reference[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ValueError("JSON Schema local reference does not resolve")


def _validate_json_instance(schema: object, value: object) -> None:
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except Exception as exc:
        if isinstance(exc, DataTransformApplicationError):
            raise
        raise DataTransformApplicationError(
            "JSON_INSTANCE_INVALID",
            "The Worker JSON value does not satisfy its exact JSON Schema",
            phase="output_validation",
        ) from exc


def _load_json(value: str) -> object:
    def reject_constant(constant: str) -> object:
        raise ValueError(f"non-finite JSON number is not allowed: {constant}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key is not allowed")
            result[key] = item
        return result

    try:
        return json.loads(
            value,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid bounded JSON text") from exc


def _json_depth(value: object) -> int:
    maximum = 1
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        node, depth = pending.pop()
        maximum = max(maximum, depth)
        if maximum > _JSON_MAX_DEPTH:
            return maximum
        if isinstance(node, dict):
            pending.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return maximum


def _json_value_from_worker_table(table: pa.Table, json_kind: str) -> object:
    if json_kind == "document":
        if table.column_names != ["json_value"] or table.num_rows != 1:
            raise DataTransformApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "A document JSON output must be exactly one json_value row and column",
                phase="output_validation",
            )
        _validate_json_text_column(table.schema.field("json_value"))
        value = table.column("json_value")[0].as_py()
        if not isinstance(value, str):
            raise DataTransformApplicationError(
                "JSON_INSTANCE_INVALID",
                "The document json_value must be non-null JSON text",
                phase="output_validation",
            )
        try:
            return _load_json(value)
        except ValueError as exc:
            raise DataTransformApplicationError(
                "JSON_INSTANCE_INVALID",
                "The document json_value is not valid JSON text",
                phase="output_validation",
            ) from exc

    if table.column_names != ["record_index", "json_value"]:
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "A records JSON output must contain record_index and json_value columns",
            phase="output_validation",
        )
    if table.schema.field("record_index").type != pa.int64():
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "A records JSON output record_index must be BIGINT/int64",
            phase="output_validation",
        )
    _validate_json_text_column(table.schema.field("json_value"))
    indices = table.column("record_index").to_pylist()
    texts = table.column("json_value").to_pylist()
    if any(index is None for index in indices) or any(text is None for text in texts):
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "JSON record indices and values must be non-null",
            phase="output_validation",
        )
    ordered = sorted(zip(indices, texts, strict=True))
    if [index for index, _ in ordered] != list(range(len(ordered))):
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "JSON record_index values must be unique and contiguous from zero",
            phase="output_validation",
        )
    try:
        return [_load_json(text) for _, text in ordered]
    except (TypeError, ValueError) as exc:
        raise DataTransformApplicationError(
            "JSON_INSTANCE_INVALID",
            "A records json_value is not valid JSON text",
            phase="output_validation",
        ) from exc


def _validate_json_text_column(field: pa.Field) -> None:
    if not (pa.types.is_string(field.type) or pa.types.is_large_string(field.type)):
        raise DataTransformApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "JSON json_value columns must use JSON/VARCHAR Arrow text",
            phase="output_validation",
        )


def _derived_manifest(
    *,
    command: DataTransformCommand,
    synopsis: PackageSynopsis,
    resolved: tuple[_ResolvedInput, ...],
    operation_id: str,
    package_id: str,
    created_at: datetime,
    code_artifact: ArtifactRecord,
    description_artifact: ArtifactRecord,
    payload_artifacts: Mapping[str, ArtifactRecord],
    schema_artifacts: Mapping[str, ArtifactRecord],
    validated_outputs: tuple[_ValidatedOutput, ...],
    ifc_source: IfcReadSourceRef | None = None,
) -> dict[str, object]:
    payloads: dict[str, object] = {}
    for declaration, validated in zip(command.outputs, validated_outputs, strict=True):
        payload_member_name = _payload_member_name(declaration)
        payload_artifact = payload_artifacts[declaration.payload_name]
        if isinstance(validated, _ValidatedParquetOutput):
            payloads[declaration.payload_name] = {
                "format": "parquet",
                "media_type": _PARQUET_MEDIA_TYPE,
                "description": declaration.description,
                "logical_key_fields": list(declaration.logical_key_fields),
                "row_count": validated.worker_output.receipt.row_count,
                "logical_byte_count": validated.worker_output.receipt.arrow_nbytes,
                "arrow_schema": [
                    item.model_dump(mode="json") for item in _arrow_schema(validated.persisted_schema)
                ],
                "members": [
                    {
                        "logical_name": payload_member_name,
                        "size_bytes": payload_artifact.size_bytes,
                        "artifact_version_ref": _artifact_ref(payload_artifact).model_dump(
                            mode="json"
                        ),
                    }
                ],
            }
        else:
            schema_artifact = schema_artifacts[declaration.payload_name]
            payloads[declaration.payload_name] = {
                "format": "json",
                "media_type": _JSON_MEDIA_TYPE,
                "description": declaration.description,
                "json_kind": declaration.json_kind,
                "logical_byte_count": len(validated.content),
                "members": [
                    {
                        "logical_name": payload_member_name,
                        "size_bytes": payload_artifact.size_bytes,
                        "artifact_version_ref": _artifact_ref(payload_artifact).model_dump(
                            mode="json"
                        ),
                    }
                ],
                "schema": {
                    "logical_name": _schema_member_name(declaration.payload_name),
                    "size_bytes": schema_artifact.size_bytes,
                    "artifact_version_ref": _artifact_ref(schema_artifact).model_dump(mode="json"),
                },
            }
    return {
        "publication_state": "published",
        "package_id": package_id,
        "package_kind": "derived",
        "name": command.output_package.name,
        "created_at": created_at,
        "description": {
            "member_name": _DESCRIPTION_MEMBER,
            "description_title": synopsis.description_title,
            "summary": synopsis.summary,
            "section_titles": list(synopsis.section_titles),
            "artifact_version_ref": _artifact_ref(description_artifact).model_dump(mode="json"),
        },
        "transformation": {
            "operation_id": operation_id,
            **({"ifc_source": ifc_source.model_dump(mode="json")} if ifc_source is not None else {}),
            "engine": command.execution.engine,
            "code_member_name": _code_member_name(command),
            "code_artifact_version_ref": _artifact_ref(code_artifact).model_dump(mode="json"),
            "inputs": [
                {
                    "ordinal": index,
                    "input_name": item.request_name,
                    "package_id": item.package_id,
                    "member_id": item.payload.member.member_id,
                    "payload_name": item.payload.payload_name,
                    "format": ("json" if isinstance(item.payload, ExactJsonPayload) else "parquet"),
                }
                for index, item in enumerate(resolved, 1)
            ],
            "parameters": [item.model_dump(mode="json") for item in command.parameters],
            "inline_tables": [item.model_dump(mode="json") for item in command.inline_tables],
        },
        "payloads": payloads,
    }


def _ordered_output_members(
    members: tuple[DataPackageMemberRecord, ...],
    declarations: tuple[TransformOutputRecord, ...],
) -> tuple[DataPackageMemberRecord, ...]:
    payloads = {
        item.logical_payload_name: item
        for item in members
        if item.member_kind is PackageMemberKind.PAYLOAD and item.logical_payload_name is not None
    }
    expected_names = tuple(item.payload_name for item in declarations)
    if len(payloads) != len(expected_names) or set(payloads) != set(expected_names):
        raise DataTransformApplicationError(
            "PACKAGE_INTEGRITY_ERROR",
            "The derived Package Payloads do not match the exact output declarations",
            phase="publication",
        )
    return tuple(payloads[name] for name in expected_names)


def _arrow_schema(schema: pa.Schema) -> tuple[ArrowColumnRecord, ...]:
    return tuple(
        ArrowColumnRecord(name=field.name, type=str(field.type), nullable=field.nullable)
        for field in schema
    )


def _artifact_ref(artifact: ArtifactRecord) -> ArtifactVersionRef:
    return ArtifactVersionRef(
        artifact_id=artifact.artifact_id,
        version_id=artifact.version_id,
        sha256=artifact.sha256,
    )


def _member_id(package_id: str, member_name: str) -> str:
    return stable_id("package_member", {"package_id": package_id, "name": member_name})


def _intent_summary(synopsis: PackageSynopsis) -> str:
    return f"{synopsis.description_title} — {synopsis.summary}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_deadline(deadline: float, phase: TransformPhase) -> None:
    if monotonic() >= deadline:
        raise DataTransformApplicationError(
            "TRANSFORM_TIMEOUT",
            "The transform request deadline was reached",
            phase=phase,
        )


async def _await_before_deadline(
    awaitable: Awaitable[_T],
    deadline: float,
    *,
    phase: TransformPhase,
) -> _T:
    """Bound an async stage by the one request-level monotonic deadline."""

    remaining = max(0.0, deadline - monotonic())
    try:
        async with asyncio.timeout(remaining):
            return await awaitable
    except TimeoutError:
        raise DataTransformApplicationError(
            "TRANSFORM_TIMEOUT",
            "The transform request deadline was reached",
            phase=phase,
        ) from None


async def _cancelled_worker_failure(
    task: asyncio.Task[tuple[ArrowTransformOutput, ...]],
) -> DataTransformWorkerError | None:
    """Collect bounded worker cleanup and retain only an explicit lifecycle fact."""

    try:
        await task
    except DataTransformWorkerError as exc:
        return exc
    except asyncio.CancelledError as exc:
        failure = getattr(exc, "worker_failure", None)
        return failure if isinstance(failure, DataTransformWorkerError) else None
    except Exception:
        return None
    return None


async def _await_publication_io(awaitable: Awaitable[_T], deadline: float) -> _T:
    """Keep a small request-deadline reserve for commit reconciliation."""

    remaining = max(
        0.0,
        deadline - monotonic() - _PUBLICATION_RECONCILE_RESERVE_SECONDS,
    )
    try:
        async with asyncio.timeout(remaining):
            return await awaitable
    except TimeoutError:
        raise DataTransformApplicationError(
            "TRANSFORM_SERVICE_UNAVAILABLE",
            "Publication did not finish before the request deadline",
            phase="publication",
            retryable=True,
        ) from None


async def _file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while chunk := await asyncio.to_thread(handle.read, chunk_size):
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


__all__ = [
    "DataTransformApplicationError",
    "DataTransformApplicationResult",
    "DataTransformService",
]
