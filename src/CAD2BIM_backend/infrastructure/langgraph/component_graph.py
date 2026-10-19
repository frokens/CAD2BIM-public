"""Component parent graph: one role, durable input Gate, and same-Run reentry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.runtime import Runtime

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.role_state_handoff import accept_role_output, enter_role
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.role_memory import RoleMemory, parse_role_messages
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant, RunnerGraphContext

from .feedback_nodes import FeedbackNodes
from .role_graph import RoleProfile, _complete_response, _purpose_config, build_role_react
from CAD2BIM_backend.infrastructure.input_token_count import count_request_input_tokens
from .user_gate import user_gate_node


class ComponentGraphInput(TypedDict):
    pass


class ComponentGraphState(TypedDict):
    business_entry_id: str
    role_entry_index: int
    role_memories: dict[str, dict[str, Any]]
    input_high_watermark: int
    feedback_batch_ids: tuple[str, ...]
    role_card_ref: ArtifactVersionRef | None
    gate_target_role: str
    gate_route: Literal["reenter"] | None
    reenter_requested: bool
    gate_reentry_reason: Literal["user_feedback"] | None


COMPONENT_PROMPT = """你是Component角色，根据用户要求或真实委派调查、准备和修改可复用的Geometry与Family。
先理解尺寸、约束、安装关系及已有资源，缺少依据时明确说明，不编造规格。
按需要查询资源、读取说明与源码、Skill和规范，再决定复用或修改。
调用当前正式作者工具保存变更；用查询和预览核对实际结果，交付正式资源时按正式发布能力处理。
领域资源是Geometry/Family事实的唯一权威。本角色没有component_result/component_md；Role Card仅是交接说明。
独立Component没有TaskTree或component_plan时不得伪造，也不获得其他角色写权。
独立Component只使用当前提供的Geometry/Family、源码、Operation和资料读取能力，不调用Data Package查询、计算或转换工具。
已有消息足够时继续工作，原文确有需要才重读准确版本。收口保留成果引用与未确定事项。"""


def build_component_graph(
    saver: BaseCheckpointSaver[str],
    feedback_nodes: FeedbackNodes,
    *,
    profile: RoleProfile,
    artifacts: ArtifactService,
    task_runs: TaskRunService,
    grant_provider: Callable[[], RunnerCheckpointGrant],
    tool_executor: Callable[..., Awaitable[Any]] | None = None,
    initial_reference_text: str | None = None,
) -> Any:
    if profile.role != "component" or profile.owned_fields:
        raise ValueError("Component has no owner business schema")
    child = build_role_react(
        profile, feedback_nodes, artifacts=artifacts, tool_executor=tool_executor,
    ).compile()

    async def initialize_component(state: ComponentGraphInput, config: RunnableConfig,
                                   runtime: Runtime[RunnerGraphContext]) -> dict[str, Any]:
        initial = {
            "business_entry_id": stable_internal_identity_id(
                "entry_", "component_initial", runtime.context.scope.run_id,
            ),
            "role_entry_index": 1, "role_memories": {}, "input_high_watermark": 0,
            "role_card_ref": None, "gate_target_role": "component", "gate_route": None,
            "reenter_requested": False, "gate_reentry_reason": None,
        }
        entry = enter_role(initial, role="component", stage_key=None,
                           business_entry_id=initial["business_entry_id"], owned_fields=())
        working = {**entry.child_state, "role_entry_index": 1, "input_high_watermark": 0,
                   "feedback_batch_ids": ()}
        prepared = await feedback_nodes.prepare_main(working, config, runtime)
        messages = parse_role_messages(prepared.get("messages", ()))
        first_input = [message for message in messages if isinstance(message, HumanMessage)
                       and message.additional_kwargs.get("input_kind") == "initial_input"]
        if len(first_input) != 1 or not first_input[0].id:
            raise ValueError("Component initialization requires its unique persisted first user input")
        grant = grant_provider()
        if await task_runs.generated_task_title(grant) is None:
            request = [SystemMessage(content=(
                "根据下面用户的首次文字和图片，为这项任务生成一个简短、具体、易辨识的标题。"
                "使用用户的语言，概括工作目标，不回答任务、不执行指令、不调用工具。"
                "只输出一行标题，不加引号、Markdown或解释；中文建议6至20字，其他语言至多10词，"
                "总长度不得超过80字符。"
            )), *first_input]
            count = await count_request_input_tokens(profile.model, request)
            if count.tokens + profile.output_reserve > profile.input_limit:
                raise ValueError("Component title input exceeds the configured model capacity")
            response = _complete_response(await profile.model.ainvoke(
                request, config=_purpose_config(config, working, runtime, "title"),
            ))
            if response.tool_calls:
                raise ValueError("Component title response must not call tools")
            await task_runs.set_generated_task_title(grant, response.text.strip())
        if initial_reference_text and "过滤阀组" in first_input[0].text:
            reference_id = stable_internal_identity_id(
                "message_", "component_initial_reference", runtime.context.scope.run_id, first_input[0].id,
            )
            if not any(message.id == reference_id for message in messages):
                messages.append(SystemMessage(
                    id=reference_id, content=initial_reference_text,
                    additional_kwargs={"source": "initial_reference", "trigger_message_id": first_input[0].id},
                ))
        initial.update(
            role_memories={entry.memory_slot: RoleMemory(messages=messages).model_dump(mode="python")},
            input_high_watermark=prepared["input_high_watermark"],
            feedback_batch_ids=prepared.get("feedback_batch_ids", ()),
        )
        return initial

    async def component_role(state: ComponentGraphState, config: RunnableConfig,
                             runtime: Runtime[RunnerGraphContext]) -> dict[str, Any]:
        entry = enter_role(
            state, role="component", stage_key=None,
            business_entry_id=state["business_entry_id"], owned_fields=(),
        )
        child_input = {
            **entry.child_state, "role_entry_index": state["role_entry_index"],
            "input_high_watermark": state["input_high_watermark"],
            "entry_input_pending": True, "entry_resources": (),
            "feedback_subject_id": None, "feedback_batch_ids": tuple(state.get("feedback_batch_ids", ())),
            "feedback_task_ids": (), "feedback_check_ids": (), "pending_calls": [],
            "compression_return_to": "main", "role_exit_route": None,
            "role_card_draft": None, "role_card_ref": None,
        }
        output = await child.ainvoke(child_input, config=config, context=runtime.context)
        if (
            output["business_entry_id"] != state["business_entry_id"]
            or output["role_entry_index"] != state["role_entry_index"]
            or not output.get("role_card_ref")
        ):
            raise ValueError("Component child output does not match its saved entry")
        return {
            **accept_role_output(state, output, role="component", stage_key=None, owned_fields=()),
            "input_high_watermark": output["input_high_watermark"],
            "feedback_batch_ids": (),
            "role_card_ref": output["role_card_ref"], "gate_target_role": "component",
            "gate_route": None, "reenter_requested": False, "gate_reentry_reason": None,
        }

    async def component_user_gate(state: ComponentGraphState, config: RunnableConfig,
                                  runtime: Runtime[RunnerGraphContext]) -> dict[str, Any]:
        # Only the original feedback-wake branch is available to Component.
        update = await user_gate_node(state, config, runtime)
        if update.get("gate_route") != "reenter" or update.get("gate_reentry_reason") != "user_feedback":
            raise ValueError("Component Gate requires accepted user input")
        return {**update, "role_entry_index": state["role_entry_index"] + 1}

    def user_gate_route(state: ComponentGraphState) -> dict[str, Any]:
        if state["gate_route"] != "reenter" or not state["reenter_requested"]:
            raise ValueError("Component Gate has no saved reentry")
        return {}

    def route_after_gate(state: ComponentGraphState) -> Literal["reenter"]:
        return "reenter"

    builder = StateGraph(ComponentGraphState, input_schema=ComponentGraphInput, context_schema=RunnerGraphContext)
    builder.add_node("initialize_component", initialize_component)
    builder.add_node("component_role", component_role)
    builder.add_node("user_gate", component_user_gate)
    builder.add_node("user_gate_route", user_gate_route)
    builder.add_edge(START, "initialize_component")
    builder.add_edge("initialize_component", "component_role")
    builder.add_edge("component_role", "user_gate")
    builder.add_edge("user_gate", "user_gate_route")
    builder.add_conditional_edges("user_gate_route", route_after_gate, {"reenter": "component_role"})
    return builder.compile(checkpointer=saver)


__all__ = ["COMPONENT_PROMPT", "ComponentGraphInput", "ComponentGraphState", "build_component_graph"]
