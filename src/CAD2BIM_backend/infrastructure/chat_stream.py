"""Process-local, disposable Main text delivery; durable messages live in checkpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.types import Command

from CAD2BIM_backend.application.chat_projection import public_text_fragment
from CAD2BIM_backend.domain.run_events import compression_activity_id


class DeltaSubscription:
    def __init__(self, capacity: int) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(capacity)
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)

    def offer(self, value: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(value)
        except asyncio.QueueFull:
            self.close()

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while (value := await self.queue.get()) is not None:
            yield value


class MainDeltaBridge:
    """Bounded queues never await a subscriber or persist token fragments."""

    def __init__(self, *, queue_capacity: int = 128) -> None:
        if queue_capacity < 1:
            raise ValueError("delta queue capacity must be positive")
        self._capacity = queue_capacity
        self._subscribers: dict[str, set[DeltaSubscription]] = {}
        self._calls: dict[str, tuple[str, str, int, str]] = {}
        self._compression_activities: dict[str, tuple[str, str, int]] = {}
        self._summary_calls: dict[str, str] = {}
        self._activity_ancestors: dict[str, tuple[str, ...]] = {}

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[DeltaSubscription]:
        subscription = DeltaSubscription(self._capacity)
        self._subscribers.setdefault(run_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            subscription.close()
            subscribers = self._subscribers.get(run_id)
            if subscribers is not None:
                subscribers.discard(subscription)
                if not subscribers:
                    self._subscribers.pop(run_id, None)

    def start(self, call_id: str, run_id: str, message_id: str) -> None:
        if call_id in self._calls:
            return
        self._calls[call_id] = (run_id, message_id, 0, "")
        self._publish(run_id, {"phase": "start", "message_id": message_id, "call_id": call_id, "delta_index": 0, "text": ""})

    def token(self, call_id: str, text: str) -> None:
        current = self._calls.get(call_id)
        if current is None or not text:
            return
        run_id, message_id, index, tail = current
        visible, tail = public_text_fragment(tail + text)
        self._calls[call_id] = (run_id, message_id, index + bool(visible), tail)
        if visible:
            self._publish(run_id, {"phase": "delta", "message_id": message_id, "call_id": call_id, "delta_index": index, "text": visible})

    def completed(self, call_id: str) -> None:
        current = self._calls.get(call_id)
        if current is None:
            return
        run_id, message_id, index, tail = current
        visible, _ = public_text_fragment(tail, final=True)
        self._calls[call_id] = (run_id, message_id, index + bool(visible), "")
        if visible:
            self._publish(run_id, {"phase": "delta", "message_id": message_id, "call_id": call_id, "delta_index": index, "text": visible})

    def retry(self, call_id: str, attempt: int) -> None:
        current = self._calls.get(call_id)
        if current is None:
            return
        run_id, message_id, _, _ = current
        self._calls[call_id] = (run_id, message_id, 0, "")
        self._discard_pending(run_id, "message_id", message_id)
        self._publish(run_id, {"phase": "retry", "message_id": message_id,
                              "call_id": call_id, "delta_index": 0, "text": "",
                              "retry_attempt": attempt})

    def _publish(self, run_id: str, value: dict[str, Any]) -> None:
        for subscription in tuple(self._subscribers.get(run_id, ())):
            subscription.offer(value)
            if subscription.closed:
                self._subscribers[run_id].discard(subscription)
        if run_id in self._subscribers and not self._subscribers[run_id]:
            self._subscribers.pop(run_id, None)

    def end_call(self, call_id: str) -> None:
        self._calls.pop(call_id, None)

    def compression_started(self, call_id: str, run_id: str, activity_id: str, checkpoint_ns: str, step: int, ancestors: tuple[str, ...]) -> None:
        self._summary_calls[call_id] = activity_id
        if activity_id in self._compression_activities:
            return
        self._compression_activities[activity_id] = (run_id, checkpoint_ns, step)
        self._activity_ancestors[activity_id] = ancestors
        self._publish(run_id, {"kind": "compression", "phase": "start", "activity_id": activity_id})

    def _compression_finished(self, activity_id: str, *, failed: bool) -> None:
        source = self._compression_activities.pop(activity_id, None)
        if source is None:
            return
        run_id = source[0]
        self._activity_ancestors.pop(activity_id, None)
        for call_id, parent in tuple(self._summary_calls.items()):
            if parent == activity_id:
                self._summary_calls.pop(call_id, None)
        self._discard_pending(run_id, "activity_id", activity_id)
        self._publish(run_id, {"kind": "compression", "phase": "error" if failed else "end", "activity_id": activity_id})

    def compression_failed(self, call_or_parent_id: str) -> None:
        activity_id = self._summary_calls.get(call_or_parent_id, call_or_parent_id)
        self._compression_finished(activity_id, failed=True)
        for activity_id, ancestors in tuple(self._activity_ancestors.items()):
            if call_or_parent_id in ancestors:
                self._compression_finished(activity_id, failed=True)

    def compression_committed(self, run_id: str, checkpoint_ns: str, step: int) -> None:
        for activity_id, source in tuple(self._compression_activities.items()):
            if source == (run_id, checkpoint_ns, step):
                self._compression_finished(activity_id, failed=False)

    def compression_unchanged(self, activity_id: str) -> None:
        """End a real summary attempt that returned without changing memory."""
        self._compression_finished(activity_id, failed=False)

    def failed(self, call_id: str) -> None:
        current = self._calls.get(call_id)
        if current is not None:
            run_id, message_id, index, _ = current
            self._publish(run_id, {
                "phase": "error", "message_id": message_id, "call_id": call_id,
                "delta_index": index, "text": "", "error": "Response generation failed.",
            })
        self.end_call(call_id)

    def message_committed(self, run_id: str, message_id: str) -> None:
        for call_id, (target, message, _, _) in tuple(self._calls.items()):
            if (target, message) == (run_id, message_id):
                self.end_call(call_id)
        self._discard_pending(run_id, "message_id", message_id)

    def _discard_pending(self, run_id: str, key: str, identity: str) -> None:
        for subscription in tuple(self._subscribers.get(run_id, ())):
            pending = []
            while not subscription.queue.empty():
                value = subscription.queue.get_nowait()
                if value is None or value.get(key) != identity:
                    pending.append(value)
            for value in pending:
                subscription.queue.put_nowait(value)


class MainTextDeltaCallback(AsyncCallbackHandler):
    """Ordinary callback using the trusted Main invocation identity from the role node."""

    def __init__(self, bridge: MainDeltaBridge, activity_observer: Callable[..., Awaitable[None]] | None = None) -> None:
        self._bridge = bridge
        self._chain_parents: dict[str, str | None] = {}
        self._activity_observer = activity_observer
        self._runtime_activities: dict[str, tuple[str, str, Mapping[str, Any]]] = {}

    async def _activity(self, source_id: str, activity_id: str, stage: str,
                        metadata: Mapping[str, Any], phase: str = "start") -> None:
        if self._activity_observer is not None:
            await self._activity_observer(activity_id, stage, phase, metadata)
        if phase == "start":
            self._runtime_activities[source_id] = (activity_id, stage, metadata)
        else:
            self._runtime_activities.pop(source_id, None)

    async def _finish_activity(self, source_id: str, phase: str = "end") -> None:
        activity = self._runtime_activities.get(source_id)
        if activity is not None:
            await self._activity(source_id, *activity, phase=phase)

    async def on_chain_start(self, serialized: Any, inputs: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any) -> None:
        self._chain_parents[str(run_id)] = str(parent_run_id) if parent_run_id is not None else None
        metadata = kwargs.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("langgraph_node") == "initialize_component" and kwargs.get("name") == "initialize_component":
            await self._activity(str(run_id), str(run_id), "initializing", metadata)

    async def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if isinstance(outputs, Command) and outputs.update == {}:
            # compress_memory returns this exact Command after a no-gain result.
            # It publishes no saved-compression item. Changed summaries still
            # wait for the checkpoint observer, including checkpoint failures.
            self._bridge.compression_unchanged(str(run_id))
            await self._finish_activity(str(run_id))
        elif (activity := self._runtime_activities.get(str(run_id))) is not None and activity[1] == "initializing":
            await self._finish_activity(str(run_id))
        elif activity is not None and activity[1] == "compression":
            # Changed summaries finish when the checkpoint observer commits them.
            self._runtime_activities.pop(str(run_id), None)
        self._chain_parents.pop(str(run_id), None)

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        trusted_tags = set(tags or ())
        if not isinstance(metadata, Mapping):
            return
        target = metadata.get("run_id")
        if parent_run_id is not None:
            self._chain_parents[str(run_id)] = str(parent_run_id)
        if "role_summary" in trusted_tags and "role_card" not in trusted_tags and metadata.get("langgraph_node") == "compress_memory":
            namespace, step = metadata.get("role_graph_checkpoint_ns"), metadata.get("langgraph_step")
            if isinstance(target, str) and target and parent_run_id is not None and isinstance(namespace, str) and namespace and type(step) is int:
                ancestors = [str(parent_run_id)]
                while (parent := self._chain_parents.get(ancestors[-1])) is not None and parent not in ancestors:
                    ancestors.append(parent)
                self._bridge.compression_started(str(run_id), target, str(parent_run_id), namespace, step, tuple(ancestors))
                activity_id = compression_activity_id(target, namespace, step)
                if str(parent_run_id) not in self._runtime_activities:
                    await self._activity(str(parent_run_id), activity_id, "compression", metadata)
            return
        if "role_title" in trusted_tags or "role_card" in trusted_tags:
            if isinstance(target, str) and target:
                await self._activity(str(run_id), str(run_id), "title" if "role_title" in trusted_tags else "card", metadata)
            return
        if "role_main" not in trusted_tags or trusted_tags.intersection({"role_compression", "role_summary", "role_card"}):
            return
        message = metadata.get("main_message_id")
        if isinstance(target, str) and target and isinstance(message, str) and message:
            self._bridge.start(str(run_id), target, message)
            await self._activity(str(run_id), str(run_id), "model", metadata)

    async def on_llm_new_token(self, token: str, *, run_id: UUID, **kwargs: Any) -> None:
        activity = self._runtime_activities.get(str(run_id))
        if kwargs.get("activity_stage") == "retry":
            self._bridge.retry(str(run_id), kwargs["retry_attempt"])
            if activity is not None:
                await self._activity(str(run_id), activity[0], "retry",
                                     {**activity[2], "retry_attempt": kwargs["retry_attempt"]})
        elif kwargs.get("activity_stage") == "tool_arguments" and activity is not None and activity[1] in {"model", "retry"}:
            await self._activity(str(run_id), activity[0], "tool_arguments", activity[2])
        elif token and activity is not None and activity[1] == "retry":
            await self._activity(str(run_id), activity[0], "model", activity[2])
        self._bridge.token(str(run_id), token)

    async def on_tool_start(self, serialized: Any, input_str: str, *, run_id: UUID,
                            parent_run_id: UUID | None = None, metadata: Mapping[str, Any] | None = None,
                            **kwargs: Any) -> None:
        if not isinstance(metadata, Mapping) or not isinstance(metadata.get("run_id"), str):
            return
        from .langgraph.chat_observer import tool_item_id

        call_id = kwargs.get("tool_call_id")
        if parent_run_id is not None:
            self._chain_parents[str(run_id)] = str(parent_run_id)
        await self._activity(str(run_id), str(run_id), "tools", {**metadata, **({
            "call_id": call_id, "tool_item_id": tool_item_id(metadata["run_id"], call_id),
        } if isinstance(call_id, str) else {})})

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        activity = self._runtime_activities.get(str(run_id))
        if activity is not None:
            phase = "error" if getattr(output, "status", None) == "error" else "end"
            await self._activity(str(run_id), activity[0], "tool_results", activity[2], phase=phase)
        self._chain_parents.pop(str(run_id), None)

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        await self._finish_activity(str(run_id), "error")
        self._chain_parents.pop(str(run_id), None)

    async def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        # The checkpoint observer, rather than the SDK completion, closes this ID.
        self._bridge.completed(str(run_id))
        await self._finish_activity(str(run_id))
        self._chain_parents.pop(str(run_id), None)

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._bridge.compression_failed(str(run_id))
        self._bridge.failed(str(run_id))
        await self._finish_activity(str(run_id), "error")
        parent = self._chain_parents.get(str(run_id))
        if parent is not None:
            await self._finish_activity(parent, "error")
        self._chain_parents.pop(str(run_id), None)

    async def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._bridge.compression_failed(str(run_id))
        for source_id in tuple(self._runtime_activities):
            current = source_id
            while current is not None and current != str(run_id):
                current = self._chain_parents.get(current)
            if current == str(run_id):
                await self._finish_activity(source_id, "error")
        self._chain_parents.pop(str(run_id), None)


def create_component_presentation(
    *, task_runs: Any, repository: Any, graph_runtime: Any,
    bind_identity: Any, feedback_repository: Any, artifact_service: Any,
) -> Any:
    from CAD2BIM_backend.application.chat_projection import ChatProjectionService
    from CAD2BIM_backend.infrastructure.langgraph.chat_observer import ChatCheckpointObserver

    bridge = MainDeltaBridge()
    observer = ChatCheckpointObserver(
        task_runs=task_runs, repository=repository,
        bind_identity=bind_identity, delta_bridge=bridge,
    )
    return ChatProjectionService(
        task_runs=task_runs, repository=repository, graph_runtime=graph_runtime,
        bind_identity=bind_identity, feedback_repository=feedback_repository,
        artifact_service=artifact_service, delta_bridge=bridge,
        checkpoint_observer=observer, callback_factory=lambda: MainTextDeltaCallback(bridge, observer.activity_changed),
    )


__all__ = ["MainDeltaBridge", "MainTextDeltaCallback", "create_component_presentation"]
