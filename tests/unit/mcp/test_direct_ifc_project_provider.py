from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectNoChangeError,
    IfcProjectPatch,
    IfcProjectSummary,
    IfcProjectUpdateResult as DomainIfcProjectUpdateResult,
    UpdateIfcProjectCommand,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.project.application import IfcProjectApplicationMixin
from CAD2BIM_mcp.direct.ifc.project.contracts import (
    IfcProjectPatchInput,
    IfcProjectResult,
    IfcProjectUpdateResult,
    to_domain_project_update_command,
)
from CAD2BIM_mcp.direct.ifc.project.tools import register_ifc_project_update
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key


_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}
_INTERNAL_NAMES = {
    "ifc_session_alias",
    "model_id",
    "revision_id",
    "artifact_id",
    "artifact_version_id",
    "writer_id",
    "run_id",
    "task_id",
    "idempotency_key",
}


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _server(application: object) -> FastMCP:
    server = FastMCP(
        name="IFC Project Provider Test",
        strict_input_validation=True,
        mask_error_details=True,
    )
    register_ifc_project_update(server, application)
    return server


def _result() -> IfcProjectUpdateResult:
    return IfcProjectUpdateResult(
        operation_alias="op_002",
        project=IfcProjectResult(
            global_id="0" * 22,
            name="Gas Network",
            description="Authoritative model",
            object_type="Distribution Project",
            long_name="City Gas Distribution Network",
            phase=None,
        ),
    )


async def test_i0b2_project_schema_is_closed_native_visible_and_hides_internal_identity() -> None:
    server = _server(Mock())
    tools = {tool.name: tool for tool in await server.list_tools()}
    update = tools["ifc_project_update"]

    assert update.annotations.readOnlyHint is False
    assert update.annotations.idempotentHint is False
    assert tuple(update.parameters["properties"]) == (
        "direct_session_alias",
        "patch",
        "clear_fields",
    )
    assert tuple(update.parameters["required"]) == (
        "direct_session_alias",
        "patch",
    )
    assert "ctx" not in update.parameters["properties"]
    patch = update.parameters["properties"]["patch"]
    assert tuple(patch["properties"]) == (
        "name",
        "description",
        "object_type",
        "long_name",
        "phase",
    )
    assert patch["additionalProperties"] is False
    assert patch.get("required", []) == []
    assert all(
        property_schema.get("type") == "string" for property_schema in patch["properties"].values()
    )
    assert update.parameters["properties"]["clear_fields"]["items"]["enum"] == [
        "description",
        "object_type",
        "long_name",
        "phase",
    ]
    assert tuple(update.output_schema["properties"]) == ("operation_alias", "project")
    project = update.output_schema["properties"]["project"]
    assert tuple(project["properties"]) == (
        "global_id",
        "name",
        "description",
        "object_type",
        "long_name",
        "phase",
    )
    assert tuple(project["required"]) == (
        "global_id",
        "name",
        "description",
        "object_type",
        "long_name",
        "phase",
    )

    for schema in (update.parameters, update.output_schema):
        Draft202012Validator.check_schema(schema)
        for node in _walk(schema):
            if isinstance(node, Mapping):
                assert node != {}
                assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
                if node.get("type") == "object" and "properties" in node:
                    assert node.get("additionalProperties") is False
        serialized = json.dumps(schema, sort_keys=True).lower()
        assert "unknown" not in serialized
        for internal_name in _INTERNAL_NAMES:
            assert internal_name not in serialized

    async with Client(server) as client:
        native = {tool.name: tool for tool in await client.list_tools()}
    assert native[update.name].inputSchema == update.parameters
    assert native[update.name].outputSchema == update.output_schema
    assert native[update.name].description == update.description


async def test_i0b2_project_provider_rejects_unknown_null_and_illegal_clear_fields() -> None:
    with pytest.raises(ValidationError):
        IfcProjectPatchInput.model_validate({"global_id": "0" * 22})
    with pytest.raises(ValidationError):
        IfcProjectPatchInput.model_validate({"description": None})

    application = SimpleNamespace(update_ifc_project=AsyncMock())
    tools_server = _server(application)
    validator = Draft202012Validator((await tools_server.list_tools())[0].parameters)
    alias = {"direct_session_alias": "direct_001"}
    invalid_payloads = (
        {**alias, "patch": {"global_id": "0" * 22}},
        {**alias, "patch": {"description": None}},
        {**alias, "patch": {}, "clear_fields": ["name"]},
        {**alias, "patch": {}, "clear_fields": ["units_in_context"]},
        {**alias, "patch": {}, "unexpected": True},
    )
    for payload in invalid_payloads:
        assert not validator.is_valid(payload)
        with pytest.raises(ToolError):
            async with Client(tools_server) as client:
                await client.call_tool("ifc_project_update", payload)

    application.update_ifc_project.assert_not_awaited()


async def test_i0b2_project_cross_field_errors_stop_before_application_dispatch() -> None:
    application = SimpleNamespace(update_ifc_project=AsyncMock())

    for payload in (
        {"direct_session_alias": "direct_001", "patch": {}},
        {
            "direct_session_alias": "direct_001",
            "patch": {"description": "replacement"},
            "clear_fields": ["description"],
        },
        {
            "direct_session_alias": "direct_001",
            "patch": {},
            "clear_fields": ["phase", "phase"],
        },
    ):
        with pytest.raises(ToolError) as captured:
            async with Client(_server(application)) as client:
                await client.call_tool("ifc_project_update", payload)
        assert "INVALID_ARGUMENT" in str(captured.value)

    application.update_ifc_project.assert_not_awaited()


async def test_i0b2_project_uses_server_request_identity_and_preserves_explicit_command() -> None:
    result = _result()
    application = SimpleNamespace(update_ifc_project=AsyncMock(return_value=result))

    async with Client(_server(application)) as client:
        response = await client.call_tool(
            "ifc_project_update",
            {
                "direct_session_alias": "direct_001",
                "patch": {"name": "  Gas Network  ", "phase": "Design"},
                "clear_fields": ["description"],
            },
        )

    assert response.structured_content == result.model_dump(mode="json")
    alias, command = application.update_ifc_project.await_args.args
    assert alias == "direct_001"
    assert command == UpdateIfcProjectCommand(
        patch=IfcProjectPatch(name="Gas Network", phase="Design"),
        clear_fields=("description",),
    )
    request_id = application.update_ifc_project.await_args.kwargs["idempotency_key"]
    assert isinstance(request_id, str)
    assert request_id.strip() == request_id
    assert request_id


async def test_i0b2_project_requires_a_real_server_request_identity() -> None:
    application = SimpleNamespace(update_ifc_project=AsyncMock())
    tool = (await _server(application).list_tools())[0]

    with pytest.raises(ToolError) as captured:
        await tool.fn(
            direct_session_alias="direct_001",
            ctx=SimpleNamespace(request_id=None),
            patch=IfcProjectPatchInput(name="Gas Network"),
            clear_fields=(),
        )

    assert "SYSTEM_PAUSED" in str(captured.value)
    application.update_ifc_project.assert_not_awaited()


async def test_i0b2_write_identity_namespaces_reused_request_ids_by_mcp_session() -> None:
    first = server_ifc_invocation_key(
        SimpleNamespace(session_id="session_a", request_id="1")
    )
    replay = server_ifc_invocation_key(
        SimpleNamespace(session_id="session_a", request_id="1")
    )
    restarted = server_ifc_invocation_key(
        SimpleNamespace(session_id="session_b", request_id="1")
    )

    assert first == replay
    assert first != restarted
    assert first.startswith("mcp:")
    assert len(first) == len("mcp:") + 64


def test_i0b2_write_identity_requires_both_mcp_session_and_request() -> None:
    for context in (
        SimpleNamespace(session_id=None, request_id="1"),
        SimpleNamespace(session_id="session_a", request_id=None),
        SimpleNamespace(request_id="1"),
    ):
        with pytest.raises(DirectApplicationError) as captured:
            server_ifc_invocation_key(context)
        assert captured.value.code == "SYSTEM_PAUSED"


async def test_i0b2_project_application_binds_scope_and_projects_safe_result() -> None:
    identity = SimpleNamespace(task_id="task_001", tenant_id="tenant_001")
    invocation = SimpleNamespace(
        identity=identity,
        owner_id="owner@example.com",
        run_id="run_001",
    )
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=invocation))
    domain_result = DomainIfcProjectUpdateResult(
        operation_alias="op_002",
        project=IfcProjectSummary(
            global_id="0" * 22,
            name="Gas Network",
            description=None,
            object_type="Distribution Project",
            long_name=None,
            phase="Design",
        ),
    )
    service = SimpleNamespace(update=AsyncMock(return_value=domain_result))
    bound: list[object] = []

    class Binder:
        def __enter__(self):
            bound.append(identity)

        def __exit__(self, *_args):
            return None

    class Application(IfcProjectApplicationMixin):
        _ifc_project_service = service
        _scope_binder = staticmethod(
            lambda actual: Binder() if actual is identity else nullcontext()
        )

        async def _resolve_scope(self, alias: str, action: str):
            return await resolver.resolve(alias, command_id=f"command_direct_{action}_test")

    command = to_domain_project_update_command(
        IfcProjectPatchInput(object_type="Distribution Project", phase="Design"),
        (),
    )
    result = await Application().update_ifc_project(
        "direct_001",
        command,
        idempotency_key="mcp_request_001",
    )

    assert result == IfcProjectUpdateResult(
        operation_alias="op_002",
        project=IfcProjectResult(
            global_id="0" * 22,
            name="Gas Network",
            description=None,
            object_type="Distribution Project",
            long_name=None,
            phase="Design",
        ),
    )
    service.update.assert_awaited_once_with(
        IfcRunIdentity(
            owner_id="owner@example.com",
            tenant_id="tenant_001",
            task_id="task_001",
            run_id="run_001",
        ),
        command,
        idempotency_key="mcp_request_001",
    )
    assert bound == [identity]


async def test_i0b2_project_application_preserves_failed_operation_alias() -> None:
    identity = SimpleNamespace(task_id="task_001", tenant_id="tenant_001")
    invocation = SimpleNamespace(
        identity=identity,
        owner_id="owner@example.com",
        run_id="run_001",
    )
    error = IfcProjectNoChangeError("internal compiler detail")
    error.operation_alias = "op_009"
    service = SimpleNamespace(update=AsyncMock(side_effect=error))

    class Application(IfcProjectApplicationMixin):
        _ifc_project_service = service
        _scope_binder = staticmethod(lambda _identity: nullcontext())

        async def _resolve_scope(self, _alias: str, _action: str):
            return invocation

    with pytest.raises(DirectApplicationError) as captured:
        await Application().update_ifc_project(
            "direct_001",
            UpdateIfcProjectCommand(patch=IfcProjectPatch(name="Gas Network")),
            idempotency_key="mcp_request_001",
        )

    assert captured.value.code == "IFC_NO_CHANGES"
    assert captured.value.operation_alias == "op_009"
    assert "internal compiler detail" not in str(captured.value)
