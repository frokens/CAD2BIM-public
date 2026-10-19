"""Retired Family Host edits are rejected by the current execution contracts."""

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationExecutionPolicy, ProductRealizationInvocation,
    ProductRealizationValidationScenario, SolvedProductRealizationProposal,
)
from tests.unit.backend.product_family_fixtures import current_family_document


@pytest.mark.parametrize("kind", ["connected", "current_ifc", "explicit_host_interfaces"])
def test_retired_invocation_kinds_are_unsupported(kind):
    with pytest.raises(ValidationError):
        ProductRealizationInvocation(realization_kind=kind)


@pytest.mark.parametrize("field", ["attachments", "interface_bindings"])
@pytest.mark.parametrize("value", [[], [{"attachment_key": "old_host"}]])
def test_current_invocation_rejects_host_fields_even_when_empty(field, value):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProductRealizationInvocation.model_validate({field: value})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProductRealizationValidationScenario.model_validate({
            "scenario_key": "nominal", "expected_status": "SOLVED", field: value,
        })


@pytest.mark.parametrize("kind", [
    "none", "open_boundary", "split_centerline", "trim_straight_host",
    "retain_straight_sections", "replace_node", "surface_tap",
])
def test_current_program_cannot_publish_any_retired_host_edit(kind):
    proposal = {"status": "SOLVED", "root_world_transform": {},
                "components": [{"component_key": "body"}],
                "fragments": [{"fragment_key": "body_shape", "instance_handle": "instance_0001"}],
                "predicted_host_edits": [{"edit_kind": kind, "attachment_key": "old_host"}]}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SolvedProductRealizationProposal.model_validate(proposal)


@pytest.mark.parametrize("field", ["predicted_host_edits", "interface_bindings", "interfaces", "host_edit_applied"])
def test_current_proposal_has_no_legacy_empty_field_escape(field):
    proposal = {"root_world_transform": {}, "components": [{"component_key": "body"}],
                "fragments": [{"fragment_key": "body_shape", "instance_handle": "instance_0001"}],
                field: False if field == "host_edit_applied" else []}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SolvedProductRealizationProposal.model_validate(proposal)


def test_host_policy_and_attachment_roles_do_not_reenter_current_family():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProductRealizationExecutionPolicy(allow_host_edits=False)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProductFamilyContract.model_validate(current_family_document(attachment_roles=[]))
