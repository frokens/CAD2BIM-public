"""Schema-derived Direct reference projection over the existing authenticated owners.

Only declared reference fields are traversed. User source, arbitrary JSON data,
pagination cursors and business object identities are never rewritten.
"""

import json
from uuid import uuid4

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from mcp.types import TextContent

from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.tool_docs.common import REFERENCE_USAGE_DESCRIPTION as REFERENCE_USAGE_DESCRIPTION


from CAD2BIM_mcp.reference_projection import (
    REFERENCE_TOOL_NAMES, reference_schema_projection, project_reference_fields, result_operation_alias,
)


class DirectReferenceMiddleware(Middleware):
    def __init__(self, service: OpaqueReferenceService, scope_resolver, scope_binder) -> None:
        self._service = service
        self._scope_resolver = scope_resolver
        self._scope_binder = scope_binder

    async def _scope(self, alias):
        return await self._scope_resolver.resolve(alias, command_id=f"reference_{uuid4().hex}")

    @staticmethod
    def _failure(exc):
        return _tool_error(DirectApplicationError(
            "REFERENCE_INVALID", f"{exc}; copy the correct typed reference unchanged from a "
            "current-scope list/info/write response; discard the invalid value", phase="preflight",
        ))

    async def on_call_tool(self, context, call_next):
        if context.message.name not in REFERENCE_TOOL_NAMES:
            return await call_next(context)
        tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
        arguments = context.message.arguments or {}
        try:
            scope = await self._scope(arguments.get("direct_session_alias"))
            incoming, outgoing = {}, {}

            async def expand(value, kinds):
                # Existing op_NNN/data_NNN owners are not short-reference-index entries.
                if value.startswith(("op_", "data_", "cur.")):
                    return value
                key = (value, kinds)
                if key not in incoming:
                    with self._scope_binder(scope.identity):
                        incoming[key] = await self._service.expand(scope.identity, value, kinds)
                return incoming[key]

            async def shorten(value, kinds):
                if not value.startswith("cur."):
                    return value
                key = (value, kinds)
                if key not in outgoing:
                    with self._scope_binder(scope.identity):
                        outgoing[key] = await self._service.shorten(scope.identity, value, kinds)
                return outgoing[key]

            expanded = await project_reference_fields(arguments, tool._reference_input_paths, expand)
            for field, (schema, maximum) in tool._reference_documents.items():
                container = expanded
                parts = field.split(".")
                for part in parts[:-1]:
                    container = container.get(part, {}) if isinstance(container, dict) else {}
                leaf = parts[-1]
                if not isinstance(container, dict) or leaf not in container:
                    continue
                from CAD2BIM_mcp.direct.tool_support import (
                    ProviderJsonDocumentError, decode_provider_json_object,
                )

                try:
                    document = decode_provider_json_object(container[leaf], label=field,
                                                          maximum_utf8_bytes=maximum)
                except ProviderJsonDocumentError as exc:
                    raise _tool_error(DirectApplicationError(
                        "INVALID_ARGUMENT", str(exc), phase="preflight",
                    )) from None
                _, paths = reference_schema_projection(schema)
                translated = await project_reference_fields(document, paths, expand)
                if translated != document:
                    container[leaf] = json.dumps(translated, ensure_ascii=False, separators=(",", ":"))
            request = context.message.model_copy(update={"arguments": expanded})
        except ReferenceIndexError as exc:
            raise self._failure(exc) from None
        except DataRuntimeApplicationError as exc:
            raise _tool_error(DirectApplicationError(exc.code, str(exc), phase="preflight")) from None

        try:
            result = await call_next(context.copy(message=request))
        except ToolError as exc:
            # Failure to shorten a diagnostic must preserve the original business error.
            prefix, separator, diagnostic = str(exc).partition("; diagnostic=")
            if not separator:
                raise
            try:
                payload = json.loads(diagnostic)
                translated = await project_reference_fields(payload, {("attempt_alias",): ("gq",)}, shorten)
            except Exception:
                raise exc from None
            if translated == payload:
                raise
            raise ToolError(prefix + separator + json.dumps(translated, separators=(",", ":"))) from None

        try:
            structured = await project_reference_fields(result.structured_content,
                                                         tool._reference_output_paths, shorten)
            content = []
            for block in result.content:
                if block.type == "text":
                    try:
                        payload = json.loads(block.text)
                    except ValueError:
                        content.append(block)
                        continue
                    translated = await project_reference_fields(payload, tool._reference_output_paths, shorten)
                    content.append(TextContent(type="text", text=json.dumps(translated, ensure_ascii=False)))
                else:
                    content.append(block)
            prepared = result.model_copy(update={"structured_content": structured, "content": content})
        except Exception:
            raise _tool_error(DirectApplicationError(
                "OUTPUT_REFERENCE_PROJECTION_FAILED",
                "The tool returned a result, but its output references could not be prepared. "
                "Preserve the original request and recover any write through its original Operation; "
                "do not create a replacement write.",
                phase="output_validation",
                operation_alias=result_operation_alias(result),
                diagnostic={"tool_result_returned": True},
            )) from None

        from CAD2BIM_backend.application.preview_report_projection import preview_response_budget_error
        error = preview_response_budget_error(prepared.structured_content)
        if error is not None:
            raise ToolError(error)
        return prepared
