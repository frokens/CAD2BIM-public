"""Source-first orchestration for durable, read-only IFC Python queries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from time import monotonic
from CAD2BIM_backend.domain.runtime_execution import IFC_QUERY_EXECUTION
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn

from CAD2BIM_backend.application.artifact_service import ArtifactDownload, ArtifactService
from CAD2BIM_backend.application.async_settlement import drain_shielded_task
from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError
from CAD2BIM_backend.application.exact_artifact_reader import (
    ExactArtifactReadError,
    ExactArtifactReader,
)
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactJsonPayload,
    ExactPayloadInput,
    ExactPayloadResolutionError,
    ExactPayloadResolver,
    ExactTransformPayload,
    exact_payload_arrow_inputs,
    exact_payload_members,
)
from CAD2BIM_backend.application.text_source_view import TextSourceViewError, TextSourceViewService
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcRevision,
    IfcModelFrozenError,
    IfcModelIntegrityError,
    IfcModelNotInitializedError,
    IfcRunIdentity,
)
from CAD2BIM_backend.domain.ifc_query import (
    IfcQueryArtifactRef,
    IfcQueryAttemptPreparation,
    IfcQueryAttemptRecord,
    IfcQueryAttemptStatus,
    IfcQueryAttemptTerminal,
    IfcQueryBudgetSummary,
    IfcQueryDiagnostic,
    IfcQueryExactInputRef,
    IfcQueryInputBinding,
    IfcQueryPage,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.data_runtime import PublishedDataPackage
from CAD2BIM_backend.domain.text_source import SourceEdit, TextSourceView, TextSourceViewResult
from CAD2BIM_backend.ports.ifc_queries import IfcQueryRepository
from CAD2BIM_backend.ports.ifc_query_worker import (
    IfcQueryWorkerError,
    IfcQueryWorkerPort,
    IfcQueryWorkerRequest,
)
from CAD2BIM_backend.ports.ifc_revision_reader import IfcRevisionReaderPort
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError, OpaqueCursorCodecPort
from CAD2BIM_backend.ports.source_patch import SourcePatchPort, SourcePatchPortError


_ATTEMPT_ALIAS_DOMAIN = "ifc_query_attempt_alias"
_PAGE_CURSOR_DOMAIN = "ifc_query_result_page"
_PYTHON_MEDIA_TYPE = "text/x-python"
_JSON_MEDIA_TYPE = "application/json"
_MAX_SOURCE_BYTES = 65_536
_MAX_DIAGNOSTIC_BYTES = 2_000
_MAX_RESULT_BYTES = 256 * 1024
_MAX_PAGE_BYTES = 65_536
_MAX_INPUT_STORED_BYTES = 256 * 1024 * 1024
_MAX_PAGE_SIZE = 200
_CURSOR_TTL_SECONDS = 15 * 60
_REQUEST_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class IfcQueryApplicationError(RuntimeError):
    """Stable bounded failure suitable for provider projection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal["read", "preflight", "execution", "output_validation"] = "read",
        retryable: bool = False,
        attempt_alias: str | None = None,
        diagnostic: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(_bounded_message(message))
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.attempt_alias = attempt_alias
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


class IfcQueryService:
    """Persist exact source and inputs, execute once, and page one result Artifact."""

    def __init__(
        self,
        repository: IfcQueryRepository,
        artifacts: ArtifactService,
        payloads: ExactPayloadResolver,
        revisions: IfcRevisionReaderPort,
        worker: IfcQueryWorkerPort,
        cursor_codec: OpaqueCursorCodecPort,
        source_patches: SourcePatchPort,
        *,
        clock: Callable[[], datetime] | None = None,
        text_views: TextSourceViewService | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._exact_artifacts = ExactArtifactReader(artifacts)
        self._payloads = payloads
        self._revisions = revisions
        self._worker = worker
        self._cursor_codec = cursor_codec
        self._source_patches = source_patches
        self._clock = clock or (lambda: datetime.now(UTC))
        self._text_views = text_views or TextSourceViewService()

    async def fresh(
        self,
        scope: InvocationScope,
        source_code: str,
        request_identity: str,
        inputs: Sequence[IfcQueryInputBinding] = (),
        page_size: int = 100,
        *, deadline: float | None = None,
    ) -> IfcQueryPage:
        """Resume an admitted request before pinning CURRENT for a new one."""

        deadline = monotonic() + IFC_QUERY_EXECUTION.total_seconds if deadline is None else deadline
        _validate_source(source_code)
        _validate_request_identity(request_identity)
        _validate_page_size(page_size)
        attempt_id = _attempt_id(scope, request_identity)
        request_digest = sha256_digest(
            {
                "domain": "ifc_query_request_v2",
                "mode": "fresh",
                "source_code": source_code,
                "inputs": [item.model_dump(mode="json") for item in inputs],
            }
        )
        previous = await self._repository.get_ifc_query_attempt(scope, attempt_id=attempt_id)
        if previous is not None:
            return await self._resume_attempt(scope, previous, request_digest, page_size)
        try:
            exact = await self._revisions.pin_current(_ifc_scope(scope))
            exact_inputs = await self._resolve_fresh_inputs(scope, inputs)
        except IfcQueryApplicationError:
            raise
        except ExactPayloadResolutionError as exc:
            raise _payload_error(exc) from None
        except (IfcModelNotInitializedError, IfcModelFrozenError, IfcModelIntegrityError) as exc:
            raise _revision_error(exc) from None
        return await self._prepare_and_execute(
            scope,
            exact=exact,
            source_code=source_code,
            exact_inputs=exact_inputs,
            deadline=deadline,
            repair_parent_attempt_id=None,
            request_identity=request_identity,
            request_digest=request_digest,
            page_size=page_size,
        )

    async def repair(
        self,
        scope: InvocationScope,
        parent_attempt_id: str,

        edits: Sequence[SourceEdit],
        request_identity: str,
        page_size: int = 100,
        *, deadline: float | None = None,
        request_digest: str | None = None,
    ) -> IfcQueryPage:
        """Patch one terminal parent's source while retaining every exact input."""

        deadline = monotonic() + IFC_QUERY_EXECUTION.total_seconds if deadline is None else deadline
        _validate_request_identity(request_identity)
        _validate_page_size(page_size)
        attempt_id = _attempt_id(scope, request_identity)
        request_digest = request_digest or sha256_digest(
            {
                "domain": "ifc_query_request_v2",
                "mode": "repair",
                "parent_attempt_id": parent_attempt_id,

                "edits": [
                    {"old_text": item.old_text, "new_text": item.new_text}
                    for item in edits
                ],
            }
        )
        previous = await self._repository.get_ifc_query_attempt(scope, attempt_id=attempt_id)
        if previous is not None:
            return await self._resume_attempt(scope, previous, request_digest, page_size)
        parent = await self._repository.get_ifc_query_attempt(
            scope,
            attempt_id=parent_attempt_id,
        )
        if parent is None or parent.run_id != scope.run_id:
            raise IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_NOT_FOUND",
                "The parent IFC query attempt was not found",
                phase="preflight",
            )
        if parent.status is IfcQueryAttemptStatus.PREPARED:
            raise IfcQueryApplicationError(
                "IFC_QUERY_PARENT_NOT_TERMINAL",
                "The parent IFC query attempt is not terminal",
                phase="preflight",
                attempt_alias=self._attempt_alias(scope, parent),
            )
        source = await self._read_exact_artifact(
            scope,
            parent.source_artifact,
            expected_content_type=_PYTHON_MEDIA_TYPE,
        )
        try:
            decoded = source.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IfcQueryApplicationError(
                "IFC_QUERY_SOURCE_INVALID",
                "The parent IFC query source is not valid UTF-8",
                phase="preflight",
                attempt_alias=self._attempt_alias(scope, parent),
            ) from exc
        try:
            patched = self._source_patches.apply_text(decoded, edits, maximum_source_bytes=_MAX_SOURCE_BYTES)
        except SourcePatchPortError as exc:
            code, retryable = _patch_error(exc.failure)
            raise IfcQueryApplicationError(
                code,
                str(exc),
                phase="preflight",
                retryable=retryable,
                attempt_alias=self._attempt_alias(scope, parent),
            ) from None
        _validate_source(patched)
        try:
            exact = await self._revisions.pin_exact(
                _ifc_scope(scope),
                revision_id=parent.ifc_revision_id,
            )
        except (IfcModelNotInitializedError, IfcModelFrozenError, IfcModelIntegrityError) as exc:
            raise _revision_error(exc, attempt_alias=self._attempt_alias(scope, parent)) from None
        if exact.model_id != parent.ifc_model_id:
            raise IfcQueryApplicationError(
                "IFC_QUERY_IFC_INTEGRITY_FAILED",
                "The parent attempt exact IFC binding failed integrity validation",
                phase="preflight",
                attempt_alias=self._attempt_alias(scope, parent),
            )
        return await self._prepare_and_execute(
            scope,
            exact=exact,
            source_code=patched,
            exact_inputs=parent.inputs,
            deadline=deadline,
            repair_parent_attempt_id=parent.attempt_id,
            request_identity=request_identity,
            request_digest=request_digest,
            page_size=page_size,
        )

    async def repair_from_alias(
        self,
        scope: InvocationScope,
        parent_attempt_alias: str,

        edits: Sequence[SourceEdit],
        request_identity: str,
        page_size: int = 100,
        *, deadline: float | None = None,
    ) -> IfcQueryPage:
        """Replay an admitted repair before resolving its original parent alias."""

        _validate_request_identity(request_identity)
        _validate_page_size(page_size)
        attempt_id = _attempt_id(scope, request_identity)
        request_digest = sha256_digest(
            {
                "domain": "ifc_query_request_v2",
                "mode": "repair",
                "request_identity": request_identity,
                "parent_attempt_alias": parent_attempt_alias,

                "edits": [
                    {"old_text": item.old_text, "new_text": item.new_text}
                    for item in edits
                ],
            }
        )
        previous = await self._repository.get_ifc_query_attempt(scope, attempt_id=attempt_id)
        if previous is not None:
            return await self._resume_attempt(scope, previous, request_digest, page_size)
        try:
            parent_id = await self.resolve_attempt_alias(scope, parent_attempt_alias)
        except IfcQueryApplicationError:
            previous = await self._repository.get_ifc_query_attempt(scope, attempt_id=attempt_id)
            if previous is not None:
                return await self._resume_attempt(scope, previous, request_digest, page_size)
            raise
        return await self.repair(
            scope, parent_id, edits, request_identity,
            page_size, deadline=deadline, request_digest=request_digest,
        )

    async def continue_page(self, scope: InvocationScope, cursor: str) -> IfcQueryPage:
        """Read a later page from one exact result Artifact without running code."""

        try:
            claims = self._cursor_codec.decode(cursor)
        except OpaqueCursorCodecError as exc:
            raise IfcQueryApplicationError(
                "IFC_QUERY_CURSOR_INVALID", "The IFC query cursor is invalid"
            ) from exc
        if claims.get("domain") != _PAGE_CURSOR_DOMAIN:
            raise IfcQueryApplicationError(
                "IFC_QUERY_CURSOR_INVALID", "The IFC query cursor is invalid"
            )
        if claims.get("scope") != _scope_digest(scope):
            raise IfcQueryApplicationError(
                "IFC_QUERY_CURSOR_SCOPE_MISMATCH",
                "The IFC query cursor belongs to a different Run",
            )
        attempt_id = claims.get("attempt_id")
        offset = claims.get("offset")
        page_size = claims.get("page_size")
        expires_at = claims.get("expires_at")
        if (
            not isinstance(attempt_id, str)
            or _REQUEST_IDENTITY.fullmatch(attempt_id) is None
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
        ):
            raise IfcQueryApplicationError(
                "IFC_QUERY_CURSOR_INVALID", "The IFC query cursor is invalid"
            )
        _validate_page_size(page_size, cursor=True)
        if self._now().timestamp() > expires_at:
            raise IfcQueryApplicationError(
                "IFC_QUERY_CURSOR_EXPIRED",
                "The IFC query cursor expired; restart from a fresh query",
            )
        attempt = await self._repository.get_ifc_query_attempt(
            scope,
            attempt_id=attempt_id,
        )
        if (
            attempt is None
            or attempt.run_id != scope.run_id
            or attempt.status is not IfcQueryAttemptStatus.SUCCEEDED
        ):
            raise IfcQueryApplicationError(
                "IFC_QUERY_CURSOR_INVALID",
                "The cursor's terminal IFC query attempt was not found",
            )
        _validate_result_claim(claims.get("result"), attempt)
        return await self._page_from_attempt(
            scope,
            attempt,
            offset,
            page_size,
            expires_at=expires_at,
        )

    async def read_source(
        self,
        scope: InvocationScope,
        attempt_id: str,
        view: TextSourceView,
    ) -> TextSourceViewResult:
        """Read one RLS-visible attempt's exact immutable source version."""

        attempt = await self._repository.get_ifc_query_attempt(
            scope,
            attempt_id=attempt_id,
        )
        if attempt is None or attempt.run_id != scope.run_id:
            raise IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_NOT_FOUND", "The IFC query attempt was not found"
            )
        downloaded = await self._read_exact_artifact(
            scope,
            attempt.source_artifact,
            expected_content_type=_PYTHON_MEDIA_TYPE,
        )
        try:
            return self._text_views.view(downloaded.content.decode("utf-8"), view)
        except UnicodeDecodeError as exc:
            raise IfcQueryApplicationError(
                "IFC_QUERY_SOURCE_INVALID",
                "The exact IFC query source is not valid UTF-8",
            ) from exc
        except TextSourceViewError as exc:
            raise IfcQueryApplicationError("IFC_QUERY_SOURCE_READ_INVALID", str(exc)) from exc

    async def list_terminal_ifc_query_attempts(
        self,
        scope: InvocationScope,
        *,
        statuses: tuple[IfcQueryAttemptStatus, ...],
        before_created_at: datetime | None,
        before_attempt_id: str | None,
        limit: int,
    ) -> tuple[IfcQueryAttemptRecord, ...]:
        """List terminal attempts from the current Run using a stable raw keyset."""

        _validate_terminal_attempt_list(
            statuses=statuses,
            before_created_at=before_created_at,
            before_attempt_id=before_attempt_id,
            limit=limit,
        )
        return await self._repository.list_terminal_ifc_query_attempts(
            scope,
            statuses=statuses,
            before_created_at=before_created_at,
            before_attempt_id=before_attempt_id,
            limit=limit,
        )

    def attempt_alias(
        self,
        scope: InvocationScope,
        attempt: IfcQueryAttemptRecord,
    ) -> str:
        """Project the existing Run-bound opaque alias for one visible attempt."""

        return self._attempt_alias(scope, attempt)

    async def attempt_alias_for_visible_id(
        self,
        scope: InvocationScope,
        attempt_id: str,
    ) -> str:
        """Reauthorize one Run-bound attempt before projecting its opaque alias."""

        attempt = await self._repository.get_ifc_query_attempt(
            scope,
            attempt_id=attempt_id,
        )
        if attempt is None or attempt.run_id != scope.run_id:
            raise IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_NOT_FOUND",
                "The IFC query attempt was not found",
            )
        return self._attempt_alias(scope, attempt)

    async def resolve_attempt_alias(self, scope: InvocationScope, attempt_alias: str) -> str:
        """Decode a Run-bound alias, then reauthorize its attempt through RLS."""

        try:
            claims = self._cursor_codec.decode(attempt_alias)
        except OpaqueCursorCodecError as exc:
            raise IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_ALIAS_INVALID",
                "The IFC query attempt alias is invalid",
            ) from exc
        if claims.get("domain") != _ATTEMPT_ALIAS_DOMAIN:
            raise IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_ALIAS_INVALID",
                "The IFC query attempt alias is invalid",
            )
        if claims.get("scope") != _scope_digest(scope):
            raise IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_SCOPE_MISMATCH",
                "The IFC query attempt alias belongs to a different Run",
            )
        attempt_id = claims.get("attempt_id")
        if not isinstance(attempt_id, str) or _REQUEST_IDENTITY.fullmatch(attempt_id) is None:
            raise IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_ALIAS_INVALID",
                "The IFC query attempt alias is invalid",
            )
        attempt = await self._repository.get_ifc_query_attempt(
            scope,
            attempt_id=attempt_id,
        )
        if attempt is None or attempt.run_id != scope.run_id:
            raise IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_NOT_FOUND", "The IFC query attempt was not found"
            )
        return attempt.attempt_id

    async def _resolve_fresh_inputs(
        self,
        scope: InvocationScope,
        bindings: Sequence[IfcQueryInputBinding],
    ) -> tuple[IfcQueryExactInputRef, ...]:
        if len(bindings) > 16:
            raise IfcQueryApplicationError(
                "INVALID_ARGUMENT",
                "At most 16 exact Package Payloads may be bound",
                phase="preflight",
            )
        names: set[str] = set()
        exact_members: set[tuple[str, str]] = set()
        cache: dict[
            str,
            tuple[PublishedDataPackage, dict[str, ExactTransformPayload]],
        ] = {}
        result: list[IfcQueryExactInputRef] = []
        for ordinal, binding in enumerate(bindings, 1):
            if not isinstance(binding, IfcQueryInputBinding):
                raise IfcQueryApplicationError(
                    "INVALID_ARGUMENT",
                    "An IFC query input binding is invalid",
                    phase="preflight",
                )
            if binding.input_name in names:
                raise IfcQueryApplicationError(
                    "INVALID_ARGUMENT",
                    "IFC query input_name values must be unique",
                    phase="preflight",
                )
            names.add(binding.input_name)
            cached = cache.get(binding.package_alias)
            if cached is None:
                package = await self._payloads.package_by_alias(scope, binding.package_alias)
                payload_map = dict(self._payloads.transform_payloads(package))
                cache[binding.package_alias] = package, payload_map
            else:
                package, payload_map = cached
            payload = payload_map.get(binding.payload_name)
            if payload is None:
                raise IfcQueryApplicationError(
                    "PAYLOAD_NOT_FOUND",
                    "The requested exact Package Payload was not found",
                    phase="preflight",
                )
            exact = (package.package.package_id, payload.member.member_id)
            if exact in exact_members:
                raise IfcQueryApplicationError(
                    "INVALID_ARGUMENT",
                    "The same exact Package Payload cannot be bound twice",
                    phase="preflight",
                )
            exact_members.add(exact)
            result.append(
                IfcQueryExactInputRef(
                    ordinal=ordinal,
                    input_name=binding.input_name,
                    package_id=package.package.package_id,
                    payload_member_id=payload.member.member_id,
                    schema_member_id=(
                        payload.schema_member.member_id
                        if isinstance(payload, ExactJsonPayload)
                        else None
                    ),
                )
            )
        return tuple(result)

    async def _prepare_and_execute(
        self, scope: InvocationScope, *, exact: ExactIfcRevision, source_code: str,
        exact_inputs: Sequence[IfcQueryExactInputRef], repair_parent_attempt_id: str | None,
        request_identity: str, request_digest: str, page_size: int, deadline: float,
    ) -> IfcQueryPage:
        async with asyncio.timeout_at(deadline - IFC_QUERY_EXECUTION.cleanup_seconds):
            return await self._prepare_and_execute_scoped(
                scope, exact=exact, source_code=source_code, exact_inputs=exact_inputs,
                repair_parent_attempt_id=repair_parent_attempt_id, request_identity=request_identity,
                request_digest=request_digest,
                page_size=page_size, deadline=deadline,
            )

    async def _prepare_and_execute_scoped(
        self,
        scope: InvocationScope,
        *,
        exact: ExactIfcRevision,
        source_code: str,
        exact_inputs: Sequence[IfcQueryExactInputRef],
        deadline: float,
        repair_parent_attempt_id: str | None,
        request_identity: str,
        request_digest: str,
        page_size: int,
    ) -> IfcQueryPage:
        source_bytes = source_code.encode("utf-8")
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        attempt_id = _attempt_id(scope, request_identity)
        try:
            source_record = await self._artifacts.upload_bytes(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename="ifc_query.py",
                content_type=_PYTHON_MEDIA_TYPE,
                content=source_bytes,
                idempotency_key=f"ifc-query-source:{attempt_id}",
                kind="ifc_query_source",
                run_id=scope.run_id,
                version_metadata={
                    "source_sha256": source_sha256,
                },
            )
            prepared = await self._repository.prepare_ifc_query_attempt(
                scope,
                IfcQueryAttemptPreparation(
                    attempt_id=attempt_id,
                    run_id=scope.run_id,
                    ifc_model_id=exact.model_id,
                    ifc_revision_id=exact.revision_id,
                    source_artifact=_artifact_ref(source_record),
                    inputs=tuple(exact_inputs),
                    repair_parent_attempt_id=repair_parent_attempt_id,
                    request_identity=request_identity,
                    request_digest=request_digest,
                    budget=IfcQueryBudgetSummary(max_result_bytes=_MAX_RESULT_BYTES),
                    created_at=self._now(),
                ),
            )
        except ConflictError as exc:
            previous = await self._repository.get_ifc_query_attempt(scope, attempt_id=attempt_id)
            if previous is not None:
                return await self._resume_attempt(scope, previous, request_digest, page_size)
            raise IfcQueryApplicationError(
                "IFC_QUERY_IDEMPOTENCY_CONFLICT",
                "The IFC query request identity was reused with different exact inputs",
                phase="preflight",
            ) from exc
        except NotFoundError as exc:
            raise IfcQueryApplicationError(
                "IFC_QUERY_SCOPE_NOT_FOUND",
                "The IFC query Scope or one of its exact inputs is unavailable",
                phase="preflight",
            ) from exc
        attempt = prepared.attempt
        if not prepared.created:
            return await self._resume_attempt(scope, attempt, request_digest, page_size)
        return await self._execute_prepared(scope, attempt, page_size, deadline=deadline)

    async def _resume_attempt(
        self, scope: InvocationScope, attempt: IfcQueryAttemptRecord,
        request_digest: str, page_size: int,
    ) -> IfcQueryPage:
        if attempt.request_digest != request_digest:
            raise IfcQueryApplicationError(
                "IFC_QUERY_IDEMPOTENCY_CONFLICT",
                "The IFC query request identity was reused with different arguments",
                phase="preflight",
                attempt_alias=self._attempt_alias(scope, attempt),
            )
        if attempt.status is IfcQueryAttemptStatus.SUCCEEDED:
            return await self._page_from_attempt(scope, attempt, 0, page_size)
        if attempt.status is IfcQueryAttemptStatus.FAILED:
            self._raise_persisted_failure(scope, attempt)
        raise IfcQueryApplicationError(
            "IFC_QUERY_ATTEMPT_IN_PROGRESS",
            "The identical IFC query attempt is still being reconciled",
            phase="execution",
            retryable=True,
            attempt_alias=self._attempt_alias(scope, attempt),
        )

    async def _execute_prepared(
        self,
        scope: InvocationScope,
        attempt: IfcQueryAttemptRecord,
        page_size: int,
        *, deadline: float,
    ) -> IfcQueryPage:
        materialized: dict[str, Path] = {}
        try:
            source = await self._read_exact_artifact(
                scope,
                attempt.source_artifact,
                expected_content_type=_PYTHON_MEDIA_TYPE,
            )
            try:
                source_code = source.content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IfcQueryApplicationError(
                    "IFC_QUERY_SOURCE_INVALID",
                    "The exact IFC query source is not valid UTF-8",
                    phase="preflight",
                ) from exc
            exact = await self._revisions.pin_exact(
                _ifc_scope(scope),
                revision_id=attempt.ifc_revision_id,
            )
            if exact.model_id != attempt.ifc_model_id:
                raise IfcQueryApplicationError(
                    "IFC_QUERY_IFC_INTEGRITY_FAILED",
                    "The persisted IFC query revision binding failed integrity validation",
                    phase="preflight",
                )
            async with self._worker.execution_scope(IFC_QUERY_EXECUTION, deadline=deadline):
                verified = await self._revisions.read_exact(exact)
                resolved = await self._resolve_exact_inputs(scope, attempt.inputs)
                materialized = await self._payloads.materialize_members(
                    exact_payload_members(resolved),
                    check_budget=lambda: None,
                    max_total_bytes=_MAX_INPUT_STORED_BYTES,
                )
                worker_inputs = await asyncio.to_thread(
                    exact_payload_arrow_inputs,
                    resolved,
                    materialized,
                )
                execution = await self._worker.execute_query(
                    IfcQueryWorkerRequest(
                        code=source_code,
                        ifc_bytes=verified.content,
                        expected_ifc_sha256=exact.artifact.sha256,
                        max_result_bytes=attempt.budget.max_result_bytes,
                    ),
                    worker_inputs,
                    deadline=deadline,
                )
            _validate_worker_result(execution, expected_ifc_sha256=exact.artifact.sha256)
            _validate_pageability(execution.result)
            document = canonical_json(
                {
                    "ifc_sha256": execution.ifc_sha256,
                    "result": execution.result,
                    "result_bytes": execution.result_bytes,
                }
            ).encode("utf-8")
            result_record = await self._artifacts.upload_bytes(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename="ifc_query_result.json",
                content_type=_JSON_MEDIA_TYPE,
                content=document,
                idempotency_key=f"ifc-query-result:{attempt.attempt_id}",
                kind="ifc_query_result",
                run_id=attempt.run_id,
                version_metadata={
                    "ifc_revision_id": attempt.ifc_revision_id,
                    "ifc_sha256": execution.ifc_sha256,
                },
            )
            terminal = IfcQueryAttemptTerminal(
                status=IfcQueryAttemptStatus.SUCCEEDED,
                result_artifact=_artifact_ref(result_record),
                finished_at=self._now(),
            )
            completed = await self._complete_or_reconcile(scope, attempt, terminal)
            if completed.status is IfcQueryAttemptStatus.FAILED:
                self._raise_persisted_failure(scope, completed)
            return await self._page_from_attempt(scope, completed, 0, page_size)
        except asyncio.CancelledError as exc:
            worker_failure = getattr(exc, "worker_failure", None)

            async def settle_cancelled() -> None:
                async with asyncio.timeout(max(0.0, min(IFC_QUERY_EXECUTION.terminal_seconds, deadline - monotonic()))):
                    await self._persist_failure(
                        scope, attempt,
                        IfcQueryApplicationError(
                            "IFC_QUERY_CANCELLED",
                            "The query was cancelled; read the saved attempt before submitting a new query",
                            phase="execution",
                        ),
                        execution_started=bool(getattr(worker_failure, "execution_started", False)),
                    )

            settlement = asyncio.create_task(settle_cancelled())
            failure = await drain_shielded_task(settlement)
            if failure is not None:
                setattr(exc, "query_settlement_failure", failure)
            raise
        except IfcQueryApplicationError as exc:
            if exc.code == "IFC_QUERY_RECONCILIATION_REQUIRED":
                raise
            await self._persist_failure(scope, attempt, exc, execution_started=False)
        except IfcQueryWorkerError as exc:
            await self._persist_failure(
                scope,
                attempt,
                _worker_error(exc),
                execution_started=exc.execution_started,
            )
        except IfcRevisionRuntimeError as exc:
            await self._persist_failure(
                scope, attempt,
                IfcQueryApplicationError(
                    "IFC_QUERY_RUNTIME_UNAVAILABLE",
                    "The exact IFC inspection could not finish in the supervised runtime",
                    phase="preflight", retryable=exc.retryable, diagnostic=exc.diagnostic,
                ),
                execution_started=exc.execution_started,
            )
        except ExactPayloadResolutionError as exc:
            await self._persist_failure(
                scope,
                attempt,
                _payload_error(exc),
                execution_started=False,
            )
        except (IfcModelNotInitializedError, IfcModelFrozenError, IfcModelIntegrityError) as exc:
            await self._persist_failure(
                scope,
                attempt,
                _revision_error(exc),
                execution_started=False,
            )
        except Exception:
            await self._persist_failure(
                scope,
                attempt,
                IfcQueryApplicationError(
                    "IFC_QUERY_SERVICE_UNAVAILABLE",
                    "The IFC query could not publish a terminal result",
                    phase="output_validation",
                    retryable=True,
                ),
                execution_started=False,
            )
        finally:
            self._payloads.remove_paths(materialized.values())
        raise AssertionError("IFC query terminal path did not return or raise")

    async def _resolve_exact_inputs(
        self,
        scope: InvocationScope,
        references: Sequence[IfcQueryExactInputRef],
    ) -> tuple[ExactPayloadInput, ...]:
        result: list[ExactPayloadInput] = []
        for reference in references:
            _package, payload = await self._payloads.payload_by_exact_member(
                scope,
                package_id=reference.package_id,
                payload_member_id=reference.payload_member_id,
                schema_member_id=reference.schema_member_id,
            )
            result.append(ExactPayloadInput(input_name=reference.input_name, payload=payload))
        return tuple(result)

    async def _persist_failure(
        self,
        scope: InvocationScope,
        attempt: IfcQueryAttemptRecord,
        error: IfcQueryApplicationError,
        *,
        execution_started: bool,
    ) -> NoReturn:
        terminal = IfcQueryAttemptTerminal(
            status=IfcQueryAttemptStatus.FAILED,
            diagnostics=(
                IfcQueryDiagnostic(
                    code=error.code,
                    message=_bounded_message(str(error)),
                    phase=error.phase,
                    execution_started=execution_started,
                    retryable=error.retryable,
                    detail=error.diagnostic,
                ),
            ),
            finished_at=self._now(),
        )
        completed = await self._complete_or_reconcile(scope, attempt, terminal)
        if completed.status is IfcQueryAttemptStatus.SUCCEEDED:
            raise IfcQueryApplicationError(
                "IFC_QUERY_RESULT_RECONCILIATION_REQUIRED",
                "The query completed while failure publication lost its response",
                phase="output_validation",
                retryable=True,
                attempt_alias=self._attempt_alias(scope, completed),
            )
        self._raise_persisted_failure(scope, completed)

    async def _complete_or_reconcile(
        self,
        scope: InvocationScope,
        attempt: IfcQueryAttemptRecord,
        terminal: IfcQueryAttemptTerminal,
    ) -> IfcQueryAttemptRecord:
        try:
            return await self._repository.commit_ifc_query_attempt_terminal(
                scope,
                attempt_id=attempt.attempt_id,
                terminal=terminal,
            )
        except Exception:
            recovered = await self._repository.get_ifc_query_attempt(
                scope,
                attempt_id=attempt.attempt_id,
            )
            if recovered is not None and recovered.status is not IfcQueryAttemptStatus.PREPARED:
                return recovered
            raise IfcQueryApplicationError(
                "IFC_QUERY_RECONCILIATION_REQUIRED",
                "The IFC query terminal state is still being reconciled",
                phase="output_validation",
                retryable=True,
                attempt_alias=self._attempt_alias(scope, recovered or attempt),
            ) from None

    async def _page_from_attempt(
        self,
        scope: InvocationScope,
        attempt: IfcQueryAttemptRecord,
        offset: int,
        page_size: int,
        *,
        expires_at: int | None = None,
    ) -> IfcQueryPage:
        if attempt.status is not IfcQueryAttemptStatus.SUCCEEDED:
            raise IfcQueryApplicationError(
                "IFC_QUERY_RESULT_MISSING", "The IFC query attempt has no successful result"
            )
        if attempt.result_artifact is None:
            raise IfcQueryApplicationError(
                "IFC_QUERY_RESULT_INTEGRITY_FAILED",
                "The successful attempt has no exact result Artifact",
            )
        downloaded = await self._read_exact_artifact(
            scope,
            attempt.result_artifact,
            expected_content_type=_JSON_MEDIA_TYPE,
        )
        document = _decode_result_document(downloaded.content, attempt.budget.max_result_bytes)
        raw_result = document["result"]
        results = raw_result if isinstance(raw_result, list) else [raw_result]
        if offset > len(results):
            raise IfcQueryApplicationError(
                "IFC_QUERY_CURSOR_INVALID",
                "The IFC query cursor offset exceeds the exact persisted result",
            )
        page_results, end = _bounded_page(results, offset, page_size)
        next_cursor = None
        if end < len(results):
            if expires_at is None:
                expires_at = int(self._now().timestamp()) + _CURSOR_TTL_SECONDS
            next_cursor = self._cursor_codec.encode(
                {
                    "domain": _PAGE_CURSOR_DOMAIN,
                    "scope": _scope_digest(scope),
                    "attempt_id": attempt.attempt_id,
                    "result": _artifact_claim(attempt.result_artifact),
                    "offset": end,
                    "page_size": page_size,
                    "expires_at": expires_at,
                }
            )
        return IfcQueryPage(
            attempt_alias=self._attempt_alias(scope, attempt),
            parent_attempt_alias=(
                self._attempt_alias_for_id(scope, attempt.repair_parent_attempt_id)
                if attempt.repair_parent_attempt_id is not None
                else None
            ),
            source_sha256=attempt.source_artifact.sha256,
            result_bytes=document["result_bytes"],
            total_count=len(results),
            returned_count=len(page_results),
            results=tuple(page_results),
            next_cursor=next_cursor,
        )

    async def _read_exact_artifact(
        self,
        scope: InvocationScope,
        reference: IfcQueryArtifactRef,
        *,
        expected_content_type: str,
    ) -> ArtifactDownload:
        try:
            return await self._exact_artifacts.read(
                scope,
                reference,
                expected_content_type=expected_content_type,
            )
        except ExactArtifactReadError as exc:
            raise IfcQueryApplicationError(
                "IFC_QUERY_ARTIFACT_INTEGRITY_FAILED",
                "The exact IFC query Artifact is unavailable",
                phase="read",
            ) from exc

    def _attempt_alias(self, scope: InvocationScope, attempt: IfcQueryAttemptRecord) -> str:
        return self._attempt_alias_for_id(scope, attempt.attempt_id)

    def _attempt_alias_for_id(self, scope: InvocationScope, attempt_id: str) -> str:
        return self._cursor_codec.encode(
            {
                "domain": _ATTEMPT_ALIAS_DOMAIN,
                "scope": _scope_digest(scope),
                "attempt_id": attempt_id,
            }
        )

    def _raise_persisted_failure(
        self,
        scope: InvocationScope,
        attempt: IfcQueryAttemptRecord,
    ) -> NoReturn:
        diagnostic = attempt.diagnostics[0]
        raise IfcQueryApplicationError(
            diagnostic.code,
            diagnostic.message,
            phase=diagnostic.phase,
            retryable=diagnostic.retryable,
            attempt_alias=self._attempt_alias(scope, attempt),
            diagnostic=diagnostic.detail,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("IFC query clock must return an aware datetime")
        return value


def _artifact_ref(record) -> IfcQueryArtifactRef:
    return IfcQueryArtifactRef(
        artifact_id=record.artifact_id,
        version_id=record.version_id,
        sha256=record.sha256,
    )


def _artifact_claim(reference: IfcQueryArtifactRef) -> dict[str, str]:
    return {
        "artifact_id": reference.artifact_id,
        "version_id": reference.version_id,
        "sha256": reference.sha256,
    }


def _validate_result_claim(value: object, attempt: IfcQueryAttemptRecord) -> None:
    if attempt.result_artifact is None or value != _artifact_claim(attempt.result_artifact):
        raise IfcQueryApplicationError(
            "IFC_QUERY_CURSOR_INVALID",
            "The IFC query cursor does not bind the exact result Artifact",
        )


def _decode_result_document(content: bytes, max_result_bytes: int) -> dict:
    try:
        value = json.loads(content.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "ifc_sha256",
            "result",
            "result_bytes",
        }:
            raise ValueError("unexpected result document shape")
        if canonical_json(value).encode("utf-8") != content:
            raise ValueError("result document is not canonical")
        ifc_sha256 = value["ifc_sha256"]
        result_bytes = value["result_bytes"]
        result_json = canonical_json(value["result"]).encode("utf-8")
        if (
            not isinstance(ifc_sha256, str)
            or len(ifc_sha256) != 64
            or any(character not in "0123456789abcdef" for character in ifc_sha256)
            or not isinstance(result_bytes, int)
            or isinstance(result_bytes, bool)
            or result_bytes != len(result_json)
            or result_bytes > max_result_bytes
        ):
            raise ValueError("result metadata is invalid")
        _validate_pageability(value["result"])
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise IfcQueryApplicationError(
            "IFC_QUERY_RESULT_INTEGRITY_FAILED",
            "The exact IFC query result Artifact failed integrity validation",
            phase="read",
        ) from exc


def _bounded_page(results: list, offset: int, page_size: int) -> tuple[list, int]:
    selected: list = []
    for item in results[offset : offset + page_size]:
        candidate = [*selected, item]
        if len(canonical_json(candidate).encode("utf-8")) > _MAX_PAGE_BYTES:
            break
        selected = candidate
    if offset < len(results) and not selected:
        raise IfcQueryApplicationError(
            "IFC_QUERY_RESULT_ITEM_TOO_LARGE",
            "One IFC query result item exceeds the response byte budget",
            phase="output_validation",
        )
    return selected, offset + len(selected)


def _validate_pageability(result: object) -> None:
    results = result if isinstance(result, list) else [result]
    for item in results:
        if len(canonical_json([item]).encode("utf-8")) > _MAX_PAGE_BYTES:
            raise IfcQueryApplicationError(
                "IFC_QUERY_RESULT_ITEM_TOO_LARGE",
                "One IFC query result item exceeds the response byte budget",
                phase="output_validation",
            )


def _validate_worker_result(execution, *, expected_ifc_sha256: str) -> None:
    try:
        encoded = execution.result_json.encode("utf-8")
        if (
            execution.ifc_sha256 != expected_ifc_sha256
            or execution.result_bytes != len(encoded)
            or execution.result_bytes > _MAX_RESULT_BYTES
            or canonical_json(execution.result) != execution.result_json
        ):
            raise ValueError("inconsistent worker result")
    except (AttributeError, TypeError, UnicodeError, ValueError) as exc:
        raise IfcQueryApplicationError(
            "IFC_QUERY_RESULT_INVALID",
            "The IFC query Worker returned an inconsistent result",
            phase="output_validation",
        ) from exc


def _ifc_scope(scope: InvocationScope) -> IfcRunIdentity:
    task_id = scope.identity.task_id
    if task_id is None:
        raise IfcQueryApplicationError(
            "IFC_QUERY_SCOPE_INVALID",
            "The IFC query invocation has no Task",
            phase="preflight",
        )
    return IfcRunIdentity(
        owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id,
        task_id=task_id,
        run_id=scope.run_id,
    )


def _scope_digest(scope: InvocationScope) -> str:
    return sha256_digest(
        {
            "domain": "ifc_query_scope",
            "owner_id": scope.owner_id,
            "tenant_id": scope.identity.tenant_id,
            "task_id": scope.identity.task_id,
            "run_id": scope.run_id,
        }
    )


def _validate_source(source: str) -> None:
    if not isinstance(source, str) or not source.strip() or "\x00" in source:
        raise IfcQueryApplicationError(
            "IFC_QUERY_SOURCE_INVALID",
            "IFC query source must be non-blank UTF-8 text without NUL",
            phase="preflight",
        )
    if len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise IfcQueryApplicationError(
            "IFC_QUERY_SOURCE_TOO_LARGE",
            "IFC query source exceeds 65,536 UTF-8 bytes",
            phase="preflight",
        )


def _validate_request_identity(value: str) -> None:
    if not isinstance(value, str) or _REQUEST_IDENTITY.fullmatch(value) is None:
        raise IfcQueryApplicationError(
            "INVALID_ARGUMENT",
            "The server IFC query request identity is invalid",
            phase="preflight",
        )


def _attempt_id(scope: InvocationScope, request_identity: str) -> str:
    return stable_internal_identity_id(
        "ifcqa_",
        "ifc_query_attempt",
        scope.identity.tenant_id,
        scope.identity.task_id or "",
        scope.run_id,
        request_identity,
    )


def _validate_page_size(value: int, *, cursor: bool = False) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _MAX_PAGE_SIZE:
        raise IfcQueryApplicationError(
            "IFC_QUERY_CURSOR_INVALID" if cursor else "INVALID_ARGUMENT",
            "IFC query page_size must be an integer between 1 and 200",
            phase="read" if cursor else "preflight",
        )


def _validate_terminal_attempt_list(
    *,
    statuses: tuple[IfcQueryAttemptStatus, ...],
    before_created_at: datetime | None,
    before_attempt_id: str | None,
    limit: int,
) -> None:
    terminal_statuses = {
        IfcQueryAttemptStatus.SUCCEEDED,
        IfcQueryAttemptStatus.FAILED,
    }
    if (
        not isinstance(statuses, tuple)
        or not statuses
        or any(
            not isinstance(status, IfcQueryAttemptStatus) or status not in terminal_statuses
            for status in statuses
        )
        or len({status.value for status in statuses}) != len(statuses)
    ):
        raise IfcQueryApplicationError(
            "INVALID_ARGUMENT",
            "IFC query attempt statuses must be a non-empty unique tuple of terminal statuses",
        )
    if (before_created_at is None) != (before_attempt_id is None):
        raise IfcQueryApplicationError(
            "INVALID_ARGUMENT",
            "IFC query attempt keyset fields must be provided together",
        )
    if before_created_at is not None and (
        not isinstance(before_created_at, datetime)
        or before_created_at.tzinfo is None
        or before_created_at.utcoffset() is None
    ):
        raise IfcQueryApplicationError(
            "INVALID_ARGUMENT",
            "IFC query attempt keyset timestamp must be timezone-aware",
        )
    if before_attempt_id is not None and (
        not isinstance(before_attempt_id, str)
        or _REQUEST_IDENTITY.fullmatch(before_attempt_id) is None
        or len(before_attempt_id) > 80
    ):
        raise IfcQueryApplicationError(
            "INVALID_ARGUMENT",
            "IFC query attempt keyset id is invalid",
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 201:
        raise IfcQueryApplicationError(
            "INVALID_ARGUMENT",
            "IFC query attempt list limit must be an integer from 1 through 201",
        )


def _revision_error(
    error: Exception,
    *,
    attempt_alias: str | None = None,
) -> IfcQueryApplicationError:
    if isinstance(error, IfcModelNotInitializedError):
        return IfcQueryApplicationError(
            "IFC_MODEL_NOT_INITIALIZED",
            "The selected Run has no visible exact IFC revision",
            phase="preflight",
            attempt_alias=attempt_alias,
        )
    return IfcQueryApplicationError(
        "IFC_QUERY_IFC_INTEGRITY_FAILED",
        "The exact IFC revision failed integrity validation",
        phase="preflight",
        attempt_alias=attempt_alias,
    )


def _payload_error(error: ExactPayloadResolutionError) -> IfcQueryApplicationError:
    code = (
        "IFC_QUERY_INPUT_BUDGET_EXCEEDED"
        if error.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED"
        else error.code
    )
    return IfcQueryApplicationError(code, str(error), phase="preflight")


def _worker_error(error: IfcQueryWorkerError) -> IfcQueryApplicationError:
    phase: Literal["read", "preflight", "execution", "output_validation"]
    phase = {
        "code_validation": "preflight",
        "input_validation": "preflight",
        "execution": "execution",
        "result_validation": "output_validation",
        "runtime": "execution",
    }.get(error.phase, "execution")
    return IfcQueryApplicationError(
        error.code,
        error.message,
        phase=phase,
        retryable=bool(getattr(error, "retryable", False)),
        diagnostic=_safe_worker_diagnostic(error.diagnostic),
    )


def _safe_worker_diagnostic(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    allowed = (
        "json_path",
        "value_type",
        "reason",
        "exception_type",
        "message",
        "line",
        "column",
        "end_line",
        "end_column",
        "source_line",
        "caret",
        "code_sha256",
        "function",
    )
    safe: dict[str, object] = {}
    for key in allowed:
        if key not in value:
            continue
        item = value.get(key)
        if item is None or isinstance(item, (bool, int, float)):
            candidate = item
        elif isinstance(item, str):
            candidate = re.sub(
                r"/(?:Users|home|private|var|tmp)/[^\s:'\"]+",
                "<host-path>",
                item,
            )
            candidate = candidate.encode("utf-8")[:1_000].decode("utf-8", errors="ignore")
        else:
            continue
        proposed = {**safe, key: candidate}
        if len(canonical_json(proposed).encode("utf-8")) <= 1_800:
            safe = proposed
    return safe or None


def _patch_error(failure: str) -> tuple[str, bool]:
    if failure == "budget_exceeded":
        return "IFC_QUERY_REPAIR_BUDGET_EXCEEDED", False
    if failure == "engine_failed":
        return "IFC_QUERY_REPAIR_UNAVAILABLE", True
    return "IFC_QUERY_REPAIR_INVALID", False


def _bounded_message(message: str) -> str:
    value = (message or "IFC query failed").strip() or "IFC query failed"
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_DIAGNOSTIC_BYTES:
        return value
    suffix = "..."
    budget = _MAX_DIAGNOSTIC_BYTES - len(suffix.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


__all__ = [
    "IfcQueryApplicationError",
    "IfcQueryInputBinding",
    "IfcQueryService",
]
