"""Authenticated Runner IFC tools over the shared backend Application Core."""

from __future__ import annotations

import json
from typing import Annotated, Any, Protocol

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.utilities.json_schema import dereference_refs
from pydantic import Field, TypeAdapter

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    IfcRepresentationContextCatalog,
)
from CAD2BIM_backend.domain.ifc_model import (
    IfcGeometryBasisConfigureResult,
    IfcModelInfo,
    IfcModelInitializeResult,
)
from CAD2BIM_backend.domain.ifc_project import IfcProjectUpdateResult
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    IfcSpatialStructureApplyResult,
    IfcSpatialStructureListResult,
)
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcModelInfoResult,
    IfcContextOperationInput,
    IfcCoordinateReferenceChangeInput,
    IfcProjectUnitChangeInput,
    to_domain_geometry_basis_command,
)
from CAD2BIM_mcp.direct.ifc.project.contracts import (
    IfcNullableProjectFieldInput,
    IfcProjectPatchInput,
    to_domain_project_update_command,
)
from CAD2BIM_mcp.direct.ifc.query.contracts import (
    IfcQueryExactInput,
    IfcQueryPythonResult,
    IfcQuerySourceEdit,
)
from CAD2BIM_mcp.direct.ifc.spatial.contracts import (
    IfcGlobalIdInput,
    IfcSpatialClassInput,
    IfcSpatialOperationInput,
    to_domain_spatial_apply_command,
)
from CAD2BIM_mcp.direct.ifc.products.contracts import ProductsApplyInput, IfcProductsApplyResult, ProductsReadInput, IfcProductsReadResult, ProductsEditInput, IfcProductsEditResult
from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadGroups
from CAD2BIM_mcp.direct.provider_schema import compact_provider_input_schema, _object_union_output_schema
from CAD2BIM_mcp.direct.tool_support import provider_input_schema, register_provider_document, register_provider_tool
from CAD2BIM_mcp.tool_docs import ifc_products as products_docs
from CAD2BIM_mcp.direct.provider_schema import provider_reference_schema
from CAD2BIM_mcp.tool_docs import ifc_query as query_docs
from CAD2BIM_mcp.runner.ifc_contracts import (
    RunnerIfcQueryInput,
    RunnerIfcSpatialListContinuationInput,
    RunnerIfcSpatialListInput,
    validate_runner_ifc_query,
    validate_runner_ifc_spatial_list,
)
from CAD2BIM_mcp.runner.backend_client import BackendDomainError
from CAD2BIM_mcp.runner.domain_errors import backend_domain_tool_error
from CAD2BIM_mcp.runner.resources import request_domain_credential


class RunnerIfcBackend(Protocol):
    async def call_ifc_domain(
        self, *, access_token: str, endpoint: str,
        body: dict[str, Any], timeout: float | None = None,
    ) -> dict[str, Any]: ...


def register_runner_ifc_tools(server: FastMCP, backend: RunnerIfcBackend) -> None:
    """Register the fixed IFC surface; the backend validates each action claim."""

    example = json.loads(query_docs.IFC_QUERY_FRESH_EXAMPLE)
    example.pop("direct_session_alias")
    register_provider_document(
        server, tool_name="ifc_query_python", document_name="program_api",
        description=query_docs.IFC_QUERY_PROGRAM_RESOURCE_DESCRIPTION.replace(
            "Direct IfcQueryExactInput", "IfcQueryExactInput"),
        document={
            "usage": query_docs.IFC_QUERY_PROGRAM_USAGE,
            "input_binding_schema": provider_reference_schema(IfcQueryExactInput, mode="validation"),
            "return_contract": query_docs.IFC_QUERY_RETURN_CONTRACT,
            "examples": {"ifc_only": example},
        },
        schema_roots=(("input_binding_schema",),),
    )

    async def call(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await backend.call_ifc_domain(
                access_token=request_domain_credential(), endpoint=endpoint, body=body,
            )
        except BackendDomainError as exc:
            raise backend_domain_tool_error(exc) from None

    @server.tool(
        name="ifc_model_initialize",
        description=(
            "Create the IFC4X3_ADD2 model for this Run from a nonblank project_name "
            "(at most 255 characters). The server supplies Run identity and returns the "
            "committed model summary and Operation alias. A different existing model conflicts."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    async def ifc_model_initialize(
        project_name: Annotated[str, Field(min_length=1, max_length=255)],
    ) -> IfcModelInitializeResult:
        payload = await call("/internal/domain/ifc/model/initialize", {"project_name": project_name})
        return IfcModelInitializeResult.model_validate(payload)

    @server.tool(
        name="ifc_model_info",
        description=(
            "Read this Run's current IFC model, project identity, counts and geometry basis. "
            "size_bytes is the verified byte count of that exact stored IFC file. "
            "source_revision identifies that same read's exact basis; use revision_ref unchanged for edit when Package system source is insufficient. "
            "Requires an initialized model; no page cursor or Operation is created."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def ifc_model_info() -> IfcModelInfoResult:
        payload = await call("/internal/domain/ifc/model/info", {})
        return IfcModelInfoResult.model_validate(payload)

    @server.tool(
        name="ifc_model_configure_geometry_basis",
        description=(
            "Configure IFC project units, coordinate reference or representation contexts "
            "using the explicit change fields. Submit at least one change; inspect current "
            "contexts first. Runner supplies action identity, and conflicts do not publish a "
            "partial revision."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )
    async def ifc_model_configure_geometry_basis(
        project_unit_change: IfcProjectUnitChangeInput | None = None,
        coordinate_reference_change: IfcCoordinateReferenceChangeInput | None = None,
        context_operations: tuple[IfcContextOperationInput, ...] = (),
    ) -> IfcGeometryBasisConfigureResult:
        command = to_domain_geometry_basis_command(
            project_unit_change, coordinate_reference_change, context_operations,
        )
        payload = await call(
            "/internal/domain/ifc/model/geometry-basis/configure",
            {"command": command.operation_request_payload()},
        )
        return IfcGeometryBasisConfigureResult.model_validate(payload)

    @server.tool(
        name="ifc_representation_context_list",
        description=(
            "List this Run's exact current IFC representation contexts and bounded properties. "
            "Requires an initialized model; the catalog has no page cursor."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def ifc_representation_context_list() -> IfcRepresentationContextCatalog:
        payload = await call("/internal/domain/ifc/model/representation-contexts/list", {})
        return IfcRepresentationContextCatalog.model_validate(payload)

    @server.tool(
        name="ifc_project_update",
        description=(
            "Update current IFC Project business metadata. Patch fields must contain values; "
            "clear_fields explicitly removes nullable fields. Read model info for the current "
            "project first. Runner supplies action identity and returns the committed Operation."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )
    async def ifc_project_update(
        patch: IfcProjectPatchInput,
        clear_fields: tuple[IfcNullableProjectFieldInput, ...] = (),
    ) -> IfcProjectUpdateResult:
        command = to_domain_project_update_command(patch, clear_fields)
        payload = await call(
            "/internal/domain/ifc/project/update",
            {"command": command.operation_request_payload()},
        )
        return IfcProjectUpdateResult.model_validate(payload)

    async def ifc_spatial_structure_apply(
        operations: Annotated[tuple[IfcSpatialOperationInput, ...], Field(min_length=1, max_length=256)],
    ) -> IfcSpatialStructureApplyResult:
        command = to_domain_spatial_apply_command(operations)
        payload = await call(
            "/internal/domain/ifc/spatial-structure/apply",
            {"command": command.operation_request_payload()},
        )
        return IfcSpatialStructureApplyResult.model_validate(payload)

    spatial_apply = FunctionTool.from_function(
        ifc_spatial_structure_apply,
        name="ifc_spatial_structure_apply",
        description=(
            "Apply one ordered batch of 1 to 256 IFC spatial create, update, reparent, "
            "placement or delete operations. Read the current structure first and use exact "
            "GlobalIds; Runner supplies action identity. A failed batch publishes no partial edit."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    )
    spatial_apply.parameters = compact_provider_input_schema(
        dereference_refs(spatial_apply.parameters)
    )
    server.add_tool(spatial_apply)

    async def ifc_spatial_structure_list(
        spatial_classes: tuple[IfcSpatialClassInput, ...] = (),
        global_ids: tuple[IfcGlobalIdInput, ...] = (),
        parent_global_id: str | None = None,
        page_size: int = 64,
        cursor: str | None = None,
    ) -> IfcSpatialStructureListResult:
        value: dict[str, Any] = (
            {"cursor": cursor} if cursor is not None else {
                "spatial_classes": spatial_classes, "global_ids": global_ids,
                "parent_global_id": parent_global_id, "page_size": page_size,
            }
        )
        request = validate_runner_ifc_spatial_list(value)
        body = (
            {"query": None, "cursor": request.cursor}
            if isinstance(request, RunnerIfcSpatialListContinuationInput)
            else {"query": request.model_dump(mode="json"), "cursor": None}
        )
        payload = await call("/internal/domain/ifc/spatial-structure/list", body)
        return IfcSpatialStructureListResult.model_validate(payload)

    spatial_list = FunctionTool.from_function(
        ifc_spatial_structure_list,
        name="ifc_spatial_structure_list",
        description=(
            "Read bounded IFC spatial nodes by class, GlobalId or parent, with page_size up "
            "to 256. First page uses filters; later pages pass only the returned cursor. "
            "A changed or invalid cursor is rejected."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    spatial_list.parameters = provider_input_schema(
        RunnerIfcSpatialListInput, omit_generated_titles=True,
    )
    server.add_tool(spatial_list)

    async def ifc_query_python(
        mode: str,
        inputs: tuple[IfcQueryExactInput, ...] = (),
        code: str | None = None,
        parent_attempt_alias: str | None = None,

        edits: tuple[IfcQuerySourceEdit, ...] | None = None,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> IfcQueryPythonResult:
        value: dict[str, Any] = {"mode": mode}
        if mode == "fresh":
            value.update(inputs=inputs, code=code, page_size=page_size)
        elif mode == "repair":
            value.update(
                parent_attempt_alias=parent_attempt_alias,

                edits=edits, page_size=page_size,
            )
        elif mode == "cursor":
            value["cursor"] = cursor
        request = validate_runner_ifc_query(value)
        payload = await call(
            "/internal/domain/ifc/query/python", request.model_dump(mode="json"),
        )
        return IfcQueryPythonResult.model_validate(payload)

    query = FunctionTool.from_function(
        ifc_query_python,
        name="ifc_query_python",
        description=(
            "Run bounded read-only Python against exact CURRENT IFC and optional exact Data "
            "Payloads, repair a saved attempt using its alias and edits, or read its next "
            "result page. Use one complete fresh, repair or cursor branch; page_size is 1 to "
            "200. Runner supplies action identity. Query failures preserve the saved attempt. "
            "Program API: " + query_docs.IFC_QUERY_PROGRAM_RESOURCE_URI + "."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    )
    query.parameters = provider_input_schema(RunnerIfcQueryInput, omit_generated_titles=True)
    server.add_tool(query)

    async def ifc_products_apply(
        mode: str,
        family_version_alias: str | None = None,
        package_alias: str | None = None,
        payload_name: str | None = None,
        operation_ref: str | None = None,
    ) -> IfcProductsApplyResult:
        values = {"mode": mode}
        values.update({name: value for name, value in dict(
            family_version_alias=family_version_alias, package_alias=package_alias,
            payload_name=payload_name, operation_ref=operation_ref,
        ).items() if value is not None})
        request = TypeAdapter(ProductsApplyInput).validate_python(values)
        result = await call("/internal/domain/ifc/products/apply", request.model_dump(mode="json"))
        return TypeAdapter(IfcProductsApplyResult).validate_python(result)

    products_apply = FunctionTool.from_function(
        ifc_products_apply, name="ifc_products_apply", description=products_docs.APPLY_DESCRIPTION,
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        output_schema=_object_union_output_schema(IfcProductsApplyResult, compact=True),
    )
    products_apply.parameters = provider_input_schema(ProductsApplyInput, omit_generated_titles=True)
    server.add_tool(products_apply)
    register_provider_document(server, tool_name="ifc_products_apply", document_name="input",
        description="Single-Family Parquet input, report tables and original-operation recovery.",
        document=products_docs.INPUT_GUIDE)

    async def ifc_products_edit(mode: str, package_alias: str | None = None,
        payload_name: str | None = None, base_revision_ref: str | None = None,
        operation_ref: str | None = None) -> IfcProductsEditResult:
        values = {"mode": mode}
        values.update({name: value for name, value in dict(package_alias=package_alias,
            payload_name=payload_name, base_revision_ref=base_revision_ref, operation_ref=operation_ref).items()
            if value is not None})
        request = TypeAdapter(ProductsEditInput).validate_python(values)
        result = await call("/internal/domain/ifc/products/edit", request.model_dump(mode="json"))
        return TypeAdapter(IfcProductsEditResult).validate_python(result)

    edit = FunctionTool.from_function(ifc_products_edit, name="ifc_products_edit",
        description=products_docs.EDIT_DESCRIPTION,
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
        output_schema=_object_union_output_schema(IfcProductsEditResult, compact=True))
    edit.parameters = provider_input_schema(ProductsEditInput, omit_generated_titles=True)
    server.add_tool(edit)
    register_provider_document(server, tool_name="ifc_products_edit", document_name="input",
        description=products_docs.EDIT_INPUT_GUIDE_DESCRIPTION, document=products_docs.EDIT_INPUT_GUIDE)

    async def ifc_products_read(mode: str, operation_ref: str | None = None,
        product_keys: list[str] | None = None, view: str | None = None,
        global_ids: list[str] | None = None, groups: IfcProductsReadGroups | None = None) -> IfcProductsReadResult:
        values = {"mode": mode}
        values.update({name: value for name, value in dict(operation_ref=operation_ref,
            product_keys=product_keys, view=view, global_ids=global_ids, groups=groups).items()
            if value is not None})
        request = TypeAdapter(ProductsReadInput).validate_python(values)
        payload = await call("/internal/domain/ifc/products/read", request.model_dump(mode="json"))
        return IfcProductsReadResult.model_validate(payload)

    register_provider_tool(server, ifc_products_read, name="ifc_products_read", description=products_docs.READ_DESCRIPTION,
        input_type=ProductsReadInput, output_type=IfcProductsReadResult,
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        compact_input=True, meta={"effect": "READ", "parallel_safe": True})
    register_provider_document(server, tool_name="ifc_products_read", document_name="usage",
        description="Selected product views, group discovery and values, exact Apply association and Data navigation.",
        document=products_docs.READ_GUIDE)


__all__ = ["RunnerIfcBackend", "register_runner_ifc_tools"]
