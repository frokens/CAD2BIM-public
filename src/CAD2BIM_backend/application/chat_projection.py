"""Authorized display views over original RunEvents, checkpoints and feedback."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from CAD2BIM_backend.domain.control_plane import ResourceNotFoundError, RunRecord
from CAD2BIM_backend.domain.identity import AuthenticatedSubject, TenantExecutionContext
from CAD2BIM_backend.domain.role_memory import parse_role_messages
from CAD2BIM_backend.domain.run_events import RunEvent, RunEventAudience, runtime_activity_item_id
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointState


_PRIVATE_KEYS = frozenset({
    "additional_kwargs", "response_metadata", "reasoning", "reasoning_content",
    "encrypted_content", "opaque", "grant", "runner_grant", "lease_token",
    "authorization", "api_key", "password", "secret", "token", "access_token",
    "refresh_token", "id_token", "cookie", "credentials", "source_path", "storage_key",
    "execution_protocol", "checkpoint_map", "checkpoint_map_digest",
    "system_prompt", "runner_context",
})
_PATH_PREFIXES = ("file://", *(f"/{root}/" for root in ("users", "private", "tmp", "var", "home", "etc", "opt", "usr", "applications", "volumes", "library")))
_SECRET_LABELS = (
    "authorization", "password", "secret", "token",
    *(f"{prefix}{separator}{suffix}" for prefix, suffix in (("api", "key"), ("access", "token"), ("refresh", "token"), ("id", "token")) for separator in ("", "_", "-")),
)
_SECRET_LABEL = "(?:" + "|".join(map(re.escape, _SECRET_LABELS)) + ")"
_PATH = re.compile(r"(?i)(?:(?:" + "|".join(map(re.escape, _PATH_PREFIXES)) + r")[^\s\"<>]+|\b[a-z]:[\\/][^\s\"<>]+)")
_SECRET_VALUE = r'''(?:"(?:\\.|[^"\\])*(?:"|\\?$)|'(?:\\.|[^'\\])*(?:'|\\?$)|[^\s"'<>]+)'''
_SECRET = re.compile(r'''(?i)(?:bearer\s+[^\s"'<>]+|''' + _SECRET_LABEL + r'''["']?\s*[:=]\s*''' + _SECRET_VALUE + ")")
_SECRET_PENDING = re.compile(
    r'''(?i)(?:bearer(?:\s+[^\s"'<>]*)?|''' + _SECRET_LABEL
    + r'''["']?\s*(?:[:=]\s*(?:"(?:\\.|[^"\\])*\\?|'(?:\\.|[^'\\])*\\?|[^\s"'<>]*))?)$'''
)
_DRIVE_PENDING = re.compile(r"(?i)\b[a-z](?::[\\/]?)?$")
_DATA_IMAGE = re.compile(r"^data:(image/(?:png|jpeg|gif|webp));base64,(.*)$", re.DOTALL)
_ACTIVITY_LABELS = {
    "initializing": "正在初始化对话", "title": "正在生成任务标题",
    "model": "模型正在思考", "tool_arguments": "模型正在准备工具参数",
    "compression": "正在压缩上下文", "card": "正在整理本轮工作结果",
    "tools": "正在执行工具", "tool_results": "工具结果已返回",
    "retry": "网络中断，正在重试模型请求",
}


def _activity_payload(event: RunEvent) -> dict[str, Any] | None:
    if event.audience is not RunEventAudience.PUBLIC:
        return None
    if event.event_type == "runtime.activity.changed":
        stage, phase = event.attributes.get("stage"), event.attributes.get("phase")
        if stage not in _ACTIVITY_LABELS or phase not in {"start", "end", "error"}:
            return None
        activity = {"activity_id": event.attributes["activity_id"], "stage": stage,
                    "phase": phase, "label": _ACTIVITY_LABELS[stage]}
        if stage == "model" and event.attributes.get("continuation") == "true":
            activity["label"] = "模型继续思考"
        if stage == "tool_results" and phase == "error":
            activity["label"] = "工具返回失败结果"
        if "retry_attempt" in event.attributes:
            activity["retry_attempt"] = int(event.attributes["retry_attempt"])
        for key in ("main_message_id", "call_id", "tool_item_id"):
            if key in event.attributes:
                activity[key] = event.attributes[key]
    elif event.event_type.startswith("tool.call.") and event.attributes.get("source_kind") in {"tool_request", "tool_result", "tool_rejection"}:
        started = event.event_type == "tool.call.started"
        stage = "tools" if started else "tool_results"
        activity = {"activity_id": event.item_id, "stage": stage,
                    "phase": "start" if started else "error" if event.event_type == "tool.call.failed" else "end",
                    "label": "工具调用已准备，等待执行" if started else _ACTIVITY_LABELS[stage],
                    "call_id": event.attributes["call_id"]}
        if event.attributes.get("source_kind") == "tool_rejection":
            activity["label"] = "工具调用未执行"
        elif event.event_type == "tool.call.failed":
            activity["label"] = "工具返回失败结果"
        message_id = event.attributes.get("message_id") if started else event.attributes.get("rejected_ai_message_id")
        if message_id:
            activity["main_message_id"] = message_id
    else:
        return None
    return {"kind": "runtime", **activity, "event_sequence": event.sequence}


def public_text(value: str) -> str:
    return _SECRET.sub("[redacted]", _PATH.sub("[local path]", value))


def public_text_fragment(value: str, *, final: bool = False) -> tuple[str, str]:
    """Project a decided prefix; retain only a possibly protected unfinished tail."""
    if final:
        return public_text(value), ""
    cut = len(value)
    for pending in (_SECRET_PENDING.search(value), _DRIVE_PENDING.search(value)):
        if pending is not None:
            cut = min(cut, pending.start())
    folded = value.lower()
    for prefix in (*_PATH_PREFIXES, *_SECRET_LABELS, "bearer"):
        for size in range(1, min(len(prefix), len(value)) + 1):
            if folded.endswith(prefix[:size]):
                cut = min(cut, len(value) - size)
    matches = [*_PATH.finditer(value), *_SECRET.finditer(value)]
    for match in matches:
        if match.re is _PATH and match.end() == len(value):
            cut = min(cut, match.start())
    # Never cut inside a complete guarded value (including a quoted value).
    while True:
        previous = cut
        for match in matches:
            if match.start() < cut < match.end():
                cut = match.start()
        if cut == previous:
            break
    return public_text(value[:cut]), value[cut:]


def public_json(value: Any) -> Any:
    """Select public business values, never native message/protocol internals."""
    if isinstance(value, str):
        return public_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): public_json(item) for key, item in value.items()
            if isinstance(key, str) and key.lower() not in _PRIVATE_KEYS
            and not key.lower().endswith(("_grant", "_secret", "_password", "_token"))
            and not key.lower().startswith(("opaque", "encrypted"))
        }
    if isinstance(value, (list, tuple)):
        return [public_json(item) for item in value]
    return None


def _display_text(value: str) -> str:
    try:
        parsed = json.loads(value)
    except ValueError:
        return public_text(value)
    if isinstance(parsed, (dict, list)):
        return json.dumps(public_json(parsed), ensure_ascii=False, separators=(",", ":"))
    return public_text(value)


@dataclass(frozen=True, slots=True)
class ChatContent:
    content: bytes
    media_type: str


def _image(block: Mapping[str, Any]) -> ChatContent | None:
    kind = block.get("type")
    mime = block.get("mimeType", block.get("mime_type"))
    data = block.get("data", block.get("base64"))
    if kind == "image" and isinstance(data, str) and mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        try:
            return ChatContent(base64.b64decode(data, validate=True), str(mime))
        except ValueError:
            return None
    source = block.get("image_url", block.get("url"))
    if isinstance(source, Mapping):
        source = source.get("url")
    if kind in {"image_url", "input_image", "image"} and isinstance(source, str):
        match = _DATA_IMAGE.fullmatch(source)
        if match:
            try:
                return ChatContent(base64.b64decode(match[2], validate=True), match[1])
            except ValueError:
                return None
    return None


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, (list, tuple)):
        blocks = [block.model_dump(mode="json") if hasattr(block, "model_dump") else block for block in content]
        return [dict(block) for block in blocks if isinstance(block, Mapping)]
    return []


def _tool_result(message: ToolMessage) -> dict[str, Any]:
    # The locked langchain-mcp-adapters converter keeps these two parts separate.
    result = {"content": _blocks(message.content)}
    if isinstance(message.artifact, Mapping) and "structured_content" in message.artifact:
        result["structuredContent"] = message.artifact["structured_content"]
    return result


def _memory_values(values: Mapping[str, Any], event: RunEvent) -> Mapping[str, Any]:
    location = event.attributes.get("memory_slot")
    if location is None:
        return values
    else:
        memories = values.get("role_memories", {})
        memory = memories.get(location, {}) if isinstance(memories, Mapping) else {}
        if hasattr(memory, "model_dump"):
            memory = memory.model_dump()
        return memory if isinstance(memory, Mapping) else {}


def _message(values: Mapping[str, Any], event: RunEvent) -> BaseMessage:
    messages = parse_role_messages(_memory_values(values, event).get("messages", ()))
    index = int(event.attributes["message_index"])
    if index < 0 or index >= len(messages):
        raise ResourceNotFoundError("saved chat source is missing")
    message = messages[index]
    if message.id != event.attributes.get("message_id"):
        raise ResourceNotFoundError("saved chat message identity differs")
    return message


class ChatProjectionService:
    def __init__(
        self, *, task_runs: Any, repository: Any, graph_runtime: Any,
        bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
        feedback_repository: Any, artifact_service: Any,
        delta_bridge: Any, checkpoint_observer: Any, callback_factory: Callable[[], Any],
    ) -> None:
        self._task_runs = task_runs
        self._repository = repository
        self._runtime = graph_runtime
        self._bind_identity = bind_identity
        self._feedback = feedback_repository
        self._artifacts = artifact_service
        self.delta_bridge = delta_bridge
        self.checkpoint_observer = checkpoint_observer
        self._callback_factory = callback_factory

    def callback_factory(self) -> Any:
        return self._callback_factory()

    async def snapshot(
        self, subject: AuthenticatedSubject, task_id: str, run_id: str, *,
        before_sequence: int | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        if (before_sequence is not None and before_sequence < 1) or not 1 <= limit <= 100:
            raise ValueError("invalid chat page")
        identity, authorized = await self._task_runs.authorized_run_context(subject, task_id, run_id)
        with self._bind_identity(identity):
            run, events, boundary, before, activity_events = await self._repository.read_chat_snapshot(
                identity.tenant_id, run_id, before_sequence=before_sequence, limit=limit,
            )
            if run.task_id != authorized.task_id:
                raise ResourceNotFoundError("run not found")
            task = await self._repository.get_task(identity.tenant_id, task_id)
            if task is None:
                raise ResourceNotFoundError("task not found")
            items = await self._items(run, events, through_sequence=boundary, activity_events=activity_events)
            active: dict[str, dict[str, Any]] = {}
            latest = None
            for event in sorted(activity_events, key=lambda event: event.sequence):
                if event.sequence > boundary:
                    continue
                if event.event_type == "run.status.changed" and event.attributes.get("run_status") != "running":
                    active.clear()
                    latest = None
                    continue
                if (activity := _activity_payload(event)) is None:
                    continue
                latest = activity
                if event.event_type == "runtime.activity.changed" and activity.get("tool_item_id"):
                    active.pop(activity["tool_item_id"], None)
                if activity["phase"] == "start":
                    active[activity["activity_id"]] = activity
                else:
                    active.pop(activity["activity_id"], None)
            activity = max(active.values(), key=lambda value: value["event_sequence"], default=latest)
            if run.status.value != "running":
                activity = None
        return {"run": self._public_run(run), "task": self._public_task(task),
                "items": items, "activity": activity, "event_sequence": boundary,
                "next_before_sequence": before}

    async def item(
        self, subject: AuthenticatedSubject, task_id: str, run_id: str, item_id: str,
    ) -> dict[str, Any]:
        identity, run = await self._task_runs.authorized_run_context(subject, task_id, run_id)
        with self._bind_identity(identity):
            events = await self._repository.get_item_events(identity.tenant_id, run_id, item_id)
            items = await self._items(run, events)
        if len(items) != 1 or items[0]["item_id"] != item_id:
            raise ResourceNotFoundError("chat item not found")
        return items[0]

    async def content(
        self, subject: AuthenticatedSubject, task_id: str, run_id: str, item_id: str, index: int,
    ) -> ChatContent:
        if index < 0:
            raise ResourceNotFoundError("chat content not found")
        identity, run = await self._task_runs.authorized_run_context(subject, task_id, run_id)
        with self._bind_identity(identity):
            events = await self._repository.get_item_events(identity.tenant_id, run_id, item_id)
            public = [event for event in events if event.audience is RunEventAudience.PUBLIC]
            if not public:
                raise ResourceNotFoundError("chat content not found")
            event = max(public, key=lambda event: event.sequence)
            _, blocks = await self._source(run, event)
            if index >= len(blocks):
                raise ResourceNotFoundError("chat content not found")
            block = blocks[index]
            embedded = _image(block)
            if embedded is not None:
                return embedded
            artifact_id, version_id = block.get("artifact_id"), block.get("version_id")
            if block.get("type") in {"image", "file"} and isinstance(artifact_id, str) and isinstance(version_id, str):
                download = await self._artifacts.download_version(
                    identity.tenant_id, artifact_id, version_id,
                )
                if download.artifact.task_id != task_id:
                    raise ResourceNotFoundError("chat content not found")
                return ChatContent(download.content, download.artifact.content_type)
        raise ResourceNotFoundError("chat content not found")

    async def stream(
        self, subject: AuthenticatedSubject, task_id: str, run_id: str, *, after_sequence: int = 0,
    ) -> AsyncIterator[str]:
        if after_sequence < 0:
            raise ValueError("invalid event cursor")
        identity, run = await self._task_runs.authorized_run_context(subject, task_id, run_id)
        cursor = after_sequence
        async with self.delta_bridge.subscribe(run_id) as subscription:
            while not subscription.closed:
                with self._bind_identity(identity):
                    events = await self._repository.list_events(identity.tenant_id, run_id, after_sequence=cursor, limit=100)
                    if events:
                        run = await self._repository.get_run(identity.tenant_id, run_id)
                        if run is None or run.task_id != task_id:
                            raise ResourceNotFoundError("run not found")
                    for event in events:
                        cursor = max(cursor, event.sequence)
                        if event.audience is not RunEventAudience.PUBLIC:
                            continue
                        if event.event_type == "runtime.activity.changed":
                            if (activity := _activity_payload(event)) is not None:
                                if event.attributes.get("call_id"):
                                    history = await self._repository.get_item_events(identity.tenant_id, run_id, activity.get("tool_item_id", activity["activity_id"]))
                                    items = await self._items(run, history, through_sequence=event.sequence)
                                    if items:
                                        activity["item"] = items[0]
                                yield _sse("activity", activity, sequence=event.sequence)
                        elif event.event_type == "task.title.updated":
                            task = await self._repository.get_task(identity.tenant_id, task_id)
                            if task is None:
                                raise ResourceNotFoundError("task not found")
                            yield _sse("task", self._public_task(task), sequence=event.sequence)
                        elif event.event_type.startswith(("chat.", "tool.call.", "feedback.accepted")) or event.attributes.get("source_kind") == "skill":
                            history = await self._repository.get_item_events(identity.tenant_id, run_id, event.item_id)
                            items = await self._items(
                                run,
                                tuple(item for item in history if item.sequence <= event.sequence),
                                through_sequence=event.sequence,
                            )
                            if items:
                                yield _sse("item", items[0], sequence=event.sequence)
                        else:
                            yield _sse("run", self._public_run(run), sequence=event.sequence)
                if len(events) == 100:
                    continue
                try:
                    delta = await asyncio.wait_for(subscription.queue.get(), timeout=0.5)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if delta is None:
                    return
                yield _sse("activity" if delta.get("kind") == "compression" else "text_delta", delta)

    async def _items(self, run: RunRecord, events: Any, *, through_sequence: int | None = None,
                     activity_events: Any = None) -> list[dict[str, Any]]:
        checkpoint_states: dict[tuple[str, str, str], GraphCheckpointState] = {}
        grouped: dict[str, list[RunEvent]] = {}
        for event in sorted(events, key=lambda event: event.sequence):
            if through_sequence is not None and event.sequence > through_sequence:
                continue
            if event.audience is RunEventAudience.PUBLIC and (
                event.event_type.startswith(("chat.", "tool.call.")) or event.event_type == "feedback.accepted" or event.attributes.get("source_kind") == "skill"
            ):
                grouped.setdefault(event.item_id, []).append(event)
        activities: dict[str, RunEvent] = {}
        if any(history[-1].attributes.get("source_kind") == "tool_request" for history in grouped.values()):
            observed = activity_events if activity_events is not None else await self._repository.get_item_events(
                run.owner_id, run.run_id, runtime_activity_item_id(run.run_id),
            )
            for event in observed:
                if event.event_type != "runtime.activity.changed":
                    continue
                call_id = event.attributes.get("call_id")
                if call_id and (through_sequence is None or event.sequence <= through_sequence):
                    activities[call_id] = event
        result = []
        for item_id, history in grouped.items():
            latest = history[-1]
            item: dict[str, Any] = {
                "item_id": item_id, "kind": "assistant", "role": "assistant",
                "text": "", "content": [], "status": str(latest.status),
                "sequence": history[0].sequence, "detail_available": True,
            }
            for event in history:
                source, blocks = await self._source(run, event, checkpoint_states=checkpoint_states)
                kind = event.attributes.get("source_kind", "message")
                if event.event_type == "feedback.accepted":
                    item.update(kind="user", role="user", feedback_id=event.attributes.get("feedback_id", event.item_id))
                    item["client_request_id"] = getattr(source, "client_request_id", None)
                elif kind == "tool_request" and isinstance(source, AIMessage):
                    call_id = event.attributes["call_id"]
                    call = next((call for call in source.tool_calls if call["id"] == call_id), None)
                    if call is None:
                        raise ResourceNotFoundError("saved tool call not found")
                    item.update(kind="tool", role="tool", call_id=call_id, main_message_id=event.attributes["message_id"], name=public_text(call["name"]), arguments=public_json(call["args"]))
                    blocks = []
                elif isinstance(source, ToolMessage):
                    if source.tool_call_id != event.attributes.get("call_id"):
                        raise ResourceNotFoundError("saved tool result identity differs")
                    envelope = _tool_result(source)
                    item.update(kind="tool", role="tool", call_id=source.tool_call_id)
                    item["result"] = public_json({key: value for key, value in envelope.items() if key in {"structuredContent", "isError"}})
                elif kind == "tool_rejection":
                    item.update(kind="tool", role="tool", call_id=event.attributes["call_id"], main_message_id=event.attributes["rejected_ai_message_id"], status="not_executed")
                elif kind == "compression":
                    item.update(kind="compression", role="system", result=public_json(source))
                elif kind == "card":
                    item.update(kind="card", role="assistant", result=public_json(source))
                elif isinstance(source, BaseMessage):
                    item["message_id"] = source.id
                    if isinstance(source, AIMessage):
                        phases = [
                            block.get("phase") for block in blocks
                            if block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str)
                        ]
                        if phases and phases[0] in ("commentary", "final_answer") and all(phase == phases[0] for phase in phases):
                            item["message_phase"] = phases[0]
                    elif isinstance(source, HumanMessage):
                        item.update(kind="user", role="user")
                    elif isinstance(source, SystemMessage):
                        item.update(role="system", status="not_executed")
                if item["kind"] == "tool" and event.attributes.get("effect") in ("READ", "WRITE"):
                    item["effect"] = event.attributes["effect"]
                if blocks or isinstance(source, ToolMessage):
                    item["content"] = await self._public_blocks(run, item_id, blocks)
                    item["text"] = "\n".join(block["text"] for block in item["content"] if block["type"] == "text")
                    if item["kind"] == "tool" and "result" in item:
                        item["result"]["content"] = item["content"]
            if item["kind"] == "tool":
                item["status_sequence"] = latest.sequence
            if latest.attributes.get("source_kind") == "tool_request":
                item["status"] = "queued"
                if (activity := activities.get(item["call_id"])) is not None:
                    phase, stage = activity.attributes.get("phase"), activity.attributes.get("stage")
                    item["status"] = "interrupted" if phase == "error" else "result_received" if stage == "tool_results" else "running"
                    item["status_sequence"] = activity.sequence
            result.append(item)
        return result

    async def _source(
        self, run: RunRecord, event: RunEvent, *,
        checkpoint_states: dict[tuple[str, str, str], GraphCheckpointState] | None = None,
    ) -> tuple[Any, list[dict[str, Any]]]:
        if event.event_type == "feedback.accepted":
            feedback_id = event.attributes.get("feedback_id", event.item_id)
            feedback = await self._feedback.get_feedback(run.owner_id, run.run_id, feedback_id)
            if feedback is None or feedback.run_id != run.run_id:
                raise ResourceNotFoundError("saved feedback not found")
            return feedback, _blocks(feedback.content)
        if not event.payload_ref or not event.payload_ref.startswith("chat-checkpoint:"):
            raise ResourceNotFoundError("chat source not found")
        checkpoint_id = event.payload_ref.removeprefix("chat-checkpoint:")
        namespace = event.attributes.get("checkpoint_ns", "")
        key = (run.thread_id, namespace, checkpoint_id)
        state = checkpoint_states.get(key) if checkpoint_states is not None else None
        if state is None:
            state = await self._runtime.get_checkpoint_state(
                thread_id=run.thread_id, checkpoint_id=checkpoint_id, checkpoint_ns=namespace, observe=False,
            )
        if state is None or state.target_run_id != run.run_id or (
            state.checkpoint.thread_id, state.checkpoint.checkpoint_id, state.checkpoint.checkpoint_ns
        ) != (run.thread_id, checkpoint_id, namespace):
            raise ResourceNotFoundError("saved chat checkpoint not found")
        if checkpoint_states is not None:
            checkpoint_states[key] = state
        values = state.values
        kind = event.attributes.get("source_kind")
        if kind == "compression":
            source = {name: values.get(name, "") for name in ("conversation_summary", "tool_chain_summary")}
            return source, []
        if kind == "card":
            reference = values.get("role_card_ref")
            if hasattr(reference, "model_dump"):
                reference = reference.model_dump(mode="json")
            if not isinstance(reference, Mapping) or (
                reference.get("artifact_id"), reference.get("version_id")
            ) != (event.attributes.get("artifact_id"), event.attributes.get("version_id")):
                raise ResourceNotFoundError("saved Card identity differs")
            download = await self._artifacts.download_version(run.owner_id, reference["artifact_id"], reference["version_id"])
            if download.artifact.task_id != run.task_id:
                raise ResourceNotFoundError("saved Card not found")
            source = {key: reference[key] for key in ("artifact_id", "version_id")}
            return source, [{"type": "text", "text": download.content.decode("utf-8")}]
        source = _message(values, event)
        if isinstance(source, SystemMessage) and source.additional_kwargs.get("source") != "tool_batch_rejection":
            raise ResourceNotFoundError("chat message is not public")
        return source, _blocks(_tool_result(source).get("content")) if isinstance(source, ToolMessage) else _blocks(source.content)

    async def _public_blocks(self, run: RunRecord, item_id: str, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for index, block in enumerate(blocks):
            kind = block.get("type")
            image = _image(block)
            url = f"/tasks/{quote(run.task_id, safe='')}/runs/{quote(run.run_id, safe='')}/items/{quote(item_id, safe='')}/content/{index}"
            if image is not None:
                result.append({"type": "image", "mimeType": image.media_type, "content_url": url})
            elif kind in {"image", "file"} and isinstance(block.get("artifact_id"), str) and isinstance(block.get("version_id"), str):
                metadata = await self._artifacts.get_version_metadata(run.owner_id, block["artifact_id"], block["version_id"])
                if metadata.task_id != run.task_id:
                    raise ResourceNotFoundError("chat attachment not found")
                selected = {"artifact_id": block["artifact_id"], "version_id": block["version_id"],
                            "filename": public_text(metadata.filename), "mimeType": metadata.content_type,
                            "content_type": metadata.content_type, "size_bytes": metadata.size_bytes}
                result.append({"type": kind, **selected, "content_url": url})
            elif kind in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
                result.append({"type": "text", "text": _display_text(block["text"])})
            elif kind == "resource_link":
                result.append({"type": kind, **public_json({key: block[key] for key in ("uri", "name", "title", "description", "mimeType") if key in block})})
            elif kind in {"image", "file"} and isinstance(block.get("url"), str) and not block["url"].startswith(("data:", "file:")):
                mime = block.get("mime_type")
                result.append({"type": "resource_link", "uri": public_text(block["url"]), "mimeType": public_text(mime) if isinstance(mime, str) else None})
            elif kind == "resource" and isinstance(block.get("resource"), Mapping):
                resource = block["resource"]
                result.append({"type": kind, "resource": public_json({key: resource[key] for key in ("uri", "mimeType", "text") if key in resource})})
        return result

    @staticmethod
    def _public_task(task: Any) -> dict[str, str]:
        return {"task_id": task.task_id, "title": public_text(task.title)}

    @staticmethod
    def _public_run(run: RunRecord) -> dict[str, Any]:
        return {
            "run_id": run.run_id, "task_id": run.task_id, "module": run.module,
            "stage_key": run.stage_key, "status": str(run.status),
            "pause_requested": run.pause_requested,
            "error": public_text(run.error) if run.status.value == "paused" and run.error else None,
            "created_at": run.created_at.isoformat(), "updated_at": run.updated_at.isoformat(),
        }


def _sse(event: str, data: Any, *, sequence: int | None = None) -> str:
    identity = f"id: {sequence}\n" if sequence is not None else ""
    return f"{identity}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


__all__ = ["ChatContent", "ChatProjectionService"]
