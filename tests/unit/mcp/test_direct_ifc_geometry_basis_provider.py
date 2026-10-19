from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcRepresentationContextCatalog,
    IfcRepresentationContextSummary,
    IfcWorldCoordinateSystem3D,
)
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcLocalCoordinateReferenceChangeInput,
    IfcModelConfigureGeometryBasisResult,
    IfcModelInfoResult,
    IfcProjectResult,
    IfcRepresentationContextCatalogResult,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp


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
}


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _unconfigured_model() -> IfcModelInfoResult:
    return IfcModelInfoResult(
        ifc_schema="IFC4X3_ADD2",
        size_bytes=320,
        project=IfcProjectResult(global_id="0" * 22, name="Gas Network Project"),
        geometry_basis_configured=False,
        geometry_basis=None,
        site_count=0,
        element_count=0,
    )


async def test_i0b1_provider_schema_is_closed_native_visible_and_hides_server_identity() -> None:
    server = create_direct_mcp(Mock())
    tools = {tool.name: tool for tool in await server.list_tools()}
    configure = tools["ifc_model_configure_geometry_basis"]
    contexts = tools["ifc_representation_context_list"]

    def resolved(node):
        while "$ref" in node:
            assert node["$ref"].startswith("#/$defs/")
            node = configure.parameters["$defs"][node["$ref"].rsplit("/", 1)[-1]]
        return node

    assert configure.annotations.readOnlyHint is False
    assert configure.annotations.idempotentHint is False
    assert contexts.annotations.readOnlyHint is True
    assert "discriminator field action (never operation)" in configure.description
    assert "world_coordinate_system:{kind:3d, origin:[0,0,0]" in configure.description
    assert "parent_context_identifier:Model, target_view:MODEL_VIEW" in configure.description
    assert "submitted object shape only" in configure.description
    assert tuple(configure.parameters["properties"]) == (
        "direct_session_alias",
        "project_unit_change",
        "coordinate_reference_change",
        "context_operations",
    )
    assert tuple(configure.parameters["required"]) == ("direct_session_alias",)
    assert tuple(contexts.parameters["properties"]) == ("direct_session_alias",)
    assert "ctx" not in configure.parameters["properties"]
    coordinate_branches = [
        branch
        for branch in configure.parameters["properties"]["coordinate_reference_change"][
            "anyOf"
        ]
        if branch.get("type") != "null"
    ]
    assert [branch["properties"]["mode"]["const"] for branch in coordinate_branches] == [
        "local",
        "projected_crs",
    ]
    assert all("mode" in branch["required"] for branch in coordinate_branches)
    projected_crs = coordinate_branches[1]
    assert "IfcMapConversion.SourceCRS" in projected_crs["properties"][
        "source_context_identifier"
    ]["description"]
    context_branches = configure.parameters["properties"]["context_operations"][
        "items"
    ]["anyOf"]
    assert [branch["properties"]["action"]["const"] for branch in context_branches] == [
        "create_parent_context",
        "update_parent_context",
        "create_subcontext",
        "update_subcontext",
    ]
    assert all("action" in branch["required"] for branch in context_branches)
    parent_wcs = [
        resolved(branch)
        for branch in context_branches[0]["properties"]["world_coordinate_system"]["anyOf"]
    ]
    assert [branch["properties"]["kind"]["const"] for branch in parent_wcs] == [
        "2d",
        "3d",
    ]
    assert all("kind" in branch["required"] for branch in parent_wcs)
    project_units = configure.parameters["properties"]["project_unit_change"]["anyOf"][0]
    for unit_name in ("length_unit", "plane_angle_unit"):
        unit = resolved(project_units["properties"][unit_name]["anyOf"][0])
        assert "name" in unit["required"]
    for branch in context_branches:
        assert all(
            "description" in resolved(property_schema)
            for property_schema in branch["properties"].values()
        )

    for tool in (configure, contexts):
        for schema in (tool.parameters, tool.output_schema):
            assert schema is not None
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
    assert "oneOf" not in json.dumps(configure.parameters)

    async with Client(server) as client:
        native = {tool.name: tool for tool in await client.list_tools()}
    assert native[configure.name].inputSchema == configure.parameters
    assert native[configure.name].outputSchema == configure.output_schema
    assert native[contexts.name].inputSchema == contexts.parameters
    assert native[contexts.name].outputSchema == contexts.output_schema


async def test_i0b1_configure_schema_exposes_required_literals_for_every_input_branch() -> None:
    tools = {tool.name: tool for tool in await create_direct_mcp(Mock()).list_tools()}
    schema = tools["ifc_model_configure_geometry_basis"].parameters
    validator = Draft202012Validator(schema)
    alias = {"direct_session_alias": "direct_001"}
    parent = {
        "action": "create_parent_context",
        "context_identifier": "Model",
        "context_type": "Model",
        "coordinate_space_dimension": 3,
        "precision": 0.0001,
        "world_coordinate_system": {
            "kind": "3d",
            "origin": [0.0, 0.0, 0.0],
            "axis": [0.0, 0.0, 1.0],
            "ref_direction": [1.0, 0.0, 0.0],
        },
    }
    valid_payloads = (
        {
            **alias,
            "project_unit_change": {
                "length_unit": {"name": "METRE"},
                "plane_angle_unit": {"name": "RADIAN"},
            },
        },
        {**alias, "coordinate_reference_change": {"mode": "local"}},
        {
            **alias,
            "coordinate_reference_change": {
                "mode": "projected_crs",
                "source_context_identifier": "Model",
                "crs_name": "EPSG:27700",
                "map_unit": {"name": "METRE"},
                "eastings": 0.0,
                "northings": 0.0,
                "orthogonal_height": 0.0,
                "x_axis_abscissa": 1.0,
                "x_axis_ordinate": 0.0,
                "scale": 1.0,
            },
        },
        {**alias, "context_operations": [parent]},
        {
            **alias,
            "context_operations": [
                {
                    "action": "update_parent_context",
                    "context_identifier": "Model",
                    "precision": 0.00001,
                }
            ],
        },
        {
            **alias,
            "context_operations": [
                {
                    "action": "create_subcontext",
                    "context_identifier": "Body",
                    "context_type": "Model",
                    "parent_context_identifier": "Model",
                    "target_view": "MODEL_VIEW",
                }
            ],
        },
        {
            **alias,
            "context_operations": [
                {
                    "action": "update_subcontext",
                    "context_identifier": "Body",
                    "target_view": "NOTDEFINED",
                }
            ],
        },
    )
    for payload in valid_payloads:
        assert validator.is_valid(payload), list(validator.iter_errors(payload))

    invalid_payloads = (
        {**alias, "project_unit_change": {"length_unit": {}}},
        {**alias, "coordinate_reference_change": {}},
        {**alias, "context_operations": [{**parent, "action": None}]},
        {
            **alias,
            "context_operations": [
                {key: value for key, value in parent.items() if key != "action"}
            ],
        },
        {
            **alias,
            "context_operations": [
                {
                    **{key: value for key, value in parent.items() if key != "action"},
                    "operation": "create_parent_context",
                }
            ],
        },
        {
            **alias,
            "context_operations": [
                {
                    **parent,
                    "world_coordinate_system": {
                        key: value
                        for key, value in parent["world_coordinate_system"].items()
                        if key != "kind"
                    },
                }
            ],
        },
    )
    for payload in invalid_payloads:
        assert not validator.is_valid(payload)


async def test_i0b1_configure_uses_server_request_identity_and_preserves_explicit_command() -> None:
    result = IfcModelConfigureGeometryBasisResult(
        operation_alias="op_002",
        model=_unconfigured_model(),
    )
    application = SimpleNamespace(
        configure_ifc_geometry_basis=AsyncMock(return_value=result),
    )

    async with Client(create_direct_mcp(application)) as client:
        response = await client.call_tool(
            "ifc_model_configure_geometry_basis",
            {
                "direct_session_alias": "direct_001",
                "project_unit_change": {
                    "length_unit": {"name": "METRE", "prefix": "MILLI"},
                    "plane_angle_unit": {"name": "RADIAN"},
                },
                "coordinate_reference_change": {"mode": "local"},
                "context_operations": [
                    {
                        "action": "create_parent_context",
                        "context_identifier": "Model",
                        "context_type": "Model",
                        "coordinate_space_dimension": 3,
                        "precision": 0.00001,
                        "world_coordinate_system": {
                            "kind": "3d",
                            "origin": [0.0, 0.0, 0.0],
                            "axis": [0.0, 0.0, 1.0],
                            "ref_direction": [1.0, 0.0, 0.0],
                        },
                    }
                ],
            },
        )

    assert response.structured_content == result.model_dump(mode="json")
    alias, command = application.configure_ifc_geometry_basis.await_args.args
    assert alias == "direct_001"
    assert command == ConfigureIfcGeometryBasisCommand(
        project_unit_change=IfcProjectUnitChange(
            length_unit=IfcLengthUnit(name="METRE", prefix="MILLI"),
            plane_angle_unit=IfcPlaneAngleUnit(name="RADIAN"),
        ),
        coordinate_reference_change=IfcLocalCoordinateReferenceChange(mode="local"),
        context_operations=(
            CreateParentContextOperation(
                context_identifier="Model",
                context_type="Model",
                coordinate_space_dimension=3,
                precision=0.00001,
                world_coordinate_system=IfcWorldCoordinateSystem3D(
                    origin=(0.0, 0.0, 0.0),
                    axis=(0.0, 0.0, 1.0),
                    ref_direction=(1.0, 0.0, 0.0),
                ),
            ),
        ),
    )
    idempotency_key = application.configure_ifc_geometry_basis.await_args.kwargs[
        "idempotency_key"
    ]
    assert isinstance(idempotency_key, str)
    assert idempotency_key.strip() == idempotency_key
    assert idempotency_key


async def test_i0b1_empty_configure_is_rejected_before_application_dispatch() -> None:
    application = SimpleNamespace(configure_ifc_geometry_basis=AsyncMock())

    with pytest.raises(ToolError) as captured:
        async with Client(create_direct_mcp(application)) as client:
            await client.call_tool(
                "ifc_model_configure_geometry_basis",
                {"direct_session_alias": "direct_001"},
            )

    assert "INVALID_ARGUMENT" in str(captured.value)
    application.configure_ifc_geometry_basis.assert_not_awaited()


async def test_i0b1_configure_requires_a_real_server_request_identity() -> None:
    application = SimpleNamespace(configure_ifc_geometry_basis=AsyncMock())
    tools = {
        tool.name: tool
        for tool in await create_direct_mcp(application).list_tools()
    }

    with pytest.raises(ToolError) as captured:
        await tools["ifc_model_configure_geometry_basis"].fn(
            direct_session_alias="direct_001",
            ctx=SimpleNamespace(request_id=None),
            project_unit_change=None,
            coordinate_reference_change=IfcLocalCoordinateReferenceChangeInput(
                mode="local"
            ),
            context_operations=(),
        )

    assert "SYSTEM_PAUSED" in str(captured.value)
    application.configure_ifc_geometry_basis.assert_not_awaited()


async def test_i0b1_context_list_returns_only_sorted_provider_safe_catalog() -> None:
    catalog = IfcRepresentationContextCatalog(
        contexts=(
            IfcRepresentationContextSummary(
                context_identifier="Axis",
                kind="subcontext",
                context_type="Model",
                coordinate_space_dimension=3,
                parent_context_identifier="Model",
                target_view="GRAPH_VIEW",
                representation_count=2,
            ),
            IfcRepresentationContextSummary(
                context_identifier="Model",
                kind="parent_context",
                context_type="Model",
                coordinate_space_dimension=3,
                representation_count=0,
                precision=0.00001,
                world_coordinate_system=IfcWorldCoordinateSystem3D(
                    origin=(0.0, 0.0, 0.0),
                    axis=(0.0, 0.0, 1.0),
                    ref_direction=(1.0, 0.0, 0.0),
                ),
            ),
        )
    )
    application = SimpleNamespace(
        list_ifc_representation_contexts=AsyncMock(
            return_value=IfcRepresentationContextCatalogResult.model_validate(
                catalog.model_dump(mode="python")
            )
        )
    )

    async with Client(create_direct_mcp(application)) as client:
        response = await client.call_tool(
            "ifc_representation_context_list",
            {"direct_session_alias": "direct_001"},
        )

    assert [item.context_identifier for item in response.data.contexts] == ["Axis", "Model"]
    application.list_ifc_representation_contexts.assert_awaited_once_with("direct_001")
