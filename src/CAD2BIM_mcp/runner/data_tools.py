"""Runner Data and owner-backed Code/Operation MCP registrations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from pydantic import TypeAdapter

from CAD2BIM_backend.application.code_discovery_service import (
    CodeListResult, CodeReadBatchResult, CodeReadResult,
)
from CAD2BIM_backend.application.data_runtime_service import (
    DataPackageCatalog, OperationSummaryCatalog, OperationInspection, PackageDescription,
)
from CAD2BIM_backend.application.data_transform_service import DataTransformApplicationResult
from CAD2BIM_backend.domain.data_transform import DataTransformInputRead
from CAD2BIM_backend.domain.data_json import JsonReadResult
from CAD2BIM_backend.domain.data_query import DataDescribePackageResult, DataQueryRowsResult
from CAD2BIM_backend.domain.data_query_python import DataQueryPythonResult
from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlResult
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionOperationResult
from CAD2BIM_mcp.direct.catalog.contracts import (
    DirectDataDescribePackageResult,
    DirectPackageListResult,
    PackageDescriptionResult,
)
from CAD2BIM_mcp.direct.catalog.application import (
    _bounded_package_list_result, _catalog_package_card, _describe_package_result,
    _description_result,
)
from CAD2BIM_mcp.direct.code.application import _project_code_list, _project_code_read
from CAD2BIM_mcp.direct.code.contracts import (
    BatchCodeReadFields, DirectCodeListResult, DirectCodeReadResponse,
    DirectCodeReadBatchResult, to_code_list_query, to_code_read_selection,
)
from CAD2BIM_mcp.direct.operation.application import (
    _bounded_operation_catalog_result, _operation_get_result, _operation_summary_card,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    IfcProductsRecoveryListResult, OperationGetResult, OperationListResult,
)
from CAD2BIM_mcp.direct.ifc.products.contracts import IfcProductsApplyResult, IfcProductsEditResult
from CAD2BIM_mcp.direct.provider_schema import _object_union_output_schema
from CAD2BIM_mcp.direct.transform.application import (
    _data_repair_transform_result, _data_transform_result,
)
from CAD2BIM_mcp.direct.transform.contracts import DataTransformWriteToolResult

from CAD2BIM_mcp.runner.data_contracts import (
    CodeListInput,
    CodeReadInput,
    DescribePackageInput,
    JsonInput,
    OperationGetInput,
    OperationListInput,
    PackageDescriptionInput,
    PackageListInput,
    PythonInput,
    RowsInput,
    SqlInput,
    TransformInput,
)
from CAD2BIM_mcp.runner.backend_client import BackendDomainError
from CAD2BIM_mcp.runner.domain_errors import backend_domain_tool_error
from CAD2BIM_mcp.runner.resources import request_domain_credential
from CAD2BIM_mcp.tool_docs import code as code_docs, operation as operation_docs
from CAD2BIM_mcp.runner.tool_projection import runner_tool_description


class DataBackend(Protocol):
    async def call_data_tool(
        self, *, access_token: str, tool_name: str,
        body: dict[str, Any], timeout: float | None = None,
    ) -> dict[str, Any]: ...


def _decode(type_, payload: object):
    return TypeAdapter(type_).validate_python(payload)


def register_runner_data_tools(server: FastMCP, backend: DataBackend) -> None:
    from CAD2BIM_mcp.direct.tool_support import register_provider_document
    from CAD2BIM_mcp.tool_docs import data_query as query_docs
    from CAD2BIM_mcp.tool_docs import data_transform as transform_docs

    for name, description, shared in (
        ("duckdb_sql", transform_docs.DATA_TRANSFORM_DUCKDB_RESOURCE_DESCRIPTION,
         transform_docs.DATA_TRANSFORM_DUCKDB_DOCUMENT),
        ("python_arrow", transform_docs.DATA_TRANSFORM_PYTHON_RESOURCE_DESCRIPTION,
         transform_docs.DATA_TRANSFORM_PYTHON_DOCUMENT),
        ("repair", transform_docs.DATA_TRANSFORM_REPAIR_RESOURCE_DESCRIPTION,
         transform_docs.DATA_TRANSFORM_REPAIR_DOCUMENT),
    ):
        document = deepcopy(shared)
        examples = document if name == "repair" else document["examples"]
        field = "complete_repair_request" if name == "repair" else "complete_create_request"
        request = examples[field]
        request.pop("direct_session_alias")
        examples[field] = {"request": request}
        if name == "python_arrow":
            ifc_request = examples["complete_ifc_create_request"]
            ifc_request.pop("direct_session_alias")
            examples["complete_ifc_create_request"] = {"request": ifc_request}
        register_provider_document(server, tool_name="data_derive", document_name=name,
            description=description, document=document)

    document = query_docs.data_query_python_program_document()
    # The shared program contract has Direct entry examples. Project only those
    # two complete requests into this endpoint's existing typed request wrapper.
    for field in ("complete_fresh_request", "complete_repair_request"):
        request = document["examples"][field]
        request.pop("direct_session_alias")
        document["examples"][field] = {"request": request}
    register_provider_document(
        server, tool_name="data_query_python", document_name="program_api",
        description=query_docs.DATA_QUERY_PYTHON_PROGRAM_RESOURCE_DESCRIPTION,
        document=document,
    )
    async def call(name: str, request: Any, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await backend.call_data_tool(
                access_token=request_domain_credential(),
                tool_name=name,
                body=request.model_dump(mode="json") if body is None else body,
            )
        except BackendDomainError as exc:
            raise backend_domain_tool_error(exc) from None

    @server.tool(
        name="data_package_list",
        meta={"effect": "READ", "parallel_safe": True},
        description=(
            "List immutable Data Packages visible to the current Run. Supply optional "
            "before_sequence from the previous page and page_size 1-50; use returned "
            "aliases for exact reads. Invisible or inconsistent Catalog entries fail."
        ),
    )
    async def data_package_list(request: PackageListInput) -> DirectPackageListResult:
        catalog = _decode(DataPackageCatalog, await call("data_package_list", request))
        cards = tuple(
            _catalog_package_card(
                item, catalog.producing_operations.get(item.package.package_id)
            ) for item in catalog.packages
        )
        return _bounded_package_list_result(
            cards, complete=catalog.complete,
            next_before_sequence=catalog.next_before_sequence,
        )

    @server.tool(
        name="data_describe_package",
        meta={"effect": "READ", "parallel_safe": True},
        description=(
            "Describe a visible immutable Package by data_* alias. Optionally select one "
            "payload_name to inspect its bounded Parquet or JSON schema; missing or "
            "invisible payloads fail."
        ),
    )
    async def data_describe_package(request: DescribePackageInput) -> DirectDataDescribePackageResult:
        result = _decode(DataDescribePackageResult, await call("data_describe_package", request))
        return _describe_package_result(result)

    @server.tool(
        name="data_get_package_description",
        meta={"effect": "READ", "parallel_safe": True},
        description=(
            "Read the saved Markdown description for one visible immutable Package using "
            "its data_* alias from data_package_list. Missing or invisible aliases fail."
        ),
    )
    async def data_get_package_description(request: PackageDescriptionInput) -> PackageDescriptionResult:
        result = _decode(PackageDescription, await call("data_get_package_description", request))
        return _description_result(result)

    @server.tool(
        name="data_query_rows",
        meta={"effect": "READ", "parallel_safe": True},
        description=(
            "Query selected columns and filters from one visible Parquet Package Payload. "
            "Use a complete first-page query or its unchanged cursor, with at most 200 "
            "rows per page; JSON payloads and invalid cursors fail."
        ),
    )
    async def data_query_rows(request: RowsInput) -> DataQueryRowsResult:
        return _decode(DataQueryRowsResult, await call("data_query_rows", request))

    @server.tool(
        name="data_query_sql",
        meta={"effect": "READ", "parallel_safe": True},
        description=(
            "Run a read-only DuckDB query over declared visible Package Payload bindings. "
            "Choose fresh with query, repair with saved attempt alias and edits, "
            "or cursor for the next bounded page. The server binds fresh/repair to this "
            "saved action; invalid exact inputs or execution fail with an attempt code."
        ),
    )
    async def data_query_sql(request: SqlInput) -> DataQuerySqlResult:
        return _decode(DataQuerySqlResult, await call("data_query_sql", request))

    @server.tool(
        name="data_query_python",
        meta={"effect": "READ", "parallel_safe": True},
        description=(
            "Run read-only Python/Arrow over declared visible Package Payloads and small "
            "inline tables. Choose fresh, repair by saved attempt alias and edits, "
            "or cursor for an unchanged bounded page. Fresh/repair use the saved action "
            "identity; Worker or exact-input failures return an attempt code."
        ),
    )
    async def data_query_python(request: PythonInput) -> DataQueryPythonResult:
        return _decode(DataQueryPythonResult, await call("data_query_python", request))

    @server.tool(
        name="data_read_json",
        meta={"effect": "READ", "parallel_safe": True},
        description=(
            "Read a JSON Payload by visible Package alias and payload name. Select one "
            "value, children, or records query, or continue with its unchanged cursor. "
            "Reads are bounded and exact; invalid pointers or cursors fail."
        ),
    )
    async def data_read_json(request: JsonInput) -> JsonReadResult:
        return _decode(JsonReadResult, await call("data_read_json", request))

    async def data_transform(request: TransformInput) -> DataTransformWriteToolResult:
        result = _decode(DataTransformApplicationResult, await call("data_derive", request))
        if request.mode == "create":
            return _data_transform_result(result)
        parent_alias = result.repair_parent_operation_alias
        if parent_alias is None:
            raise ValueError("repaired Operation is missing its parent alias")
        return _data_repair_transform_result(
            result, repair_parent_operation_alias=parent_alias
        )

    async def code_list(request: CodeListInput) -> DirectCodeListResult:
        body = ({"query": to_code_list_query(request.query).model_dump(mode="json")}
                if request.mode == "first" else {"cursor": request.cursor})
        return _project_code_list(_decode(CodeListResult, await call("code_list", request, body=body)))

    async def code_read(request: CodeReadInput) -> DirectCodeReadResponse:
        body = ({"reads": [to_code_read_selection(read).model_dump(mode="json") for read in request.reads]}
                if isinstance(request, BatchCodeReadFields)
                else to_code_read_selection(request).model_dump(mode="json"))
        payload = await call("code_read", request, body=body)
        if not isinstance(request, BatchCodeReadFields):
            return _project_code_read(_decode(CodeReadResult, payload))
        batch = _decode(CodeReadBatchResult, payload)
        return DirectCodeReadBatchResult(
            reads=tuple(_project_code_read(item) for item in batch.reads)
        )

    async def operation_list(request: OperationListInput) -> OperationListResult | IfcProductsRecoveryListResult:
        payload = await call("operation_list", request)
        if request.mode == "ifc_products_recovery":
            return _decode(IfcProductsRecoveryListResult, payload)
        catalog = _decode(OperationSummaryCatalog, payload["catalog"])
        cards = tuple(
            _operation_summary_card(
                item,
                operation_ref=payload["operation_refs"].get(item.operation.operation_id),
            ) for item in catalog.operations
        )
        return _bounded_operation_catalog_result(
            cards, complete=catalog.complete,
            next_before_sequence=catalog.next_before_sequence,
        )

    async def operation_get(request: OperationGetInput) -> OperationGetResult:
        payload = await call("operation_get", request)
        if request.mode == "transform_input":
            return _decode(DataTransformInputRead, payload)
        if "preview_result" in payload:
            from CAD2BIM_backend.domain.preview_report_read import PreviewOperationRead
            return _decode(PreviewOperationRead, payload["preview_result"])
        if "apply_result" in payload:
            return _decode(IfcProductsApplyResult, payload["apply_result"])
        if "edit_result" in payload:
            return _decode(IfcProductsEditResult, payload["edit_result"])
        inspection = _decode(OperationInspection, payload["inspection"])
        inspection_result = (
            _decode(GeometryInspectionOperationResult, payload["inspection_result"])
            if payload["inspection_result"] is not None else None
        )
        return _operation_get_result(
            inspection, inspection_result=inspection_result,
            authoring_result=payload["authoring_result"],
            case_key=payload["case_key"], after_case_key=request.after_case_key if request.mode != "inspection" else None,
            page_size=request.page_size if request.mode != "inspection" else 1,
            operation_ref=payload["operation_ref"], same_run=payload["same_run"],
            family_version_ref=payload.get("family_version_ref"),
            code_locator=payload.get("code_locator"),
        )

    for function, name, description, output_type in (
        (data_transform, "data_derive", transform_docs.IFC_DERIVE_CONTRACT + " Execute SQL/Python and publish a new immutable Data Package, or repair a failed Operation/succeeded Package by changing source, outputs or Package metadata. Omitted repair fields inherit the exact parent. description_markdown uses set/edit; outputs uses set/edit by payload_name, and schema_json carries action JSON with a full object/boolean Schema or literal edits. Read saved description/Schema with operation_get transform_input. Every repair executes a new Operation; invalid declarations, Worker or publication failures return its diagnostic. Programming and complete request examples: cad2bim://contracts/data_derive/duckdb_sql, cad2bim://contracts/data_derive/python_arrow; repair: cad2bim://contracts/data_derive/repair.", DataTransformWriteToolResult),
        (code_list, "code_list", runner_tool_description("code_list", code_docs.CODE_LIST_DESCRIPTION.replace("with direct_session_alias and mode", "with mode inside request")), DirectCodeListResult),
        (code_read, "code_read", runner_tool_description("code_read", code_docs.CODE_READ_DESCRIPTION.replace("use direct_session_alias, mode=batch", "use mode=batch inside request")), DirectCodeReadResponse),
        (operation_list, "operation_list", runner_tool_description("operation_list", operation_docs.OPERATION_LIST_DESCRIPTION.replace("this Direct scope", "the current Runner scope")), OperationListResult | IfcProductsRecoveryListResult),
        (operation_get, "operation_get", runner_tool_description("operation_get", operation_docs.OPERATION_GET_DESCRIPTION), OperationGetResult),
    ):
        server.add_tool(FunctionTool.from_function(
            function,
            name=name,
            meta={"effect": "WRITE" if name == "data_derive" else "READ", "parallel_safe": name != "data_derive"},
            description=description,
            output_schema=_object_union_output_schema(output_type, compact=True),
        ))


__all__ = ["register_runner_data_tools"]
