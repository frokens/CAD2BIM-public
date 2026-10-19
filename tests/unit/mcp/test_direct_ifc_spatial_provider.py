from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.utilities.json_schema import dereference_refs
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.ifc_model import (
    IfcInvocationIdentityConflictError,
    IfcRunIdentity,
)
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    IfcProjectParentReference,
    IfcProjectWorldPlacement,
    IfcSpatialBlockerSummary,
    IfcSpatialCursorExpiredError,
    IfcSpatialNodeSummary,
    IfcSpatialNotEmptyError,
    IfcSpatialStructureListQuery,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.spatial.application import IfcSpatialStructureApplicationMixin
from CAD2BIM_mcp.direct.ifc.application_support import ifc_direct_error
from CAD2BIM_mcp.direct.ifc.spatial.contracts import (
    CreateIfcSiteOperationInput,
    IfcProjectParentInput,
    IfcProjectWorldPlacementInput,
    IfcSpatialOperationInput,
    IfcSpatialStructureApplyResult,
    IfcSpatialStructureListQueryInput,
    IfcSpatialStructureListResult,
    to_domain_spatial_apply_command,
)
from CAD2BIM_mcp.direct.ifc.spatial.tools import (
    register_ifc_spatial_structure_apply,
    register_ifc_spatial_structure_list,
)


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
    "step_id",
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
        name="IFC Spatial Provider Test",
        strict_input_validation=True,
        mask_error_details=True,
    )
    register_ifc_spatial_structure_list(server, application)
    register_ifc_spatial_structure_apply(server, application)
    return server


def _placement() -> IfcProjectWorldPlacementInput:
    return IfcProjectWorldPlacementInput(
        origin=(100.0, 200.0, 3.0),
        axis=(0.0, 0.0, 1.0),
        ref_direction=(1.0, 0.0, 0.0),
    )


def _node(global_id: str = "0" * 22) -> IfcSpatialNodeSummary:
    return IfcSpatialNodeSummary(
        global_id=global_id,
        ifc_class="IfcSite",
        spatial_class="site",
        name="Root Site",
        description=None,
        object_type=None,
        long_name=None,
        composition_type="ELEMENT",
        predefined_type=None,
        ref_latitude=None,
        ref_longitude=None,
        ref_elevation=None,
        elevation_with_flooring=None,
        parent_global_id="1" * 22,
        parent_ifc_class="IfcProject",
        project_world_placement=IfcProjectWorldPlacement(
            origin=(100.0, 200.0, 3.0),
            axis=(0.0, 0.0, 1.0),
            ref_direction=(1.0, 0.0, 0.0),
        ),
        blockers=IfcSpatialBlockerSummary(),
    )


def _apply_domain_result() -> SimpleNamespace:
    return SimpleNamespace(
        operation_alias="op_002",
        applied_count=1,
        applied=(
            SimpleNamespace(
                action="create",
                client_ref="root_site",
                target_global_id=None,
                global_id="0" * 22,
            ),
        ),
        spatial_summary=(_node(),),
    )


def _assert_safe_schema(schema: Mapping[str, object]) -> None:
    Draft202012Validator.check_schema(schema)
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert node != {}
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
    serialized = json.dumps(schema, sort_keys=True).lower()
    assert "unknown" not in serialized
    for name in _INTERNAL_NAMES:
        assert name not in serialized


async def test_i0b2_spatial_schemas_are_closed_native_visible_and_hides_internal_identity() -> None:
    server = _server(Mock())
    tools = {tool.name: tool for tool in await server.list_tools()}
    apply_tool = tools["ifc_spatial_structure_apply"]
    list_tool = tools["ifc_spatial_structure_list"]

    assert tuple(apply_tool.parameters["properties"]) == (
        "direct_session_alias",
        "operations",
    )
    operation_union = apply_tool.parameters["properties"]["operations"]["items"]
    assert len(operation_union["oneOf"]) == 5
    create = next(branch for branch in operation_union["oneOf"] if "oneOf" in branch)
    non_create = [branch for branch in operation_union["oneOf"] if "properties" in branch]
    assert {branch["properties"]["action"]["const"] for branch in non_create} == {
        "update",
        "reparent_preserve_world",
        "set_project_world_placement",
        "delete_if_empty",
    }
    assert len(create["oneOf"]) == 3
    spatial_classes = [branch["properties"]["spatial_class"] for branch in create["oneOf"]]
    assert {value["const"] for value in spatial_classes if "const" in value} == {"site", "space"}
    grouped_class = next(value for value in spatial_classes if "oneOf" in value)
    assert [value["const"] for value in grouped_class["oneOf"]] == [
        "building", "building_storey",
    ]
    for branch in create["oneOf"]:
        assert branch["properties"]["action"]["const"] == "create"
        assert len(branch["properties"]["parent"]["oneOf"]) == 3
    site_create = next(
        branch
        for branch in create["oneOf"]
        if branch["properties"]["spatial_class"].get("const") == "site"
    )
    space_create = next(
        branch
        for branch in create["oneOf"]
        if branch["properties"]["spatial_class"].get("const") == "space"
    )
    assert {"ref_latitude", "ref_longitude", "ref_elevation"} <= set(
        site_create["properties"]
    )
    assert "predefined_type" not in site_create["properties"]
    assert {"predefined_type", "elevation_with_flooring"} <= set(
        space_create["properties"]
    )
    assert "ref_latitude" not in space_create["properties"]
    assert "force" not in json.dumps(apply_tool.parameters).lower()
    assert "cascade" not in json.dumps(apply_tool.parameters).lower()
    assert "JSON numbers, not quoted numeric strings" in apply_tool.description
    assert '"origin":[0,0,0]' in apply_tool.description
    assert "current project length unit" in apply_tool.description
    assert "IFC_CURRENT_CONFLICT" in apply_tool.description
    assert "SYSTEM_PAUSED" in apply_tool.description
    assert "invalid or expired" in list_tool.description
    assert apply_tool.annotations.readOnlyHint is False
    assert apply_tool.annotations.destructiveHint is True

    branches = list_tool.parameters["anyOf"]
    assert tuple(branches[0]["properties"]) == (
        "direct_session_alias",
        "spatial_classes",
        "global_ids",
        "parent_global_id",
        "page_size",
    )
    assert tuple(branches[1]["properties"]) == ("direct_session_alias", "cursor")
    assert list_tool.annotations.readOnlyHint is True
    assert list_tool.annotations.idempotentHint is True
    for tool in (apply_tool, list_tool):
        _assert_safe_schema(tool.parameters)
        _assert_safe_schema(tool.output_schema)

    async with Client(server) as client:
        native = {tool.name: tool for tool in await client.list_tools()}
    for name, tool in tools.items():
        assert native[name].inputSchema == tool.parameters
        assert native[name].outputSchema == tool.output_schema
        assert native[name].description == tool.description


async def test_spatial_create_factoring_preserves_required_fields_and_domain_conversion() -> None:
    tool = await _server(Mock()).get_tool("ifc_spatial_structure_apply")
    original = dereference_refs(FunctionTool.from_function(tool.fn).parameters)
    before = Draft202012Validator(original)
    after = Draft202012Validator(tool.parameters)
    adapter = TypeAdapter(tuple[IfcSpatialOperationInput, ...])
    for spatial_class in ("site", "building", "building_storey", "space"):
        operation = {
            "action": "create", "client_ref": "new_node", "spatial_class": spatial_class,
            "parent": {"kind": "project"},
            "project_world_placement": _placement().model_dump(mode="json"),
            "name": " New Node ", "composition_type": "ELEMENT",
        }
        payload = {"direct_session_alias": "direct_001", "operations": [operation]}
        assert before.is_valid(payload) and after.is_valid(payload)
        command = to_domain_spatial_apply_command(adapter.validate_python(payload["operations"]))
        assert command.operations[0].spatial_class == spatial_class
        assert command.operations[0].name == "New Node"
        for key in ("action", "spatial_class", "parent", "project_world_placement"):
            missing = {name: value for name, value in operation.items() if name != key}
            candidate = {**payload, "operations": [missing]}
            assert not before.is_valid(candidate) and not after.is_valid(candidate)
        for extra in ({"unknown": 1}, {"spatial_class": "unsupported"}):
            candidate = {**payload, "operations": [{**operation, **extra}]}
            assert not before.is_valid(candidate) and not after.is_valid(candidate)
        if spatial_class in ("building", "building_storey"):
            for extra in ({"ref_latitude": 51.5}, {"elevation_with_flooring": 1.0}):
                candidate = {**payload, "operations": [{**operation, **extra}]}
                assert not before.is_valid(candidate) and not after.is_valid(candidate)


async def test_i0b2_spatial_provider_rejects_unknown_or_ambiguous_inputs_before_dispatch() -> None:
    application = SimpleNamespace(
        apply_ifc_spatial_structure=AsyncMock(),
        list_ifc_spatial_structure=AsyncMock(),
    )
    server = _server(application)
    invalid_apply = (
        {
            "direct_session_alias": "direct_001",
            "operations": [
                {"action": "delete_if_empty", "target_global_id": "0" * 22, "force": True}
            ],
        },
        {
            "direct_session_alias": "direct_001",
            "operations": [
                {
                    "action": "create",
                    "client_ref": "same",
                    "spatial_class": "site",
                    "parent": {"kind": "project"},
                    "project_world_placement": _placement().model_dump(),
                    "name": "A",
                    "composition_type": "ELEMENT",
                },
                {
                    "action": "create",
                    "client_ref": "same",
                    "spatial_class": "site",
                    "parent": {"kind": "project"},
                    "project_world_placement": _placement().model_dump(),
                    "name": "B",
                    "composition_type": "ELEMENT",
                },
            ],
        },
        {
            "direct_session_alias": "direct_001",
            "operations": [
                {
                    "action": "create",
                    "client_ref": "building_with_site_field",
                    "spatial_class": "building",
                    "parent": {"kind": "existing", "global_id": "0" * 22},
                    "project_world_placement": _placement().model_dump(),
                    "name": "Building",
                    "composition_type": "ELEMENT",
                    "ref_latitude": 51.5,
                }
            ],
        },
    )
    for payload in invalid_apply:
        with pytest.raises(ToolError):
            async with Client(server) as client:
                await client.call_tool("ifc_spatial_structure_apply", payload)

    for payload in (
        {
            "direct_session_alias": "direct_001",
            "spatial_classes": ["site"],
            "cursor": "cur.x",
        },
        {"direct_session_alias": "direct_001", "global_ids": ["short"]},
        {"direct_session_alias": "direct_001", "query": {}},
        {"direct_session_alias": "direct_001", "unexpected": True},
    ):
        with pytest.raises(ToolError):
            async with Client(server) as client:
                await client.call_tool("ifc_spatial_structure_list", payload)

    application.apply_ifc_spatial_structure.assert_not_awaited()
    application.list_ifc_spatial_structure.assert_not_awaited()


async def test_i0b2_spatial_apply_uses_server_request_identity_and_domain_command() -> None:
    projected = IfcSpatialStructureApplyResult.model_validate(
        {
            "operation_alias": "op_002",
            "applied_count": 1,
            "applied": [
                {
                    "action": "create",
                    "client_ref": "root_site",
                    "target_global_id": None,
                    "global_id": "0" * 22,
                }
            ],
            "spatial_summary": [_node().model_dump(mode="python")],
        }
    )
    application = SimpleNamespace(apply_ifc_spatial_structure=AsyncMock(return_value=projected))
    payload = {
        "direct_session_alias": "direct_001",
        "operations": [
            {
                "action": "create",
                "client_ref": "root_site",
                "spatial_class": "site",
                "parent": {"kind": "project"},
                "project_world_placement": _placement().model_dump(),
                "name": " Root Site ",
                "composition_type": "ELEMENT",
            }
        ],
    }

    async with Client(_server(application)) as client:
        response = await client.call_tool("ifc_spatial_structure_apply", payload)

    assert response.structured_content == projected.model_dump(mode="json")
    alias, command = application.apply_ifc_spatial_structure.await_args.args
    assert alias == "direct_001"
    assert command == ApplyIfcSpatialStructureCommand(
        operations=(
            CreateIfcSpatialOperation(
                client_ref="root_site",
                spatial_class="site",
                parent=IfcProjectParentReference(),
                project_world_placement=IfcProjectWorldPlacement(
                    origin=(100.0, 200.0, 3.0),
                    axis=(0.0, 0.0, 1.0),
                    ref_direction=(1.0, 0.0, 0.0),
                ),
                name="Root Site",
                composition_type="ELEMENT",
            ),
        )
    )
    request_id = application.apply_ifc_spatial_structure.await_args.kwargs["idempotency_key"]
    assert isinstance(request_id, str) and request_id


async def test_i0b2_spatial_list_dispatches_first_page_and_cursor_as_distinct_branches() -> None:
    result = IfcSpatialStructureListResult(items=(), returned_count=0, next_cursor=None)
    application = SimpleNamespace(list_ifc_spatial_structure=AsyncMock(return_value=result))

    async with Client(_server(application)) as client:
        await client.call_tool(
            "ifc_spatial_structure_list",
            {
                "direct_session_alias": "direct_001",
                "spatial_classes": ["site"],
                "page_size": 2,
            },
        )
        await client.call_tool(
            "ifc_spatial_structure_list",
            {"direct_session_alias": "direct_001", "cursor": "cur.exact"},
        )

    first, continuation = application.list_ifc_spatial_structure.await_args_list
    assert first.args == (
        "direct_001",
        IfcSpatialStructureListQuery(spatial_classes=("site",), page_size=2),
        None,
    )
    assert continuation.args == ("direct_001", None, "cur.exact")


async def test_i0b2_spatial_application_binds_scope_and_projects_saved_facts() -> None:
    identity = SimpleNamespace(task_id="task_001", tenant_id="tenant_001")
    invocation = SimpleNamespace(
        identity=identity,
        owner_id="owner@example.com",
        run_id="run_001",
    )
    service = SimpleNamespace(
        apply=AsyncMock(return_value=_apply_domain_result()),
        list=AsyncMock(
            return_value=SimpleNamespace(items=(_node(),), returned_count=1, next_cursor="cur.next")
        ),
    )
    bound: list[object] = []

    class Binder:
        def __enter__(self):
            bound.append(identity)

        def __exit__(self, *_args):
            return None

    class Application(IfcSpatialStructureApplicationMixin):
        _ifc_spatial_structure_service = service
        _scope_binder = staticmethod(
            lambda actual: Binder() if actual is identity else nullcontext()
        )

        async def _resolve_scope(self, _alias: str, _action: str):
            return invocation

    command = to_domain_spatial_apply_command(
        (
            CreateIfcSiteOperationInput(
                client_ref="root_site",
                spatial_class="site",
                parent=IfcProjectParentInput(),
                project_world_placement=_placement(),
                name="Root Site",
                composition_type="ELEMENT",
            ),
        )
    )
    application = Application()
    applied = await application.apply_ifc_spatial_structure(
        "direct_001", command, idempotency_key="request_001"
    )
    listed = await application.list_ifc_spatial_structure(
        "direct_001", IfcSpatialStructureListQuery(page_size=2), None
    )

    scope = IfcRunIdentity(
        owner_id="owner@example.com",
        tenant_id="tenant_001",
        task_id="task_001",
        run_id="run_001",
    )
    service.apply.assert_awaited_once_with(scope, command, idempotency_key="request_001")
    service.list.assert_awaited_once_with(
        scope,
        IfcSpatialStructureListQuery(page_size=2),
        None,
    )
    assert applied.operation_alias == "op_002"
    assert applied.spatial_summary[0].global_id == "0" * 22
    assert listed.returned_count == 1
    assert listed.next_cursor == "cur.next"
    assert bound == [identity, identity]


async def test_i0b2_spatial_application_uses_shared_safe_error_projection() -> None:
    identity = SimpleNamespace(task_id="task_001", tenant_id="tenant_001")
    invocation = SimpleNamespace(
        identity=identity,
        owner_id="owner@example.com",
        run_id="run_001",
    )
    service = SimpleNamespace(list=AsyncMock(side_effect=RuntimeError("internal path and ids")))

    class Application(IfcSpatialStructureApplicationMixin):
        _ifc_spatial_structure_service = service
        _scope_binder = staticmethod(lambda _identity: nullcontext())

        async def _resolve_scope(self, _alias: str, _action: str):
            return invocation

    with pytest.raises(DirectApplicationError) as captured:
        await Application().list_ifc_spatial_structure(
            "direct_001", IfcSpatialStructureListQuery(), None
        )

    assert captured.value.code == "SYSTEM_PAUSED"
    assert "internal path and ids" not in str(captured.value)


def test_i0b2_invocation_identity_conflict_is_not_reported_as_current_cas() -> None:
    projected = ifc_direct_error(
        IfcInvocationIdentityConflictError("durable identity reused with new request")
    )

    assert projected.code == "SYSTEM_PAUSED"
    assert projected.phase == "preflight"
    assert projected.retryable is False
    assert "identity reused" not in str(projected)


def test_i0b2_spatial_patch_and_query_contracts_reject_explicit_invalid_values() -> None:
    with pytest.raises(ValidationError):
        IfcSpatialStructureListQueryInput(spatial_classes=("site", "site"))
    with pytest.raises(ValidationError):
        TypeAdapter(IfcSpatialOperationInput).validate_python(
            {
                "client_ref": "site",
                "spatial_class": "site",
                "parent": {"kind": "project", "global_id": "0" * 22},
                "project_world_placement": _placement().model_dump(),
                "name": "Site",
                "composition_type": "ELEMENT",
            }
        )


def test_i0b2_spatial_errors_use_shared_safe_projection() -> None:
    blockers = IfcSpatialBlockerSummary(
        contained_element_count=3,
        sample_global_ids=("1" * 22,),
    )
    blocked = IfcSpatialNotEmptyError("0" * 22, blockers)
    blocked.operation_alias = "op_009"
    blocked.diagnostic = blockers.model_dump(mode="json")

    projected = ifc_direct_error(blocked)
    assert projected.code == "IFC_SPATIAL_NOT_EMPTY"
    assert projected.phase == "preflight"
    assert projected.operation_alias == "op_009"
    assert projected.diagnostic == blockers.model_dump(mode="json")

    expired = ifc_direct_error(IfcSpatialCursorExpiredError("expired"))
    assert expired.code == "CURSOR_EXPIRED"
    assert expired.phase == "read"
    assert expired.retryable is False
