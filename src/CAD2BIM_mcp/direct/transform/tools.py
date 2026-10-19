"""Direct tool registrations owned by this capability family."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from fastmcp import Context, FastMCP
from fastmcp.tools.function_tool import FunctionTool
from pydantic import Field, ValidationError
from CAD2BIM_backend.domain.data_transform import OutputPackageUpdate, TransformOutputsChange

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.invocation import server_direct_invocation_key
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.direct.provider_schema import (
    _object_union_output_schema,
    compact_provider_input_schema,
)
from CAD2BIM_mcp.direct.tool_support import (
    provider_input_schema,
    register_provider_document,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    DataTransformWriteToolResult,
)
from CAD2BIM_mcp.direct.transform.inputs import (
    DirectDataTransformCreateInput,
    DuckDbSqlExecution,
    InlineTable,
    OutputPackageDeclaration,
    PythonArrowExecution,
    TransformInputBinding,
    TransformOutputDeclaration,
    TransformParameter,
    to_domain_data_transform_create_command,
    validate_direct_data_transform_create_input,
)
from CAD2BIM_mcp.direct.transform.repair_inputs import (
    DirectRepairTransformInput,
    DirectTransformSourceEdit,
    FailedOperationRepairSource,
    SucceededPackageRepairSource,
    validate_direct_repair_transform_input,
)
from CAD2BIM_mcp.tool_docs import data_transform as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


DirectDataTransformInput: TypeAlias = DirectDataTransformCreateInput | DirectRepairTransformInput


def _closed_transform_schema() -> dict[str, object]:
    schema = provider_input_schema(
        DirectDataTransformInput,
        omit_generated_titles=True,
    )
    branches = schema.pop("anyOf", None)
    if isinstance(branches, list):
        schema["oneOf"] = branches
    return compact_provider_input_schema(schema)


def register_data_transform(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def data_transform(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        ctx: Context,
        mode: str,
        inputs: Annotated[
            tuple[TransformInputBinding, ...] | None,
            Field(
                min_length=0,
                max_length=16,
                description=(_tool_docs.DATA_TRANSFORM_INPUTS_DESCRIPTION),
            ),
        ] = None,
        execution: Annotated[
            DuckDbSqlExecution | PythonArrowExecution | None,
            Field(
                discriminator="engine",
                description=_tool_docs.DATA_TRANSFORM_EXECUTION_DESCRIPTION,
            ),
        ] = None,
        outputs: Annotated[
            TransformOutputsChange | None,
            Field(
                description=(_tool_docs.DATA_TRANSFORM_OUTPUTS_DESCRIPTION),
            ),
        ] = None,
        output_package: Annotated[
            OutputPackageDeclaration | OutputPackageUpdate | None,
            Field(description=_tool_docs.DATA_TRANSFORM_OUTPUT_PACKAGE_DESCRIPTION),
        ] = None,
        parameters: Annotated[
            tuple[TransformParameter, ...] | None,
            Field(
                max_length=32,
                description=(_tool_docs.DATA_TRANSFORM_PARAMETERS_DESCRIPTION),
            ),
        ] = None,
        inline_tables: Annotated[
            tuple[InlineTable, ...] | None,
            Field(
                max_length=8,
                description=(_tool_docs.DATA_TRANSFORM_INLINE_TABLES_DESCRIPTION),
            ),
        ] = None,
        source: FailedOperationRepairSource | SucceededPackageRepairSource | None = None,
        edits: tuple[DirectTransformSourceEdit, ...] | None = None,
        ifc_source: Literal["current"] | None = None,
    ) -> DataTransformWriteToolResult:
        try:
            value: dict[str, object] = {
                "mode": mode,
                "direct_session_alias": direct_session_alias,
            }
            for name, selected in {
                "inputs": inputs, "inline_tables": inline_tables, "parameters": parameters,
                "execution": execution, "outputs": outputs, "output_package": output_package,
                "ifc_source": ifc_source, "source": source, "edits": edits,
            }.items():
                if selected is not None:
                    value[name] = selected
            if mode == "create":
                if ifc_source is not None:
                    value["ifc_source"] = ifc_source
                if inputs is not None:
                    value["inputs"] = inputs
                if inline_tables is not None:
                    value["inline_tables"] = inline_tables
                if parameters is not None:
                    value["parameters"] = parameters
                if execution is not None:
                    value["execution"] = execution
                if outputs is not None:
                    value["outputs"] = outputs
                if output_package is not None:
                    value["output_package"] = output_package
                request = validate_direct_data_transform_create_input(value)
                command = to_domain_data_transform_create_command(request)
                return await application.transform_data(
                    request.direct_session_alias,
                    command,
                    request_identity=server_direct_invocation_key(ctx),
                )
            if ifc_source is not None:
                value["ifc_source"] = ifc_source
            if source is not None:
                value["source"] = source
            if edits is not None:
                value["edits"] = edits
            if outputs is not None:
                value["outputs"] = outputs
            if output_package is not None:
                value["output_package"] = output_package
            request = validate_direct_repair_transform_input(value)
            return await application.repair_transform(
                request,
                request_identity=server_direct_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "Select exactly one complete data_derive mode: create or repair",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    output_schema = _object_union_output_schema(DataTransformWriteToolResult, compact=True)
    register_provider_document(
        server,
        tool_name="data_derive",
        document_name="duckdb_sql",
        description=_tool_docs.DATA_TRANSFORM_DUCKDB_RESOURCE_DESCRIPTION,
        document=_tool_docs.DATA_TRANSFORM_DUCKDB_DOCUMENT,
    )
    register_provider_document(
        server,
        tool_name="data_derive",
        document_name="python_arrow",
        description=_tool_docs.DATA_TRANSFORM_PYTHON_RESOURCE_DESCRIPTION,
        document=_tool_docs.DATA_TRANSFORM_PYTHON_DOCUMENT,
    )
    register_provider_document(
        server,
        tool_name="data_derive",
        document_name="repair",
        description=_tool_docs.DATA_TRANSFORM_REPAIR_RESOURCE_DESCRIPTION,
        document=_tool_docs.DATA_TRANSFORM_REPAIR_DOCUMENT,
    )
    data_transform_tool = FunctionTool.from_function(
        data_transform,
        name="data_derive",
        description=_tool_docs.DATA_TRANSFORM_DESCRIPTION,
        output_schema=output_schema,
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    data_transform_tool.parameters = _closed_transform_schema()
    server.add_tool(data_transform_tool)
