"""Historical transport bytes remain immutable and are not a current typed contract."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationExecutionPolicy, ProductRealizationInvocation,
    ProductRealizationResult, SolvedProductRealizationProposal,
)


def test_pre_interface_literal_bytes_are_preserved_and_online_execution_is_unsupported():
    path = Path(__file__).parents[2] / "fixtures/product_realization/legacy_family_before_interfaces.json"
    payload = path.read_bytes()
    before = json.loads(payload)
    assert before["family_contract"]["schema"] == "product_family_contract_v1"
    for model, value in (
        (ProductFamilyContract, before["family_contract"]),
        (ProductRealizationInvocation, before["invocation"]),
        (ProductRealizationExecutionPolicy, before["policy"]),
        (SolvedProductRealizationProposal, before["proposal"]),
        (ProductRealizationResult, before["result"]),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(value)
    assert path.read_bytes() == payload
