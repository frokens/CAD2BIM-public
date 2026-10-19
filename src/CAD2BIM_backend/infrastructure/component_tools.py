"""Component composition over current Runner MCP definitions and existing owners."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Annotated, Any

from fastmcp import FastMCP
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolArg, StructuredTool, ToolException
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
from CAD2BIM_backend.domain.control_plane import ConflictError, ResourceNotFoundError
from CAD2BIM_backend.domain.state_read_binding import STATE_RESOURCE_FIELDS
from CAD2BIM_backend.infrastructure.langgraph.node_read_binding import resolve_node_read_binding
from CAD2BIM_backend.infrastructure.langgraph.role_tools import RoleTool, RoleToolResult
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainMcpClient
from CAD2BIM_backend.infrastructure.mcp_state_client import RunnerStateMcpClient
from CAD2BIM_mcp.runner.references import project_runner_tool_declaration

# Role permission only: schemas, descriptions, effects and execution remain on
# their actual server definitions and Application owners.
_COMPONENT_TOOLS = frozenset({
    "ifc_geometry_resource_list", "ifc_geometry_resource_info",
    "ifc_geometry_resource_source_read", "ifc_geometry_resource_write",
    "ifc_geometry_preview", "ifc_geometry_query_python", "ifc_authoring_publish", "ifc_product_family_list",
    "ifc_product_family_info", "ifc_product_family_write", "ifc_product_realization_preview",
    "code_list", "code_read", "operation_list", "operation_get",
})


async def assemble_component_tools(
    server: FastMCP,
    *,
    domain_client: RunnerDomainMcpClient,
    state_client: RunnerStateMcpClient,
    artifacts: ArtifactService,
    grant_provider: Callable[..., Any],
    run_provider: Callable[..., Any],
    graph_runtime_provider: Callable[..., Any],
    skills: AgentSkills,
) -> tuple[Mapping[str, RoleTool], Callable[..., Any]]:
    """Assemble once; providers accept ``(config, runtime)`` and may be async."""

    async def provided(provider, config, runtime):
        value = provider(config, runtime)
        return await value if inspect.isawaitable(value) else value

    async def read_binding(invocation):
        run = await provided(run_provider, invocation.config, invocation.runtime)
        graph_runtime = await provided(graph_runtime_provider, invocation.config, invocation.runtime)
        return await resolve_node_read_binding(
            run=run, grant=invocation.grant, node_config=invocation.config,
            registered_graph_paths=("", "component_role"),
            field_owner_paths={field: "" for field in STATE_RESOURCE_FIELDS},
            graph_runtime=graph_runtime,
        )

    async def dispatch_saved_runner_call(request, _handler):
        invocation = request.runtime
        if invocation is None:
            raise RuntimeError("A Component tool requires its saved Role invocation")
        return await domain_client.call_tool(
            grant=invocation.grant, action_id=invocation.action_id,
            mcp_tool_name=request.name, domain_tool_name=request.name,
            arguments=request.args, effect=invocation.binding.effect,
        )

    bindings: dict[str, RoleTool] = {}
    # This is trusted bootstrap discovery, with no active request credential.
    # Runtime calls still use the protected HTTP clients and exact grants.
    declarations = await server.list_tools(run_middleware=False)
    for definition in declarations:
        if definition.name not in _COMPONENT_TOOLS:
            continue
        definition = project_runner_tool_declaration(definition)
        metadata = definition.meta or {}
        effect = metadata.get("effect")
        parallel_safe = metadata.get("parallel_safe")
        if effect not in ("READ", "WRITE") or type(parallel_safe) is not bool:
            raise ValueError(f"Controlled execution effect is absent on {definition.name}")
        tool = convert_mcp_tool_to_langchain_tool(
            None, definition.to_mcp_tool(),
            connection={"transport": "streamable_http", "url": domain_client.mcp_url},
            tool_interceptors=[dispatch_saved_runner_call],
            server_name="CAD2BIM Runner",
        )
        bindings[tool.name] = RoleTool(tool, effect, parallel_safe)
    missing = _COMPONENT_TOOLS - bindings.keys()
    if missing:
        raise ValueError(f"The current Runner server is missing Component tools: {', '.join(sorted(missing))}")

    async def role_card_read(artifact_id: str, version_id: str,
                             runtime: Annotated[Any, InjectedToolArg()]) -> tuple[str, dict[str, str]]:
        """Read an exact immutable Role or Stage Card from its ArtifactVersion reference.

        Use artifact_id and version_id from a saved Card reference. Returns its
        complete UTF-8 text and the actual reference. Invisible versions, versions
        outside this Task, and artifacts that are not Cards fail.
        """
        try:
            result = await artifacts.download_version(runtime.grant.scope.owner_id, artifact_id, version_id)
            if result.artifact.task_id != runtime.grant.scope.identity.task_id or result.artifact.kind not in ("role_card", "stage_card"):
                raise ToolException("The exact ArtifactVersion is not a Card in the current Task")
        except (ConflictError, ResourceNotFoundError) as exc:
            raise ToolException(str(exc)) from exc
        reference = {"artifact_id": result.artifact.artifact_id, "version_id": result.artifact.version_id}
        return json.dumps(reference, ensure_ascii=False) + "\n\n" + result.content.decode("utf-8"), reference

    async def state_resource_list(runtime: Annotated[Any, InjectedToolArg()]) -> str:
        """List authorized State resources and current tool contract resources with their actual URIs.

        Discover complete Geometry/Family program APIs, shared authoring/preview
        rules, and typed definitions here before constructing a business call.
        """
        binding = await read_binding(runtime)
        async with state_client.open_read_batch(grant=runtime.grant, binding=binding) as batch:
            resources = await batch.list_resources()
        return json.dumps([resource.model_dump(mode="json") for resource in resources], ensure_ascii=False)

    async def state_resource_read(uri: str, runtime: Annotated[Any, InjectedToolArg()]) -> str:
        """Read one State or tool contract URI returned by state_resource_list at this saved input.

        This uses the exact current node checkpoint binding. Unsupported or
        unauthorized URIs fail at the original State Resource owner.
        """
        binding = await read_binding(runtime)
        async with state_client.open_read_batch(grant=runtime.grant, binding=binding) as batch:
            return await batch.read_text(uri)

    for function in (role_card_read, state_resource_list, state_resource_read):
        tool = StructuredTool.from_function(
            coroutine=function, response_format="content_and_artifact" if function is role_card_read else "content",
            handle_tool_error=True, handle_validation_error=True,
        )
        bindings[tool.name] = RoleTool(tool, "READ", True)

    bindings[skills.read_tool.name] = RoleTool(skills.read_tool, "READ", True)

    async def executor(*, binding, call, action_id, state, config, runtime):
        if binding.tool is skills.read_tool:
            return await skills.aread(call, state=state, config=config, runtime=runtime)
        grant = await provided(grant_provider, config, runtime)
        invocation = SimpleNamespace(
            grant=grant, action_id=action_id, state=state, config=config,
            runtime=runtime, binding=binding,
        )
        injected_call = {**call, "args": {**call["args"], "runtime": invocation}}
        message = await binding.tool.ainvoke(injected_call, config=config)
        if not isinstance(message, ToolMessage):
            raise ValueError("The official tool adapter did not return a paired ToolMessage")
        return RoleToolResult(message=message)

    return bindings, executor


__all__ = ["assemble_component_tools"]
