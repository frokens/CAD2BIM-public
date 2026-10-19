"""FastMCP registrations for Direct geometry-resource discovery and inspection."""

from __future__ import annotations

from CAD2BIM_backend.domain.preview_cases import GeometryCasesInput, PreviewCaseRender
from CAD2BIM_mcp.direct.ifc.preview_cases import GeometryPreviewSource, CASES_MAX_BYTES
from CAD2BIM_mcp.direct.provider_schema import provider_reference_schema
from CAD2BIM_mcp.direct.tool_support import register_provider_document

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_inspection import GeometryInspectionCoordinates, GeometryInspectionSelection
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionObservation

import base64
import json
from fastmcp import Context, FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic import ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.geometry_preview_contracts import IfcGeometryPreviewReferences
from CAD2BIM_mcp.direct.ifc.geometry.application import GeometryProviderApplication
from CAD2BIM_mcp.direct.ifc.geometry.contracts import (
    DirectIfcGeometryPreviewInput,
    DirectIfcGeometryQueryPythonInput,
    DirectIfcGeometryResourceInfoInput,
    DirectIfcGeometryResourceListInput,
    DirectIfcGeometryResourceSourceReadInput,
    IfcGeometryPreviewOutcomeResult,
    IfcGeometryQueryFreshTarget,
    IfcGeometryQueryPythonResult,
    IfcGeometryResourceVersionQueryTarget,
    IfcGeometryInspectionQueryTarget,
    IfcFamilyStandardModelQueryTarget,
    IfcGeometryResourceInfoTarget,
    IfcGeometryResourceInfoResult,
    IfcGeometryResourceKind,
    IfcGeometryResourceListResult,
    IfcGeometrySourceEdit,
    IfcGeometryResourceSourceReadResult,
    validate_geometry_preview_input,
    validate_geometry_query_input,
    validate_geometry_resource_list_input,
    validate_geometry_resource_source_read_input,
)
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.references import reference_schema_projection
from CAD2BIM_mcp.direct.tool_support import (
    DIRECT_SESSION_ALIAS as _DIRECT_ALIAS,
    invalid_provider_input as _invalid,
    register_provider_tool as _register,
)
from CAD2BIM_mcp.tool_docs.ifc_geometry import IFC_GEOMETRY_PREVIEW_DESCRIPTION
from CAD2BIM_mcp.tool_docs import ifc_geometry as _tool_docs


def register_ifc_geometry_resource_list(
    server: FastMCP,
    application: GeometryProviderApplication,
) -> None:
    async def ifc_geometry_resource_list(
        direct_session_alias: _DIRECT_ALIAS,
        resource_kinds: tuple[IfcGeometryResourceKind, ...] = (),
        page_size: int = 50,
        cursor: str | None = None,
    ) -> IfcGeometryResourceListResult:
        try:
            value: dict[str, object] = {"direct_session_alias": direct_session_alias}
            if cursor is None:
                value.update(resource_kinds=resource_kinds, page_size=page_size)
            else:
                value["cursor"] = cursor
            request = validate_geometry_resource_list_input(value)
            return await application.list_ifc_geometry_resources(request)
        except ValidationError as exc:
            raise _invalid(
                "Submit either complete first-page geometry filters or one unchanged cursor",
                exc,
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    _register(
        server,
        ifc_geometry_resource_list,
        name="ifc_geometry_resource_list",
        compact_input=True,
        input_type=DirectIfcGeometryResourceListInput,
        output_type=IfcGeometryResourceListResult,
        description=(_tool_docs.IFC_GEOMETRY_RESOURCE_LIST_DESCRIPTION),
    )


def register_ifc_geometry_resource_info(
    server: FastMCP,
    application: GeometryProviderApplication,
) -> None:
    async def ifc_geometry_resource_info(
        direct_session_alias: _DIRECT_ALIAS,
        target: IfcGeometryResourceInfoTarget,
    ) -> IfcGeometryResourceInfoResult:
        try:
            request = DirectIfcGeometryResourceInfoInput(
                direct_session_alias=direct_session_alias,
                target=target,
            )
            return await application.get_ifc_geometry_resource_info(request)
        except ValidationError as exc:
            raise _invalid(
                "Use one complete resource_root or resource_version target", exc
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    _register(
        server,
        ifc_geometry_resource_info,
        name="ifc_geometry_resource_info",
        compact_input=True,
        compact_output=True,
        input_type=DirectIfcGeometryResourceInfoInput,
        output_type=IfcGeometryResourceInfoResult,
        description=(_tool_docs.IFC_GEOMETRY_RESOURCE_INFO_DESCRIPTION),
    )


def _source_value(
    *,
    direct_session_alias: str,
    resource_version_alias: str | None,
    source_path: str | None,
    attempt_alias: str | None,
    view_mode: str,
    start_line: int | None,
    line_count: int | None,
    text: str | None,
    context_lines: int,
    max_matches: int,
) -> dict[str, object]:
    value: dict[str, object] = {
        "direct_session_alias": direct_session_alias,
        "view_mode": view_mode,
    }
    if resource_version_alias is not None:
        value["resource_version_alias"] = resource_version_alias
    if source_path is not None:
        value["source_path"] = source_path
    if attempt_alias is not None:
        value["attempt_alias"] = attempt_alias
    if view_mode == "lines":
        value.update(start_line=start_line, line_count=line_count)
    elif view_mode == "search":
        value.update(text=text, context_lines=context_lines, max_matches=max_matches)
    return value


def register_ifc_geometry_resource_source_read(
    server: FastMCP,
    application: GeometryProviderApplication,
) -> None:
    async def ifc_geometry_resource_source_read(
        direct_session_alias: _DIRECT_ALIAS,
        resource_version_alias: str,
        source_path: str,
        view_mode: str = "full",
        start_line: int | None = None,
        line_count: int | None = None,
        text: str | None = None,
        context_lines: int = 3,
        max_matches: int = 20,
    ) -> IfcGeometryResourceSourceReadResult:
        try:
            request = validate_geometry_resource_source_read_input(
                _source_value(
                    direct_session_alias=direct_session_alias,
                    resource_version_alias=resource_version_alias,
                    source_path=source_path,
                    attempt_alias=None,
                    view_mode=view_mode,
                    start_line=start_line,
                    line_count=line_count,
                    text=text,
                    context_lines=context_lines,
                    max_matches=max_matches,
                )
            )
            return await application.read_ifc_geometry_resource_source(request)
        except ValidationError as exc:
            raise _invalid(
                "Use one complete full, lines, or search source-read branch", exc
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    _register(
        server,
        ifc_geometry_resource_source_read,
        name="ifc_geometry_resource_source_read",
        compact_input=True,
        input_type=DirectIfcGeometryResourceSourceReadInput,
        output_type=IfcGeometryResourceSourceReadResult,
        description=(_tool_docs.IFC_GEOMETRY_RESOURCE_SOURCE_READ_DESCRIPTION),
    )


def register_ifc_geometry_preview(
    server: FastMCP,
    application: GeometryProviderApplication,
) -> None:
    async def ifc_geometry_preview(
        direct_session_alias: _DIRECT_ALIAS,
        ctx: Context,
        mode: str,
        source: GeometryPreviewSource | None = None,
        sample_parameters_json: str | None = None,
        preview_profile_json: str | None = None,
        cases_json: str | None = None,
        render_json: str | None = None,
        datum_transform: RigidTransform3D | None = None,
        reference_geometries: IfcGeometryPreviewReferences | None = None,
        retain: bool = False,
        inspection_ref: str | None = None,
        family_version_alias: str | None = None,
        selection: GeometryInspectionSelection | None = None,
        coordinate_space: GeometryInspectionCoordinates | None = None,
        observation: GeometryInspectionObservation | None = None,
    ) -> ToolResult:
        try:
            value: dict[str, object] = {"direct_session_alias": direct_session_alias, "mode": mode}
            values = dict(source=source, sample_parameters_json=sample_parameters_json,
                preview_profile_json=preview_profile_json, cases_json=cases_json, render_json=render_json,
                datum_transform=datum_transform, reference_geometries=reference_geometries,
                inspection_ref=inspection_ref, selection=selection, coordinate_space=coordinate_space,
                observation=observation, family_version_alias=family_version_alias)
            value.update({key: item for key, item in values.items() if item is not None})
            if mode not in {"inspection", "family_standard_model"}:
                value["retain"] = retain
            request = validate_geometry_preview_input(value)
            authorization = await application.preview_ifc_geometry(
                request, request_identity=server_ifc_invocation_key(ctx),
            )
            preview = authorization.preview
            content: list[TextContent | ImageContent] = [
                TextContent(type="text", text=preview.model_dump_json(exclude_none=True)),
            ]
            images = (() if authorization.content is None else (authorization.content,)) + authorization.images
            content.extend(ImageContent(type="image", data=base64.b64encode(item).decode("ascii"),
                                        mimeType="image/png") for item in images)
            return ToolResult(
                content=content,
                structured_content=preview.model_dump(mode="json", exclude_none=True),
            )
        except ValidationError as exc:
            raise _invalid("Preview requires one complete single, cases, or inspection branch", exc) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    cases_schema = provider_reference_schema(GeometryCasesInput)
    register_provider_document(
        server, tool_name="ifc_geometry_preview", document_name="tutorial",
        description=_tool_docs.GEOMETRY_PREVIEW_TUTORIAL_DESCRIPTION,
        document=_tool_docs.GEOMETRY_PREVIEW_TUTORIAL,
    )
    register_provider_document(
        server, tool_name="ifc_geometry_preview", document_name="cases_json",
        description=_tool_docs.GEOMETRY_PREVIEW_CASES_RESOURCE_DESCRIPTION,
        document=cases_schema, schema_roots=((),),
    )
    register_provider_document(
        server, tool_name="ifc_geometry_preview", document_name="render_json",
        description=_tool_docs.GEOMETRY_PREVIEW_RENDER_RESOURCE_DESCRIPTION,
        document=provider_reference_schema(PreviewCaseRender), schema_roots=((),),
    )
    _register(
        server,
        ifc_geometry_preview,
        name="ifc_geometry_preview",
        reference_documents={"cases_json": (cases_schema, CASES_MAX_BYTES)},
        compact_input=True,
        compact_output=True,
        multimodal_output=True,
        input_type=DirectIfcGeometryPreviewInput,
        output_type=IfcGeometryPreviewOutcomeResult,
        description=IFC_GEOMETRY_PREVIEW_DESCRIPTION,
        annotations={"readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False},
    )


def register_ifc_geometry_query_python(
    server: FastMCP,
    application: GeometryProviderApplication,
) -> None:
    async def ifc_geometry_query_python(
        direct_session_alias: _DIRECT_ALIAS,
        ctx: Context,
        mode: str,
        target: IfcGeometryQueryFreshTarget | None = None,
        code: str | None = None,
        parent_attempt_alias: str | None = None,

        edits: tuple[IfcGeometrySourceEdit, ...] | None = None,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> IfcGeometryQueryPythonResult:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias,
                "mode": mode,
            }
            if mode == "fresh":
                value.update(
                    target=target,
                    code=code,
                    page_size=page_size,
                )
            elif mode == "repair":
                value.update(
                    parent_attempt_alias=parent_attempt_alias,

                    edits=edits,
                    page_size=page_size,
                )
            elif mode == "cursor":
                value["cursor"] = cursor
            request = validate_geometry_query_input(value)
            return await application.query_ifc_geometry_python(
                request,
                request_identity=server_ifc_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise _invalid(
                "Use one complete fresh, repair, or cursor geometry-query branch", exc
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_document(
        server,
        tool_name="ifc_geometry_query_python",
        document_name="program_api",
        description=_tool_docs.GEOMETRY_QUERY_PROGRAM_RESOURCE_DESCRIPTION,
        document={
            "usage": _tool_docs.GEOMETRY_QUERY_PROGRAM_USAGE,
            "target_schemas": {
                "geometry_resource_version": (
                    reference_schema_projection(
                        provider_reference_schema(
                            IfcGeometryResourceVersionQueryTarget,
                            mode="validation",
                        )
                    )[0]
                ),
                "geometry_inspection": reference_schema_projection(
                    provider_reference_schema(IfcGeometryInspectionQueryTarget, mode="validation")
                )[0],
                "family_standard_model": reference_schema_projection(
                    provider_reference_schema(IfcFamilyStandardModelQueryTarget, mode="validation")
                )[0],
            },
            "return_contract": _tool_docs.GEOMETRY_QUERY_RETURN_CONTRACT,
            "examples": {
                "bounding_box": json.loads(_tool_docs.GEOMETRY_QUERY_FRESH_EXAMPLE),
                "display_diagnostics": _tool_docs.GEOMETRY_QUERY_DISPLAY_DIAGNOSTICS_EXAMPLE,
                "plane_section": json.loads(
                    _tool_docs.GEOMETRY_QUERY_SECTION_FRESH_EXAMPLE
                ),
            },
        },
        schema_roots=(("target_schemas", "geometry_resource_version"),
                      ("target_schemas", "geometry_inspection"),
                      ("target_schemas", "family_standard_model")),
    )
    _register(
        server,
        ifc_geometry_query_python,
        name="ifc_geometry_query_python",
        compact_input=True,
        input_type=DirectIfcGeometryQueryPythonInput,
        output_type=IfcGeometryQueryPythonResult,
        description=(_tool_docs.IFC_GEOMETRY_QUERY_PYTHON_DESCRIPTION),
    )


IFC_GEOMETRY_TOOL_REGISTRARS = (
    register_ifc_geometry_resource_list,
    register_ifc_geometry_resource_info,
    register_ifc_geometry_resource_source_read,
    register_ifc_geometry_preview,
    register_ifc_geometry_query_python,
)


__all__ = [
    "IFC_GEOMETRY_TOOL_REGISTRARS",
    "register_ifc_geometry_preview",
    "register_ifc_geometry_query_python",
    "register_ifc_geometry_resource_info",
    "register_ifc_geometry_resource_list",
    "register_ifc_geometry_resource_source_read",
]
