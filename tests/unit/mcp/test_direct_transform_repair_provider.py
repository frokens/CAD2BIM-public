from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp.exceptions import ToolError

from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationStatus,
    DataTransformOperationDetail,
)
from CAD2BIM_mcp.direct.transform import application as transform_application
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.transform.contracts import (
    DataRepairTransformResult,
    DataTransformOutputResult,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.server import create_direct_mcp
from CAD2BIM_mcp.direct.transform.repair_inputs import (
    to_domain_repair_transform_command,
    validate_direct_repair_transform_input,
)


async def test_repair_tool_dispatches_one_provider_request_without_domain_ids() -> None:
    response = DataRepairTransformResult(
        repair_parent_operation_alias="op_012",
        operation_alias="op_013",
        package_alias="data_021",
        package_name="repaired",
        engine="python_arrow",
        code_sha256="1" * 64,
        output=DataTransformOutputResult(
            payload_name="products",
            format="parquet",
            row_count=1,
            logical_key_fields=("product_key",),
        ),
    )
    application = SimpleNamespace(repair_transform=AsyncMock(return_value=response))
    tools = {tool.name: tool for tool in await create_direct_mcp(application).list_tools()}

    actual = await tools["data_derive"].fn(
        direct_session_alias="direct_001",
        ctx=SimpleNamespace(session_id="session-one", request_id="request-one"),
        mode="repair",
        source={"source_kind": "failed_operation", "operation_alias": "op_012"},
        edits=({"old_text": "bad(", "new_text": "good("},),
    )

    assert actual == response
    request = application.repair_transform.await_args.args[0]
    assert request.source.operation_alias == "op_012"
    assert not hasattr(request, "parent_operation_id")
    assert application.repair_transform.await_args.kwargs["request_identity"].startswith("mcp:")


@pytest.mark.parametrize("input_count", [0, 2])
async def test_ifc_derive_sdk_dispatches_exact_create_and_public_source_summary(input_count) -> None:
    from fastmcp import Client
    from CAD2BIM_mcp.direct.transform.contracts import MultiOutputDataTransformResult
    from CAD2BIM_mcp.tool_docs.data_transform import DATA_DERIVE_IFC_CREATE_EXAMPLE

    request = deepcopy(DATA_DERIVE_IFC_CREATE_EXAMPLE)
    request["inputs"] = [
        {"input_name": f"records_{index}", "package_alias": f"data_{index + 1:03}", "payload_name": "records"}
        for index in range(input_count)
    ]
    response = MultiOutputDataTransformResult(
        operation_alias="op_013", package_alias="data_021", package_name="IFC product inventory",
        outputs=(
            {"payload_name": "selected", "format": "parquet", "row_count": 0,
             "logical_key_fields": ["entity_key"]},
            {"payload_name": "summary", "format": "json", "json_kind": "document", "size_bytes": 19},
        ),
        ifc_source={"revision_no": 7},
    )
    application = SimpleNamespace(transform_data=AsyncMock(return_value=response))
    async with Client(create_direct_mcp(application)) as client:
        result = await client.call_tool("data_derive", request)
    command = application.transform_data.await_args.args[1]
    assert command.ifc_source == "current"
    assert len(command.inputs) == input_count
    assert result.structured_content["ifc_source"] == {"revision_no": 7}
    assert len(result.structured_content["outputs"]) == 2


@pytest.mark.parametrize("invalid_source", ["sql_with_ifc", "no_source", "repair_source_override"])
async def test_derive_sdk_rejects_mixed_or_missing_sources_before_application(invalid_source) -> None:
    from fastmcp import Client
    from CAD2BIM_mcp.tool_docs.data_transform import DATA_DERIVE_IFC_CREATE_EXAMPLE

    request = deepcopy(DATA_DERIVE_IFC_CREATE_EXAMPLE)
    if invalid_source == "sql_with_ifc":
        request["execution"] = {"engine": "duckdb_sql", "code": "SELECT 1"}
        request["inputs"] = [{"input_name": "records", "package_alias": "data_001", "payload_name": "records"}]
    elif invalid_source == "no_source":
        request.pop("ifc_source")
    else:
        request = {"mode": "repair", "direct_session_alias": "direct_001",
                   "source": {"source_kind": "failed_operation", "operation_alias": "op_012"},
                   "ifc_source": "current"}
    application = SimpleNamespace(transform_data=AsyncMock(), repair_transform=AsyncMock())
    with pytest.raises(ToolError):
        async with Client(create_direct_mcp(application)) as client:
            await client.call_tool("data_derive", request)
    application.transform_data.assert_not_awaited()
    application.repair_transform.assert_not_awaited()


async def test_operation_get_registered_transform_read_dispatches_closed_selection() -> None:
    from CAD2BIM_backend.domain.data_transform import DataTransformTextInputRead
    from CAD2BIM_backend.application.text_source_view import TextSourceViewService
    from CAD2BIM_backend.domain.text_source import TextSourceView
    result = DataTransformTextInputRead(operation_alias="op_012", input_kind="package_description", source=TextSourceViewService().view("# Original", TextSourceView(view_mode="full")))
    application = SimpleNamespace(get_operation=AsyncMock(return_value=result))
    tools = {tool.name: tool for tool in await create_direct_mcp(application).list_tools()}
    actual = await tools["operation_get"].fn(direct_session_alias="direct_001", mode="transform_input", operation_alias="op_012", input_kind="package_description", view=TextSourceView(view_mode="full"))
    assert actual == result
    selected = application.get_operation.await_args.kwargs["selection"]
    assert selected.input_kind == "package_description"
    assert selected.view.view_mode == "full"
    assert "transform_input" in repr(tools["operation_get"].parameters)


@pytest.mark.parametrize("selection", [
    {"input_kind": "metadata", "section": "parameters"},
    {"input_kind": "inline_schema", "input_name": "original", "column_count": 1},
    {"input_kind": "inline_rows", "input_name": "original", "start_row": 1, "row_count": 2, "select": ["text"]},
    {"input_kind": "parameter", "parameter_name": "limit"},
    {"input_kind": "inline_text", "input_name": "original", "row_index": 1, "column_name": "text", "start_character": 2, "character_count": 3},
])
async def test_saved_input_sdk_branches_reach_the_original_runtime_owner(selection):
    from fastmcp import Client
    from CAD2BIM_mcp.direct.operation.application import OperationApplicationMixin
    from tests.unit.backend.test_data_runtime_application import _saved_transform_reader

    reader, _published = _saved_transform_reader()
    scope = object()
    class Application(OperationApplicationMixin):
        _application = reader
        async def _run_runtime_read(self, alias, tool_name, invoke):
            assert alias == "direct_001" and tool_name == "operation_get"
            return await invoke(scope)
    async with Client(create_direct_mcp(Application())) as client:
        if selection["input_kind"] == "metadata":
            tool = next(t for t in await client.list_tools() if t.name == "operation_get")
            def check_schema(node):
                if isinstance(node, dict):
                    assert node.get("type") != "unknown"
                    assert not ({"allOf", "if", "then", "else", "not"} & node.keys())
                    for child in node.values():
                        check_schema(child)
                elif isinstance(node, list):
                    for child in node:
                        check_schema(child)
            check_schema(tool.inputSchema)
            check_schema(tool.outputSchema)
        result = await client.call_tool("operation_get", {"direct_session_alias": "direct_001",
            "mode": "transform_input", "operation_alias": "op_004", **selection})
        assert result.structured_content["input_kind"] == selection["input_kind"]
        assert result.structured_content["operation_alias"] == "op_004"
        with pytest.raises(ToolError):
            await client.call_tool("operation_get", {"direct_session_alias": "direct_001",
                "mode": "transform_input", "operation_alias": "op_004", **selection,
                "unknown_field": "PRIVATE_UNKNOWN_VALUE"})
    assert reader._operation_by_alias.await_count == 1



async def test_repair_alias_schema_documents_immediate_error_and_rediscovery_sources() -> None:
    tools = {tool.name: tool for tool in await create_direct_mcp(Mock()).list_tools()}

    schema = tools["data_derive"].parameters
    def resolved(node):
        while "$ref" in node:
            node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
        return node

    assert len(schema["oneOf"]) == 4
    *create_branches, repair_branch = schema["oneOf"]
    assert all(resolved(branch["properties"]["mode"])["const"] == "create" for branch in create_branches)
    assert repair_branch["properties"]["mode"]["const"] == "repair"
    assert [
        resolved(branch["properties"]["execution"])["properties"]["engine"]["const"]
        for branch in create_branches
    ] == ["duckdb_sql", "python_arrow", "python_arrow"]
    assert create_branches[2]["properties"]["ifc_source"]["const"] == "current"
    assert create_branches[2]["properties"]["inputs"].get("minItems", 0) == 0
    assert "ifc_source" in create_branches[2]["required"]
    assert all("ifc_source" not in branch["properties"] for branch in create_branches[:2])
    failed_branch, package_branch = repair_branch["properties"]["source"]["oneOf"]
    assert "failed_operation_alias" not in repr(schema)
    assert (
        failed_branch["properties"]["source_kind"]["const"]
        == "failed_operation"
    )
    assert (
        package_branch["properties"]["source_kind"]["const"]
        == "succeeded_package"
    )
    description = failed_branch["properties"]["operation_alias"]["description"]

    assert "failing ToolError" in description
    assert "rediscovered with operation_list" in description
    assert "operation_get may be used" in description

    tool_description = tools["data_derive"].description
    assert tool_description is not None
    for fragment in (
        "mode=repair",
        "Code edits may be omitted",
        "cad2bim://contracts/data_derive/repair",
        "ordinary create does not require the repair document",
        "failures expose no partial Package",
    ):
        assert fragment in tool_description


def test_structured_compiler_diagnostic_survives_tool_error_projection() -> None:
    error = DirectApplicationError(
        "TRANSFORM_CODE_INVALID",
        "Python source did not compile",
        phase="code_validation",
        operation_alias="op_013",
        diagnostic={
            "line": 418,
            "column": 9,
            "end_line": 418,
            "end_column": 10,
            "source_line": "    bad_call(]",
            "caret": "        ^",
            "code_sha256": "2" * 64,
        },
    )

    projected = _tool_error(error)

    assert isinstance(projected, ToolError)
    message = str(projected)
    assert "phase=code_validation" in message
    assert 'diagnostic={"caret":"        ^"' in message
    assert '"line":418' in message
    assert '"code_sha256":"' + "2" * 64 + '"' in message


def test_worker_json_diagnostic_is_recovered_before_message_normalization() -> None:
    class Error(RuntimeError):
        phase = "code_validation"

    diagnostic = transform_application._transform_application_diagnostic(
        Error('{"line":4,"source_line":"    bad(]"}')
    )

    assert diagnostic == {"line": 4, "source_line": "    bad(]"}


def test_repair_provider_alias_maps_to_only_the_resolved_internal_parent_id() -> None:
    request = validate_direct_repair_transform_input(
        {
            "mode": "repair",
            "direct_session_alias": "direct_001",
            "source": {"source_kind": "failed_operation", "operation_alias": "op_012"},
            "edits": ({"old_text": "bad", "new_text": "good"},),
        }
    )

    command = to_domain_repair_transform_command(
        request,
        parent_operation_id="operation-internal-uuid",
    )

    assert command.parent_operation_id == "operation-internal-uuid"
    assert not hasattr(command, "expected_code_sha256")
    assert not hasattr(command, "failed_operation_alias")


async def test_direct_facade_resolves_parent_alias_before_calling_entry_neutral_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_record = SimpleNamespace(
        operation_id="operation-internal-uuid",
        status=CriticalOperationStatus.FAILED,
        tool_name="data_derive",
        detail=DataTransformOperationDetail.model_construct(engine="python_arrow"),
    )
    published_parent = SimpleNamespace(operation=parent_record)
    input_package = SimpleNamespace(catalog_entry=SimpleNamespace(alias="data_001"))
    inspection = SimpleNamespace(
        operation=published_parent,
        input_packages={"entities_in": input_package},
    )
    runtime = SimpleNamespace(get_operation=AsyncMock(return_value=inspection))
    service_result = object()
    service = SimpleNamespace(
        resume_by_identity=AsyncMock(return_value=None),
        repair_transform=AsyncMock(return_value=service_result),
    )
    scope = SimpleNamespace(identity=object())
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=scope))
    facade = DirectApplicationFacade(
        runtime,
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        transform_service=service,
    )
    expected = object()
    monkeypatch.setattr(
        transform_application,
        "_data_repair_transform_result",
        lambda *a, **k: expected,
    )
    request = validate_direct_repair_transform_input(
        {
            "mode": "repair",
            "direct_session_alias": "direct_001",
            "source": {"source_kind": "failed_operation", "operation_alias": "op_012"},
            "edits": ({"old_text": "bad", "new_text": "good"},),
        }
    )

    actual = await facade.repair_transform(request, request_identity="test-action-one")

    assert actual is expected
    call = service.repair_transform.await_args.kwargs
    assert call["scope"] is scope
    assert call["parent_operation"] is published_parent
    assert call["catalog_packages"] == {"data_001": input_package}
    assert call["command"].parent_operation_id == "operation-internal-uuid"
    assert call["request_identity"] == "test-action-one"
    assert call["request_payload"] == {
        "mode": "repair",
        "request": request.model_dump(mode="json", exclude={"direct_session_alias"}),
    }
    assert service.resume_by_identity.await_args.args[2] == call["request_payload"]
    assert call["repair_parent_operation_alias"] == "op_012"
    assert isinstance(call["deadline"], float)


async def test_repair_replay_uses_original_action_before_parent_alias_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed = SimpleNamespace(repair_parent_operation_alias="op_012")
    service = SimpleNamespace(resume_by_identity=AsyncMock(return_value=resumed))
    runtime = SimpleNamespace(get_operation=AsyncMock())
    scope = SimpleNamespace(identity=object())
    facade = DirectApplicationFacade(
        runtime,
        Mock(),
        SimpleNamespace(resolve=AsyncMock(return_value=scope)),
        lambda _identity: nullcontext(),
        transform_service=service,
    )
    expected = object()
    monkeypatch.setattr(
        transform_application,
        "_data_repair_transform_result",
        lambda *a, **k: expected,
    )
    request = validate_direct_repair_transform_input(
        {
            "mode": "repair",
            "direct_session_alias": "direct_001",
            "source": {"source_kind": "failed_operation", "operation_alias": "op_012"},
            "edits": ({"old_text": "bad", "new_text": "good"},),
        }
    )

    actual = await facade.repair_transform(request, request_identity="test-repair-replay")

    assert actual is expected
    runtime.get_operation.assert_not_awaited()


async def test_direct_facade_resolves_succeeded_package_to_exact_producing_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_record = SimpleNamespace(
        operation_id="producer-internal-uuid",
        status=CriticalOperationStatus.SUCCEEDED,
        tool_name="data_derive",
        detail=DataTransformOperationDetail.model_construct(engine="duckdb_sql"),
    )
    published_parent = SimpleNamespace(
        operation=parent_record,
        catalog_entry=SimpleNamespace(alias="op_020"),
    )
    input_package = SimpleNamespace(catalog_entry=SimpleNamespace(alias="data_001"))
    inspection = SimpleNamespace(
        operation=published_parent,
        input_packages={"entities_in": input_package},
    )
    runtime = SimpleNamespace(
        get_transformation_code=AsyncMock(return_value=SimpleNamespace(operation=published_parent)),
        get_operation=AsyncMock(return_value=inspection),
    )
    service_result = object()
    service = SimpleNamespace(
        resume_by_identity=AsyncMock(return_value=None),
        repair_transform=AsyncMock(return_value=service_result),
    )
    scope = SimpleNamespace(identity=object())
    facade = DirectApplicationFacade(
        runtime,
        Mock(),
        SimpleNamespace(resolve=AsyncMock(return_value=scope)),
        lambda _identity: nullcontext(),
        transform_service=service,
    )
    expected = object()
    projection = Mock(return_value=expected)
    monkeypatch.setattr(transform_application, "_data_repair_transform_result", projection)
    request = validate_direct_repair_transform_input(
        {
            "mode": "repair",
            "direct_session_alias": "direct_001",
            "source": {"source_kind": "succeeded_package", "package_alias": "data_021"},
            "edits": ({"old_text": "bad", "new_text": "good"},),
        }
    )

    actual = await facade.repair_transform(request, request_identity="test-action-two")

    assert actual is expected
    reference = runtime.get_transformation_code.await_args.args[1]
    assert reference.package_alias == "data_021"
    runtime.get_operation.assert_awaited_once_with(scope, "op_020")
    call = service.repair_transform.await_args.kwargs
    assert call["parent_operation"] is published_parent
    assert call["command"].parent_operation_id == "producer-internal-uuid"
    assert projection.call_args.kwargs["repair_parent_operation_alias"] == "op_020"
