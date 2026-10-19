from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from CAD2BIM_backend.infrastructure.langgraph.role_tools import RoleTool, RoleToolResult, execute_saved_calls
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainOutcomeUnknown


def _binding(name, effect="READ", parallel_safe=True):
    async def body(**arguments):
        return arguments
    return RoleTool(StructuredTool(name=name, description=name, args_schema={"type": "object"}, coroutine=body), effect, parallel_safe)


def _state(names):
    ai = AIMessage(id="saved-ai", content="proposal", tool_calls=[
        {"id": f"call-{index}", "name": name, "args": {"original": index}}
        for index, name in enumerate(names)
    ])
    return {
        "messages": [HumanMessage(id="user", content="original input"), ai],
        "pending_calls": [{"ai_message_id": ai.id, "tool_call_id": call["id"], "action_id": f"action-{call['id']}"} for call in ai.tool_calls],
    }


@pytest.mark.asyncio
async def test_parallel_read_results_follow_saved_order_and_keep_native_content():
    state = _state(["read-a", "read-b"])
    before = deepcopy(state)
    started = set()
    both_started = asyncio.Event()

    async def execute(*, binding, call, action_id, state, config, runtime):
        started.add(call["id"])
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        assert call["args"] == {"original": int(call["id"][-1])}
        assert action_id == f"action-{call['id']}"
        return ToolMessage(tool_call_id=call["id"], content=[{"type": "image", "base64": "aW1hZ2U=", "mime_type": "image/png"}], artifact={"structured_content": {"native": call["id"]}})

    update = await execute_saved_calls(state, {}, object(), {name: _binding(name) for name in ("read-a", "read-b")}, execute)
    assert [message.tool_call_id for message in update["messages"]] == ["call-0", "call-1"]
    assert update["messages"][0].artifact == {"structured_content": {"native": "call-0"}}
    assert update["messages"][0].content[0]["type"] == "image"
    assert all(message.additional_kwargs["tool_effect"] == "READ" for message in update["messages"])
    assert update["pending_calls"] == []
    assert state == before


@pytest.mark.asyncio
@pytest.mark.parametrize("names,bindings", [
    (["read", "write"], {"read": _binding("read"), "write": _binding("write", "WRITE")}),
    (["write", "write"], {"write": _binding("write", "WRITE")}),
    (["missing"], {}),
    (["safe", "unsafe"], {"safe": _binding("safe"), "unsafe": _binding("unsafe", parallel_safe=False)}),
])
async def test_rejection_removes_whole_unsent_ai_and_adds_one_stable_system_message(names, bindings):
    dispatched = []
    async def execute(**kwargs):
        dispatched.append(kwargs)
    state = _state(names)
    first = await execute_saved_calls(state, {}, None, bindings, execute)
    second = await execute_saved_calls(state, {}, None, bindings, execute)
    assert dispatched == []
    assert len(first["messages"]) == 2
    assert isinstance(first["messages"][0], RemoveMessage)
    rejection = first["messages"][1]
    assert isinstance(rejection, SystemMessage)
    assert rejection.id == second["messages"][1].id
    assert rejection.additional_kwargs == {"source": "tool_batch_rejection", "rejected_ai_message_id": "saved-ai", "rejected_tool_call_ids": [f"call-{index}" for index in range(len(names))]}
    assert "All calls were left unexecuted" in rejection.content
    assert first["pending_calls"] == []


@pytest.mark.asyncio
async def test_unknown_write_keeps_original_action_pending_and_replay_returns_only_owner_update():
    state = _state(["write"])
    before = deepcopy(state)
    attempts = []
    async def execute(*, call, action_id, runtime, **kwargs):
        attempts.append((action_id, deepcopy(call["args"]), runtime))
        if len(attempts) == 1:
            raise RunnerDomainOutcomeUnknown("unknown")
        return RoleToolResult(ToolMessage(tool_call_id=call["id"], content="confirmed"), {"requirement_md": "updated"})
    bindings = {"write": _binding("write", "WRITE")}
    with pytest.raises(RunnerDomainOutcomeUnknown):
        await execute_saved_calls(state, {}, "old-grant", bindings, execute)
    assert state == before
    update = await execute_saved_calls(state, {}, "current-grant", bindings, execute)
    assert attempts == [("action-call-0", {"original": 0}, "old-grant"), ("action-call-0", {"original": 0}, "current-grant")]
    assert update["requirement_md"] == "updated"
    assert update["pending_calls"] == []


@pytest.mark.asyncio
async def test_official_adapter_server_parameter_error_pairs_diagnostics_without_local_schema_check():
    received = []
    async def interceptor(request, handler):
        received.append(request.args)
        return CallToolResult(isError=True, content=[
            TextContent(type="text", text="Missing field count; guide=state://tool-guide/read"),
            ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
        ])
    official = convert_mcp_tool_to_langchain_tool(
        object(), Tool(name="read", description="read", inputSchema={"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"], "additionalProperties": False}),
        tool_interceptors=[interceptor],
    )
    state = _state(["read"])
    update = await execute_saved_calls(state, {}, None, {"read": RoleTool(official, "READ", True)})
    assert received == [{"original": 0}]
    message = update["messages"][0]
    assert message.tool_call_id == "call-0"
    assert message.status == "error"
    assert message.content[0]["text"].endswith("guide=state://tool-guide/read")
    assert message.content[1]["type"] == "image"


@pytest.mark.asyncio
async def test_component_binds_complete_current_runner_surface_and_closed_contract_resources(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastmcp import FastMCP
    from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
    from CAD2BIM_backend.infrastructure.chatgpt_model import create_chatgpt_plan_model
    from CAD2BIM_backend.infrastructure.component_tools import _COMPONENT_TOOLS, assemble_component_tools
    from CAD2BIM_backend.infrastructure.runner_domain_tokens import RunnerDomainTokenCodec
    from CAD2BIM_backend.infrastructure.state_access_tokens import StateAccessTokenCodec
    from CAD2BIM_mcp.runner.server import create_runner_state_mcp

    secret = b"component-tools-unit-secret-32-byte"
    skills = AgentSkills(tmp_path)
    await skills.discover()
    backend = SimpleNamespace(list_resources=AsyncMock(return_value=[]), authorize=AsyncMock())
    server = create_runner_state_mcp(codec=StateAccessTokenCodec(secret), domain_codec=RunnerDomainTokenCodec(secret),
                                     backend=backend)
    monkeypatch.setattr("CAD2BIM_mcp.runner.resources.request_state_credentials", lambda _codec: ("unit", "unit-binding"))
    bindings, _executor = await assemble_component_tools(
        server, domain_client=SimpleNamespace(mcp_url="http://unit/mcp"),
        state_client=SimpleNamespace(mcp_url="http://unit/mcp"), artifacts=object(),
        grant_provider=lambda *_args: None, run_provider=lambda *_args: None,
        graph_runtime_provider=lambda *_args: None, skills=skills,
    )
    expected = _COMPONENT_TOOLS | {"read_file", "role_card_read", "state_resource_list", "state_resource_read"}
    assert bindings.keys() == expected
    assert len(bindings) == 19
    assert not {"data_package_list", "data_describe_package", "data_get_package_description",
                "data_query_rows", "data_query_sql", "data_query_python", "data_read_json",
                "data_transform"} & bindings.keys()
    forbidden = _state(["data_query_python"])
    denied = await execute_saved_calls(forbidden, {}, None, bindings)
    assert isinstance(denied["messages"][1], SystemMessage)
    assert denied["pending_calls"] == []
    assert not {"ifc_project_update", "ifc_spatial_structure_apply", "history_message_read", "history_image_read"} & bindings.keys()
    writes = {"ifc_geometry_resource_write", "ifc_product_family_write", "ifc_authoring_publish",
              "ifc_geometry_preview", "ifc_product_realization_preview"}
    assert {name for name, binding in bindings.items() if binding.effect == "WRITE"} == writes
    assert all(not bindings[name].parallel_safe for name in writes)

    model = create_chatgpt_plan_model(lambda: "unit-no-network")
    provider_tools = model.bind_tools([binding.tool for binding in bindings.values()]).kwargs["tools"]
    payload = model._get_request_payload([HumanMessage(content="Contract projection only.")], tools=provider_tools)
    assert len(payload["tools"]) == 1 and payload["tools"][0]["type"] == "namespace"
    assert payload["tools"][0]["name"] == "component"
    declarations = payload["tools"][0]["tools"]
    assert {declaration["name"] for declaration in declarations} == expected

    # Reproduce the real P4 false-capacity boundary with the actual full tool
    # surface and a complete recent exchange. Schema JSON whitespace must not
    # consume the remaining window and force deletion of required history.
    from CAD2BIM_backend.infrastructure.langgraph.role_compression import compression_update
    class NoSummaryExpected:
        async def ainvoke(self, _request):
            raise AssertionError("This complete recent history fits without summarization")
    recent = [
        HumanMessage(id="initial", content="Keep the original input.",
                     additional_kwargs={"input_kind": "initial_input"}),
        AIMessage(id="recent", content="A recent observation. " * 4_400),
    ]
    assert await compression_update(
        {"messages": recent}, request_messages=[], provider_tools=provider_tools,
        input_limit=256_000, output_reserve=8_192,
        summary_model=NoSummaryExpected(), target="main",
    ) == {}

    def visit(value):
        if isinstance(value, dict):
            yield value
            for item in value.values():
                yield from visit(item)
        elif isinstance(value, list):
            for item in value:
                yield from visit(item)

    for declaration in declarations:
        encoded = json.dumps(declaration)
        assert "direct_session_alias" not in encoded
        assert "InjectedToolArg" not in encoded and '"runtime"' not in encoded
        for node in visit(declaration):
            assert not {"allOf", "if", "then", "else", "not"} & node.keys()
            assert node.get("type") != "unknown"

    resources = {str(resource.uri): json.loads(resource.text)
                 for resource in await server.list_resources(run_middleware=False)}
    assert "cad2bim://contracts/common/authoring" in resources
    assert "cad2bim://contracts/ifc_product_family_write/program_api" in resources
    assert "cad2bim://contracts/ifc_geometry_resource_write/program_api" in resources
    assert "cad2bim://contracts/data_query_python/program_api" in resources
    assert "direct_session_alias" not in json.dumps(resources)
    for document in resources.values():
        for node in visit(document):
            reference = node.get("$ref", "")
            if reference.startswith("cad2bim://"):
                uri, _, fragment = reference.partition("#")
                target = resources[uri]
                for part in fragment.removeprefix("/").split("/") if fragment else ():
                    target = target[part.replace("~1", "/").replace("~0", "~")]

    incomplete = FastMCP("missing-tool-unit")
    with pytest.raises(ValueError, match="missing Component tools"):
        await assemble_component_tools(
            incomplete, domain_client=SimpleNamespace(mcp_url="http://unit/mcp"),
            state_client=SimpleNamespace(mcp_url="http://unit/mcp"), artifacts=object(),
            grant_provider=lambda *_args: None, run_provider=lambda *_args: None,
            graph_runtime_provider=lambda *_args: None, skills=skills,
        )
