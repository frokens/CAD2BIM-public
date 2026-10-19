"""Runner adapter for the shared, durable scoped reference index.

The Backend owns identity, the index and authenticated references. This provider
only selects declared fields and sends one batch per direction to that owner.
"""

import json
import re

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from CAD2BIM_mcp.reference_projection import (
    REFERENCE_TOOL_NAMES, project_reference_fields, reference_schema_projection, result_operation_alias,
)
from CAD2BIM_mcp.runner.backend_client import BackendDomainError, RunnerStateBackendClient
from CAD2BIM_mcp.runner.domain_errors import backend_domain_tool_error
from CAD2BIM_mcp.runner.resources import request_domain_credential
from CAD2BIM_mcp.tool_docs.common import REFERENCE_USAGE_NAVIGATION
from CAD2BIM_mcp.direct.provider_schema import compact_provider_input_schema, share_provider_input_nodes


def project_runner_tool_declaration(tool):
    """Identity-free public declaration used by discovery and trusted bootstrap."""
    parameters = compact_provider_input_schema(tool.parameters)
    output = tool.output_schema
    description = tool.description or ""
    if tool.name in REFERENCE_TOOL_NAMES:
        parameters, _ = reference_schema_projection(parameters)
        output, _ = reference_schema_projection(output)
        if REFERENCE_USAGE_NAVIGATION not in description:
            description += REFERENCE_USAGE_NAVIGATION
    return tool.model_copy(update={
        "parameters": share_provider_input_nodes(parameters),
        "output_schema": output, "description": description,
    })


class RunnerReferenceMiddleware(Middleware):
    def __init__(self, backend: RunnerStateBackendClient) -> None:
        self._backend = backend

    async def on_list_tools(self, context, call_next):
        return [project_runner_tool_declaration(tool) for tool in await call_next(context)]

    async def _project(self, payloads, paths, *, direction):
        selected = {}
        selected_paths = paths if isinstance(paths, list) else [paths] * len(payloads)

        async def collect(value, kinds):
            if (direction == "shorten" and value.startswith("cur.")) or (
                direction == "expand" and not value.startswith(("cur.", "op_", "data_"))
            ):
                selected.setdefault((value, kinds), None)
            return value

        for payload, payload_paths in zip(payloads, selected_paths, strict=True):
            await project_reference_fields(payload, payload_paths, collect)
        if not selected:
            return payloads
        translated = await self._backend.translate_references(
            access_token=request_domain_credential(), direction=direction,
            items=[{"value": value, "kinds": list(kinds)} for value, kinds in selected],
        )
        mapping = dict(zip(selected, translated, strict=True))

        async def replace(value, kinds):
            return mapping.get((value, kinds), value)

        return [await project_reference_fields(payload, payload_paths, replace)
                for payload, payload_paths in zip(payloads, selected_paths, strict=True)]

    async def on_call_tool(self, context, call_next):
        if context.message.name not in REFERENCE_TOOL_NAMES:
            return await call_next(context)
        tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
        _, input_paths = reference_schema_projection(tool.parameters)
        _, output_paths = reference_schema_projection(tool.output_schema)
        output_paths = getattr(tool, "_reference_output_paths", {}) or output_paths
        try:
            from CAD2BIM_mcp.direct.tool_support import decode_provider_json_object, ProviderJsonDocumentError
            payloads, paths, documents = [context.message.arguments or {}], [input_paths], []
            for field, (schema, maximum) in getattr(tool, "_reference_documents", {}).items():
                parts = field.split(".")
                container = payloads[0]
                for part in parts[:-1]:
                    container = container.get(part, {}) if isinstance(container, dict) else {}
                if not isinstance(container, dict) or parts[-1] not in container:
                    continue
                payloads.append(decode_provider_json_object(container[parts[-1]], label=field, maximum_utf8_bytes=maximum))
                paths.append(reference_schema_projection(schema)[1])
                documents.append(parts)
            arguments, *translated_documents = await self._project(payloads, paths, direction="expand")
            for parts, document, original in zip(documents, translated_documents, payloads[1:], strict=True):
                if document == original:
                    continue
                container = arguments
                for part in parts[:-1]:
                    container = container[part]
                container[parts[-1]] = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        except ProviderJsonDocumentError as exc:
            raise ToolError(f"INVALID_ARGUMENT; phase=preflight; retryable=false; operation_status=not_started; message={exc}") from None
        except BackendDomainError as exc:
            if exc.status_code == 409 and isinstance(exc.detail, dict) and exc.detail.get("code") == "REFERENCE_INVALID":
                # This invocation has not reached the business tool. A failed output
                # translation is handled separately and never receives this proof.
                raise ToolError(
                    "REFERENCE_INVALID; phase=preflight; retryable=false; operation_status=not_started; "
                    "message=Copy the correct current-scope typed reference from Catalog or Operation. "
                    "The business tool was not invoked."
                ) from None
            raise backend_domain_tool_error(exc) from None
        request = context.message.model_copy(update={"arguments": arguments})
        try:
            result = await call_next(context.copy(message=request))
        except ToolError as exc:
            # Query errors carry an exact attempt locator in the bounded diagnostic
            # prefix. Never rewrite source/user text in the message body.
            error_text, diagnostic_separator, diagnostic_json = str(exc).partition("; diagnostic=")
            prefix, separator, message = error_text.partition("; message=")
            match = re.search(r"(?:^|; )attempt_alias=(cur\.[A-Za-z0-9._~-]+)(?=;|$)", prefix)
            try:
                diagnostic = json.loads(diagnostic_json) if diagnostic_separator else None
                projected, projected_diagnostic = await self._project(
                    [{"attempt_alias": match.group(1)} if match else {}, diagnostic],
                    {("attempt_alias",): ("gq",)}, direction="shorten",
                )
            except Exception:
                raise exc from None
            if match:
                prefix = prefix[:match.start(1)] + projected["attempt_alias"] + prefix[match.end(1):]
            suffix = diagnostic_separator + json.dumps(projected_diagnostic, ensure_ascii=False, separators=(",", ":")) if diagnostic_separator else ""
            raise ToolError(prefix + separator + message + suffix) from None

        try:
            payloads = [result.structured_content]
            text_indexes = []
            for index, block in enumerate(result.content):
                if block.type == "text":
                    try:
                        payload = json.loads(block.text)
                    except ValueError:
                        continue
                    payloads.append(payload)
                    text_indexes.append(index)
            projected = await self._project(payloads, output_paths, direction="shorten")
            content = list(result.content)
            for index, payload in zip(text_indexes, projected[1:], strict=True):
                content[index] = content[index].model_copy(update={"text": json.dumps(payload, ensure_ascii=False)})
            prepared = result.model_copy(update={"structured_content": projected[0], "content": content})
        except Exception:
            alias = result_operation_alias(result)
            operation = f"; operation_alias={alias}" if isinstance(alias, str) and re.fullmatch(r"op_[0-9]{3,}", alias) else ""
            raise ToolError(
                "OUTPUT_REFERENCE_PROJECTION_FAILED; phase=output_validation; retryable=false; "
                f"operation_status=unknown{operation}; message=The business tool returned a result but "
                "its references could not be prepared. Preserve the original action and request; "
                "reconcile its Operation before any new write."
            ) from None

        from CAD2BIM_backend.application.preview_report_projection import preview_response_budget_error
        error = preview_response_budget_error(prepared.structured_content)
        if error is not None:
            raise ToolError(error)
        return prepared
