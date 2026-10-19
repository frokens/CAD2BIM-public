"""Shared state and atomic helpers for in-memory control-plane repositories."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from CAD2BIM_backend.domain.control_plane import (
    ArtifactRecord,
    ConflictError,
    ExecutionProtocolStatus,
    FeedbackCommand,
    FeedbackDeliveryBatch,
    IdempotencyConflictError,
    InterruptRecord,
    LeaseConflictError,
    ResourceNotFoundError,
    RunRecord,
    RunStatus,
    SavepointRecord,
    TaskRecord,
    TaskStatus,
    TERMINAL_RUN_STATUSES,
)
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationRecord,
    DataCatalogEntry,
    DataPackageMemberRecord,
    DataPackageRecord,
    DataTransformOperationState,
    DirectSessionBinding,
)
from CAD2BIM_backend.domain.data_query_attempt import DataQueryAttemptRecord
from CAD2BIM_backend.domain.run_events import RunEvent, validate_run_event_stream
from CAD2BIM_backend.domain.resume_operations import ResumeOperationRecord
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _event_fingerprint(value: RunEvent) -> str:
    return _fingerprint(value.model_dump(mode="json", exclude={"sequence"}))


class MemoryState:
    """All mutable adapter data guarded by one lock."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.tasks: dict[str, TaskRecord] = {}
        self.runs: dict[str, RunRecord] = {}
        self.resume_operations: dict[str, ResumeOperationRecord] = {}
        self.events: dict[str, list[RunEvent]] = {}
        self.feedback: dict[str, FeedbackCommand] = {}
        self.feedback_batches: dict[str, FeedbackDeliveryBatch] = {}
        self.interrupts: dict[str, InterruptRecord] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.savepoints: dict[str, SavepointRecord] = {}
        self.idempotency: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.event_replays: dict[tuple[str, str], tuple[str, RunEvent]] = {}
        self.direct_sessions: dict[tuple[str, str, str], DirectSessionBinding] = {}
        self.data_packages: dict[str, DataPackageRecord] = {}
        self.data_package_members: dict[str, tuple[DataPackageMemberRecord, ...]] = {}
        self.critical_operations: dict[str, CriticalOperationRecord] = {}
        self.data_catalog_entries: dict[str, list[DataCatalogEntry]] = {}
        self.data_package_replays: dict[tuple[str, str], str] = {}
        self.critical_operation_replays: dict[tuple[str, str], str] = {}
        self.data_transform_operations: dict[str, DataTransformOperationState] = {}
        self.data_transform_request_replays: dict[tuple[str, str], str] = {}
        self.data_query_attempts: dict[str, DataQueryAttemptRecord] = {}
        self.data_query_attempt_replays: dict[tuple[str, str], str] = {}

class MemoryRepositoryBase:
    """Own the shared state and helpers used by repository mixins."""

    def __init__(self, state: MemoryState | None = None) -> None:
        state = state or MemoryState()
        self._state = state
        self._lock = state.lock
        self._tasks = state.tasks
        self._runs = state.runs
        self._resume_operations = state.resume_operations
        self._events = state.events
        self._feedback = state.feedback
        self._feedback_batches = state.feedback_batches
        self._interrupts = state.interrupts
        self._artifacts = state.artifacts
        self._savepoints = state.savepoints
        self._idempotency = state.idempotency
        self._event_replays = state.event_replays

    def _require_task(self, owner_id: str, task_id: str, *, include_archived: bool = False) -> TaskRecord:
        task = self._tasks.get(task_id)
        if (task is None or task.owner_id != owner_id
            or (not include_archived and task.status is TaskStatus.ARCHIVED)):
            raise ResourceNotFoundError("task not found")
        return task

    def _require_run(self, owner_id: str, run_id: str) -> RunRecord:
        run = self._runs.get(run_id)
        if run is None or run.owner_id != owner_id:
            raise ResourceNotFoundError("run not found")
        self._require_task(owner_id, run.task_id)
        return run

    def _require_lease(self, run_id: str, runner_id: str, lease_token: str) -> RunRecord:
        run = self._runs.get(run_id)
        if run is None:
            raise ResourceNotFoundError("run not found")
        if run.execution_protocol_status is not ExecutionProtocolStatus.PINNED:
            raise LeaseConflictError("run execution protocol is quarantined")
        now = _now()
        if (
            run.status != RunStatus.RUNNING
            or run.claimed_by != runner_id
            or run.lease_token != lease_token
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise LeaseConflictError("runner does not own an active lease")
        return run

    def _set_task_status(self, task_id: str, status: TaskStatus, now: datetime) -> None:
        task = self._tasks[task_id]
        self._tasks[task_id] = task.model_copy(update={"status": status, "updated_at": now})

    def _idempotent_replay(
        self,
        operation: str,
        scope: str,
        key: str | None,
        request: Any,
        records: Mapping[str, Any],
    ) -> Any | None:
        if key is None:
            return None
        entry = self._idempotency.get((operation, scope, key))
        if entry is None:
            return None
        expected_fingerprint, record_id = entry
        if expected_fingerprint != _fingerprint(request):
            raise IdempotencyConflictError("idempotency key was reused with different input")
        return records[record_id]

    def _remember(
        self,
        operation: str,
        scope: str,
        key: str | None,
        request: Any,
        record_id: str,
    ) -> None:
        if key is not None:
            self._idempotency[(operation, scope, key)] = (_fingerprint(request), record_id)

    def _append_events_locked(
        self,
        owner_id: str,
        run: RunRecord,
        events: Sequence[RunEvent],
    ) -> tuple[RunEvent, ...]:
        if run.owner_id != owner_id:
            raise ResourceNotFoundError("run not found")
        stored = self._events.setdefault(run.run_id, [])
        working = list(stored)
        appended: list[RunEvent] = []
        staged_replays: dict[tuple[str, str], tuple[str, RunEvent]] = {}
        for source in events:
            if source.run_id != run.run_id or source.task_id != run.task_id:
                raise ValueError("event does not belong to the run")
            replay_key = (run.run_id, source.event_id)
            source_fingerprint = _event_fingerprint(source)
            replay = staged_replays.get(replay_key) or self._event_replays.get(replay_key)
            if replay is not None:
                if replay[0] != source_fingerprint:
                    raise ConflictError("event_id was reused with different content")
                appended.append(replay[1])
                continue
            if run.status in TERMINAL_RUN_STATUSES:
                raise ConflictError("events cannot be appended to a terminal run")
            sequence = len(working) + 1
            persisted = source.model_copy(update={"sequence": sequence})
            working.append(persisted)
            staged_replays[replay_key] = (source_fingerprint, persisted)
            appended.append(persisted)
        try:
            validate_run_event_stream(tuple(working))
        except ValueError as exc:
            raise ConflictError(f"invalid run event transition: {exc}") from exc
        self._events[run.run_id] = working
        self._event_replays.update(staged_replays)
        return tuple(appended)


__all__ = ["MemoryRepositoryBase", "MemoryState"]
