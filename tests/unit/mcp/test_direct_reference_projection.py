from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_mcp.direct.references import DirectReferenceMiddleware
from CAD2BIM_mcp.direct.tool_support import DirectToolInputErrorMiddleware, register_provider_tool
from CAD2BIM_mcp.direct.ifc.product_family.write_tools import register_ifc_product_family_write
from CAD2BIM_mcp.direct.operation.tools import register_operation_get, register_operation_list
from CAD2BIM_mcp.direct.operation.contracts import OperationListResult
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from tests.unit.backend.test_opaque_reference_service import (
    MemoryReferenceIndex, reference_codec, reference_scope,
)
from tests.unit.mcp.test_direct_ifc_product_family_write_provider import (
    _definition_json, _result, _source_files, _USAGE as _FAMILY_USAGE,
)
from tests.unit.mcp.test_direct_ifc_geometry_authoring_provider import _USAGE as _GEOMETRY_USAGE


class ProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direct_session_alias: str
    resource_version_alias: str = Field(pattern=r"^cur\..+$")
    code: str = ""


class ProbeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_version_alias: str = Field(pattern=r"^cur\..+$")
    arbitrary_data: dict[str, str]
    code: str
    next_cursor: str = Field(pattern=r"^cur\..+$")


@pytest.mark.asyncio
async def test_provider_does_not_project_arbitrary_validator_value_errors():
    class SensitiveInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str

        @field_validator("text")
        @classmethod
        def reject_text(cls, value):
            raise ValueError(f"sensitive diagnostic {value}")

    calls = []

    async def inspect_text(text: str):
        calls.append(text)
        return {"text": text}

    server = FastMCP("Diagnostic privacy", strict_input_validation=False)
    register_provider_tool(server, inspect_text, name="diagnostic_privacy_probe",
                           description="Probe", input_type=SensitiveInput, output_type=dict[str, str])
    async with Client(server) as client:
        result = await client.call_tool("diagnostic_privacy_probe", {"text": "do_not_echo"}, raise_on_error=False)
    message = " ".join(item.text for item in result.content if item.type == "text")
    assert result.is_error and not calls
    assert "invalid_path=text; error_type=value_error" in message
    assert "reason=" not in message
    assert "sensitive diagnostic" not in message and "do_not_echo" not in message


def _setup():
    index, codec, scope = MemoryReferenceIndex(), reference_codec(), reference_scope()
    service = OpaqueReferenceService(index, codec)
    resolver = AsyncMock()
    resolver.resolve.return_value = SimpleNamespace(identity=scope)
    server = FastMCP("Reference projection", strict_input_validation=False, mask_error_details=True)
    server.add_middleware(DirectToolInputErrorMiddleware())
    server.add_middleware(DirectReferenceMiddleware(service, resolver, bind_tenant_execution_context))
    return server, index, codec, scope, service, resolver


@pytest.mark.asyncio
async def test_inspection_and_source_version_short_handles_roundtrip_without_changing_authority():
    server, index, codec, scope, service, resolver = _setup()
    inspection = codec.encode({"domain": "geometry_inspection_reference", "artifact_id": "exact-result"})
    source = codec.encode({"domain": "geometry_catalog_reference", "reference_kind": "resource_version"})
    seen = []

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
        direct_session_alias: str
        inspection_ref: str = Field(pattern=r"^cur\..+$")

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        inspection_ref: str = Field(pattern=r"^cur\..+$")
        source_version_ref: str = Field(pattern=r"^cur\..+$")

    async def observe(direct_session_alias: str, inspection_ref: str):
        seen.append(inspection_ref)
        return Output(inspection_ref=inspection_ref, source_version_ref=source)

    register_provider_tool(server, observe, name="ifc_geometry_preview", description="Observe retained result",
        input_type=Input, output_type=Output)
    async with Client(server) as client:
        first = await client.call_tool("ifc_geometry_preview", {"direct_session_alias": "direct_001", "inspection_ref": inspection})
        short = first.structured_content["inspection_ref"]
        second = await client.call_tool("ifc_geometry_preview", {"direct_session_alias": "direct_001", "inspection_ref": short})
    assert seen == [inspection, inspection]
    assert short.startswith("gi_") and len(short) == 25
    assert second.structured_content == first.structured_content
    assert first.structured_content["source_version_ref"].startswith("gv_")
    assert await service.expand(scope, short, ("gi",)) == inspection
    assert index.writes == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("nested_operation", [False, True])
@pytest.mark.parametrize("error_type", [ReferenceIndexError, ConnectionError])
async def test_output_reference_failure_keeps_completed_operation(nested_operation, error_type):
    server, index, codec, scope, service, resolver = _setup()
    reference = codec.encode({"domain": "geometry_catalog_reference", "reference_kind": "resource_version"})
    short = await service.shorten(scope, reference, ("gv",))

    class WriteReceipt(BaseModel):
        operation_alias: str
        resource_version_alias: str

    class NestedReceipt(BaseModel):
        operation: WriteReceipt

    completed = WriteReceipt(operation_alias="op_041", resource_version_alias=reference)
    dispatch = AsyncMock(return_value=NestedReceipt(operation=completed) if nested_operation else completed)
    saved = {}

    async def write(direct_session_alias: str, resource_version_alias: str, code: str = ""):
        saved[completed.operation_alias] = await dispatch()
        return saved[completed.operation_alias]

    class OperationInput(BaseModel):
        direct_session_alias: str
        operation_alias: str

    async def read_operation(direct_session_alias: str, operation_alias: str):
        return saved[operation_alias]

    register_provider_tool(server, write, name="ifc_geometry_resource_write", description="Write receipt probe",
        input_type=ProbeInput, output_type=NestedReceipt if nested_operation else WriteReceipt)
    register_provider_tool(server, read_operation, name="operation_get", description="Original receipt read",
        input_type=OperationInput, output_type=NestedReceipt if nested_operation else WriteReceipt)
    original_shorten = service.shorten
    service.shorten = AsyncMock(side_effect=error_type("private index details"))
    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("ifc_geometry_resource_write", {
                "direct_session_alias": "direct_001", "resource_version_alias": short,
            })
        service.shorten = original_shorten
        restored = await client.call_tool("operation_get", {
            "direct_session_alias": "direct_001", "operation_alias": completed.operation_alias,
        })
        receipt = restored.structured_content["operation"] if nested_operation else restored.structured_content
        assert receipt["operation_alias"] == completed.operation_alias
        assert receipt["resource_version_alias"] == short
    message = str(caught.value)
    assert "OUTPUT_REFERENCE_PROJECTION_FAILED" in message
    assert "phase=output_validation" in message and "operation_alias=op_041" in message
    assert "preflight" not in message and "private index details" not in message
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["source_read", "source_read_legacy", "mapped_create", "function_create", "mapped_preview", "function_preview", "family_preview"])
async def test_asset_input_callable_receives_expanded_catalog_references(case):
    """Use actual registered callables, not only DTO/Application validation."""
    from CAD2BIM_mcp.direct.ifc.geometry.tools import register_ifc_geometry_resource_source_read, register_ifc_geometry_preview
    from CAD2BIM_mcp.direct.ifc.geometry.authoring_tools import register_ifc_geometry_resource_write
    from CAD2BIM_mcp.direct.ifc.product_family.preview_tools import register_ifc_product_realization_preview

    server, _, codec, scope, service, _ = _setup()
    version = codec.encode({"domain":"geometry_catalog_reference", "reference_kind":"resource_version", "exact":"body"})
    short = await service.shorten(scope, version, ("gv",))
    observed = AsyncMock(side_effect=DirectApplicationError("OBSERVED_TYPED_APPLICATION_BOUNDARY", "probe", phase="read"))
    application = SimpleNamespace(read_ifc_geometry_resource_source=observed, write_ifc_geometry_resource=observed,
        preview_ifc_geometry=observed, preview_ifc_product_realization=observed)
    request = {"direct_session_alias":"direct_001"}
    if case.startswith("source_read"):
        register_ifc_geometry_resource_source_read(server, application)
        tool = "ifc_geometry_resource_source_read"
        request.update(resource_version_alias=version if case=="source_read_legacy" else short, source_path="README.md", view_mode="full")
    elif case.endswith("create"):
        register_ifc_geometry_resource_write(server, application)
        tool = "ifc_geometry_resource_write"
        request.update(mode="create",display_name="Derived body",usage_instructions={"action":"set","value":_GEOMETRY_USAGE},
            source={"source_kind":"mapped_model" if case=="mapped_create" else "modeling_function",
                    "source_files":[{"source_path":"main.py","source":"def build(ctx): return ctx.geometry('body')"}],
                    "geometry_inputs":[{"input_key":"body","resource_version_alias":short}]})
        if case=="function_create":
            from tests.unit.mcp.test_direct_ifc_geometry_authoring_provider import _DIAMETER_SCHEMA_JSON
            request["source"]["parameter_schema_json"] = json.dumps({"action":"set","value":json.loads(_DIAMETER_SCHEMA_JSON)})
    else:
        request["reference_geometries"]=[{"reference_key":"original","resource_version_alias":short,"datum_transform":{"translation_m":[1,2,3]}}]
        if case=="family_preview":
            register_ifc_product_realization_preview(server, application)
            tool="ifc_product_realization_preview"
            family=codec.encode({"domain":"product_family_catalog_reference","reference_kind":"family_version","exact":"family"})
            request.update(mode="single",attachment_kind="self_geometry",source={"source_kind":"published","family_version_alias":family},
                invocation_json=json.dumps({"public_parameters":{},"product_origin":[0,0,0],"length_unit":{"name":"METRE"}}))
        else:
            register_ifc_geometry_preview(server, application)
            tool="ifc_geometry_preview"
            request.update(mode="single",source={"source_kind":"published","resource_version_alias":short})
            if case == "function_preview":
                request["sample_parameters_json"] = "{}"
    async with Client(server) as client:
        with pytest.raises(ToolError,match="OBSERVED_TYPED_APPLICATION_BOUNDARY"):
            await client.call_tool(tool,request)
    observed.assert_awaited_once()
    typed=observed.await_args.args[0]
    if case.startswith("source_read"):
        assert typed.resource_version_alias == version
    elif case.endswith("create"):
        assert typed.source.geometry_inputs[0].resource_version_alias==version
        assert typed.source.geometry_inputs[0].input_key=="body"
    else:
        assert typed.reference_geometries[0].resource_version_alias==version
        assert typed.reference_geometries[0].datum_transform.translation_m==(1,2,3)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["wrong_type", "other_scope"])
async def test_source_read_rejects_wrong_or_invisible_short_reference_before_application(invalid_kind):
    from CAD2BIM_mcp.direct.ifc.geometry.tools import register_ifc_geometry_resource_source_read
    server, _, codec, scope, service, _ = _setup()
    claims={"domain":"geometry_catalog_reference","reference_kind":"resource_root" if invalid_kind=="wrong_type" else "resource_version","exact":"private"}
    legacy=codec.encode(claims)
    short=await service.shorten(scope if invalid_kind=="wrong_type" else scope.model_copy(update={"task_id":"other-task"}),legacy,("gr",) if invalid_kind=="wrong_type" else ("gv",))
    application=SimpleNamespace(read_ifc_geometry_resource_source=AsyncMock())
    register_ifc_geometry_resource_source_read(server,application)
    async with Client(server) as client:
        with pytest.raises(ToolError,match="INVALID_ARGUMENT" if invalid_kind=="wrong_type" else "REFERENCE_INVALID"):
            await client.call_tool("ifc_geometry_resource_source_read",{"direct_session_alias":"direct_001","resource_version_alias":short,"source_path":"README.md","view_mode":"full"})
    application.read_ifc_geometry_resource_source.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["geometry", "family"])
async def test_exact_inspect_cannot_replace_reference_layer_at_registered_callable(kind):
    from CAD2BIM_mcp.direct.ifc.geometry.tools import register_ifc_geometry_preview
    from CAD2BIM_mcp.direct.ifc.product_family.preview_tools import register_ifc_product_realization_preview
    server, _, codec, _, _, _ = _setup()
    application=SimpleNamespace(preview_ifc_geometry=AsyncMock(),preview_ifc_product_realization=AsyncMock())
    request={"direct_session_alias":"direct_001","reference_geometries":[]}
    capability=codec.encode({"domain":"geometry_preview_capability_v2","target_kind":"geometry_resource" if kind=="geometry" else "product_realization_preview","exact":"snapshot"})
    if kind=="geometry":
        register_ifc_geometry_preview(server,application)
        tool = "ifc_geometry_preview"
        request.update(preview_kind="inspect", geometry_preview_capability=capability)
    else:
        register_ifc_product_realization_preview(server,application)
        tool = "ifc_product_realization_preview"
        request.update(mode="inspect", product_realization_preview_capability=capability)
    async with Client(server) as client:
        with pytest.raises(ToolError,match="INVALID_ARGUMENT"):
            await client.call_tool(tool,request)
    application.preview_ifc_geometry.assert_not_awaited()
    application.preview_ifc_product_realization.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_reference_tools_have_the_existing_projection_registration():
    from unittest.mock import Mock
    from CAD2BIM_mcp.direct.server import create_direct_mcp
    from CAD2BIM_mcp.direct.references import REFERENCE_TOOL_NAMES, reference_schema_projection
    from CAD2BIM_mcp.direct.tool_support import ProviderFunctionTool
    from CAD2BIM_mcp.direct.provider_schema import provider_output_schema
    from CAD2BIM_mcp.direct.ifc.geometry.contracts import IfcGeometryPreviewOutcomeResult
    from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import IfcProductRealizationPreviewResult

    server = create_direct_mcp(Mock())
    for name in REFERENCE_TOOL_NAMES:
        tool = await server.get_tool(name)
        assert isinstance(tool, ProviderFunctionTool), name
        assert tool._reference_input_paths == reference_schema_projection(tool.parameters)[1]
        schema = tool.output_schema
        if name in {"ifc_geometry_preview", "ifc_product_realization_preview"}:
            assert schema is None
            result_type = (IfcGeometryPreviewOutcomeResult if name == "ifc_geometry_preview"
                           else IfcProductRealizationPreviewResult)
            schema = provider_output_schema(result_type, compact=True, preserve_references=True)
        assert tool._reference_output_paths == reference_schema_projection(schema)[1]


@pytest.mark.asyncio
async def test_operation_directory_and_read_dispatch_through_real_reference_middleware():
    server, _, _, _, _, _ = _setup()
    application = SimpleNamespace(
        list_operations=AsyncMock(return_value=OperationListResult(operations=(), complete=True, next_before_sequence=None)),
        get_operation=AsyncMock(side_effect=DirectApplicationError("OPERATION_NOT_FOUND", "No visible Operation", phase="read")),
    )
    register_operation_list(server, application)
    register_operation_get(server, application)
    async with Client(server) as client:
        result = await client.call_tool("operation_list", {"direct_session_alias": "direct_001"})
        assert result.structured_content == {"operations": [], "complete": True, "next_before_sequence": None}
        with pytest.raises(ToolError, match="OPERATION_NOT_FOUND.*phase=read"):
            await client.call_tool("operation_get", {"direct_session_alias": "direct_001", "operation_alias": "op_001"})
    application.list_operations.assert_awaited_once_with("direct_001", None, 20, None, tool_name=None, outcome=None)
    assert application.get_operation.await_args.args == ("direct_001",)
    assert application.get_operation.await_args.kwargs["selection"].operation_alias == "op_001"


@pytest.mark.asyncio
async def test_real_fastmcp_schema_projection_preserves_sources_data_and_cursors():
    server, index, codec, scope, service, resolver = _setup()
    legacy = codec.encode({"domain":"geometry_catalog_reference", "reference_kind":"resource_version", "exact":"v1"})
    cursor = codec.encode({"domain":"geometry_catalog_list_cursor", "page":1})
    received = []

    async def inspect_resource(direct_session_alias: str, resource_version_alias: str, code: str = ""):
        received.append(resource_version_alias)
        return ProbeOutput(resource_version_alias=resource_version_alias,
                           arbitrary_data={"resource_version_alias": legacy}, code=code, next_cursor=cursor)

    register_provider_tool(server, inspect_resource, name="ifc_geometry_resource_info",
                           description="Probe", input_type=ProbeInput, output_type=ProbeOutput)
    async with Client(server) as client:
        tool = next(t for t in await client.list_tools() if t.name == "ifc_geometry_resource_info")
        first = await client.call_tool(tool.name, {"direct_session_alias":"direct_001", "resource_version_alias":legacy, "code":legacy})
        value = first.structured_content
        short = value["resource_version_alias"]
        assert short.startswith("gv_") and len(short) == 25
        Draft202012Validator(tool.outputSchema).validate(value)
        Draft202012Validator(tool.inputSchema).validate({"direct_session_alias":"direct_001", "resource_version_alias":short})
        assert value["arbitrary_data"]["resource_version_alias"] == legacy
        assert value["code"] == legacy and value["next_cursor"] == cursor
        assert json.loads(first.content[0].text) == value
        second = await client.call_tool(tool.name, {"direct_session_alias":"direct_001", "resource_version_alias":short})
        assert second.structured_content["resource_version_alias"] == short
        assert received == [legacy, legacy] and index.writes == 1
        with pytest.raises(ToolError):
            await client.call_tool(tool.name, {"direct_session_alias":"direct_001", "resource_version_alias":"fv_"+short[3:]})
        resolver.resolve.return_value = SimpleNamespace(identity=reference_scope(task_id="task_b"))
        with pytest.raises(ToolError, match="REFERENCE_INVALID"):
            await client.call_tool(tool.name, {"direct_session_alias":"direct_001", "resource_version_alias":short})


@pytest.mark.asyncio
async def test_family_json_binding_expands_before_real_dto_without_touching_source():
    server, index, codec, scope, service, resolver = _setup()
    geometry = codec.encode({"domain":"geometry_catalog_reference", "reference_kind":"resource_version", "exact":"v1"})
    draft = codec.encode({"domain":"authoring_draft", "root_kind":"family", "root_id":"family-1"})
    short = await service.shorten(scope, geometry, ("gv",))
    result = _result().model_copy(update={"draft_ref": draft})
    application = SimpleNamespace(write_ifc_product_family=AsyncMock(return_value=result))
    register_ifc_product_family_write(server, application)
    definition = json.loads(_definition_json())
    definition["geometry_bindings"][0]["source"]["resource_version_alias"] = short
    sources = _source_files()
    sources[0]["source"] += f"\n# source literal must stay {short}\n"
    request = {"direct_session_alias":"direct_001", "mode":"create", "display_name":"Probe",
               "purpose":"Reference transport", "usage_instructions":{"action":"set","value":_FAMILY_USAGE},
               "source":{"source_kind":"inline","definition_json":json.dumps({"action":"set","value":definition}),"source_files":sources}}
    async with Client(server) as client:
        returned = await client.call_tool("ifc_product_family_write", request)
        output = returned.structured_content
        assert output["draft_ref"].startswith("fd_")
        actual = application.write_ifc_product_family.call_args.args[0]
        assert actual.to_domain_declaration()["source"]["definition"]["value"]["geometry_bindings"][0]["source"]["resource_version_alias"] == geometry
        assert short in actual.source.source_files[0].source
        bad = dict(request)
        bad["source"] = {**request["source"], "definition_json": '{"schema":"x","schema":"y"}'}
        with pytest.raises(ToolError, match="duplicate object key"):
            await client.call_tool("ifc_product_family_write", bad)
        assert application.write_ifc_product_family.call_count == 1






@pytest.mark.asyncio
@pytest.mark.parametrize("projection_fails", [False, True])
async def test_query_failure_keeps_error_and_returns_exact_recoverable_short_attempt(projection_fails):
    server, index, codec, scope, service, resolver = _setup()
    reference = codec.encode({"domain": "geometry_catalog_reference", "reference_kind": "resource_version"})
    attempt = codec.encode({"domain": "geometry_query_attempt_alias", "attempt_id": "failed-query"})

    async def failed_query(direct_session_alias: str, resource_version_alias: str, code: str = ""):
        from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
        raise _tool_error(DirectApplicationError("GEOMETRY_QUERY_EXECUTION_FAILED", "inspect failed source",
            phase="execution", diagnostic={"attempt_alias": attempt}))

    register_provider_tool(server, failed_query, name="ifc_geometry_query_python", description="Failure probe",
        input_type=ProbeInput, output_type=ProbeOutput)
    if projection_fails:
        service.shorten = AsyncMock(side_effect=ReferenceIndexError("private index failure"))
    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("ifc_geometry_query_python", {
                "direct_session_alias": "direct_001", "resource_version_alias": reference})
    prefix, diagnostic = str(caught.value).split("; diagnostic=", 1)
    assert "GEOMETRY_QUERY_EXECUTION_FAILED" in prefix and "phase=execution" in prefix
    short = json.loads(diagnostic)["attempt_alias"]
    if projection_fails:
        assert short == attempt and "private index failure" not in str(caught.value)
        return
    assert len(short) == 25 and short.startswith("gq_")
    assert await service.expand(scope, short, ("gq",)) == attempt
