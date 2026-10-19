"""Current-only entry fences preserve historical bytes without typed legacy execution."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from CAD2BIM_backend.application.code_discovery_service import (
    CodeDiscoveryApplicationError,
    CodeKind,
    CodeListQuery,
)
from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
from CAD2BIM_backend.domain.data_runtime import ProductFamilyWriteOperationDetail
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogReferenceError
from CAD2BIM_backend.domain.text_source import TextSourceView
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import PostgresDataRuntimeRepositoryMixin
from tests.unit.backend import test_code_discovery_service as code


@pytest.mark.asyncio
@pytest.mark.parametrize("schema,abi", [
    ("product_family_contract_v1", "product_realization_v1"),
    ("product_family_contract_v1", "product_realization_v2"),
    ("product_family_contract_v2", "product_realization_v1"),
    ("product_family_contract_v2", "product_realization_v2"),
    ("product_family_contract_v3", "product_realization_v3"),
    ("product_family_contract_v4", "product_realization_v4"),
    (None, None),
])
async def test_old_operation_is_rejected_before_typed_decode_without_rewriting_json(monkeypatch, schema, abi):
    payload = {"version_draft": {"family_contract": {"schema": schema}, "abi_profile": abi}}
    retained = deepcopy(payload)
    model = SimpleNamespace(operation_id="history", operation_kind="product_family_write", operation_detail_json=payload)
    session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: ())))
    decode = Mock(side_effect=AssertionError("historical typed decoder must not run"))
    monkeypatch.setattr(ProductFamilyWriteOperationDetail, "model_validate", decode)
    with pytest.raises(ValueError):
        await PostgresDataRuntimeRepositoryMixin._operation_record(session, model)
    decode.assert_not_called()
    assert payload == retained


def test_retired_repair_input_has_no_current_write_branch():
    from pydantic import TypeAdapter, ValidationError
    from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftWriteCommand
    with pytest.raises(ValidationError):
        TypeAdapter(FamilyDraftWriteCommand).validate_python({"mode": "repair_attempt", "parent_operation_alias": "op_old"})


@pytest.mark.asyncio
async def test_old_attempt_source_is_absent_from_online_code_discovery_and_read():
    service, runtime, _, _, exact, _operation = code._product_family_service()
    rejected = DataRuntimeApplicationError(
        "OPERATION_NOT_FOUND",
        "Family source Operation was not found in this invocation scope",
    )
    runtime.list_product_family_operation_sources.side_effect = rejected
    with pytest.raises(CodeDiscoveryApplicationError) as listed:
        await service.list_codes(
            code._scope(),
            CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT),
            None,
        )
    assert listed.value.code == "OPERATION_NOT_FOUND"

    runtime.get_product_family_operation_source.side_effect = rejected
    locator = service._locator(
        code._scope(),
        CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT,
        "product_family_authoring_operation",
        operation_alias="op_old",
    )
    with pytest.raises(CodeDiscoveryApplicationError) as read:
        await service.read_code(
            code._scope(), locator, "main.py", TextSourceView(view_mode="full")
        )
    assert read.value.code == "OPERATION_NOT_FOUND"
    exact.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_historical_program_locator_is_not_online_readable():
    service, _, catalog, assets, _, _ = code._product_family_service()
    catalog.resolve_program_source.side_effect = ProductFamilyCatalogReferenceError(
        "product family source reference is invalid"
    )
    locator = service._locator(
        code._scope(),
        CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
        "product_family_version",
        family_version_ref="pfv_old.exact",
    )
    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.read_code(
            code._scope(), locator, "main.py", TextSourceView(view_mode="full")
        )
    assert caught.value.code == "PRODUCT_FAMILY_REFERENCE_INVALID"
    catalog.resolve_program_source.assert_awaited_once_with(
        code._scope().identity, "pfv_old.exact"
    )
    assets.read_program_source.assert_not_awaited()
