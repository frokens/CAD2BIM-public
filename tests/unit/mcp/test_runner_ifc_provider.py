from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from pydantic import ValidationError, TypeAdapter

from CAD2BIM_mcp.direct.ifc.model.contracts import IfcProjectUnitChangeInput
from CAD2BIM_mcp.direct.ifc.project.contracts import IfcProjectPatchInput
from CAD2BIM_mcp.runner import ifc_tools
from CAD2BIM_mcp.runner.ifc_contracts import (
    validate_runner_ifc_query,
    validate_runner_ifc_spatial_list,
)


_TOOLS = (
    "ifc_model_initialize", "ifc_model_info",
    "ifc_model_configure_geometry_basis", "ifc_representation_context_list",
    "ifc_project_update", "ifc_spatial_structure_list",
    "ifc_spatial_structure_apply", "ifc_query_python",
    "ifc_products_apply",
    "ifc_products_edit",
    "ifc_products_read",
)


def _walk(value):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


class _Backend:
    def __init__(self):
        self.calls = []

    async def call_ifc_domain(self, *, access_token, endpoint, body, timeout=None):
        self.calls.append((access_token, endpoint, body, timeout))
        if endpoint.endswith(("/model/info", "/model/initialize", "/model/geometry-basis/configure")):
            model = {
                "ifc_schema": "IFC4X3_ADD2",
                "project": {"global_id": "A" * 22, "name": "Plant"},
                "size_bytes": 320,
                "geometry_basis_configured": False, "geometry_basis": None,
                "site_count": 0, "element_count": 0,
            }
            if endpoint.endswith("/model/info"):
                return model
            return {"operation_alias": "op_001", "model": model}
        if endpoint.endswith("/project/update"):
            return {
                "operation_alias": "op_001",
                "project": {
                    "global_id": "A" * 22, "name": "Renamed",
                    "description": None, "object_type": None,
                    "long_name": None, "phase": None,
                },
            }
        if endpoint.endswith("/query/python"):
            return {
                "attempt_alias": "cur.example", "parent_attempt_alias": None,
                "source_sha256": "a" * 64, "status": "succeeded",
                "summary": "Returned 0 of 0 results.", "result_json": "[]",
                "next_cursor": None,
            }
        raise AssertionError(endpoint)


@pytest.mark.asyncio
async def test_runner_ifc_schema_is_closed_and_has_no_direct_identity():
    server = FastMCP("runner-ifc-contract")
    ifc_tools.register_runner_ifc_tools(server, _Backend())
    for name in _TOOLS:
        tool = await server.get_tool(name)
        assert tool is not None
        schema = tool.parameters
        Draft202012Validator.check_schema(schema)
        assert "direct_session_alias" not in str(schema)
        assert "source_path" not in str(schema)
        for node in _walk(schema):
            if isinstance(node, Mapping):
                assert not ({"allOf", "if", "then", "else", "not"} & node.keys())
                assert node.get("type") != "unknown"


async def test_runner_ifc_query_program_resource_reuses_current_api_and_valid_example():
    from fastmcp import Client
    from CAD2BIM_mcp.tool_docs import ifc_query as docs

    server = FastMCP("Runner IFC query program")
    ifc_tools.register_runner_ifc_tools(server, _Backend())
    async with Client(server) as client:
        assert docs.IFC_QUERY_PROGRAM_RESOURCE_URI in {
            str(resource.uri) for resource in await client.list_resources()}
        document = json.loads((await client.read_resource(docs.IFC_QUERY_PROGRAM_RESOURCE_URI))[0].text)
    assert document["usage"] == docs.IFC_QUERY_PROGRAM_USAGE
    assert document["return_contract"] == docs.IFC_QUERY_RETURN_CONTRACT
    example = json.loads(docs.IFC_QUERY_FRESH_EXAMPLE)
    example.pop("direct_session_alias")
    assert document["examples"]["ifc_only"] == example
    validate_runner_ifc_query(example)
    tool = await server.get_tool("ifc_query_python")
    Draft202012Validator(tool.parameters).validate(example)
    assert docs.IFC_QUERY_PROGRAM_RESOURCE_URI in tool.description
    assert "direct_session_alias" not in json.dumps(document)
    assert document["input_binding_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_runner_apply_and_recover_share_public_contract_and_preserve_original_reference(monkeypatch):
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    from CAD2BIM_mcp.direct.operation.application import project_ifc_apply_operation
    from CAD2BIM_mcp.direct.ifc.products.contracts import ProductsApplyInput
    result = project_ifc_apply_operation(_apply_inspection(),
        family_version_ref="cur.family", operation_ref="cur.original")
    seen = []

    class Backend:
        async def call_ifc_domain(self, **kwargs):
            seen.append(kwargs)
            return result.model_dump(mode="json")

    monkeypatch.setattr(ifc_tools, "request_domain_credential", lambda: "current-action-token")
    server = FastMCP("runner-product-apply")
    ifc_tools.register_runner_ifc_tools(server, Backend())
    requests = [dict(mode="apply", family_version_alias="cur.family", package_alias="data_001", payload_name="products"),
                dict(mode="recover", operation_ref="cur.original")]
    for request in requests:
        response = await server.call_tool("ifc_products_apply", request)
        assert response.structured_content["operation"]["operation_ref"] == "cur.original"
        assert response.structured_content["operation"]["primary_result"]["committed_revision"] == {
            "revision_no": 4, "size_bytes": 987654}
    assert [item["body"] for item in seen] == requests
    assert all(item["access_token"] == "current-action-token" for item in seen)
    assert all(item["endpoint"] == "/internal/domain/ifc/products/apply" for item in seen)
    for request in (dict(requests[1], package_alias="data_001"), dict(requests[0], direct_session_alias="direct_001")):
        with pytest.raises(ValidationError):
            TypeAdapter(ProductsApplyInput).validate_python(request)
    contents = await server.read_resource("cad2bim://contracts/ifc_products_apply/input")
    assert "logical_key_fields" in contents.contents[0].content
    assert "direct_session_alias" not in contents.contents[0].content


async def test_runner_product_read_forwards_closed_domain_request_as_read(monkeypatch):
    seen = []
    guid = "0000000000000000000000"

    class Backend:
        async def call_ifc_domain(self, **kwargs):
            seen.append(kwargs)
            return {"view": "current", "items": [{"global_id": guid,
                "apply_match": {"status": "not_checked"},
                "current": {"global_id": guid, "status": "not_found", "match_count": 0}}]}

    monkeypatch.setattr(ifc_tools, "request_domain_credential", lambda: "current-read-token")
    server = FastMCP("runner-product-read")
    ifc_tools.register_runner_ifc_tools(server, Backend())
    tool = await server.get_tool("ifc_products_read")
    result = await tool.fn(mode="from_ifc", global_ids=[guid], groups={"ports": {"mode": "names"}})
    assert result.items[0].apply_match.status == "not_checked"
    assert result.operation is None
    assert seen[0]["access_token"] == "current-read-token"
    assert seen[0]["endpoint"] == "/internal/domain/ifc/products/read"
    assert seen[0]["body"]["groups"]["ports"] == {"mode": "names"}
    assert "direct_session_alias" not in str(tool.parameters)
    assert tool.annotations.readOnlyHint is True
    assert tool.meta == {"effect": "READ", "parallel_safe": True}
    for value in ({"mode": "from_apply", "operation_ref": "cur.original", "product_keys": ["a"], "groups": {}},
        {"mode": "from_ifc", "global_ids": [guid], "source_path": "private"}):
        assert not Draft202012Validator(tool.parameters).is_valid(value)
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await server.call_tool("ifc_products_read", value)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_runner_ifc_tools_forward_current_token_and_domain_payload(monkeypatch):
    backend = _Backend()
    server = FastMCP("runner-ifc-projection")
    ifc_tools.register_runner_ifc_tools(server, backend)
    monkeypatch.setattr(ifc_tools, "request_domain_credential", lambda: "action-token")

    info = await (await server.get_tool("ifc_model_info")).fn()
    assert info.project.name == "Plant"
    assert info.size_bytes == 320
    updated = await (await server.get_tool("ifc_project_update")).fn(
        patch=IfcProjectPatchInput(name="Renamed"), clear_fields=(),
    )
    assert updated.operation_alias == "op_001"
    query = await (await server.get_tool("ifc_query_python")).fn(
        mode="fresh", code="def query(ctx):\n    return []\n",
    )
    assert query.result_json == "[]"
    assert all(call[0] == "action-token" for call in backend.calls)
    assert backend.calls[1][2] == {
        "command": {"patch": {"name": "Renamed"}, "clear_fields": []}
    }
    assert backend.calls[2][2]["mode"] == "fresh"
    assert "direct_session_alias" not in str(backend.calls)

    initialized = await (await server.get_tool("ifc_model_initialize")).fn(project_name="Plant")
    configured = await (await server.get_tool("ifc_model_configure_geometry_basis")).fn(
        project_unit_change=IfcProjectUnitChangeInput(length_unit={"name": "METRE"}),
    )
    assert initialized.model.size_bytes == configured.model.size_bytes == info.size_bytes


def test_runner_ifc_query_and_spatial_cursor_branches_are_exclusive():
    with pytest.raises(ValidationError):
        validate_runner_ifc_query({"mode": "fresh", "code": " "})
    with pytest.raises(ValidationError):
        validate_runner_ifc_query({"mode": "cursor", "cursor": "cur.page", "code": "x"})
    with pytest.raises(ValidationError):
        validate_runner_ifc_spatial_list({"cursor": "cur.page", "page_size": 10})
