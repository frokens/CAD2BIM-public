"""Shared durable lifecycle for temporary SQL and Python data queries."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.errors import (
    ConflictError as ArtifactConflictError,
    NotFoundError as ArtifactNotFoundError,
    ValidationError as ArtifactValidationError,
)
from CAD2BIM_backend.application.exact_artifact_reader import (
    ExactArtifactReadError,
    ExactArtifactReader,
)
from CAD2BIM_backend.application.text_source_view import TextSourceViewError, TextSourceViewService
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryArtifactRef,
    DataQueryAttemptPreparation,
    DataQueryAttemptRecord,
    DataQueryAttemptStatus,
    DataQueryAttemptTerminal,
    DataQueryDiagnostic,
    DataQueryEngine,
    DataQueryExactInputRef,
    DataQueryPrepared,
    DataQueryResultSummary,
)
from CAD2BIM_backend.domain.control_plane import (
    ConflictError as DataQueryConflictError,
    ResourceNotFoundError as DataQueryResourceNotFoundError,
)
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.invocation import InvocationScope, invocation_scope_digest
from CAD2BIM_backend.domain.text_source import SourceEdit, TextSourceView, TextSourceViewResult
from CAD2BIM_backend.ports.data_query_attempts import DataQueryAttemptRepository
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError, OpaqueCursorCodecPort
from CAD2BIM_backend.ports.source_patch import SourcePatchPort, SourcePatchPortError


_ALIAS_DOMAIN = "data_query_attempt_alias"
_SOURCE_MEDIA_TYPES = {
    DataQueryEngine.DUCKDB_SQL: "application/sql",
    DataQueryEngine.PYTHON_ARROW: "text/x-python",
}
_SOURCE_FILENAMES = {
    DataQueryEngine.DUCKDB_SQL: "data_query.sql",
    DataQueryEngine.PYTHON_ARROW: "data_query.py",
}
_PUBLICATION_RECONCILE_RESERVE_SECONDS = 0.25


@dataclass(slots=True)
class _ExecutionClaim:
    lock: asyncio.Lock
    users: int = 0


class DataQueryAttemptApplicationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "read",
        retryable: bool = False,
        attempt_alias: str | None = None,
    ) -> None:
        super().__init__(message[:2000])
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.attempt_alias = attempt_alias


class DataQueryAttemptService:
    """Own Attempt persistence while leaving execution to the engine services."""

    def __init__(
        self,
        repository: DataQueryAttemptRepository,
        artifacts: ArtifactService,
        cursor_codec: OpaqueCursorCodecPort,
        source_patches: SourcePatchPort,
        snapshots=None,
        *,
        text_views: TextSourceViewService | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._exact_artifacts = ExactArtifactReader(artifacts)
        self._cursor_codec = cursor_codec
        self._source_patches = source_patches
        self._snapshots = snapshots
        self._text_views = text_views or TextSourceViewService()
        self._execution_claims: dict[str, _ExecutionClaim] = {}

    async def acquire_execution(
        self,
        scope: InvocationScope,
        attempt_id: str,
        *,
        wait_timeout_seconds: float,
    ) -> DataQueryAttemptRecord:
        """Serialize one Attempt in this process, then refresh its durable state."""

        claim = self._execution_claims.setdefault(attempt_id, _ExecutionClaim(lock=asyncio.Lock()))
        claim.users += 1
        try:
            async with asyncio.timeout(max(0.0, wait_timeout_seconds)):
                await claim.lock.acquire()
        except TimeoutError as exc:
            self._drop_claim_user(attempt_id, claim)
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_UNAVAILABLE",
                "Another invocation is still executing this data query Attempt",
                phase="preflight",
                retryable=True,
            ) from exc
        except BaseException:
            self._drop_claim_user(attempt_id, claim)
            raise
        try:
            attempt = await self._repository.get_data_query_attempt(scope, attempt_id=attempt_id)
            if attempt is None:
                raise DataQueryAttemptApplicationError(
                    "RESOURCE_NOT_FOUND", "Data query Attempt is unavailable"
                )
            return attempt
        except asyncio.CancelledError:
            self._release_claim(attempt_id, claim)
            raise
        except Exception as exc:
            self._release_claim(attempt_id, claim)
            if isinstance(exc, DataQueryAttemptApplicationError):
                raise
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_UNAVAILABLE",
                "Data query Attempt state is unavailable",
                phase="preflight",
                retryable=True,
            ) from exc

    def release_execution(self, attempt_id: str) -> None:
        claim = self._execution_claims.get(attempt_id)
        if claim is not None and claim.lock.locked():
            self._release_claim(attempt_id, claim)

    def _release_claim(self, attempt_id: str, claim: _ExecutionClaim) -> None:
        claim.lock.release()
        self._drop_claim_user(attempt_id, claim)

    def _drop_claim_user(self, attempt_id: str, claim: _ExecutionClaim) -> None:
        claim.users -= 1
        if claim.users == 0 and self._execution_claims.get(attempt_id) is claim:
            self._execution_claims.pop(attempt_id, None)

    async def prepare(
        self,
        scope: InvocationScope,
        *,
        engine: DataQueryEngine,
        source_code: str,
        inputs: tuple[DataQueryExactInputRef, ...],
        parameters: tuple[dict, ...],
        inline_tables: tuple[dict, ...],
        page_size: int,
        request_identity: str,
        repair_parent_attempt_id: str | None = None,
    ) -> DataQueryPrepared:
        if not isinstance(source_code, str) or not source_code.strip() or "\x00" in source_code:
            raise DataQueryAttemptApplicationError(
                "INVALID_ARGUMENT",
                "Data query source must be non-blank UTF-8 text without NUL",
                phase="preflight",
            )
        source_bytes = source_code.encode("utf-8")
        if len(source_bytes) > 65_536:
            raise DataQueryAttemptApplicationError(
                "INVALID_ARGUMENT",
                "Data query source exceeds 65,536 UTF-8 bytes",
                phase="preflight",
            )
        attempt_id = stable_internal_identity_id(
            "dqa_",
            "cad2bim.data_query_attempt",
            scope.identity.tenant_id,
            scope.identity.task_id or "",
            scope.run_id,
            engine.value,
            request_identity,
        )
        request_digest = sha256_digest(
            {
                "domain": "data_query_attempt_request",
                "engine": engine,
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "inputs": inputs,
                "parameters": parameters,
                "inline_tables": inline_tables,
                "page_size": page_size,
                "repair_parent_attempt_id": repair_parent_attempt_id,
            }
        )
        try:
            source = await self._artifacts.upload_bytes(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename=_SOURCE_FILENAMES[engine],
                content_type=_SOURCE_MEDIA_TYPES[engine],
                content=source_bytes,
                idempotency_key=f"data-query-source:{attempt_id}",
                kind="data_query_source",
                run_id=scope.run_id,
                version_metadata={"engine": engine.value},
            )
        except ArtifactValidationError as exc:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_UNAVAILABLE",
                "Data query source persistence is unavailable",
                phase="preflight",
                retryable=True,
            ) from exc
        except ArtifactNotFoundError as exc:
            raise DataQueryAttemptApplicationError(
                "RESOURCE_NOT_FOUND", "Data query Run is unavailable", phase="preflight"
            ) from exc
        except ArtifactConflictError as exc:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_REQUEST_CONFLICT",
                "The server invocation identity was reused with different data query source bytes",
                phase="preflight",
                retryable=False,
            ) from exc
        except Exception as exc:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_UNAVAILABLE",
                "Data query source persistence is unavailable",
                phase="preflight",
                retryable=True,
            ) from exc
        preparation = DataQueryAttemptPreparation(
            attempt_id=attempt_id,
            run_id=scope.run_id,
            engine=engine,
            source_artifact=DataQueryArtifactRef(
                artifact_id=source.artifact_id,
                version_id=source.version_id,
                sha256=source.sha256,
            ),
            inputs=inputs,
            parameters=parameters,
            inline_tables=inline_tables,
            page_size=page_size,
            repair_parent_attempt_id=repair_parent_attempt_id,
            request_identity=request_identity,
            request_digest=request_digest,
            created_at=datetime.now(UTC),
        )
        try:
            return await self._repository.prepare_data_query_attempt(scope, preparation)
        except DataQueryConflictError as exc:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_REQUEST_CONFLICT",
                "The server invocation identity was reused with different data query facts",
                phase="preflight",
            ) from exc
        except DataQueryResourceNotFoundError as exc:
            raise DataQueryAttemptApplicationError(
                "RESOURCE_NOT_FOUND",
                "An exact data query input is unavailable in this Run",
                phase="preflight",
            ) from exc
        except Exception as exc:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_UNAVAILABLE",
                "The data query attempt could not be persisted",
                phase="preflight",
                retryable=True,
            ) from exc

    async def repair_source(
        self,
        scope: InvocationScope,
        *,
        engine: DataQueryEngine,
        parent_attempt_alias: str,

        edits: Sequence[SourceEdit],
    ) -> tuple[DataQueryAttemptRecord, str]:
        parent = await self.resolve_attempt_alias(scope, parent_attempt_alias)
        if parent.engine is not engine:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_ENGINE_MISMATCH",
                "The parent attempt belongs to the other data query engine",
                phase="preflight",
            )
        if parent.status is DataQueryAttemptStatus.PREPARED:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_PARENT_NOT_TERMINAL",
                "The parent data query attempt is not terminal",
                phase="preflight",
            )
        source = await self._read_exact_source(scope, parent)
        try:
            patched = self._source_patches.apply_text(source, edits, maximum_source_bytes=65_536)
        except SourcePatchPortError as exc:
            code = {
                "match_not_found": "DATA_QUERY_REPAIR_MATCH_NOT_FOUND",
                "match_not_unique": "DATA_QUERY_REPAIR_MATCH_NOT_UNIQUE",
                "overlap": "DATA_QUERY_REPAIR_OVERLAP",
                "budget_exceeded": "DATA_QUERY_REPAIR_BUDGET_EXCEEDED",
            }.get(exc.failure, "DATA_QUERY_REPAIR_FAILED")
            raise DataQueryAttemptApplicationError(
                code,
                str(exc),
                phase="preflight",
                attempt_alias=parent_attempt_alias,
            ) from None
        return parent, patched

    async def succeed(
        self,
        scope: InvocationScope,
        attempt: DataQueryAttemptRecord,
        summary: DataQueryResultSummary,
        *,
        publication_timeout_seconds: float = 5.0,
    ) -> DataQueryAttemptRecord:
        return await self._complete(
            scope,
            attempt,
            DataQueryAttemptTerminal(
                status=DataQueryAttemptStatus.SUCCEEDED,
                result_summary=summary,
                finished_at=datetime.now(UTC),
            ),
            publication_timeout_seconds=publication_timeout_seconds,
        )

    async def fail(
        self,
        scope: InvocationScope,
        attempt: DataQueryAttemptRecord,
        *,
        code: str,
        message: str,
        phase: str,
        retryable: bool,
        publication_timeout_seconds: float = 5.0,
    ) -> None:
        terminal = DataQueryAttemptTerminal(
            status=DataQueryAttemptStatus.FAILED,
            diagnostic=DataQueryDiagnostic(
                code=code,
                message=(message.strip() or "Data query failed")[:2000],
                phase=phase,
                retryable=retryable,
            ),
            finished_at=datetime.now(UTC),
        )
        completed = await self._complete(
            scope,
            attempt,
            terminal,
            publication_timeout_seconds=publication_timeout_seconds,
        )
        diagnostic = completed.diagnostic or terminal.diagnostic
        raise DataQueryAttemptApplicationError(
            diagnostic.code,
            diagnostic.message,
            phase=diagnostic.phase,
            retryable=diagnostic.retryable,
            attempt_alias=self.attempt_alias(scope, completed),
        )

    async def _complete(
        self,
        scope: InvocationScope,
        attempt: DataQueryAttemptRecord,
        terminal: DataQueryAttemptTerminal,
        *,
        publication_timeout_seconds: float,
    ) -> DataQueryAttemptRecord:
        deadline = monotonic() + max(0.0, publication_timeout_seconds)
        try:
            return await self._await_terminal_publication(
                self._repository.commit_data_query_attempt_terminal(
                    scope, attempt_id=attempt.attempt_id, terminal=terminal
                ),
                deadline,
            )
        except asyncio.CancelledError:
            reconcile_task = asyncio.create_task(
                self._reconcile_terminal(scope, attempt, terminal, deadline)
            )
            with suppress(Exception):
                await asyncio.shield(reconcile_task)
            raise
        except Exception as exc:
            reconciled = await self._reconcile_terminal(scope, attempt, terminal, deadline)
            if reconciled is not None:
                return reconciled
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_PUBLICATION_UNCERTAIN",
                "The data query Attempt terminal state could not be reconciled",
                phase="output_validation",
                retryable=True,
                attempt_alias=self.attempt_alias(scope, attempt),
            ) from exc

    async def _await_terminal_publication(self, awaitable, deadline: float):
        remaining = max(
            0.0,
            deadline - monotonic() - _PUBLICATION_RECONCILE_RESERVE_SECONDS,
        )
        async with asyncio.timeout(remaining):
            return await awaitable

    async def _reconcile_terminal(
        self,
        scope: InvocationScope,
        attempt: DataQueryAttemptRecord,
        terminal: DataQueryAttemptTerminal,
        deadline: float,
    ) -> DataQueryAttemptRecord | None:
        remaining = max(0.0, deadline - monotonic())
        if remaining == 0:
            return None
        try:
            async with asyncio.timeout(remaining):
                reconciled = await self._repository.get_data_query_attempt(
                    scope, attempt_id=attempt.attempt_id
                )
        except Exception:
            return None
        if (
            reconciled is not None
            and reconciled.status == terminal.status
            and reconciled.diagnostic == terminal.diagnostic
            and reconciled.result_summary == terminal.result_summary
        ):
            return reconciled
        return None

    async def get_attempt(
        self, scope: InvocationScope, attempt_id: str
    ) -> DataQueryAttemptRecord | None:
        return await self._repository.get_data_query_attempt(scope, attempt_id=attempt_id)

    async def resolve_attempt_alias(
        self, scope: InvocationScope, alias: str
    ) -> DataQueryAttemptRecord:
        try:
            claims = self._cursor_codec.decode(alias)
        except OpaqueCursorCodecError as exc:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_ALIAS_INVALID", "The data query attempt alias is invalid"
            ) from exc
        if claims.get("domain") != _ALIAS_DOMAIN or claims.get("scope") != _scope_digest(scope):
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_SCOPE_MISMATCH",
                "The data query attempt alias belongs to a different Run",
            )
        attempt_id = claims.get("attempt_id")
        if not isinstance(attempt_id, str):
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_ALIAS_INVALID", "The data query attempt alias is invalid"
            )
        attempt = await self._repository.get_data_query_attempt(scope, attempt_id=attempt_id)
        if attempt is None:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_NOT_FOUND", "The data query attempt was not found"
            )
        return attempt

    def attempt_alias(self, scope: InvocationScope, attempt: DataQueryAttemptRecord) -> str:
        return self._cursor_codec.encode(
            {
                "domain": _ALIAS_DOMAIN,
                "scope": _scope_digest(scope),
                "attempt_id": attempt.attempt_id,
            }
        )

    async def read_source(
        self, scope: InvocationScope, attempt_id: str, view: TextSourceView
    ) -> TextSourceViewResult:
        attempt = await self._repository.get_data_query_attempt(scope, attempt_id=attempt_id)
        if attempt is None:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_ATTEMPT_NOT_FOUND", "The data query attempt was not found"
            )
        source = await self._read_exact_source(scope, attempt)
        try:
            return self._text_views.view(source, view)
        except TextSourceViewError as exc:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_SOURCE_READ_INVALID", str(exc)
            ) from exc

    async def _read_exact_source(
        self, scope: InvocationScope, attempt: DataQueryAttemptRecord
    ) -> str:
        try:
            downloaded = await self._exact_artifacts.read(
                scope,
                attempt.source_artifact,
                expected_content_type=_SOURCE_MEDIA_TYPES[attempt.engine],
            )
            return downloaded.content.decode("utf-8")
        except (ExactArtifactReadError, UnicodeDecodeError) as exc:
            raise DataQueryAttemptApplicationError(
                "DATA_QUERY_SOURCE_UNAVAILABLE",
                "The exact data query source is unavailable or invalid",
            ) from exc

    async def list_terminal_attempts(
        self,
        scope: InvocationScope,
        *,
        engine: DataQueryEngine,
        statuses: tuple[DataQueryAttemptStatus, ...],
        before_created_at,
        before_attempt_id,
        limit: int,
    ) -> tuple[DataQueryAttemptRecord, ...]:
        return await self._repository.list_terminal_data_query_attempts(
            scope,
            engine=engine,
            statuses=statuses,
            before_created_at=before_created_at,
            before_attempt_id=before_attempt_id,
            limit=limit,
        )

    async def result_available(
        self, scope: InvocationScope, attempt: DataQueryAttemptRecord
    ) -> bool:
        visible = await self._repository.get_data_query_attempt(
            scope, attempt_id=attempt.attempt_id
        )
        if visible is None:
            return False
        if attempt.status is not DataQueryAttemptStatus.SUCCEEDED or self._snapshots is None:
            return False
        return await self._snapshots.available_for_attempt(
            attempt.attempt_id, scope_digest=invocation_scope_digest(scope)
        )


def _scope_digest(scope: InvocationScope) -> str:
    return sha256_digest(
        {
            "tenant_id": scope.identity.tenant_id,
            "owner_id": scope.owner_id,
            "task_id": scope.identity.task_id,
            "run_id": scope.run_id,
        }
    )


__all__ = ["DataQueryAttemptApplicationError", "DataQueryAttemptService"]
