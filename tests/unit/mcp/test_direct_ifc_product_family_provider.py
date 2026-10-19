from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator

from CAD2BIM_mcp.direct.ifc.product_family.contracts import (
    DirectIfcProductFamilyInfoInput,
    DirectIfcProductFamilyListCursorInput,
    DirectIfcProductFamilyListFirstInput,
    IfcProductFamilyCard,
    IfcProductFamilyInfoResult,
    IfcProductFamilyListResult,
    IfcProductFamilyRootInfoResult,
    IfcProductFamilyVersionCard,
    IfcProductFamilyVersionInfoResult,
)
from CAD2BIM_mcp.direct.ifc.product_family.application import (
    IfcProductFamilyApplicationMixin,
    _version_card as _project_version_card,
    _version_info,
)
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogListResult, ProductFamilyCatalogRejection,
    ProductFamilyCatalogVersionSummary,
)
from CAD2BIM_mcp.direct.ifc.product_family.tools import (
    IFC_PRODUCT_FAMILY_TOOL_REGISTRARS,
)
from CAD2BIM_mcp.tool_docs.ifc_product_family import (
    IFC_PRODUCT_FAMILY_GEOMETRY_PURPOSE_DESCRIPTION,
)
from tests.unit.backend.product_family_fixtures import current_family_contract


_TOOL_NAMES = {"ifc_product_family_list", "ifc_product_family_info"}
_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}
_INTERNAL_NAMES = {
    "family_id",
    "family_version_id",
    "geometry_resource_id",
    "geometry_resource_version_id",
    "artifact_id",
    "artifact_version_id",
    "storage_key",
    "storage_path",
    "tenant_id",
    "task_id",
    "run_id",
}
_FAMILY_ALIAS = "cur.family_ref_001"
_VERSION_ALIAS = "cur.family_version_ref_001"
_GEOMETRY_VERSION_ALIAS = "cur.geometry_version_ref_001"
_CURSOR = "cur.family_page_002"
_SHA = "a" * 64
_NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _assert_provider_safe_schema(schema: Mapping[str, object]) -> None:
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
        assert f'"{name}"' not in serialized


def _version_card() -> IfcProductFamilyVersionCard:
    return IfcProductFamilyVersionCard(
        family_version_alias=_VERSION_ALIAS,
        version_no=1,
        display_name="Regulator assembly",
        purpose="Connect two gas pipe interfaces",
        product_structure_kind="semantic_assembly",
        created_at=_NOW,
    )


def _family_card() -> IfcProductFamilyCard:
    return IfcProductFamilyCard(
        family_alias=_FAMILY_ALIAS,
        owner_scope="task",
        display_name="Regulator assembly",
        purpose="Connect two gas pipe interfaces",
        product_structure_kind="semantic_assembly",
        latest_family_version_alias=_VERSION_ALIAS,
        latest_version_no=1,
        latest_version_created_at=_NOW,
        created_at=_NOW,
    )


def _results() -> SimpleNamespace:
    listing = IfcProductFamilyListResult(
        families=(_family_card(),),
        complete=False,
        next_cursor=_CURSOR,
    )
    root = IfcProductFamilyInfoResult(
        info=IfcProductFamilyRootInfoResult(
            target_kind="family_root",
            family=_family_card(),
            versions=(_version_card(),),
            versions_complete=True,
            next_before_version_no=None,
        )
    )
    version = IfcProductFamilyInfoResult(
        info=IfcProductFamilyVersionInfoResult(
            target_kind="family_version",
            family_alias=_FAMILY_ALIAS,
            family_version_alias=_VERSION_ALIAS,
            parent_family_version_alias=None,
            owner_scope="task",
            version_no=1,
            display_name="Regulator assembly",
            purpose="Connect two gas pipe interfaces",
            usage_instructions='先读取 exact 合同。\n然后核对 "接口" 与 🙂。',
            product_structure_kind="semantic_assembly",
            family_contract_json=current_family_contract(
                binding_key="body",
                product_structure_kind="semantic_assembly",
                components=[
                    {"component_key": "assembly", "ifc_class": "IfcElementAssembly"},
                    {"component_key": "body", "ifc_class": "IfcValve", "parent_component_key": "assembly"},
                ],
            ).model_dump_json(by_alias=True),
            program_entry="main.py:realize",
            geometry_bindings=(
                {
                    "binding_key": "body",
                    "geometry_resource_version_alias": _GEOMETRY_VERSION_ALIAS,
                    "expected_resource_kind": "mapped_model",
                    "geometry_purpose": "product_component",
                },
            ),
            assets=(
                {
                    "asset_role": "realization_source_bundle",
                    "asset_format": "source_bundle_v1",
                    "filename": "source.zip",
                    "content_type": "application/zip",
                    "size_bytes": 128,
                },
            ),
            created_at=_NOW,
        )
    )
    return SimpleNamespace(listing=listing, root=root, version=version)


def test_exact_version_projection_preserves_full_multilingual_usage_body() -> None:
    prefix = '先读取 exact 合同。\n然后核对 "接口" 与 🙂。'
    usage = prefix + ("汉" * (8_192 - len(prefix)))
    item = SimpleNamespace(
        family_root_ref=_FAMILY_ALIAS,
        family_version_ref=_VERSION_ALIAS,
        parent_family_version_ref=None,
        owner_scope=SimpleNamespace(value="task"),
        version_no=1,
        display_name="Regulator assembly",
        purpose="Connect two gas pipe interfaces",
        usage_instructions=usage,
        product_structure_kind=SimpleNamespace(value="semantic_assembly"),
        family_contract=current_family_contract(
            binding_key="body",
            product_structure_kind="semantic_assembly",
            components=[
                {"component_key": "assembly", "ifc_class": "IfcElementAssembly"},
                {
                    "component_key": "body",
                    "ifc_class": "IfcValve",
                    "parent_component_key": "assembly",
                },
            ],
        ),
        contract_digest=_SHA,
        content_digest=_SHA,
        program_entry="main.py:realize",
        geometry_bindings=(
            SimpleNamespace(
                binding_key="body",
                geometry_resource_version_ref=_GEOMETRY_VERSION_ALIAS,
                geometry_content_digest=_SHA,
                expected_resource_kind=SimpleNamespace(value="mapped_model"),
                geometry_purpose=SimpleNamespace(value="product_component"),
            ),
        ),
        assets=(
            SimpleNamespace(
                asset_role="realization_source_bundle",
                asset_format="source_bundle_v1",
                filename="source.zip",
                content_type="application/zip",
                size_bytes=128,
                sha256=_SHA,
            ),
        ),
        created_at=_NOW,
    )

    projected = _version_info(item)
    serialized = projected.model_dump_json()

    assert projected.usage_instructions == usage
    assert json.loads(serialized)["usage_instructions"] == usage
    assert len(usage) == 8_192
    assert len(usage.encode("utf-8")) <= 32_768


def test_root_version_card_stays_compact_and_does_not_project_exact_usage() -> None:
    summary = ProductFamilyCatalogVersionSummary(
        family_version_ref=_VERSION_ALIAS,
        version_no=1,
        display_name="Regulator assembly",
        purpose="Connect two gas pipe interfaces",
        product_structure_kind="semantic_assembly",
        contract_digest=_SHA,
        content_digest=_SHA,
        created_at=_NOW,
    )

    card = _project_version_card(summary)

    assert "usage_instructions" not in card.model_dump(mode="json")




def _application() -> SimpleNamespace:
    results = _results()
    return SimpleNamespace(
        list_ifc_product_families=AsyncMock(return_value=results.listing),
        get_ifc_product_family_info=AsyncMock(side_effect=(results.root, results.version)),
    )


def _server(application: object) -> FastMCP:
    server = FastMCP(
        name="IFC ProductFamily Provider Test",
        strict_input_validation=True,
        mask_error_details=True,
    )
    for registrar in IFC_PRODUCT_FAMILY_TOOL_REGISTRARS:
        registrar(server, application)
    return server


async def test_rejected_family_page_remains_visible_and_can_continue_without_usable_cards() -> None:
    catalog = SimpleNamespace(list=AsyncMock(return_value=ProductFamilyCatalogListResult(
        families=(), returned_count=0, next_cursor="cur.next-page",
        rejections=(ProductFamilyCatalogRejection(
            family_version_ref="cur.rejected-version", version_no=1,
            code="PRODUCT_FAMILY_CONTRACT_INVALID", field_path="interfaces.0.position_rule.mode",
            reason="Input should be 'fixed'",
        ),),
    )))
    scope = SimpleNamespace(identity=object())

    class Application(IfcProductFamilyApplicationMixin):
        def _require_product_family_catalog_service(self):
            return catalog

        async def _product_family_read(self, alias, operation, action):
            assert (alias, operation) == ("direct_001", "list")
            return await action(scope)

    async with Client(_server(Application())) as client:
        result = await client.call_tool("ifc_product_family_list", {"direct_session_alias": "direct_001"})
    public = result.structured_content
    assert public["families"] == [] and public["complete"] is False
    assert public["next_cursor"] == "cur.next-page"
    assert public["rejections"] == [{
        "family_version_alias": "cur.rejected-version", "version_no": 1,
        "code": "PRODUCT_FAMILY_CONTRACT_INVALID", "field_path": "interfaces.0.position_rule.mode",
        "reason": "Input should be 'fixed'",
    }]


async def test_i0b305_product_family_schemas_are_closed_discriminated_and_native_visible() -> None:
    server = _server(_application())
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == _TOOL_NAMES

    list_branches = tools["ifc_product_family_list"].parameters["anyOf"]
    assert tuple(list_branches[0]["properties"]) == (
        "direct_session_alias",
        "owner_scopes",
        "product_structure_kinds",
        "purpose_contains",
        "page_size",
    )
    assert tuple(list_branches[1]["properties"]) == (
        "direct_session_alias",
        "cursor",
    )
    assert tuple(list_branches[0]["required"]) == ("direct_session_alias",)
    assert tuple(list_branches[1]["required"]) == ("direct_session_alias", "cursor")

    info_tool = tools["ifc_product_family_info"]
    assert tuple(info_tool.parameters["properties"]) == (
        "direct_session_alias",
        "target",
    )
    target_branches = info_tool.parameters["properties"]["target"]["anyOf"]
    assert [branch["properties"]["target_kind"]["const"] for branch in target_branches] == [
        "family_root",
        "family_version",
        "draft",
    ]
    assert tuple(target_branches[0]["properties"]) == (
        "target_kind",
        "family_alias",
        "before_version_no",
        "page_size",
    )
    assert tuple(target_branches[1]["properties"]) == (
        "target_kind",
        "family_version_alias",
    )
    assert "latest" not in {
        choice
        for branch in target_branches
        for choice in branch["properties"]["target_kind"].get("enum", ())
    }

    list_card = tools["ifc_product_family_list"].output_schema["properties"]["families"]["items"]
    assert "latest_family_version_alias" in list_card["properties"]
    assert {"contract_digest", "content_digest"}.isdisjoint(list_card["properties"])
    assert "family_version_alias" not in info_tool.parameters["properties"]
    info_branches = info_tool.output_schema["properties"]["info"]["anyOf"]
    assert [branch["properties"]["target_kind"]["const"] for branch in info_branches] == [
        "family_root",
        "family_version",
        "draft",
    ]
    assert set(target_branches[2]["properties"]) == {"target_kind", "draft_ref"}
    assert {"artifact_id", "snapshot", "source_files_json"}.isdisjoint(info_branches[2]["properties"])
    version_info = info_branches[1]
    root_info = info_branches[0]
    assert {"contract_digest", "content_digest"}.isdisjoint(
        root_info["properties"]["family"]["properties"]
    )
    assert {"contract_digest", "content_digest"}.isdisjoint(
        root_info["properties"]["versions"]["items"]["properties"]
    )
    assert {
        "contract_digest",
        "content_digest",
        "abi_profile",
        "runtime_profile",
        "budget_profile",
    }.isdisjoint(version_info["properties"])
    assert "geometry_content_digest" not in (
        version_info["properties"]["geometry_bindings"]["items"]["properties"]
    )
    assert "sha256" not in version_info["properties"]["assets"]["items"]["properties"]
    usage_schema = version_info["properties"]["usage_instructions"]
    assert usage_schema["anyOf"][0]["maxLength"] == 8_192
    assert version_info["properties"]["program_entry"]["const"] == "main.py:realize"
    assert version_info["properties"]["family_contract_json"]["type"] == "string"
    assert version_info["properties"]["family_contract_json"]["maxLength"] == 65_536
    assert version_info["properties"]["geometry_bindings"]["maxItems"] == 320
    assert version_info["properties"]["assets"]["maxItems"] == 4
    assert (
        version_info["properties"]["geometry_bindings"]["items"]["properties"]["geometry_purpose"][
            "description"
        ]
        == IFC_PRODUCT_FAMILY_GEOMETRY_PURPOSE_DESCRIPTION
    )

    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert "Multiple independent READ calls may be submitted together" in tool.description
        assert "Do not submit a WRITE in the same batch" in tool.description
        _assert_provider_safe_schema(tool.parameters)
        assert all(
            "title" not in node
            for node in _walk(tool.parameters)
            if isinstance(node, Mapping)
        )
        assert tool.output_schema is not None
        _assert_provider_safe_schema(tool.output_schema)

    assert "family_alias never means latest" in tools["ifc_product_family_list"].description
    assert "This tool has no mode field" in tools["ifc_product_family_list"].description
    assert '"owner_scopes":["task"]' in tools["ifc_product_family_list"].description
    assert '"cursor":"cur.product_family_page_001"' in (
        tools["ifc_product_family_list"].description
    )
    assert "There is no target=latest branch" in info_tool.description
    assert "exact family_version_alias" in info_tool.description

    async with Client(server) as client:
        native = {tool.name: tool for tool in await client.list_tools()}
    for name, tool in tools.items():
        assert native[name].inputSchema == tool.parameters
        assert native[name].outputSchema == tool.output_schema
        assert native[name].description == tool.description


async def test_i0b305_product_family_tools_dispatch_first_cursor_root_and_exact() -> None:
    application = _application()
    server = _server(application)

    async with Client(server) as client:
        first = await client.call_tool(
            "ifc_product_family_list",
            {
                "direct_session_alias": "direct_001",
                "owner_scopes": ["system", "task"],
                "product_structure_kinds": ["semantic_assembly"],
                "purpose_contains": "gas",
                "page_size": 25,
            },
        )
        cursor = await client.call_tool(
            "ifc_product_family_list",
            {"direct_session_alias": "direct_001", "cursor": _CURSOR},
        )
        root = await client.call_tool(
            "ifc_product_family_info",
            {
                "direct_session_alias": "direct_001",
                "target": {
                    "target_kind": "family_root",
                    "family_alias": _FAMILY_ALIAS,
                    "before_version_no": 3,
                    "page_size": 10,
                },
            },
        )
        version = await client.call_tool(
            "ifc_product_family_info",
            {
                "direct_session_alias": "direct_001",
                "target": {
                    "target_kind": "family_version",
                    "family_version_alias": _VERSION_ALIAS,
                },
            },
        )

    expected = _results()
    assert first.structured_content == expected.listing.model_dump(mode="json")
    assert cursor.structured_content == expected.listing.model_dump(mode="json")
    assert root.structured_content == expected.root.model_dump(mode="json")
    assert version.structured_content == expected.version.model_dump(mode="json")

    first_request = application.list_ifc_product_families.await_args_list[0].args[0]
    cursor_request = application.list_ifc_product_families.await_args_list[1].args[0]
    assert isinstance(first_request, DirectIfcProductFamilyListFirstInput)
    assert first_request.owner_scopes == ("system", "task")
    assert first_request.product_structure_kinds == ("semantic_assembly",)
    assert first_request.purpose_contains == "gas"
    assert first_request.page_size == 25
    assert isinstance(cursor_request, DirectIfcProductFamilyListCursorInput)
    assert cursor_request.cursor == _CURSOR

    root_request = application.get_ifc_product_family_info.await_args_list[0].args[0]
    version_request = application.get_ifc_product_family_info.await_args_list[1].args[0]
    assert isinstance(root_request, DirectIfcProductFamilyInfoInput)
    assert root_request.target.target_kind == "family_root"
    assert root_request.target.family_alias == _FAMILY_ALIAS
    assert root_request.target.before_version_no == 3
    assert root_request.target.page_size == 10
    assert version_request.target.target_kind == "family_version"
    assert version_request.target.family_version_alias == _VERSION_ALIAS


async def test_i0b305_product_family_provider_rejects_mixed_or_latest_targets() -> None:
    application = _application()
    server = _server(application)
    invalid_calls = (
        (
            "ifc_product_family_list",
            {
                "direct_session_alias": "direct_001",
                "cursor": _CURSOR,
                "owner_scopes": ["task"],
            },
        ),
        (
            "ifc_product_family_info",
            {
                "direct_session_alias": "direct_001",
                "target": {"target_kind": "latest", "family_alias": _FAMILY_ALIAS},
            },
        ),
        (
            "ifc_product_family_info",
            {
                "direct_session_alias": "direct_001",
                "target": {
                    "target_kind": "family_root",
                    "family_alias": _FAMILY_ALIAS,
                    "family_version_alias": _VERSION_ALIAS,
                },
            },
        ),
        (
            "ifc_product_family_info",
            {
                "direct_session_alias": "direct_001",
                "target": {
                    "target_kind": "family_version",
                    "family_version_alias": _VERSION_ALIAS,
                    "page_size": 20,
                },
            },
        ),
    )

    async with Client(server) as client:
        for name, payload in invalid_calls:
            with pytest.raises(ToolError):
                await client.call_tool(name, payload)

    application.list_ifc_product_families.assert_not_awaited()
    application.get_ifc_product_family_info.assert_not_awaited()
