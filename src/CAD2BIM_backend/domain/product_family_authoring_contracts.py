"""Operation-safe value objects shared by ProductFamily authoring and persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceKind,
    GeometryResourceVersionBundle,
)
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.product_family import (
    MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    PRODUCT_FAMILY_PROGRAM_ENTRY,
    ProductFamilyContract,
    ProductFamilyGeometryBindingRecord,
    ProductFamilyGeometryPurpose,
    _ProductFamilyModel,
    _clean_text,
    _require_aware,
    normalize_product_family_public_parameters,
    standard_product_family_structure,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationValidationScenario,
    ProductRealizationStatus,
    validate_product_realization_explicit_requirements,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope
from CAD2BIM_backend.domain.version_usage import validate_version_usage_instructions


_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ProductFamilyInvocationSemanticError(ValueError):
    """Known, safely repairable mismatch between an invocation and a Family contract."""

    def __init__(
        self,
        message: str,
        *,
        reason: Literal["SELF_GEOMETRY_REQUIREMENT_INVALID", "PUBLIC_PARAMETER_INVALID"],
        invalid_path: str,
        correction: str,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.invalid_path = invalid_path
        self.correction = correction

    def for_realization_invocation(self) -> "ProductFamilyInvocationSemanticError":
        """Preserve structured facts while retaining the invocation-specific diagnostic text."""

        return ProductFamilyInvocationSemanticError(
            str(self).replace("validation scenario", "product realization invocation"),
            reason=self.reason,
            invalid_path=self.invalid_path,
            correction=self.correction,
        )


class ResolvedProductFamilyGeometryBindingInput(_ProductFamilyModel):
    """One authorized exact GeometryResourceVersion projected for authoring."""

    binding_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    geometry_resource_id: str = Field(min_length=1, max_length=80)
    geometry_resource_version_id: str = Field(min_length=1, max_length=80)
    geometry_content_digest: str = Field(pattern=_SHA256_PATTERN)
    expected_resource_kind: GeometryResourceKind
    geometry_purpose: ProductFamilyGeometryPurpose
    geometry_owner_scope: ResourceOwnerScope

    @classmethod
    def from_version_bundle(
        cls,
        *,
        binding_key: str,
        expected_resource_kind: GeometryResourceKind,
        geometry_purpose: ProductFamilyGeometryPurpose,
        geometry_version: GeometryResourceVersionBundle,
    ) -> "ResolvedProductFamilyGeometryBindingInput":
        if geometry_version.resource.resource_kind is not expected_resource_kind:
            raise ValueError("resolved geometry resource kind does not match the family binding")
        return cls(
            binding_key=binding_key,
            geometry_resource_id=geometry_version.resource.resource_id,
            geometry_resource_version_id=geometry_version.version.resource_version_id,
            geometry_content_digest=geometry_version.version.content_digest,
            expected_resource_kind=expected_resource_kind,
            geometry_purpose=geometry_purpose,
            geometry_owner_scope=geometry_version.resource.owner_scope,
        )

    def to_record(self, *, family_version_id: str) -> ProductFamilyGeometryBindingRecord:
        return ProductFamilyGeometryBindingRecord(
            family_version_id=family_version_id,
            binding_key=self.binding_key,
            geometry_resource_id=self.geometry_resource_id,
            geometry_resource_version_id=self.geometry_resource_version_id,
            geometry_content_digest=self.geometry_content_digest,
            expected_resource_kind=self.expected_resource_kind,
            geometry_purpose=self.geometry_purpose,
        )


class ProductFamilyAuthoringVersionDraft(_ProductFamilyModel):
    """Complete immutable version input retained by a family write Operation."""

    family_version_id: str = Field(min_length=1, max_length=80)
    family_id: str = Field(min_length=1, max_length=80)
    parent_family_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    usage_instructions: str | None = None
    family_contract: ProductFamilyContract
    contract_digest: str = Field(pattern=_SHA256_PATTERN)
    program_entry: Literal["main.py:realize"] = PRODUCT_FAMILY_PROGRAM_ENTRY
    geometry_bindings: tuple[ResolvedProductFamilyGeometryBindingInput, ...] = Field(
        min_length=1,
        max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    )
    created_at: datetime

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return _clean_text(value, label="product family display_name")

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, value: str) -> str:
        return _clean_text(value, label="product family purpose")

    @field_validator("usage_instructions")
    @classmethod
    def validate_usage_instructions(cls, value: str | None) -> str | None:
        return validate_version_usage_instructions(value, required=False)

    _validate_created_at = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def validate_exact_draft(self) -> Self:
        if self.parent_family_version_id == self.family_version_id:
            raise ValueError("a product family version cannot parent itself")
        if self.contract_digest != sha256_digest(self.family_contract):
            raise ValueError("contract_digest does not match the canonical family contract")
        binding_keys = tuple(binding.binding_key for binding in self.geometry_bindings)
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("product family authoring geometry binding keys must be unique")
        required_bindings = {
            option.binding_key
            for fragment in self.family_contract.geometry_fragments
            for option in fragment.binding_options
        }
        if set(binding_keys) != required_bindings:
            raise ValueError("authoring exact geometry bindings do not match the family contract")
        return self



def normalize_product_family_validation_scenario(
    contract: ProductFamilyContract,
    scenario: ProductRealizationValidationScenario,
) -> ProductRealizationValidationScenario:
    """Validate explicit requirements and return the parameter-complete scenario."""

    try:
        normalized_parameters = normalize_product_family_public_parameters(
            contract, scenario.public_parameters,
        )
    except ValueError as exc:
        raise ProductFamilyInvocationSemanticError(
            str(exc), reason="PUBLIC_PARAMETER_INVALID", invalid_path="public_parameters",
            correction=f"{exc}; use the exact Family typed public_parameters contract.",
        ) from exc
    try:
        validate_product_realization_explicit_requirements(contract, scenario)
    except ValueError as exc:
        raise ProductFamilyInvocationSemanticError(
            str(exc), reason="SELF_GEOMETRY_REQUIREMENT_INVALID", invalid_path="external_constraints",
            correction=f"{exc}; use the exact Family interface.external_constraint requirements.",
        ) from exc
    if scenario.realization_kind == "standard_model":
        standard_product_family_structure(contract)
    return scenario.model_copy(update={"public_parameters": normalized_parameters})


__all__ = [
    "ProductFamilyAuthoringVersionDraft",
    "ProductFamilyInvocationSemanticError",
    "ResolvedProductFamilyGeometryBindingInput",
    "normalize_product_family_validation_scenario",
]
