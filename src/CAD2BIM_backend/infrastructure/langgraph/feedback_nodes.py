"""Exact checkpoint inputs for formal feedback delivery nodes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langchain_core.runnables.config import RunnableConfig
from langgraph.runtime import Runtime

from CAD2BIM_backend.application.feedback_service import (
    FeedbackService,
)
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import LeaseConflictError
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant, RunnerGraphContext
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest


def exact_node_input_cursor(
    config: Mapping[str, Any], runtime: Runtime[RunnerGraphContext]
) -> CheckpointCursor:
    """Bind a node to its saved input, including the active child namespace."""

    info = runtime.execution_info
    configurable = config.get("configurable")
    if info is None or not isinstance(configurable, Mapping):
        raise RuntimeError("feedback node has no saved execution input")
    thread_id = str(info.thread_id or "")
    checkpoint_id = str(info.checkpoint_id or "")
    if not thread_id or not checkpoint_id or configurable.get("thread_id") != thread_id:
        raise RuntimeError("feedback node input has no exact thread checkpoint")
    raw_map = configurable.get("checkpoint_map")
    if not isinstance(raw_map, Mapping):
        raise RuntimeError("feedback node input has no checkpoint map")
    checkpoint_map = tuple(
        sorted((str(namespace), str(value)) for namespace, value in raw_map.items())
    )
    active = [namespace for namespace, value in checkpoint_map if value == checkpoint_id]
    if len(active) != 1:
        raise RuntimeError("feedback node input does not identify one exact namespace")
    root_checkpoint_id = dict(checkpoint_map).get("")
    if not root_checkpoint_id:
        raise RuntimeError("feedback node input has no root checkpoint")
    return CheckpointCursor(
        thread_id=thread_id,
        checkpoint_id=checkpoint_id,
        checkpoint_ns=active[0],
        root_checkpoint_id=root_checkpoint_id,
        checkpoint_map=checkpoint_map,
        checkpoint_digest=checkpoint_cursor_digest(
            checkpoint_id=checkpoint_id,
            checkpoint_ns=active[0],
            checkpoint_map=checkpoint_map,
        ),
    )


class FeedbackNodes:
    """Main input assembly and the final pending-input exit boundary."""

    def __init__(self, service: FeedbackService) -> None:
        self._service = service
        self._grant_provider: Callable[[], RunnerCheckpointGrant] | None = None

    def bind_grant_provider(self, provider: Callable[[], RunnerCheckpointGrant]) -> None:
        """Bind the runtime's process-private execution authority once."""

        if self._grant_provider is not None:
            raise RuntimeError("feedback grant provider is already bound")
        self._grant_provider = provider

    def _current_grant(self, runtime: Runtime[RunnerGraphContext]) -> RunnerCheckpointGrant:
        if self._grant_provider is None:
            raise RuntimeError("feedback grant provider is not bound")
        grant = self._grant_provider()
        info = runtime.execution_info
        if (
            info is None
            or grant.thread_id != info.thread_id
            or grant.scope != runtime.context.scope
        ):
            raise LeaseConflictError("feedback node has no matching Run claim")
        return grant

    async def prepare_main(
        self,
        state: Mapping[str, Any],
        config: RunnableConfig,
        runtime: Runtime[RunnerGraphContext],
    ) -> dict[str, Any]:
        grant = self._current_grant(runtime)
        source = exact_node_input_cursor(config, runtime)
        role = state.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("feedback Main needs the saved role")
        business_entry_id = state.get("business_entry_id")
        if not isinstance(business_entry_id, str) or not business_entry_id:
            raise ValueError("feedback Main needs a saved business entry")
        stage_key = state.get("stage_key")
        if stage_key is not None and not isinstance(stage_key, str):
            raise ValueError("feedback Main has an invalid Stage")
        role_entry_index = state.get("role_entry_index")
        if not isinstance(role_entry_index, int) or isinstance(role_entry_index, bool) or role_entry_index < 1:
            raise ValueError("feedback Main needs the saved role_entry_index")
        subject = await self._service.activate_feedback_subject(
            grant,
            stage_key=stage_key,
            agent=role,
            attempt=role_entry_index,
            business_entry_id=business_entry_id,
            checkpoint_id=source.checkpoint_id,
            task_ids=tuple(state.get("feedback_task_ids", ())),
            check_ids=tuple(state.get("feedback_check_ids", ())),
        )
        frozen = await self._service.freeze_for_main(
            grant,
            stage_key=subject.stage_key,
            role_execution_id=subject.subject_id,
            task_ids=subject.task_ids,
            check_ids=subject.check_ids,
            input_checkpoint_cursor=source,
            delivered_batch_ids=tuple(state.get("feedback_batch_ids", ())),
        )
        if not frozen:
            return {
                "feedback_subject_id": subject.subject_id,
                "feedback_batch_ids": (),
                "input_high_watermark": int(state.get("input_high_watermark", 0)),
            }
        return {
            **await self._service.delivery_update(grant, state, frozen),
            "feedback_subject_id": subject.subject_id,
            "input_high_watermark": max(batch.batch.claim_high_watermark for batch in frozen),
        }

    async def decide_role_exit(
        self,
        state: Mapping[str, Any],
        config: RunnableConfig,
        runtime: Runtime[RunnerGraphContext],
    ) -> dict[str, Any]:
        subject_id = state.get("feedback_subject_id")
        if not isinstance(subject_id, str) or not subject_id:
            raise ValueError("role exit has no active feedback subject")
        watermark = state.get("input_high_watermark")
        if not isinstance(watermark, int) or watermark < 0:
            raise ValueError("role exit has no saved feedback boundary")
        grant = self._current_grant(runtime)
        sealed = await self._service.decide_role_exit(
            grant, subject_id, watermark,
            input_checkpoint_cursor=exact_node_input_cursor(config, runtime),
            delivered_batch_ids=tuple(state.get("feedback_batch_ids", ())),
        )
        return {
            "role_exit_route": "parent_gate" if sealed else "main",
            "feedback_batch_ids": (),
        }

    async def pause_requested(self, state, config, runtime) -> bool:
        return await self._service.pause_requested(self._current_grant(runtime))


__all__ = ["FeedbackNodes", "exact_node_input_cursor"]
