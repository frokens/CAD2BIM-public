"""Publish only locations of committed display messages to the existing RunEvent log."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.role_memory import parse_role_messages
from CAD2BIM_backend.domain.run_events import (
    RunEvent, RunEventAudience, RunEventScope, RunEventType,
    compression_activity_id, runtime_activity_item_id,
)
from CAD2BIM_backend.infrastructure.chat_stream import MainDeltaBridge
from CAD2BIM_backend.ports.checkpoint_observers import DurableCheckpointCommit


def message_item_id(run_id: str, message_id: str) -> str:
    return f"message_{uuid5(NAMESPACE_URL, f'{run_id}:message:{message_id}').hex}"


def tool_item_id(run_id: str, call_id: str) -> str:
    return f"tool_{uuid5(NAMESPACE_URL, f'{run_id}:tool:{call_id}').hex}"


class ChatCheckpointObserver:
    def __init__(
        self, *, task_runs: Any, repository: Any,
        bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
        delta_bridge: MainDeltaBridge,
    ) -> None:
        self._task_runs = task_runs
        self._repository = repository
        self._bind_identity = bind_identity
        self._deltas = delta_bridge

    async def activity_changed(self, activity_id: str, stage: str, phase: str,
                               metadata: Mapping[str, Any]) -> None:
        """Append observed execution boundaries to the existing public event log."""
        run_id, thread_id = metadata.get("run_id"), metadata.get("thread_id")
        if not isinstance(run_id, str) or not isinstance(thread_id, str):
            return
        identity, run = await self._task_runs.checkpoint_observation_context(run_id, thread_id)
        attributes = {"activity_id": activity_id, "stage": stage, "phase": phase}
        if metadata.get("model_continuation") is True:
            attributes["continuation"] = "true"
        if type(metadata.get("retry_attempt")) is int:
            attributes["retry_attempt"] = str(metadata["retry_attempt"])
        for key in ("main_message_id", "call_id", "tool_item_id"):
            if isinstance(metadata.get(key), str):
                attributes[key] = metadata[key]
        event = RunEvent(
            event_id=f"activity_{uuid4().hex}",
            sequence=1, task_id=run.task_id, run_id=run_id,
            event_type="runtime.activity.changed", occurred_at=datetime.now(timezone.utc),
            scope=RunEventScope(module=run.module, stage_key=run.stage_key),
            item_id=runtime_activity_item_id(run_id),
            status={"start": RunEventType.RUNNING, "end": RunEventType.SUCCEEDED,
                    "error": RunEventType.FAILED}[phase], attributes=attributes,
        )
        with self._bind_identity(identity):
            await self._repository.append_chat_events(identity.tenant_id, run_id, (event,))

    async def checkpoint_committed(self, commit: DurableCheckpointCommit) -> None:
        identity, run = await self._task_runs.checkpoint_observation_context(commit.target_run_id, commit.thread_id)
        if run.thread_id != commit.thread_id or run.run_id != commit.target_run_id:
            raise ValueError("chat checkpoint is outside its Run")
        events: list[RunEvent] = []
        compression_published = False
        committed_messages: list[str] = []
        scope = RunEventScope(module=run.module, stage_key=run.stage_key)
        occurred_at = datetime.fromisoformat(commit.created_at) if commit.created_at else datetime.now(timezone.utc)

        def publish(
            item_id: str, name: str, status: RunEventType,
            attributes: dict[str, str], *, parent: str | None = None,
        ) -> None:
            event_id = f"chat_{uuid5(NAMESPACE_URL, f'{run.run_id}:{item_id}:{name}').hex}"
            events.append(RunEvent(
                event_id=event_id, sequence=1, task_id=run.task_id, run_id=run.run_id,
                event_type=name, audience=RunEventAudience.PUBLIC, occurred_at=occurred_at,
                scope=scope, item_id=item_id, parent_item_id=parent, status=status,
                payload_ref=f"chat-checkpoint:{commit.checkpoint_id}",
                attributes={"checkpoint_ns": commit.checkpoint_ns, **attributes},
            ))

        card = commit.values.get("role_card_ref")
        if hasattr(card, "model_dump"):
            card = card.model_dump()
        if isinstance(card, Mapping) and isinstance(card.get("artifact_id"), str) and isinstance(card.get("version_id"), str):
            artifact_id, version_id = card["artifact_id"], card["version_id"]
            item_id = f"card_{uuid5(NAMESPACE_URL, f'{run.run_id}:{artifact_id}:{version_id}').hex}"
            publish(item_id, "chat.card.published", RunEventType.SUCCEEDED, {
                "source_kind": "card", "artifact_id": artifact_id, "version_id": version_id,
            })

        # LangGraph child initialization is loop/step=0 and can restore old
        # summaries. Later child writes to both channels are compression_update.
        if commit.checkpoint_ns and commit.source == "loop" and commit.step is not None and commit.step > 0 and {"conversation_summary", "tool_chain_summary"}.issubset(commit.updated_channels):
            if not all(isinstance(commit.values.get(name), str) and commit.values[name] for name in ("conversation_summary", "tool_chain_summary")):
                raise ValueError("committed compression has no complete dual summaries")
            item_id = f"compression_{uuid5(NAMESPACE_URL, f'{run.run_id}:{commit.checkpoint_ns}:{commit.checkpoint_id}').hex}"
            publish(item_id, "chat.compression.published", RunEventType.SUCCEEDED, {"source_kind": "compression"})
            compression_published = True

        locations: list[tuple[str | None, Mapping[str, Any]]] = [(None, commit.values)]
        memories = commit.values.get("role_memories", {})
        if isinstance(memories, Mapping):
            for slot, memory in memories.items():
                if hasattr(memory, "model_dump"):
                    memory = memory.model_dump()
                if isinstance(slot, str) and isinstance(memory, Mapping):
                    locations.append((slot, memory))
        for slot, memory in locations:
            for index, message in enumerate(parse_role_messages(memory.get("messages", ()))):
                correction = isinstance(message, SystemMessage) and message.additional_kwargs.get("source") == "tool_batch_rejection"
                if isinstance(message, SystemMessage) and not correction:
                    continue
                if not isinstance(message, (AIMessage, HumanMessage, ToolMessage, SystemMessage)):
                    continue
                if isinstance(message, HumanMessage) and message.additional_kwargs.get("feedback_id"):
                    # The original feedback.accepted event owns the user bubble.
                    continue
                if not message.id:
                    raise ValueError("public checkpoint message has no stable message ID")
                attributes = {"message_id": message.id, "message_index": str(index)}
                if slot is not None:
                    attributes["memory_slot"] = slot
                item_id = message_item_id(run.run_id, message.id)
                if isinstance(message, ToolMessage):
                    effect = message.additional_kwargs.get("tool_effect")
                    publish(
                        tool_item_id(run.run_id, message.tool_call_id),
                        "tool.call.failed" if message.status == "error" else "tool.call.completed",
                        RunEventType.FAILED if message.status == "error" else RunEventType.SUCCEEDED,
                        {**attributes, "source_kind": "tool_result", "call_id": message.tool_call_id,
                         **({"effect": effect} if effect in ("READ", "WRITE") else {})},
                    )
                    continue
                if message.content or correction:
                    publish(item_id, "chat.message.published", RunEventType.SUCCEEDED, {**attributes, "source_kind": "message"})
                if isinstance(message, AIMessage):
                    committed_messages.append(message.id)
                    effects = message.additional_kwargs.get("tool_effects", {})
                    for call in message.tool_calls:
                        effect = effects.get(call["id"]) if isinstance(effects, Mapping) else None
                        publish(
                            tool_item_id(run.run_id, call["id"]), "tool.call.started", RunEventType.RUNNING,
                            {**attributes, "source_kind": "tool_request", "call_id": call["id"],
                             **({"effect": effect} if effect in ("READ", "WRITE") else {})},
                        )
                elif correction:
                    calls = message.additional_kwargs.get("rejected_tool_call_ids", ())
                    if not isinstance(calls, (list, tuple)) or not all(isinstance(call, str) for call in calls):
                        raise ValueError("tool rejection has invalid source call identities")
                    for call in calls:
                        publish(
                            tool_item_id(run.run_id, call), "tool.call.failed", RunEventType.SKIPPED,
                            {**attributes, "source_kind": "tool_rejection", "call_id": call,
                             "rejected_ai_message_id": message.additional_kwargs["rejected_ai_message_id"]},
                        )
        with self._bind_identity(identity):
            unique = list({event.event_id: event for event in reversed(events)}.values())
            unique.reverse()
            unique.sort(key=lambda event: event.attributes["source_kind"] in {"tool_result", "tool_rejection"})
            await self._repository.append_chat_events(identity.tenant_id, run.run_id, unique)
        if compression_published:
            activity_id = compression_activity_id(run.run_id, commit.checkpoint_ns, commit.step)
            with self._bind_identity(identity):
                activities = await self._repository.get_item_events(
                    identity.tenant_id, run.run_id, runtime_activity_item_id(run.run_id),
                )
            matching = [event for event in activities if event.attributes.get("activity_id") == activity_id]
            if matching and matching[-1].attributes.get("phase") == "start":
                await self.activity_changed(activity_id, "compression", "end",
                                            {"run_id": run.run_id, "thread_id": commit.thread_id})
            self._deltas.compression_committed(run.run_id, commit.checkpoint_ns, commit.step)
        for message_id in committed_messages:
            self._deltas.message_committed(run.run_id, message_id)


__all__ = ["ChatCheckpointObserver", "message_item_id", "tool_item_id"]
