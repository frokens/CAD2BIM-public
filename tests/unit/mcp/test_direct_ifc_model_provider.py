from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator

from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcLocalCoordinateReferenceChange,
    IfcRepresentationContextCatalog,
)
from CAD2BIM_backend.domain.ifc_model import (
    IfcModelNotInitializedError,
    IfcRunIdentity,
    InitializeIfcModelCommand,
)
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcLocalCoordinateReferenceChangeInput,
    IfcModelConfigureGeometryBasisResult,
    IfcModelInfoResult,
    IfcModelInitializeOperationDetail,
    IfcModelInitializeResult,
    IfcModelPrimaryResult,
    IfcProjectResult,
    IfcRepresentationContextCatalogResult,
    UpdateParentContextInput,
    to_domain_geometry_basis_command,
)
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.server import create_direct_mcp


_BASELINE_TOOLS = {
    "runtime_open_direct_session",
    "ifc_authoring_publish",
    "data_package_list",
    "operation_list",
    "operation_get",
    "code_list",
    "code_read",
    "data_derive",
    "data_query_sql",
    "data_query_python",
    "ifc_search_official_docs",
    "ifc_read_official_doc",
    "data_read_json",
    "data_get_package_description",
    "data_describe_package",
    "data_query_rows",
    "cad_query_entities",
    "cad_get_entity",
    "cad_query_xdata",
    "cad_analyze_tolerance_candidates",
    "cad_compute_measurements",
}
_IFC_MODEL_TOOLS = {
    "ifc_model_initialize",
    "ifc_model_info",
    "ifc_model_configure_geometry_basis",
    "ifc_representation_context_list",
}
_IFC_PROJECT_SPATIAL_TOOLS = {
    "ifc_project_update",
    "ifc_spatial_structure_list",
    "ifc_spatial_structure_apply",
}
_IFC_GEOMETRY_TOOLS = {
    "ifc_geometry_resource_list",
    "ifc_geometry_resource_info",
    "ifc_geometry_resource_source_read",
    "ifc_geometry_preview",
    "ifc_geometry_query_python",
    "ifc_geometry_resource_write",
}
_IFC_QUERY_TOOLS = {
    "ifc_query_python",
}
_IFC_PRODUCT_FAMILY_TOOLS = {
    "ifc_product_family_list",
    "ifc_product_family_info",
    "ifc_product_family_write",
    "ifc_product_realization_preview",
}
_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _model_info(name: str = "Gas Network Project") -> IfcModelInfoResult:
    return IfcModelInfoResult(
        ifc_schema="IFC4X3_ADD2",
        size_bytes=320,
        project=IfcProjectResult(global_id="0" * 22, name=name),
        geometry_basis_configured=False,
        geometry_basis=None,
        site_count=0,
        element_count=0,
    )


async def test_direct_registers_foundation_plus_project_spatial_tools() -> None:
    tools = {tool.name: tool for tool in await create_direct_mcp(Mock()).list_tools()}

    assert set(tools) == (
        _BASELINE_TOOLS
        | _IFC_MODEL_TOOLS
        | _IFC_PROJECT_SPATIAL_TOOLS
        | _IFC_GEOMETRY_TOOLS
        | _IFC_PRODUCT_FAMILY_TOOLS
        | _IFC_QUERY_TOOLS
        | {"ifc_products_apply", "ifc_products_edit", "ifc_products_read"}
    )
    assert tools["ifc_model_initialize"].annotations.readOnlyHint is False
    assert tools["ifc_model_info"].annotations.readOnlyHint is True
    assert tools["ifc_model_configure_geometry_basis"].annotations.readOnlyHint is False
    assert tools["ifc_representation_context_list"].annotations.readOnlyHint is True
    assert tools["ifc_project_update"].annotations.readOnlyHint is False
    assert tools["ifc_spatial_structure_list"].annotations.readOnlyHint is True
    assert tools["ifc_spatial_structure_apply"].annotations.readOnlyHint is False


async def test_ifc_model_provider_schemas_are_exact_closed_and_native_visible() -> None:
    server = create_direct_mcp(Mock())
    tools = {tool.name: tool for tool in await server.list_tools()}
    initialize = tools["ifc_model_initialize"]
    info = tools["ifc_model_info"]
    configure = tools["ifc_model_configure_geometry_basis"]
    contexts = tools["ifc_representation_context_list"]

    assert tuple(initialize.parameters["properties"]) == (
        "direct_session_alias",
        "project_name",
    )
    assert tuple(initialize.parameters["required"]) == (
        "direct_session_alias",
        "project_name",
    )
    assert initialize.parameters["additionalProperties"] is False
    assert initialize.parameters["properties"]["project_name"]["minLength"] == 1
    assert initialize.parameters["properties"]["project_name"]["maxLength"] == 255
    assert tuple(info.parameters["properties"]) == ("direct_session_alias",)
    assert tuple(info.parameters["required"]) == ("direct_session_alias",)
    assert info.parameters["additionalProperties"] is False

    assert tuple(configure.parameters["properties"]) == (
        "direct_session_alias",
        "project_unit_change",
        "coordinate_reference_change",
        "context_operations",
    )
    assert tuple(configure.parameters["required"]) == ("direct_session_alias",)
    assert configure.parameters["additionalProperties"] is False
    coordinate_branches = [
        branch
        for branch in configure.parameters["properties"]["coordinate_reference_change"]["anyOf"]
        if branch.get("type") != "null"
    ]
    assert len(coordinate_branches) == 2
    assert all("mode" in branch["required"] for branch in coordinate_branches)
    context_branches = configure.parameters["properties"]["context_operations"]["items"]["anyOf"]
    assert len(context_branches) == 4
    assert all("action" in branch["required"] for branch in context_branches)
    assert "oneOf" not in json.dumps(configure.parameters)
    assert tuple(contexts.parameters["properties"]) == ("direct_session_alias",)
    assert tuple(contexts.parameters["required"]) == ("direct_session_alias",)
    assert contexts.parameters["additionalProperties"] is False

    for tool in (initialize, info, configure, contexts):
        Draft202012Validator.check_schema(tool.parameters)
        assert tool.output_schema is not None
        Draft202012Validator.check_schema(tool.output_schema)
        for schema in (tool.parameters, tool.output_schema):
            for node in _walk(schema):
                if isinstance(node, Mapping):
                    assert node != {}
                    assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
                    if node.get("type") == "object" and "properties" in node:
                        assert node.get("additionalProperties") is False
            serialized = json.dumps(schema, sort_keys=True).lower()
            assert "unknown" not in serialized
            assert "ifc_session_alias" not in serialized
            for internal_name in (
                "model_id",
                "revision_id",
                "artifact_id",
                "artifact_version_id",
                "writer_id",
                "run_id",
                "task_id",
            ):
                assert internal_name not in serialized

    initialize_output = initialize.output_schema
    assert tuple(initialize_output["properties"]) == ("operation_alias", "model")
    model_schema = initialize_output["properties"]["model"]
    assert tuple(model_schema["properties"]) == (
        "ifc_schema",
        "project",
        "size_bytes",
        "geometry_basis_configured",
        "geometry_basis",
        "site_count",
        "element_count",
        "source_revision",
    )
    assert "size_bytes" in model_schema["required"]
    assert model_schema["properties"]["size_bytes"]["minimum"] == 1
    assert model_schema["additionalProperties"] is False
    assert tuple(model_schema["properties"]["project"]["properties"]) == (
        "global_id",
        "name",
    )
    assert model_schema["properties"]["project"]["additionalProperties"] is False
    assert info.output_schema == model_schema
    assert tuple(configure.output_schema["properties"]) == ("operation_alias", "model")
    assert configure.output_schema["properties"]["model"] == model_schema
    assert tuple(contexts.output_schema["properties"]) == ("contexts",)

    async with Client(server) as client:
        native = {tool.name: tool for tool in await client.list_tools()}
    for name in _IFC_MODEL_TOOLS:
        assert native[name].inputSchema == tools[name].parameters
        assert native[name].outputSchema == tools[name].output_schema
        assert native[name].description == tools[name].description


async def test_ifc_model_tool_dispatch_and_descriptions_preserve_the_approved_boundary() -> None:
    initialize_result = IfcModelInitializeResult(
        operation_alias="op_001",
        model=_model_info(),
    )
    application = SimpleNamespace(
        initialize_ifc_model=AsyncMock(return_value=initialize_result),
        get_ifc_model_info=AsyncMock(return_value=initialize_result.model),
        configure_ifc_geometry_basis=AsyncMock(
            return_value=IfcModelConfigureGeometryBasisResult(
                operation_alias="op_002",
                model=initialize_result.model,
            )
        ),
        list_ifc_representation_contexts=AsyncMock(
            return_value=IfcRepresentationContextCatalogResult(contexts=())
        ),
    )
    tools = {tool.name: tool for tool in await create_direct_mcp(application).list_tools()}

    assert (
        await tools["ifc_model_initialize"].fn(
            direct_session_alias="direct_001",
            project_name="Gas Network Project",
        )
        == initialize_result
    )
    assert (
        await tools["ifc_model_info"].fn(direct_session_alias="direct_001")
        == initialize_result.model
    )
    application.initialize_ifc_model.assert_awaited_once_with(
        "direct_001",
        "Gas Network Project",
    )
    application.get_ifc_model_info.assert_awaited_once_with("direct_001")

    configured = await tools["ifc_model_configure_geometry_basis"].fn(
        direct_session_alias="direct_001",
        ctx=SimpleNamespace(
            session_id="mcp_session_001",
            request_id="mcp_request_001",
        ),
        project_unit_change=None,
        coordinate_reference_change=IfcLocalCoordinateReferenceChangeInput(mode="local"),
        context_operations=(),
    )
    assert configured.operation_alias == "op_002"
    alias, command = application.configure_ifc_geometry_basis.await_args.args
    assert alias == "direct_001"
    assert command == ConfigureIfcGeometryBasisCommand(
        coordinate_reference_change=IfcLocalCoordinateReferenceChange(mode="local")
    )
    assert application.configure_ifc_geometry_basis.await_args.kwargs == {
        "idempotency_key": server_ifc_invocation_key(
            SimpleNamespace(
                session_id="mcp_session_001",
                request_id="mcp_request_001",
            )
        )
    }
    assert await tools["ifc_representation_context_list"].fn(
        direct_session_alias="direct_001"
    ) == IfcRepresentationContextCatalogResult(contexts=())
    application.list_ifc_representation_contexts.assert_awaited_once_with("direct_001")

    for fragment in (
        "Call this once only",
        "cannot rename, update, repair, reconfigure, reset, or replace",
        "original Operation and revision 1",
        "IFC_MODEL_ALREADY_INITIALIZED",
        "SYSTEM_PAUSED",
    ):
        assert fragment in tools["ifc_model_initialize"].description
    for fragment in (
        "database CURRENT",
        "size_bytes",
        "accepts no revision selector",
        "creates no Operation",
        "geometry_basis is null",
        "Project-linked Model/3D parent context",
        "IFC_MODEL_NOT_INITIALIZED",
        "SYSTEM_PAUSED",
    ):
        assert fragment in tools["ifc_model_info"].description
    for fragment in (
        "WRITE_ONE",
        "reinterpret",
        "preserves every existing Placement",
        "never rescales, rotates, compensates, or traverses",
        "Identifiers are case-sensitive and cannot be renamed",
        "discriminator field action (never operation)",
        "create_parent_context requires context_identifier",
        "create_subcontext requires context_identifier",
        "update_parent_context and update_subcontext actions require context_identifier",
        "operation_alias and the exact reread model summary",
        "ifc_representation_context_list",
        "SYSTEM_PAUSED",
    ):
        assert fragment in tools["ifc_model_configure_geometry_basis"].description
    for fragment in (
        "database CURRENT",
        "ContextIdentifier",
        "read-only tool",
        "creates no Operation",
        "returns no revision, ArtifactVersion, STEP, database, writer, Task, or Run identity",
    ):
        assert fragment in tools["ifc_representation_context_list"].description


def test_geometry_basis_provider_conversion_preserves_explicit_null_updates() -> None:
    provider_update = UpdateParentContextInput(
        action="update_parent_context",
        context_identifier="Model",
        true_north=None,
    )

    command = to_domain_geometry_basis_command(None, None, (provider_update,))
    (domain_update,) = command.context_operations

    assert "true_north" in provider_update.model_fields_set
    assert "true_north" in domain_update.model_fields_set
    assert domain_update.true_north is None


async def test_i0b0_facade_uses_the_resolved_direct_scope_and_rls_binder() -> None:
    identity = TenantExecutionContext(
        tenant_id="tenant_001",
        principal_id="principal_001",
        task_id="task_001",
        actor_kind=TenantActorKind.USER,
        command_id="command_001",
    )
    invocation = SimpleNamespace(
        identity=identity,
        owner_id="owner@example.com",
        run_id="run_001",
    )
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=invocation))
    service_result = SimpleNamespace(
        operation_alias="op_001",
        model=SimpleNamespace(
            ifc_schema="IFC4X3_ADD2",
            project=SimpleNamespace(global_id="0" * 22, name="Gas Network Project"),
            size_bytes=320,
            geometry_basis_configured=False,
            geometry_basis=None,
            site_count=0,
            element_count=0,
        ),
    )
    service = SimpleNamespace(
        initialize=AsyncMock(return_value=service_result),
        info=AsyncMock(return_value=service_result.model),
    )
    geometry_service = SimpleNamespace(
        configure=AsyncMock(
            return_value=SimpleNamespace(operation_alias="op_002", model=service_result.model)
        ),
        list_representation_contexts=AsyncMock(
            return_value=IfcRepresentationContextCatalog(contexts=())
        ),
    )
    bound: list[TenantExecutionContext] = []

    class Binder:
        def __enter__(self):
            bound.append(identity)

        def __exit__(self, *_args):
            return None

    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda actual: Binder() if actual == identity else nullcontext(),
        ifc_model_service=service,
        ifc_geometry_basis_service=geometry_service,
    )

    initialized = await facade.initialize_ifc_model(
        "direct_001",
        "Gas Network Project",
    )
    info = await facade.get_ifc_model_info("direct_001")
    command = ConfigureIfcGeometryBasisCommand(
        coordinate_reference_change=IfcLocalCoordinateReferenceChange(mode="local")
    )
    configured = await facade.configure_ifc_geometry_basis(
        "direct_001",
        command,
        idempotency_key="mcp_request_001",
    )
    contexts = await facade.list_ifc_representation_contexts("direct_001")

    assert initialized == IfcModelInitializeResult(
        operation_alias="op_001",
        model=_model_info(),
    )
    assert info == _model_info()
    assert configured == IfcModelConfigureGeometryBasisResult(
        operation_alias="op_002",
        model=_model_info(),
    )
    assert contexts == IfcRepresentationContextCatalogResult(contexts=())
    initialize_scope, initialize_command = service.initialize.await_args.args
    assert initialize_scope == IfcRunIdentity(
        owner_id="owner@example.com",
        tenant_id="tenant_001",
        task_id="task_001",
        run_id="run_001",
    )
    assert initialize_command == InitializeIfcModelCommand(project_name="Gas Network Project")
    service.info.assert_awaited_once_with(initialize_scope)
    geometry_service.configure.assert_awaited_once_with(
        initialize_scope,
        command,
        idempotency_key="mcp_request_001",
    )
    geometry_service.list_representation_contexts.assert_awaited_once_with(initialize_scope)
    assert len(bound) == 4
    assert all(item is identity for item in bound)
    assert resolver.resolve.await_args_list[0].args == ("direct_001",)
    assert (
        resolver.resolve.await_args_list[0]
        .kwargs["command_id"]
        .startswith("command_direct_ifc_model_initialize_")
    )
    assert (
        resolver.resolve.await_args_list[1]
        .kwargs["command_id"]
        .startswith("command_direct_ifc_model_info_")
    )
    assert (
        resolver.resolve.await_args_list[2]
        .kwargs["command_id"]
        .startswith("command_direct_ifc_model_configure_geometry_basis_")
    )
    assert (
        resolver.resolve.await_args_list[3]
        .kwargs["command_id"]
        .startswith("command_direct_ifc_representation_context_list_")
    )


async def test_i0b0_facade_rejects_bad_project_name_before_scope_or_operation() -> None:
    resolver = SimpleNamespace(resolve=AsyncMock())
    service = SimpleNamespace(initialize=AsyncMock())
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        ifc_model_service=service,
    )

    with pytest.raises(DirectApplicationError) as captured:
        await facade.initialize_ifc_model("direct_001", "bad\nname")

    assert captured.value.code == "INVALID_ARGUMENT"
    resolver.resolve.assert_not_awaited()
    service.initialize.assert_not_awaited()


async def test_i0b0_errors_are_business_actionable_and_hide_technical_details() -> None:
    identity = SimpleNamespace(task_id="task_001", tenant_id="tenant_001")
    invocation = SimpleNamespace(
        identity=identity,
        owner_id="owner@example.com",
        run_id="run_001",
    )
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=invocation))
    service = SimpleNamespace(
        info=AsyncMock(side_effect=IfcModelNotInitializedError("internal model lookup"))
    )
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        ifc_model_service=service,
    )
    tools = {tool.name: tool for tool in await create_direct_mcp(facade).list_tools()}

    with pytest.raises(ToolError) as not_initialized:
        await tools["ifc_model_info"].fn(direct_session_alias="direct_001")
    assert "IFC_MODEL_NOT_INITIALIZED" in str(not_initialized.value)
    assert "internal model lookup" not in str(not_initialized.value)

    secret = "/private/storage/tenant-secret/model.ifc"
    service.info.side_effect = RuntimeError(secret)
    with pytest.raises(ToolError) as paused:
        await tools["ifc_model_info"].fn(direct_session_alias="direct_001")
    assert "SYSTEM_PAUSED" in str(paused.value)
    assert secret not in str(paused.value)
    assert "Traceback" not in str(paused.value)


def test_i0b0_operation_contracts_keep_business_facts_and_hide_internal_ids() -> None:
    primary = IfcModelPrimaryResult(
        ifc_schema="IFC4X3_ADD2",
        project_global_id="0" * 22,
        project_name="Gas Network Project",
        result="model_initialized",
    )
    detail = IfcModelInitializeOperationDetail(
        detail_kind="ifc_model_initialize",
        ifc_schema="IFC4X3_ADD2",
        project_name="Gas Network Project",
        project_global_id="0" * 22,
        result="model_initialized",
    )

    assert primary.model_dump() == {
        "result_kind": "ifc_model",
        "ifc_schema": "IFC4X3_ADD2",
        "project_global_id": "0" * 22,
        "project_name": "Gas Network Project",
        "result": "model_initialized",
    }
    assert detail.model_dump() == {
        "detail_kind": "ifc_model_initialize",
        "ifc_schema": "IFC4X3_ADD2",
        "project_name": "Gas Network Project",
        "project_global_id": "0" * 22,
        "result": "model_initialized",
    }
    serialized = json.dumps(
        {
            "primary": IfcModelPrimaryResult.model_json_schema(),
            "detail": IfcModelInitializeOperationDetail.model_json_schema(),
        },
        sort_keys=True,
    )
    for internal_name in (
        "direct_session_alias",
        "request_digest",
        "idempotency_key",
        "model_id",
        "revision_id",
        "artifact_id",
        "storage",
    ):
        assert internal_name not in serialized


def _trusted_baseline_inspection(status: str = "succeeded"):
    from CAD2BIM_backend.application.data_runtime_service import OperationInspection
    from CAD2BIM_backend.domain.data_runtime import (
        ArtifactVersionRef, CatalogResourceKind, CriticalOperationKind,
        CriticalOperationRecord, CriticalOperationStatus, DataCatalogEntry,
        IfcTrustedBaselineLoadOperationDetail, PublishedCriticalOperation,
        IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
    )
    from CAD2BIM_backend.domain.ifc_model import ExactIfcArtifactVersion, ExactIfcRevision

    now = datetime.now(timezone.utc)
    succeeded = status == "succeeded"
    record = CriticalOperationRecord(
        operation_id="baseline-operation", owner_id="owner", tenant_id="tenant",
        task_id="task", run_id="original-run", operation_kind=CriticalOperationKind.IFC_MODEL_WRITE,
        tool_name="ifc_trusted_baseline_load", intent_summary=IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
        idempotency_key="baseline-key", request_fingerprint="a" * 64,
        status=CriticalOperationStatus(status), base_ifc_revision_id="base-revision",
        expected_ifc_write_epoch=1, result_ifc_revision_id="baseline-revision" if succeeded else None,
        error_code="IFC_VALIDATION_FAILED" if status == "failed" else "IFC_COMMIT_UNCERTAIN" if status == "indeterminate" else None,
        error_message="Candidate rejected" if status == "failed" else "Commit needs internal reconciliation" if status == "indeterminate" else None,
        error_phase="ifc_validation" if status == "failed" else "reconcile" if status == "indeterminate" else None,
        detail=IfcTrustedBaselineLoadOperationDetail(
            source_asset=ArtifactVersionRef(artifact_id="private-source", version_id="private-version", sha256="b" * 64),
            result="baseline_loaded" if succeeded else None,
        ), started_at=now, finished_at=None if status == "prepared" else now,
    )
    if status not in {"succeeded", "failed"}:
        return SimpleNamespace(operation=record, catalog_entry=None)
    published = PublishedCriticalOperation(operation=record, catalog_entry=DataCatalogEntry(
        catalog_entry_id="baseline-entry", owner_id="owner", tenant_id="tenant",
        task_id="task", run_id="original-run", resource_kind=CatalogResourceKind.OPERATION,
        alias="op_001", ordinal=1, catalog_revision=1, operation_id=record.operation_id, created_at=now,
    ))
    revision = ExactIfcRevision(
        scope=IfcRunIdentity(owner_id="owner", tenant_id="tenant", task_id="task", run_id="original-run"),
        model_id="private-model", model_state="active", revision_id="baseline-revision", revision_no=2,
        write_epoch=2, project_global_id="0" * 22,
        artifact=ExactIfcArtifactVersion(artifact_id="private-output", version_id="private-output-version",
            size_bytes=18550, sha256="c" * 64),
    ) if succeeded else None
    return OperationInspection(operation=published, result_package=None, input_packages={}, committed_ifc_revision=revision)


@pytest.mark.parametrize("profile", ["direct", "runner"])
@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_trusted_baseline_actual_operation_provider_is_honest_and_read_only(monkeypatch, profile, status):
    from fastmcp import FastMCP
    from CAD2BIM_backend.internal_data_api import _operation_inspection_json
    from CAD2BIM_mcp.direct.operation.application import _operation_get_result
    from CAD2BIM_mcp.runner import data_tools as runner

    inspection = _trusted_baseline_inspection(status)
    projected = _operation_get_result(inspection, operation_ref="cur.baseline")
    if profile == "direct":
        server = create_direct_mcp(SimpleNamespace(get_operation=AsyncMock(return_value=projected)))
        arguments = {"direct_session_alias": "direct_001", "operation_alias": "op_001"}
    else:
        class Backend:
            async def call_data_tool(self, **kwargs):
                assert kwargs["tool_name"] == "operation_get"
                return {"inspection": _operation_inspection_json(inspection), "authoring_result": None,
                    "inspection_result": None, "operation_ref": "cur.baseline", "same_run": True, "case_key": None}
        monkeypatch.setattr(runner, "request_domain_credential", lambda: "token")
        server = FastMCP("Trusted baseline saved Operation")
        runner.register_runner_data_tools(server, Backend())
        arguments = {"request": {"operation_alias": "op_001"}}
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert "ifc_trusted_baseline_load" not in tools
        schema = tools["operation_get"].outputSchema
        result = await client.call_tool("operation_get", arguments)
    payload = result.structured_content
    Draft202012Validator(schema).validate(payload)
    assert payload == projected.model_dump(mode="json")
    assert payload["operation"]["tool_name"] == "ifc_trusted_baseline_load"
    assert payload["detail"] == {"detail_kind": "ifc_trusted_baseline_load",
        "result": "baseline_loaded" if status == "succeeded" else None, "code_available": False}
    if status == "succeeded":
        assert payload["operation"]["primary_result"] == {"result": "baseline_loaded",
            "committed_revision": {"revision_no": 2, "size_bytes": 18550}}
    else:
        assert payload["operation"]["error_code"] == "IFC_VALIDATION_FAILED"
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
            assert node.get("type") != "unknown"
    serialized = json.dumps(payload)
    for hidden in ("source_asset", "artifact_id", "version_id", "sha256", "model_id", "revision_id", "direct_session_alias"):
        assert hidden not in serialized


@pytest.mark.parametrize("status", ["prepared", "indeterminate"])
def test_trusted_baseline_does_not_expand_public_nonterminal_recovery(status):
    from CAD2BIM_mcp.direct.operation.application import _operation_selection_card
    with pytest.raises(ValueError, match="only terminal business Operations"):
        _operation_selection_card(_trusted_baseline_inspection(status), None)


def test_trusted_baseline_projection_requires_original_committed_metadata():
    from dataclasses import replace
    from CAD2BIM_mcp.direct.operation.application import _operation_get_result, _operation_summary_card
    inspection = _trusted_baseline_inspection()
    listed = _operation_summary_card(inspection.operation, operation_ref="cur.baseline")
    assert (listed.tool_name, listed.operation_kind, listed.outcome) == (
        "ifc_trusted_baseline_load", "ifc_model_write", "succeeded")
    with pytest.raises(ValueError, match="original committed IFC metadata"):
        _operation_get_result(replace(inspection, committed_ifc_revision=None))
