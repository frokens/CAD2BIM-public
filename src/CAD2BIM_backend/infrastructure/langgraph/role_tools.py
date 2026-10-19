"""Execute one checkpointed Role proposal through its controlled tool bindings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.tools import BaseTool

from CAD2BIM_backend.domain.role_memory import parse_role_messages


@dataclass(frozen=True, slots=True)
class RoleTool:
    """Execution policy kept beside the actual model-bindable tool definition."""

    tool: BaseTool
    effect: Literal["READ", "WRITE"]
    parallel_safe: bool = False


@dataclass(frozen=True, slots=True)
class RoleToolResult:
    """One invocation's result and its existing Application-owned update."""

    message: ToolMessage
    state_update: Mapping[str, Any] = field(default_factory=dict)


RoleToolExecutor = Callable[..., Awaitable[ToolMessage | RoleToolResult]]


def tool_batch_problem(calls: Sequence[ToolCall], bindings: Mapping[str, RoleTool]) -> str | None:
    """Check names and effects once; parameter validation belongs to each tool."""

    names = [call["name"] for call in calls]
    unauthorized = [name for name in names if name not in bindings]
    if unauthorized:
        return f"{len(unauthorized)} proposed tool call(s) are not allowed for this role."
    effects = [bindings[name].effect for name in names]
    if any(effect not in ("READ", "WRITE") for effect in effects):
        return "The batch contains a tool without an allowed READ or WRITE effect."
    if "WRITE" in effects and len(calls) != 1:
        return "A WRITE must be the only call in a batch; mixed reads/writes and multiple writes are forbidden."
    if len(calls) > 1 and any(not bindings[name].parallel_safe for name in names):
        return "A READ not declared parallel-safe must be called alone."
    return None


async def execute_saved_calls(
    state: Mapping[str, Any],
    config: Mapping[str, Any],
    runtime: Any,
    bindings: Mapping[str, RoleTool],
    executor: RoleToolExecutor | None = None,
) -> dict[str, Any]:
    """Return one atomic update; unknown outcomes raise without clearing pending."""

    pending = state["pending_calls"]
    if not pending:
        return {"pending_calls": [], "compression_return_to": "main"}
    ai_ids = {reference["ai_message_id"] for reference in pending}
    messages = parse_role_messages(state["messages"])
    proposals = [message for message in messages if message.id in ai_ids]
    if len(ai_ids) != 1 or len(proposals) != 1 or not isinstance(proposals[0], AIMessage):
        raise ValueError("Pending calls must reference one saved AIMessage")
    proposal = proposals[0]
    references = {reference["tool_call_id"]: reference for reference in pending}
    calls = proposal.tool_calls
    if (
        not proposal.id
        or proposal.invalid_tool_calls
        or len(references) != len(pending)
        or len(calls) != len(references)
        or {call["id"] for call in calls} != set(references)
    ):
        raise ValueError("Pending references do not cover the complete saved proposal")

    problem = tool_batch_problem(calls, bindings)
    if problem:
        controlled_names = [call["name"] if call["name"] in bindings else "[unapproved tool]" for call in calls]
        return {
            "messages": [
                RemoveMessage(id=proposal.id),
                SystemMessage(
                    id=f"tool-batch-rejection:{proposal.id}",
                    content=(
                        f"Rejected tool batch ({', '.join(controlled_names)}). "
                        f"{problem} All calls were left unexecuted. "
                        "Propose independent parallel-safe READs, one READ alone, or one WRITE alone."
                    ),
                    additional_kwargs={
                        "source": "tool_batch_rejection",
                        "rejected_ai_message_id": proposal.id,
                        "rejected_tool_call_ids": [call["id"] for call in calls],
                    },
                ),
            ],
            "pending_calls": [],
            "compression_return_to": "main",
        }

    async def invoke(call: ToolCall) -> RoleToolResult:
        binding = bindings[call["name"]]
        if executor is None:
            result = await binding.tool.ainvoke(call, config=config)
        else:
            result = await executor(
                binding=binding, call=call, action_id=references[call["id"]]["action_id"],
                state=state, config=config, runtime=runtime,
            )
        outcome = result if isinstance(result, RoleToolResult) else RoleToolResult(message=result)
        if not isinstance(outcome.message, ToolMessage) or outcome.message.tool_call_id != call["id"]:
            raise ValueError("A tool result must pair with its original saved call")
        return outcome

    # Every coroutine only returns its own result. An exception keeps the entire
    # checkpointed proposal pending; no partial result is presented as closed.
    results = await asyncio.gather(*(invoke(call) for call in calls), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    update: dict[str, Any] = {
        "messages": [], "pending_calls": [], "compression_return_to": "main",
    }
    for call, result in zip(calls, results, strict=True):
        if result.state_update:
            if bindings[call["name"]].effect != "WRITE":
                raise ValueError("A READ cannot replace business State fields")
            if {"messages", "pending_calls", "compression_return_to"} & result.state_update.keys():
                raise ValueError("Tool-owned updates cannot replace Role execution memory")
            update.update(result.state_update)
        update["messages"].append(result.message.model_copy(update={
            "id": f"tool-result:{call['id']}",
            "additional_kwargs": {**result.message.additional_kwargs, "tool_effect": bindings[call["name"]].effect},
        }))
    return update


__all__ = ["RoleTool", "RoleToolExecutor", "RoleToolResult", "execute_saved_calls", "tool_batch_problem"]
