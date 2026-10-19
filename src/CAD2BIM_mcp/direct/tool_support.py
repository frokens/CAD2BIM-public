"""Small shared FastMCP helpers for closed Direct Provider contracts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Collection
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.resources.types import TextResource
from jsonschema.validators import validator_for
from pydantic import Field, PrivateAttr, TypeAdapter, ValidationError
from pydantic.json_schema import GenerateJsonSchema

from CAD2BIM_backend.application.validation_support import (
    json_schema_validation_message,
    select_json_schema_error,
    validation_error_message,
)
from CAD2BIM_backend.infrastructure.execution_observability import (
    execution_observation_scope,
    observe_execution_phase,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.provider_schema import (
    _SchemaWithoutGeneratedTitles, compact_provider_input_schema,
    provider_input_array_schema, provider_output_schema,
    share_provider_reference_types,
    share_provider_input_nodes,
)
from CAD2BIM_mcp.reference_projection import (
    REFERENCE_TOOL_NAMES, reference_schema_projection,
)
from CAD2BIM_mcp.tool_docs import common as _tool_docs
from CAD2BIM_mcp.tool_docs.preview import PREVIEW_RESULT_DOCUMENTS


DIRECT_SESSION_ALIAS = Annotated[
    str,
    Field(
        pattern=r"^direct_[0-9]{3,}$",
        max_length=32,
        description=_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    ),
]
READ_BATCH_DESCRIPTION = _tool_docs.READ_BATCH_DESCRIPTION
READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
}


class ProviderJsonDocumentError(ValueError):
    """One safe, bounded Provider JSON-document validation failure."""


class ProviderFunctionTool(FunctionTool):
    """FunctionTool whose runtime input uses the same closed Provider contract it advertises."""

    _provider_input_adapter: TypeAdapter[object] | None = PrivateAttr(default=None)
    _reference_input_paths: dict = PrivateAttr(default_factory=dict)
    _reference_output_paths: dict = PrivateAttr(default_factory=dict)
    _reference_documents: dict = PrivateAttr(default_factory=dict)
    _multimodal_output: bool = PrivateAttr(default=False)

    async def run(self, arguments: dict[str, object]):
        if self._provider_input_adapter is None:  # pragma: no cover - registration invariant
            raise RuntimeError("Provider input adapter is not configured")
        input_sha256 = hashlib.sha256(
            json.dumps(
                arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        with execution_observation_scope(operation=self.name, input_sha256=input_sha256):
            with observe_execution_phase("direct_provider", "parse"):
                try:
                    self._provider_input_adapter.validate_python(arguments)
                except ValidationError as exc:
                    # Only these existing authored document errors are safe to
                    # project. Arbitrary validator messages may contain inputs.
                    document_reasons = {
                        str(error)
                        for detail in exc.errors(include_url=False, include_input=False)
                        if isinstance(
                            error := detail.get("ctx", {}).get("error"),
                            ProviderJsonDocumentError,
                        )
                    }
                    raise invalid_provider_input(
                        provider_validation_message(
                            "Use one complete closed Provider input branch",
                            exc,
                            include_value_error_reason=True,
                            allowed_value_error_reasons=document_reasons,
                            input_schema=self.parameters,
                            input_value=arguments,
                        ),
                        exc,
                    ) from None
            with observe_execution_phase("direct_provider", "dispatch"):
                result = await super().run(arguments)
                if self._multimodal_output:
                    # Native clients may select structuredContent instead of the
                    # ordered content blocks, silently dropping inline images.
                    # The typed result is already serialized in TextContent.
                    return result.model_copy(update={"structured_content": None})
                return result


class DirectProviderInputSchemaMiddleware(Middleware):
    """Project list declarations after default dereferencing, never stored tools."""

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool.model_copy(update={"parameters": share_provider_input_nodes(tool.parameters)})
                for tool in tools]


class DirectToolInputErrorMiddleware(Middleware):
    """Project FastMCP argument validation without exposing supplied values."""

    async def on_call_tool(self, context, call_next):
        if context.fastmcp_context is None:
            raise RuntimeError("Direct tool input validation context is unavailable")
        tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
        if tool is None:
            raise RuntimeError("Direct tool input validation target is unavailable")
        validator = validator_for(tool.parameters)
        validator.check_schema(tool.parameters)
        errors = tuple(validator(tool.parameters).iter_errors(context.message.arguments or {}))
        exc = select_json_schema_error(errors, schema=tool.parameters)
        if exc is not None:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    json_schema_validation_message(
                        "Use inputs that satisfy the advertised tool schema",
                        exc,
                        query_pagination=context.message.name in {"data_query_sql", "data_query_python"},
                        input_value=context.message.arguments,
                    ),
                    phase="preflight",
                )
            ) from None
        try:
            if isinstance(tool, ProviderFunctionTool):
                return await call_next(context)
            # Plain FunctionTools (including Data/CAD) use the same per-call
            # observation owner as Provider tools; otherwise sibling queue and
            # execution phases receive unrelated, unnamed correlation IDs.
            input_sha256 = hashlib.sha256(
                json.dumps(
                    context.message.arguments or {},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            with execution_observation_scope(
                operation=context.message.name, input_sha256=input_sha256
            ), observe_execution_phase("direct_provider", "dispatch"):
                return await call_next(context)
        except FastMCPValidationError as exc:
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                message = provider_validation_message(
                    "Use inputs that satisfy the advertised tool schema",
                    cause,
                )
                raise invalid_provider_input(message, cause) from None
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "Use inputs that satisfy the advertised tool schema",
                    phase="preflight",
                )
            ) from None


def decode_provider_json_object(
    value: str,
    *,
    label: str,
    maximum_utf8_bytes: int,
) -> dict[str, object]:
    """Decode a closed Provider transport document without accepting ambiguous JSON."""

    if "\x00" in value:
        raise ProviderJsonDocumentError(f"{label} must not contain NUL")
    try:
        encoded_value = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProviderJsonDocumentError(f"{label} must contain valid UTF-8 text") from exc
    if len(encoded_value) > maximum_utf8_bytes:
        raise ProviderJsonDocumentError(f"{label} exceeds {maximum_utf8_bytes} UTF-8 bytes")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, item in pairs:
            if key in decoded:
                raise ProviderJsonDocumentError(f"{label} contains a duplicate object key")
            decoded[key] = item
        return decoded

    def reject_non_finite(_token: str) -> None:
        raise ProviderJsonDocumentError(f"{label} contains a non-finite number")

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except ProviderJsonDocumentError:
        raise
    except RecursionError as exc:
        raise ProviderJsonDocumentError(f"{label} nesting is too deep") from exc
    except (TypeError, ValueError) as exc:
        raise ProviderJsonDocumentError(f"{label} must contain valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProviderJsonDocumentError(f"{label} must contain one JSON object")
    try:
        json.dumps(decoded, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProviderJsonDocumentError(f"{label} must contain valid UTF-8 text") from exc
    except RecursionError as exc:
        raise ProviderJsonDocumentError(f"{label} nesting is too deep") from exc
    except ValueError as exc:
        raise ProviderJsonDocumentError(f"{label} contains a non-finite number") from exc
    return decoded


def provider_validation_message(
    message: str,
    exc: ValidationError,
    *,
    include_value_error_reason: bool = False,
    allowed_value_error_reasons: Collection[str] | None = None,
    input_schema: dict | None = None,
    input_value: object = None,
) -> str:
    """Preserve the Direct Provider compatibility surface over the shared formatter."""

    return validation_error_message(
        message,
        exc,
        include_value_error_reason=include_value_error_reason,
        allowed_value_error_reasons=allowed_value_error_reasons,
        input_schema=input_schema,
        input_value=input_value,
    )


def provider_input_schema(type_: object, *, omit_generated_titles: bool = False) -> dict[str, object]:
    generator = _SchemaWithoutGeneratedTitles if omit_generated_titles else GenerateJsonSchema
    schema = TypeAdapter(type_).json_schema(mode="validation", schema_generator=generator)
    schema["type"] = "object"
    return provider_input_array_schema(schema)


def register_provider_document(
    server: FastMCP, *, tool_name: str, document_name: str,
    description: str, document: dict[str, object],
    schema_roots: tuple[tuple[str, ...], ...] = (),
) -> None:
    """Publish a bounded static API document, with no files, identity or DB access."""
    if tool_name in REFERENCE_TOOL_NAMES and schema_roots:
        document = deepcopy(document)
        for path in schema_roots:
            parent = document
            for key in path[:-1]:
                parent = parent[key]
            schema = parent[path[-1]] if path else document
            projected, _ = reference_schema_projection(schema)
            if path:
                parent[path[-1]] = projected
            else:
                document = projected
    content = _provider_document_content(document)
    resource = TextResource(
        uri=f"cad2bim://contracts/{tool_name}/{document_name}",
        name=f"{tool_name}_{document_name}", description=description,
        mime_type="application/json", text=content,
    )
    server.add_resource(resource)
    # Created and consumed only by the Direct composition root during registration.
    # Standalone registrar tests still publish their complete local document.
    pending = getattr(server, "_provider_contract_documents", None)
    if pending is not None and schema_roots:
        pending.append((resource, deepcopy(document), schema_roots))


def _provider_document_content(document: dict) -> str:
    content = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(content.encode("utf-8")) > 65_536:
        raise ValueError("Public Provider contract document exceeds 65,536 bytes")
    return content


def register_shared_provider_documents(server: FastMCP) -> None:
    """Finish the composition's static projections, then discard its source copies."""
    pending = server._provider_contract_documents
    del server._provider_contract_documents
    roots, primary_indexes = [], set()
    for resource, document, paths in pending:
        for path in paths:
            schema = document
            for key in path:
                schema = schema[key]
            if str(resource.uri).split("/")[-2] not in {
                "ifc_geometry_preview", "ifc_product_realization_preview",
            }:
                primary_indexes.add(len(roots))
            roots.append(schema)
    projected, shared_documents = share_provider_reference_types(
        roots, common_uri=_tool_docs.COMMON_TYPES_RESOURCE_URI,
        preview_uri=_tool_docs.COMMON_PREVIEW_TYPES_RESOURCE_URI,
        primary_indexes=primary_indexes,
    )
    schemas = iter(projected)
    for resource, document, paths in pending:
        changed = False
        for path in paths:
            schema = next(schemas)
            target = document
            for key in path[:-1]:
                target = target[key]
            original = target[path[-1]] if path else document
            changed |= original != schema
            if path:
                target[path[-1]] = schema
            else:
                document = schema
        resource.text = _provider_document_content(document)
        if changed:
            resource.description += _tool_docs.COMMON_TYPES_NAVIGATION
    register_provider_document(
        server, tool_name="common", document_name="types",
        description=_tool_docs.COMMON_TYPES_RESOURCE_DESCRIPTION,
        document=shared_documents[_tool_docs.COMMON_TYPES_RESOURCE_URI],
    )
    register_provider_document(
        server, tool_name="common", document_name="preview_types",
        description=_tool_docs.COMMON_PREVIEW_TYPES_RESOURCE_DESCRIPTION,
        document=shared_documents[_tool_docs.COMMON_PREVIEW_TYPES_RESOURCE_URI],
    )
    register_provider_document(
        server, tool_name="common", document_name="usage",
        description=_tool_docs.COMMON_USAGE_RESOURCE_DESCRIPTION,
        document=_tool_docs.common_usage_document(),
    )
    register_provider_document(
        server, tool_name="common", document_name="authoring",
        description=_tool_docs.COMMON_AUTHORING_RESOURCE_DESCRIPTION,
        document=_tool_docs.common_authoring_document(),
    )
    register_provider_document(
        server, tool_name="common", document_name="preview",
        description=_tool_docs.COMMON_PREVIEW_RESOURCE_DESCRIPTION,
        document=_tool_docs.common_preview_document(),
    )


def register_provider_tool(
    server: FastMCP,
    function,
    *,
    name: str,
    description: str,
    input_type: object,
    output_type: object,
    annotations: dict[str, bool] = READ_ANNOTATIONS,
    reference_documents: dict | None = None,
    compact_output: bool = False,
    compact_input: bool = False,
    multimodal_output: bool = False,
    meta: dict[str, object] | None = None,
) -> None:
    output_schema = provider_output_schema(
        output_type, compact=compact_output, preserve_references=multimodal_output,
    )
    input_schema = provider_input_schema(input_type, omit_generated_titles=compact_input)
    if compact_input:
        input_schema = compact_provider_input_schema(input_schema)
    input_paths, output_paths = {}, {}
    if name in REFERENCE_TOOL_NAMES:
        input_schema, input_paths = reference_schema_projection(input_schema)
        output_schema, output_paths = reference_schema_projection(output_schema)
        description += _tool_docs.REFERENCE_USAGE_NAVIGATION
    if multimodal_output:
        register_provider_document(
            server, tool_name=name, document_name="result",
            description=_tool_docs.MULTIMODAL_RESULT_GUIDE_DESCRIPTION,
            document=PREVIEW_RESULT_DOCUMENTS[name],
        )
        description += _tool_docs.MULTIMODAL_RESULT_USAGE_DESCRIPTION.format(tool_name=name)
    tool = ProviderFunctionTool.from_function(
        function,
        name=name,
        description=description,
        annotations=annotations,
        output_schema=None if multimodal_output else output_schema,
        meta=meta,
    )
    tool._provider_input_adapter = TypeAdapter(input_type)
    tool._multimodal_output = multimodal_output
    tool._reference_input_paths = input_paths
    tool._reference_output_paths = output_paths
    tool._reference_documents = reference_documents or {}
    tool.parameters = input_schema
    server.add_tool(tool)


def invalid_provider_input(message: str, _exc: ValidationError) -> Exception:
    return _tool_error(DirectApplicationError("INVALID_ARGUMENT", message, phase="preflight"))


__all__ = [
    "DIRECT_SESSION_ALIAS",
    "DirectToolInputErrorMiddleware",
    "ProviderJsonDocumentError",
    "READ_ANNOTATIONS",
    "READ_BATCH_DESCRIPTION",
    "decode_provider_json_object",
    "invalid_provider_input",
    "provider_validation_message",
    "provider_input_schema",
    "register_provider_document",
    "register_provider_tool",
]
