from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.backend.product_family_fixtures import current_family_contract
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, AuthoringDraftWriteOperationDetail, CriticalOperationCatalogSummary,
    PublishedOperationCatalogSummary,
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataCatalogEntry,
    ProductFamilyWriteOperationDetail,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyGeometryPurpose,
)
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogReferenceError
from CAD2BIM_backend.domain.product_family_authoring_contracts import (
    ProductFamilyAuthoringVersionDraft,
    ResolvedProductFamilyGeometryBindingInput,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationValidationScenario,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    PostgresDataRuntimeRepositoryMixin,
    _current_operation_contract_predicate,
    _operation_detail_json,
)
from CAD2BIM_mcp.direct.operation.application import OperationApplicationMixin, _operation_get_result, _operation_summary_card
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.operation.contracts import OperationCard


NOW = datetime(2026, 9, 4, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [("get_operation", ("direct_001", "op_007"))])
async def test_noncurrent_family_operation_is_an_invalid_online_reference(method, args):
    message = "ProductFamily Operation is unavailable under the current contract"
    application = OperationApplicationMixin()
    application._run_runtime_read = AsyncMock(
        side_effect=ProductFamilyCatalogReferenceError(message),
    )
    with pytest.raises(DirectApplicationError) as caught:
        await getattr(application, method)(*args)
    assert caught.value.code == "PRODUCT_FAMILY_REFERENCE_INVALID"
    assert str(caught.value) == message
    assert caught.value.retryable is False
    application._run_runtime_read.assert_awaited_once()


def _draft(*, parent: str | None = None) -> ProductFamilyAuthoringVersionDraft:
    contract = current_family_contract(binding_key="body", standard_model={})
    return ProductFamilyAuthoringVersionDraft(
        family_version_id="family_version_internal_2",
        family_id="family_internal",
        parent_family_version_id=parent,
        display_name="Regulator body",
        purpose="Create one reusable gas-network product",
        family_contract=contract,
        contract_digest=sha256_digest(contract),
        geometry_bindings=(
            ResolvedProductFamilyGeometryBindingInput(
                binding_key="body",
                geometry_resource_id="geometry_internal",
                geometry_resource_version_id="geometry_version_internal",
                geometry_content_digest="d" * 64,
                expected_resource_kind=GeometryResourceKind.MAPPED_MODEL,
                geometry_purpose=ProductFamilyGeometryPurpose.PRODUCT_COMPONENT,
                geometry_owner_scope=ResourceOwnerScope(
                    scope_kind="task",
                    tenant_id="tenant_local",
                    task_id="task_local",
                ),
            ),
        ),
        created_at=NOW,
    )


def _operation(**changes: object) -> CriticalOperationRecord:
    values = dict(operation_id="operation_family_write", owner_id="owner_local",
        tenant_id="tenant_local", task_id="task_local", run_id="run_local",
        operation_kind=CriticalOperationKind.PRODUCT_FAMILY_WRITE,
        tool_name="ifc_product_family_write", intent_summary="Save one exact editable Family snapshot.",
        idempotency_key="server-only-family-key", request_fingerprint="a" * 64,
        status=CriticalOperationStatus.SUCCEEDED, target_product_family_id="family_internal",
        code_artifact=ArtifactVersionRef(artifact_id="source_artifact_internal",
            version_id="source_artifact_version_internal", sha256="b" * 64),
        detail=AuthoringDraftWriteOperationDetail(root_kind="family", root_id="family_internal",
            expected_generation=1, result_generation=2), started_at=NOW, finished_at=NOW)
    values.update(changes)
    return CriticalOperationRecord.model_validate(values)


def _published(operation: CriticalOperationRecord) -> PublishedCriticalOperation:
    return PublishedCriticalOperation(
        operation=operation,
        catalog_entry=DataCatalogEntry(
            catalog_entry_id="catalog_family_operation",
            owner_id="owner_local",
            tenant_id="tenant_local",
            task_id="task_local",
            run_id="run_local",
            resource_kind=CatalogResourceKind.OPERATION,
            alias="op_007",
            ordinal=7,
            catalog_revision=7,
            operation_id=operation.operation_id,
            created_at=NOW,
        ),
    )


def test_product_family_operation_lifecycle_is_explicit_and_closed() -> None:
    saved = _operation()
    assert _operation_detail_json(saved) == {"detail_kind": "authoring_draft_write",
        "root_kind": "family", "root_id": "family_internal", "expected_generation": 1, "result_generation": 2}
    for changes in ({"target_product_family_id": "another_family"},
        {"base_product_family_version_id": "family_version_internal_1"},
        {"repair_parent_operation_id": "old_operation"},
        {"status": CriticalOperationStatus.PREPARED, "finished_at": None}):
        with pytest.raises(ValidationError):
            _operation(**changes)
    with pytest.raises(ValidationError):
        ProductFamilyWriteOperationDetail(mode="repair_attempt", version_draft=_draft())


@pytest.mark.asyncio
@pytest.mark.parametrize("root_kind", ["family", "geometry"])
async def test_family_operation_source_projects_current_draft_kind(monkeypatch, root_kind):
    operation = _operation()
    catalog = _published(operation).catalog_entry
    detail = AuthoringDraftWriteOperationDetail(root_kind=root_kind, root_id="root", result_generation=1)
    row = SimpleNamespace(operation_id=operation.operation_id, repair_parent_operation_id=None,
        code_artifact_id=operation.code_artifact.artifact_id,
        code_artifact_version_id=operation.code_artifact.version_id,
        code_artifact_sha256=operation.code_artifact.sha256,
        status=operation.status, started_at=NOW, finished_at=NOW,
        error_phase=None, error_code=None, error_message=None, result_product_family_version_id=None)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return SimpleNamespace(one_or_none=lambda: (row, catalog))

    repository = PostgresDataRuntimeRepositoryMixin(lambda: Session())
    monkeypatch.setattr(repository, "_require_data_run", AsyncMock())
    monkeypatch.setattr(repository, "_operation_record", AsyncMock(return_value=SimpleNamespace(detail=detail)))
    monkeypatch.setattr(repository, "_catalog_record", lambda value: value)
    scope = SimpleNamespace(owner_id="owner_local", run_id="run_local", catalog_frontier=None,
        identity=SimpleNamespace(tenant_id="tenant_local", task_id="task_local"))
    if root_kind == "geometry":
        with pytest.raises(ProductFamilyCatalogReferenceError):
            await repository.get_product_family_operation_source(scope, operation.operation_id)
    else:
        source = await repository.get_product_family_operation_source(scope, operation.operation_id)
        assert source.detail_kind == "authoring_draft_write"
        assert source.code_artifact == operation.code_artifact
        assert source.catalog_entry == catalog


def test_provider_operation_union_keeps_current_tools_and_rejects_retired_geometry_tools() -> None:
    adapter = TypeAdapter(OperationCard)
    common = {
        "operation_alias": "op_007",
        "sequence": 7,
        "initiator": "direct_client",
        "intent_summary": "Current immutable execution fact.",
        "started_at": NOW,
        "finished_at": NOW,
        "outcome": "failed",
        "error_code": "EXECUTION_FAILED",
    }
    current_tools = (
        ("source_ingestion", "cad_import_source"),
        ("data_transform", "data_transform"),
        ("data_transform", "data_repair_transform"),
        ("data_transform", "cad_compute_measurements"),
        ("ifc_model_write", "ifc_model_initialize"),
        ("ifc_model_write", "ifc_model_configure_geometry_basis"),
        ("ifc_model_write", "ifc_project_update"),
        ("ifc_model_write", "ifc_spatial_structure_apply"),
        ("ifc_model_write", "ifc_products_apply"),
    )

    for operation_kind, tool_name in current_tools:
        card = adapter.validate_python(
            common | {"operation_kind": operation_kind, "tool_name": tool_name}
        )
        assert card.operation_kind == operation_kind
        assert card.tool_name == tool_name

    for retired in ("ifc_geometry_resource_create", "ifc_geometry_resource_repair"):
        with pytest.raises(ValidationError):
            adapter.validate_python(common | {"operation_kind": "geometry_resource_write", "tool_name": retired})

    with pytest.raises(ValidationError):
        adapter.validate_python(
            common
            | {
                "operation_kind": "ifc_model_write",
                "tool_name": "ifc_future_unregistered_write",
            }
        )


def test_product_family_operation_serialization_and_provider_projection_are_safe() -> None:
    saved = _operation()
    model = PostgresDataRuntimeRepositoryMixin._operation_model(saved)
    assert model.target_product_family_id == "family_internal"
    assert model.result_product_family_version_id is None
    assert model.operation_detail_json["detail_kind"] == "authoring_draft_write"
    result = _operation_get_result(SimpleNamespace(operation=_published(saved), result_package=None,
        input_packages={}, repair_parent_operation=None), authoring_result={"root_kind": "family",
            "draft_ref": "opaque-draft", "edit_token": 2})
    payload = result.model_dump(mode="json")
    assert payload["operation"]["primary_result"]["result"] == "authoring_draft_saved"
    assert payload["detail"]["draft_ref"] == "opaque-draft"
    assert payload["detail"]["edit_token"] == 2 and payload["detail"]["code_available"] is True
    for private in ("owner_local", "tenant_local", "task_local", "run_local", "family_internal",
        "source_artifact_internal", "source_artifact_version_internal", "server-only-family-key"):
        assert private not in result.model_dump_json()


@pytest.mark.asyncio
async def test_product_family_operation_repository_round_trip_keeps_exact_draft() -> None:
    operation = _operation()
    model = PostgresDataRuntimeRepositoryMixin._operation_model(operation)
    restored = await PostgresDataRuntimeRepositoryMixin._operation_record(SimpleNamespace(), model)
    assert restored == operation and restored.detail.root_id == "family_internal"
    assert restored.code_artifact == operation.code_artifact and restored.detail.result_generation == 2


@pytest.mark.asyncio
async def test_noncurrent_family_operation_decoder_is_an_invalid_reference():
    from copy import deepcopy
    model = PostgresDataRuntimeRepositoryMixin._operation_model(_operation())
    model.operation_detail_json = {"detail_kind": "product_family_write", "mode": "create",
        "version_draft": {"draft_schema": "product_family_authoring_draft_v2",
            "abi_profile": "product_realization_v4", "family_contract": {
                "schema": "product_family_contract_v4", "standard_model": {}}}}
    retained = deepcopy(model.operation_detail_json)
    with pytest.raises(ProductFamilyCatalogReferenceError, match="unavailable"):
        await PostgresDataRuntimeRepositoryMixin._operation_record(SimpleNamespace(), model)
    summary = CriticalOperationCatalogSummary(**{key: getattr(model, key) for key in (
        "operation_id", "run_id", "operation_kind", "initiator", "tool_name", "intent_summary",
        "status", "started_at", "finished_at", "error_code")})
    published = PublishedOperationCatalogSummary(operation=summary, catalog_entry=_published(_operation()).catalog_entry)
    card = _operation_summary_card(published, operation_ref="opaque-original-operation")
    assert card.operation_ref == "opaque-original-operation" and card.operation_alias == "op_007"
    assert "detail" not in card.model_dump() and "primary_result" not in card.model_dump()
    assert model.operation_detail_json == retained


def test_current_operation_contract_predicate_qualifies_current_typed_business_details():
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from CAD2BIM_backend.infrastructure.postgres.models import CriticalOperationModel

    statement = select(CriticalOperationModel.operation_id).where(
        _current_operation_contract_predicate()
    )
    compiled = statement.compile(dialect=postgresql.psycopg.dialect())
    sql = str(compiled)
    values = tuple(compiled.params.values())
    for marker in (
        "authoring_draft_write",
        "system_copy",
        "preview_examples",
        "ifc_products_apply",
    ):
        assert marker in values
    assert "jsonb_path_exists" not in sql
    assert "critical_operations.tool_name !=" in sql
    assert "product_family_contract_v4" not in values
    assert "product_realization_v4" not in values
