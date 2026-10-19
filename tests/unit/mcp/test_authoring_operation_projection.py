from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftService
from CAD2BIM_backend.domain.data_runtime import (
    AuthoringDraftSelection, AuthoringPreviewExampleReference, AuthoringPublishedGeometryReference,
    AuthoringPublishOperationDetail, CriticalOperationKind,
)
from CAD2BIM_mcp.direct.operation.application import _operation_get_result, _operation_selection_card
from CAD2BIM_mcp.direct.operation.contracts import OperationListResult, OperationGetResult
from CAD2BIM_mcp.direct.provider_schema import _object_union_output_schema
from tests.unit.backend.test_authoring_draft_persistence import operation, published, scope, SNAPSHOT


def test_write_operation_recovery_uses_original_edit_token_without_current_draft_read():
    saved = published(operation())
    service = AuthoringDraftService(None, None, SimpleNamespace(encode=lambda claims: "cur.original-root"))
    recovered = service.operation_result(scope(), saved)
    assert recovered == {"root_kind": "geometry", "draft_ref": "cur.original-root", "edit_token": 2}
    result = _operation_get_result(SimpleNamespace(operation=saved, result_package=None), authoring_result=recovered)
    assert result.operation.primary_result.edit_token == result.detail.edit_token == 2
    assert result.detail.detail_kind == "authoring_draft_write"
    assert service.operation_result(scope().model_copy(update={"run_id": "another-run"}), saved) == recovered


def test_publish_directory_and_detail_expose_same_exact_geometry_closure():
    detail = AuthoringPublishOperationDetail(root_kind="geometry", root_id="geometry-a",
        expected_drafts=(AuthoringDraftSelection(root_kind="geometry", root_id="geometry-a", generation=2, snapshot=SNAPSHOT),),
        preview_examples=(AuthoringPreviewExampleReference(origin_run_id="run-1", operation_id="operation-example", case_keys=("standard",)),),
        geometry_versions=(AuthoringPublishedGeometryReference(resource_id="geometry-a", resource_version_id="geometry-v1"),))
    saved = published(operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish", detail=detail, result_geometry_resource_version_id="geometry-v1"))
    references = {"root_kind": "geometry", "geometry_versions": ({"resource_version_alias": "cur.exact-v1"},),
                  "family_version_alias": None}
    card = _operation_selection_card(saved, None, authoring_result=references)
    result = _operation_get_result(SimpleNamespace(operation=saved, result_package=None), authoring_result=references)
    assert result.operation == card
    assert result.detail.geometry_versions == card.primary_result.geometry_versions
    assert result.detail.detail_kind == "authoring_publish"
    from CAD2BIM_mcp.direct.operation.application import _operation_summary_card
    OperationListResult(operations=(_operation_summary_card(saved),), complete=True, next_before_sequence=None)


def test_authoring_operation_provider_projections_have_no_unsupported_schema_branches():
    def inspect(value):
        if isinstance(value, dict):
            assert not set(value).intersection({"allOf", "if", "then", "else", "not"})
            assert value.get("type") != "unknown"
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    for result_type in (OperationListResult, OperationGetResult):
        inspect(_object_union_output_schema(result_type, compact=True))


def test_failed_publication_is_navigable_without_formal_outputs():
    from CAD2BIM_backend.domain.data_runtime import CriticalOperationStatus, CriticalOperationErrorPhase
    detail = AuthoringPublishOperationDetail(root_kind="geometry", root_id="geometry-a",
        expected_drafts=(AuthoringDraftSelection(root_kind="geometry", root_id="geometry-a", generation=2, snapshot=SNAPSHOT),))
    saved = published(operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish", detail=detail, status=CriticalOperationStatus.FAILED,
        error_code="AUTHORING_BUILD_FAILED", error_message="Definition build failed",
        error_phase=CriticalOperationErrorPhase.EXECUTION))
    result = _operation_get_result(SimpleNamespace(operation=saved, result_package=None),
        operation_ref="cur.exact-failed-operation", same_run=False)
    assert result.operation.outcome == "failed" and result.operation.operation_alias is None
    assert result.operation.operation_ref == "cur.exact-failed-operation"
    assert "geometry_versions" not in result.detail.model_dump()
    assert result.error.code == "AUTHORING_BUILD_FAILED"


@pytest.mark.asyncio
async def test_family_publication_recovery_uses_existing_repository_read_contract():
    from CAD2BIM_backend.application.authoring_publication_service import AuthoringPublicationService
    from CAD2BIM_backend.ports.product_families import ProductFamilyRepository
    from tests.unit.backend.test_product_family_operation_projection import _draft

    draft = _draft()
    detail = AuthoringPublishOperationDetail(root_kind="family", root_id=draft.family_id,
        expected_drafts=(AuthoringDraftSelection(root_kind="family", root_id=draft.family_id,
            generation=2, snapshot=SNAPSHOT),),
        preview_examples=(), family_version=draft)
    saved = published(operation(operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        tool_name="ifc_authoring_publish", detail=detail, target_geometry_resource_id=None,
        target_product_family_id=draft.family_id, result_product_family_version_id=draft.family_version_id))
    repository = create_autospec(ProductFamilyRepository, instance=True, spec_set=True)
    bundle = SimpleNamespace(version=draft)
    repository.get_product_family_version_by_id.return_value = bundle
    def version_ref(context, actual):
        assert context == scope().identity and actual is bundle
        return "fv_exact-publication"
    service = AuthoringPublicationService(repository, None, None,
        SimpleNamespace(_family_catalog=SimpleNamespace(version_ref=version_ref)), None,
        geometry_runtime=None, realization_runtime=None)
    recovered = await service.operation_result(scope(), saved)
    repository.get_product_family_version_by_id.assert_awaited_once_with(
        scope().identity, family_version_id=draft.family_version_id)
    card = _operation_selection_card(saved, None, authoring_result=recovered)
    result = _operation_get_result(SimpleNamespace(operation=saved, result_package=None), authoring_result=recovered)
    assert result.operation == card
    assert result.detail.family_version_alias == card.primary_result.family_version_alias == "fv_exact-publication"


def test_operation_get_reference_and_inspection_are_closed_without_changing_alias_layout():
    from pydantic import TypeAdapter, ValidationError
    from CAD2BIM_mcp.direct.transform.common_inputs import DirectOperationGetInput
    adapter = TypeAdapter(DirectOperationGetInput)
    alias = adapter.validate_python({"direct_session_alias": "direct_001", "operation_alias": "op_001", "case_key": "nominal"})
    assert alias.mode == "operation" and alias.case_key == "nominal"
    ref = adapter.validate_python({"direct_session_alias": "direct_001", "mode": "operation_reference", "operation_ref": "cur.exact"})
    assert ref.mode == "operation_reference"
    for extra in ({"case_key": "nominal"}, {"operation_alias": "op_001"}):
        with pytest.raises(ValidationError):
            adapter.validate_python({"direct_session_alias": "direct_001", "mode": "inspection", "inspection_ref": "cur.saved", **extra})
