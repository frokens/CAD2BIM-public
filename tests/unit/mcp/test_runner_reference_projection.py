"""Native Runner Provider projection through the Backend-owned reference service."""

from __future__ import annotations

import base64
from copy import deepcopy
import json

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools.tool import ToolResult
from jsonschema import Draft202012Validator
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, ConfigDict, Field

from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.domain.identity import TenantActorKind
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError
from CAD2BIM_mcp.direct.ifc.authoring_publish import AuthoringPublishResult
from CAD2BIM_mcp.direct.tool_support import DirectToolInputErrorMiddleware
from CAD2BIM_mcp.runner.authoring_tools import register_runner_authoring_tools
from CAD2BIM_mcp.runner.backend_client import BackendDomainError
from CAD2BIM_mcp.runner.references import RunnerReferenceMiddleware
from tests.unit.backend.test_opaque_reference_service import MemoryReferenceIndex, reference_codec, reference_scope
from tests.unit.mcp.test_direct_ifc_product_family_write_provider import _definition_json, _source_files


_TOKEN = "current-runner-tool-credential"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII="
)


class ReferenceBackend:
    """Thin HTTP-owner substitute; projection stays in production middleware."""

    def __init__(self, scope, references):
        self.scope, self.references = scope, references
        self.translations, self.calls, self.responses = [], [], {}
        self.output_failure = None

    async def translate_references(self, *, access_token, direction, items):
        assert access_token == _TOKEN
        self.translations.append((direction, deepcopy(items)))
        if direction == "shorten" and self.output_failure is not None:
            raise self.output_failure
        convert = self.references.expand if direction == "expand" else self.references.shorten
        try:
            with bind_tenant_execution_context(self.scope):
                return tuple([await convert(self.scope, item["value"], item["kinds"]) for item in items])
        except ReferenceIndexError as exc:
            raise BackendDomainError(409, {"code": "REFERENCE_INVALID", "message": str(exc),
                "phase": "preflight", "retryable": False}) from None

    async def call_authoring(self, *, access_token, tool_name, body):
        assert access_token == _TOKEN
        self.calls.append((tool_name, deepcopy(body)))
        response = self.responses[tool_name]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def _setup(monkeypatch):
    scope = reference_scope(actor_kind=TenantActorKind.RUNNER)
    codec, index = reference_codec(), MemoryReferenceIndex()
    references = OpaqueReferenceService(index, codec)
    backend = ReferenceBackend(scope, references)
    monkeypatch.setattr("CAD2BIM_mcp.runner.references.request_domain_credential", lambda: _TOKEN)
    monkeypatch.setattr("CAD2BIM_mcp.runner.authoring_tools.request_domain_credential", lambda: _TOKEN)
    server = FastMCP("Runner reference closure", strict_input_validation=False, mask_error_details=True)
    server.add_middleware(RunnerReferenceMiddleware(backend))
    server.add_middleware(DirectToolInputErrorMiddleware())
    register_runner_authoring_tools(server, backend)
    return server, backend, codec, index, scope, references


def _sealed(codec, kind):
    claims = {
        "gr": {"domain": "geometry_catalog_reference", "reference_kind": "resource_root"},
        "gv": {"domain": "geometry_catalog_reference", "reference_kind": "resource_version"},
        "fv": {"domain": "product_family_catalog_reference", "reference_kind": "family_version"},
        "gd": {"domain": "authoring_draft", "root_kind": "geometry"},
        "fd": {"domain": "authoring_draft", "root_kind": "family"},
        "gi": {"domain": "geometry_inspection_reference"},
        "gq": {"domain": "geometry_query_attempt_alias"},
        "sc": {"domain": "code_discovery_locator", "code_kind": "geometry_resource_program"},
    }[kind]
    return codec.encode({**claims, "exact": "immutable-" + kind})


def _text(result):
    return " ".join(block.text for block in result.content if block.type == "text")


@pytest.mark.asyncio
async def test_native_runner_schema_and_typed_info_inputs_accept_short_versions_and_inspection(monkeypatch):
    server, backend, codec, _, scope, references = _setup(monkeypatch)
    version, family, inspection = (_sealed(codec, kind) for kind in ("gv", "fv", "gi"))
    shorts = [await references.shorten(scope, value, (kind,))
        for value, kind in zip((version, family, inspection), ("gv", "fv", "gi"), strict=True)]
    cases = [
        ("ifc_geometry_resource_info", {"target": {"target_kind": "resource_version", "resource_version_alias": shorts[0]}}, "resource_version_alias", version),
        ("ifc_product_family_info", {"target": {"target_kind": "family_version", "family_version_alias": shorts[1]}}, "family_version_alias", family),
        ("ifc_geometry_query_python", {"mode": "fresh", "target": {"target_kind": "geometry_inspection", "inspection_ref": shorts[2],
            "selection": {"selection_kind": "whole"}, "coordinate_space": {"coordinate_space": "result"}},
            "code": "result = {'volume': 1.0}"}, "inspection_ref", inspection),
    ]
    for name, *_ in cases:
        backend.responses[name] = BackendDomainError(409, {"code": "OBSERVED_TYPED_DOMAIN_INPUT",
            "message": "Reached the existing typed business entry", "phase": "read", "retryable": False})
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        for name, request, field, original in cases:
            assert len(request.get(field, request.get("target", {}).get(field))) == 25
            assert not list(Draft202012Validator(tools[name].inputSchema).iter_errors({"request": request}))
            outcome = await client.call_tool(name, {"request": request}, raise_on_error=False)
            assert outcome.is_error and "OBSERVED_TYPED_DOMAIN_INPUT" in _text(outcome)
            body = backend.calls[-1][1]
            backend_field = {"resource_version_alias": "resource_alias", "family_version_alias": "family_alias"}.get(field, field)
            assert body.get(backend_field, body.get("target", {}).get(backend_field)) == original
    assert len(backend.calls) == 3


@pytest.mark.asyncio
async def test_registered_catalog_and_publish_shorten_both_native_outputs_once_per_value(monkeypatch):
    from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogResourceSummary, GeometryCatalogVersionSummary
    from tests.unit.backend.test_geometry_resource_service import _copy_source
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind

    server, backend, codec, index, scope, references = _setup(monkeypatch)
    root, version, family, draft = (_sealed(codec, kind) for kind in ("gr", "gv", "fv", "fd"))
    bundle, _ = _copy_source(GeometryResourceKind.MAPPED_MODEL)
    summary = GeometryCatalogVersionSummary(resource_version_ref=version, version_no=bundle.version.version_no,
        origin_kind=bundle.version.origin_kind, content_digest=bundle.version.content_digest,
        manifest_digest=bundle.version.manifest_digest, source_available=True, preview_available=False,
        created_at=bundle.version.created_at)
    resource = GeometryCatalogResourceSummary(resource_root_ref=root, resource_kind=bundle.resource.resource_kind,
        owner_scope=bundle.resource.owner_scope.scope_kind, display_name=bundle.resource.display_name,
        latest_version=summary, created_at=bundle.resource.created_at)
    cursor = codec.encode({"domain": "geometry_catalog_list_cursor", "position": "next-page"})
    backend.responses["ifc_geometry_resource_list"] = {"resources": [resource.model_dump(mode="json")],
        "returned_count": 1, "next_cursor": cursor}
    backend.responses["ifc_authoring_publish"] = {"operation_alias": "op_041", "geometry_versions": [
        {"resource_version_alias": version}], "family_version_alias": family}
    short_draft = await references.shorten(scope, draft, ("fd",))
    async with Client(server) as client:
        catalog = await client.call_tool("ifc_geometry_resource_list", {"request": {}})
        published = await client.call_tool("ifc_authoring_publish", {"request": {
            "root_kind": "family", "draft_ref": short_draft, "expected_edit_token": 1}})
    for result in (catalog, published):
        assert json.loads(_text(result)) == result.structured_content
    card = catalog.structured_content["resources"][0]
    assert card["resource_alias"].startswith("gr_") and len(card["resource_alias"]) == 25
    assert card["latest_version_alias"].startswith("gv_") and len(card["latest_version_alias"]) == 25
    assert catalog.structured_content["next_cursor"] == cursor
    receipt = published.structured_content
    assert receipt["geometry_versions"][0]["resource_version_alias"] == card["latest_version_alias"]
    assert receipt["family_version_alias"].startswith("fv_") and len(receipt["family_version_alias"]) == 25
    assert receipt["operation_alias"] == "op_041"
    assert backend.calls[-1][1]["draft_ref"] == draft
    assert all(len(items) == len({item["value"] for item in items}) for _, items in backend.translations)
    assert index.writes == 4


@pytest.mark.asyncio
async def test_registered_family_document_nested_bindings_expand_without_rewriting_source(monkeypatch):
    server, backend, codec, _, scope, references = _setup(monkeypatch)
    version, draft = _sealed(codec, "gv"), _sealed(codec, "fd")
    short = await references.shorten(scope, version, ("gv",))
    definition = json.loads(_definition_json())
    definition["geometry_bindings"][0]["source"]["resource_version_alias"] = short
    files = [{"path": item["source_path"], "source": item["source"] + "\n# Keep literal " + short + " and " + version}
        for item in _source_files()]
    request = {"mode": "create", "display_name": "Scoped Family", "purpose": "Assemble the exact binding",
        "usage_instructions": {"action": "set", "value": "Dimensions are in metres."},
        "source": {"source_kind": "inline", "source_files": files,
            "definition_json": json.dumps({"action": "set", "value": definition})}}
    backend.responses["ifc_product_family_write"] = {"draft_ref": draft, "edit_token": 1,
        "operation_alias": "op_042", "saved": True, "runtime_checked": False}
    async with Client(server) as client:
        result = await client.call_tool("ifc_product_family_write", {"request": request})
    body = backend.calls[0][1]
    assert body["source"]["definition"]["value"]["geometry_bindings"][0]["source"]["resource_version_alias"] == version
    assert body["source"]["source_files"] == files and json.loads(request["source"]["definition_json"])["value"]["geometry_bindings"][0]["source"]["resource_version_alias"] == short
    assert result.structured_content["draft_ref"].startswith("fd_")


class _ProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_version_alias: str = Field(pattern=r"^cur\..+$")
    code: str
    arbitrary_data: dict[str, str]


class _ProbeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_version_alias: str = Field(pattern=r"^cur\..+$")
    code: str
    arbitrary_data: dict[str, str]
    next_cursor: str = Field(pattern=r"^cur\..+$")


@pytest.mark.asyncio
async def test_schema_selected_code_json_cursor_and_image_bytes_are_preserved(monkeypatch):
    server, backend, codec, _, scope, references = _setup(monkeypatch)
    version = _sealed(codec, "gv")
    short = await references.shorten(scope, version, ("gv",))
    cursor = codec.encode({"domain": "geometry_catalog_list_cursor"})
    source = f"# resource_version_alias={short}\nprint({version!r})"
    arbitrary = {"resource_version_alias": version, "source_ref": short}
    seen = []

    @server.tool(name="code_read", output_schema=_ProbeOutput.model_json_schema())
    async def code_read(request: _ProbeInput) -> ToolResult:
        seen.append(request.model_dump(mode="json"))
        payload = _ProbeOutput(resource_version_alias=request.resource_version_alias, code=request.code,
            arbitrary_data=request.arbitrary_data, next_cursor=cursor).model_dump(mode="json")
        return ToolResult(structured_content=payload, content=[TextContent(type="text", text=json.dumps(payload)),
            ImageContent(type="image", data=base64.b64encode(_PNG).decode(), mimeType="image/png")])

    async with Client(server) as client:
        result = await client.call_tool("code_read", {"request": {
            "resource_version_alias": short, "code": source, "arbitrary_data": arbitrary}})
    assert seen == [{"resource_version_alias": version, "code": source, "arbitrary_data": arbitrary}]
    assert result.structured_content == {"resource_version_alias": short, "code": source,
        "arbitrary_data": arbitrary, "next_cursor": cursor}
    assert json.loads(_text(result)) == result.structured_content
    assert base64.b64decode(result.content[1].data) == _PNG and result.content[1].mimeType == "image/png"


@pytest.mark.asyncio
async def test_query_failure_attempt_is_shortened_in_prefix_and_diagnostic_without_rewriting_message(monkeypatch):
    server, backend, codec, _, scope, references = _setup(monkeypatch)
    attempt, version = _sealed(codec, "gq"), _sealed(codec, "gv")
    version_short = await references.shorten(scope, version, ("gv",))
    message = "The source contains literal attempt_alias=" + attempt
    backend.responses["ifc_geometry_query_python"] = BackendDomainError(409, {
        "code": "GEOMETRY_QUERY_EXECUTION_FAILED", "message": message, "phase": "execution",
        "retryable": False, "attempt_alias": attempt,
        "diagnostic": {"attempt_alias": attempt, "failure": "query_execution"}})
    async with Client(server) as client:
        result = await client.call_tool("ifc_geometry_query_python", {"request": {"mode": "fresh",
            "target": {"target_kind": "geometry_resource_version", "resource_version_alias": version_short},
            "code": "result = geom.volume()"}}, raise_on_error=False)
    assert result.is_error
    error_text, diagnostic_separator, diagnostic_json = _text(result).partition("; diagnostic=")
    prefix, _, body = error_text.partition("; message=")
    assert "attempt_alias=gq_" in prefix and attempt not in prefix
    short_attempt = prefix.split("attempt_alias=", 1)[1].split(";", 1)[0]
    assert len(short_attempt) == 25 and diagnostic_separator
    assert json.loads(diagnostic_json) == {"attempt_alias": short_attempt, "failure": "query_execution"}
    assert body == message
    assert backend.calls[0][1]["target"]["resource_version_alias"] == version


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    BackendDomainError(409, {"code": "REFERENCE_INVALID", "message": "private index detail"}),
    ConnectionError("private connection detail"),
])
async def test_failed_output_projection_keeps_original_receipt_and_never_repeats_business(monkeypatch, fault):
    server, backend, codec, _, scope, references = _setup(monkeypatch)
    draft, version = _sealed(codec, "gd"), _sealed(codec, "gv")
    short = await references.shorten(scope, draft, ("gd",))
    receipt = {"operation_alias": "op_041", "geometry_versions": [{"resource_version_alias": version}]}
    backend.responses["ifc_authoring_publish"] = receipt
    backend.output_failure = fault

    @server.tool(name="operation_get")
    async def operation_get(operation_alias: str) -> AuthoringPublishResult:
        assert operation_alias == "op_041"
        return AuthoringPublishResult.model_validate(receipt)

    async with Client(server) as client:
        failed = await client.call_tool("ifc_authoring_publish", {"request": {
            "root_kind": "geometry", "draft_ref": short, "expected_edit_token": 1}}, raise_on_error=False)
        backend.output_failure = None
        restored = await client.call_tool("operation_get", {"operation_alias": "op_041"})
    diagnostic = _text(failed)
    assert failed.is_error and "OUTPUT_REFERENCE_PROJECTION_FAILED" in diagnostic
    assert "operation_alias=op_041" in diagnostic and "operation_status=unknown" in diagnostic
    assert "not_started" not in diagnostic and "preflight" not in diagnostic and "private" not in diagnostic
    assert len(backend.calls) == 1
    assert restored.structured_content["operation_alias"] == "op_041"
    assert restored.structured_content["geometry_versions"][0]["resource_version_alias"].startswith("gv_")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["other_scope", "wrong_kind", "typo"])
async def test_invalid_short_input_is_rejected_before_business_execution(monkeypatch, invalid):
    server, backend, codec, _, scope, references = _setup(monkeypatch)
    kind = "fv" if invalid == "wrong_kind" else "gv"
    value = await references.shorten(scope.model_copy(update={"task_id": "other-task"}) if invalid == "other_scope" else scope,
        _sealed(codec, kind), (kind,))
    if invalid == "typo":
        value = value[:-1]
    async with Client(server) as client:
        result = await client.call_tool("ifc_geometry_resource_info", {"request": {
            "target": {"target_kind": "resource_version", "resource_version_alias": value}}}, raise_on_error=False)
    message = _text(result)
    assert result.is_error and "REFERENCE_INVALID" in message
    assert "operation_status=not_started" in message and "phase=preflight" in message
    assert not backend.calls


@pytest.mark.asyncio
async def test_registered_public_family_preview_keeps_one_text_image_and_projected_display_artifact(monkeypatch):
    from CAD2BIM_backend.domain.product_realization import ProductRealizationStatus
    from tests.unit.mcp.test_direct_ifc_product_realization_preview_provider import _outcome

    server, backend, codec, _, scope, references = _setup(monkeypatch)
    family, inspection = _sealed(codec, "fv"), _sealed(codec, "gi")
    short_family = await references.shorten(scope, family, ("fv",))
    outcome = _outcome(ProductRealizationStatus.SOLVED)
    image = outcome.preview.content
    backend.responses["ifc_product_realization_preview"] = {
        "realization": outcome.realization.model_dump(mode="json"),
        "preview": outcome.preview.model_dump(mode="json", exclude={"content"}),
        "image_base64": base64.b64encode(image).decode(),
        "operation_alias": "op_041", "inspection_ref": inspection,
    }
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert tools["ifc_product_realization_preview"].outputSchema is None
        result = await client.call_tool("ifc_product_realization_preview", {"request": {
            "mode": "single", "attachment_kind": "self_geometry",
            "source": {"source_kind": "published", "family_version_alias": short_family},
            "invocation_json": '{"product_origin":[0,0,0],"length_unit":{"name":"METRE"}}',
            "retain": True,
        }})
    texts = [block for block in result.content if block.type == "text"]
    images = [block for block in result.content if block.type == "image"]
    assert len(texts) == len(images) == 1
    assert json.loads(texts[0].text) == result.structured_content
    assert result.structured_content["result"]["inspection_ref"].startswith("gi_")
    assert base64.b64decode(images[0].data) == image
    assert backend.calls[0][1]["source"]["family_version_alias"] == family
    assert backend.calls[0][1]["invocation"]["realization_kind"] == "self_geometry"
