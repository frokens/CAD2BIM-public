"""Closed Direct Provider contracts for read-only Product Realization preview."""

from __future__ import annotations

from CAD2BIM_mcp.direct.ifc.preview_cases import FamilyPreviewSource, PreviewCaseBatchResult, parse_cases, parse_render

from CAD2BIM_mcp.direct.geometry_preview_contracts import (
    IfcGeometryPreviewReferences,
    IfcGeometryPreviewReferenceResult,
    IfcRenderedPreviewPresentationResult,
)
from CAD2BIM_mcp.tool_docs.preview import (
    PREVIEW_REFERENCE_FIELD_DESCRIPTION, PREVIEW_RETAIN_DESCRIPTION,
    FAMILY_PREVIEW_SINGLE_SOURCE_DESCRIPTION, FAMILY_PREVIEW_CASE_SOURCE_DESCRIPTION,
    FAMILY_PREVIEW_CASES_DESCRIPTION, FAMILY_PREVIEW_RENDER_DESCRIPTION,
    FAMILY_PREVIEW_NOMINAL_DESCRIPTION,
)

import json
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from CAD2BIM_backend.domain.geometry_inspection import GeometryInspectionDiagnostic

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_runtime import (
    MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    GeometryPreviewProfile,
    GeometryRenderInterfaceOverlay,
)
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.product_family import MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationAmbiguity,
    ProductRealizationComponentPlacement,
    ProductRealizationComponentWorldTransform,
    ProductRealizationFrameDiagnostic,
    ProductRealizationExternalConstraint,
    ProductRealizationExternalConditionCheck,
    ProductRealizationInterfaceWorldFrame,
    ProductRealizationViolation,
    RigidTransform3D,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import (
    IfcProductFamilyBooleanParameterValue,
    IfcProductFamilyChoiceParameterValue,
    IfcProductFamilyIntegerParameterValue,
    IfcProductFamilyNumberParameterValue,
)
from CAD2BIM_mcp.direct.provider_schema import provider_reference_schema
from CAD2BIM_mcp.direct.tool_support import (
    ProviderJsonDocumentError,
    decode_provider_json_object,
    provider_validation_message,
)
from CAD2BIM_mcp.tool_docs.ifc_product_family import (
    PRODUCT_REALIZATION_INVOCATION_JSON_FIELD_DESCRIPTION,
)
from CAD2BIM_mcp.tool_docs.preview import (
    PRODUCT_REALIZATION_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION,
)
from CAD2BIM_mcp.tool_docs import ifc_product_family as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs
from CAD2BIM_mcp.tool_docs import preview as _preview_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_SAFE_REFERENCE_PATTERN = r"^cur\..+$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_IFC_GLOBAL_ID_PATTERN = r"^[0-9A-Za-z_$]{22}$"
_MAX_PARAMETERS = 64
_MAX_RESULT_JSON_BYTES = 65_536
_MAX_INVOCATION_DOCUMENT_BYTES = 1_048_576
_MAX_PREVIEW_PROFILE_DOCUMENT_BYTES = 65_536

IfcProductRealizationAttachmentKind: TypeAlias = Literal[
    "self_geometry",
    "standard_model",
]


class _DirectProductRealizationInput(DirectContract):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN, max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_TASK_SCOPE_ALIAS_DESCRIPTION,
    )


class IfcProductRealizationSolveProviderInput(DirectContract):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_REALIZATION_SOLVE_PROVIDER_INPUT_DESCRIPTION
    reference_geometries: IfcGeometryPreviewReferences = Field(default=(), description=PREVIEW_REFERENCE_FIELD_DESCRIPTION)
    retain: bool = Field(
        default=False, strict=True,
        description=PREVIEW_RETAIN_DESCRIPTION,
    )

    mode: Literal["single"] = Field(
        default="single",
        description=_tool_docs.DIRECT_IFC_PRODUCT_REALIZATION_SOLVE_PROVIDER_INPUT_MODE_DESCRIPTION,
    )
    attachment_kind: IfcProductRealizationAttachmentKind
    source: FamilyPreviewSource = Field(description=FAMILY_PREVIEW_SINGLE_SOURCE_DESCRIPTION)
    invocation_json: str = Field(
        min_length=2,
        max_length=_MAX_INVOCATION_DOCUMENT_BYTES,
        description=PRODUCT_REALIZATION_INVOCATION_JSON_FIELD_DESCRIPTION,
    )
    preview_profile_json: str | None = Field(
        default=None,
        min_length=2,
        max_length=_MAX_PREVIEW_PROFILE_DOCUMENT_BYTES,
        description=PRODUCT_REALIZATION_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION,
    )


class DirectIfcProductRealizationSolveProviderInput(IfcProductRealizationSolveProviderInput, _DirectProductRealizationInput):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_REALIZATION_SCOPED_SINGLE_INPUT_DESCRIPTION


class IfcProductRealizationCasesInput(DirectContract):
    mode: Literal["cases"]
    source: FamilyPreviewSource = Field(description=FAMILY_PREVIEW_CASE_SOURCE_DESCRIPTION)
    cases_json: str = Field(min_length=2, max_length=1_048_576, description=FAMILY_PREVIEW_CASES_DESCRIPTION)
    nominal_scenario_key: str | None = Field(default=None, pattern=_KEY_PATTERN, description=FAMILY_PREVIEW_NOMINAL_DESCRIPTION)
    render_json: str | None = Field(default=None, min_length=2, max_length=65_536, description=FAMILY_PREVIEW_RENDER_DESCRIPTION)
    retain: bool = Field(default=False, strict=True, description=PREVIEW_RETAIN_DESCRIPTION)
    reference_geometries: IfcGeometryPreviewReferences = Field(default=(), description=PREVIEW_REFERENCE_FIELD_DESCRIPTION)

    @field_validator("cases_json")
    @classmethod
    def validate_cases(cls, value):
        parse_cases(value, case_kind="family")
        return value

    @field_validator("render_json")
    @classmethod
    def validate_render(cls, value):
        parse_render(value)
        return value


class DirectIfcProductRealizationCasesInput(IfcProductRealizationCasesInput, _DirectProductRealizationInput):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_REALIZATION_SCOPED_CASES_INPUT_DESCRIPTION


DirectIfcProductRealizationPreviewProviderInput: TypeAlias = Annotated[
    DirectIfcProductRealizationSolveProviderInput | DirectIfcProductRealizationCasesInput,
    Field(discriminator="mode"),
]

class _DirectIfcProductRealizationPreviewBase(DirectContract):
    reference_geometries: IfcGeometryPreviewReferences = Field(default=(), description=PREVIEW_REFERENCE_FIELD_DESCRIPTION)
    retain: bool = Field(default=False, strict=True, description=PREVIEW_RETAIN_DESCRIPTION)
    mode: Literal["single"] = "single"
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_TASK_SCOPE_ALIAS_DESCRIPTION,
    )
    source: FamilyPreviewSource
    public_parameters: dict[
        Annotated[str, Field(pattern=_KEY_PATTERN, max_length=64)], JsonValue
    ] = Field(
        default_factory=dict,
        max_length=_MAX_PARAMETERS,
        json_schema_extra={"additionalProperties": False},
        description=(
            _tool_docs.DIRECT_IFC_PRODUCT_REALIZATION_PREVIEW_BASE_PUBLIC_PARAMETERS_DESCRIPTION
        ),
    )
    product_origin: tuple[float, float, float]
    length_unit: IfcLengthUnit
    render_profile: GeometryPreviewProfile | None = None


class DirectIfcProductRealizationSelfGeometryInput(_DirectIfcProductRealizationPreviewBase):
    attachment_kind: Literal["self_geometry"]
    external_constraints: tuple[ProductRealizationExternalConstraint, ...] = Field(default=(), max_length=64)


class DirectIfcProductRealizationStandardModelInput(_DirectIfcProductRealizationPreviewBase):
    attachment_kind: Literal["standard_model"]


DirectIfcProductRealizationPreviewInput: TypeAlias = Annotated[
    DirectIfcProductRealizationSelfGeometryInput
    | DirectIfcProductRealizationStandardModelInput,
    Field(discriminator="attachment_kind"),
]


DirectIfcProductRealizationPreviewRequest: TypeAlias = DirectIfcProductRealizationPreviewInput | DirectIfcProductRealizationCasesInput


class _ProductRealizationInvocationDocumentBase(DirectContract):
    public_parameters: dict[
        Annotated[str, Field(pattern=_KEY_PATTERN, max_length=64)], JsonValue
    ] = Field(
        default_factory=dict,
        max_length=_MAX_PARAMETERS,
        json_schema_extra={"additionalProperties": False},
    )
    product_origin: tuple[float, float, float]
    length_unit: IfcLengthUnit


class _ProductRealizationStandardModelInvocationDocument(_ProductRealizationInvocationDocumentBase):
    pass


class _ProductRealizationSelfGeometryInvocationDocument(_ProductRealizationInvocationDocumentBase):
    external_constraints: tuple[ProductRealizationExternalConstraint, ...] = Field(default=(), max_length=64)


class IfcProductRealizationInstanceReceiptResult(DirectContract):
    fragment_key: str = Field(pattern=_KEY_PATTERN, max_length=64)
    binding_key: str = Field(pattern=_KEY_PATTERN, max_length=64)
    normalized_parameters_json: str = Field(
        min_length=2,
        max_length=_MAX_RESULT_JSON_BYTES,
        description=_tool_docs.IFC_PRODUCT_REALIZATION_INSTANCE_RECEIPT_RESULT_NORMALIZED_PARAMETERS_JSON_DESCRIPTION,
    )
    geometry_summary_json: str = Field(
        min_length=2,
        max_length=_MAX_RESULT_JSON_BYTES,
        description=_tool_docs.IFC_PRODUCT_REALIZATION_INSTANCE_RECEIPT_RESULT_GEOMETRY_SUMMARY_JSON_DESCRIPTION,
    )
    uniform_scale: float | None = Field(default=None, gt=0.0)

    @field_validator("normalized_parameters_json", "geometry_summary_json")
    @classmethod
    def require_canonical_object(cls, value: str) -> str:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or canonical_json(decoded) != value:
            raise ValueError("result JSON text must be one canonical JSON object")
        return value


class IfcProductRealizationFragmentPlacementResult(DirectContract):
    fragment_key: str = Field(pattern=_KEY_PATTERN, max_length=64)
    binding_key: str = Field(
        pattern=_KEY_PATTERN,
        max_length=64,
        description=_tool_docs.PRODUCT_FRAGMENT_BINDING_DESCRIPTION,
    )
    geometry_parameters_json: str = Field(
        min_length=2,
        max_length=_MAX_RESULT_JSON_BYTES,
        description=(
            _tool_docs.IFC_PRODUCT_REALIZATION_FRAGMENT_RESULT_GEOMETRY_PARAMETERS_JSON_DESCRIPTION
        ),
    )
    local_transform: RigidTransform3D | None = None
    uniform_scale: float | None = Field(default=None, gt=0.0)

    @field_validator("geometry_parameters_json")
    @classmethod
    def require_canonical_parameters(cls, value: str) -> str:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or canonical_json(decoded) != value:
            raise ValueError("geometry_parameters_json must be one canonical JSON object")
        return value


class IfcProductFamilyStructuredParameterResult(DirectContract):
    parameter_kind: Literal["array", "object"]
    parameter_key: str = Field(pattern=_KEY_PATTERN, max_length=64)
    value_json: str = Field(min_length=2, max_length=_MAX_RESULT_JSON_BYTES, description=_tool_docs.PRODUCT_REALIZATION_STRUCTURED_PARAMETER_RESULT_DESCRIPTION)


IfcProductFamilyPublicParameterResult = Annotated[
    IfcProductFamilyBooleanParameterValue | IfcProductFamilyIntegerParameterValue
    | IfcProductFamilyNumberParameterValue | IfcProductFamilyChoiceParameterValue
    | IfcProductFamilyStructuredParameterResult,
    Field(discriminator="parameter_kind"),
]


class _IfcProductRealizationPreviewResultBase(DirectContract):
    operation_alias: str | None = Field(default=None, pattern=r"^op_[0-9]{3,}$", max_length=32)
    diagnostics: tuple[GeometryInspectionDiagnostic, ...] = ()
    source_ref: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN,
        max_length=16_384,
        description=_tool_docs.IFC_PRODUCT_REALIZATION_PREVIEW_RESULT_BASE_FAMILY_VERSION_ALIAS_DESCRIPTION,
    )
    attachment_kind: Literal["self_geometry", "standard_model"]
    normalized_public_parameters: tuple[IfcProductFamilyPublicParameterResult, ...] = Field(
        default=(), max_length=_MAX_PARAMETERS
    )


class _IfcProductRealizationSolvedFacts(_IfcProductRealizationPreviewResultBase):
    inspection_ref: str | None = Field(
        default=None, pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384,
        description=_tool_docs.PRODUCT_PREVIEW_INSPECTION_REF_DESCRIPTION,
    )
    status: Literal["SOLVED"]
    external_target_comparison: Literal["PASS", "FAIL", "NOT_COMPARED"] = Field(
        description=_tool_docs.PRODUCT_REALIZATION_EXTERNAL_TARGET_COMPARISON_DESCRIPTION
    )
    external_condition_checks: tuple[ProductRealizationExternalConditionCheck, ...] = Field(default=(), max_length=64)
    internal_parameters_json: str = Field(
        min_length=2,
        max_length=_MAX_RESULT_JSON_BYTES,
        description=_tool_docs.IFC_PRODUCT_REALIZATION_SOLVED_RESULT_INTERNAL_PARAMETERS_JSON_DESCRIPTION,
    )
    components: tuple[ProductRealizationComponentPlacement, ...] = Field(
        min_length=1, max_length=64
    )
    fragments: tuple[IfcProductRealizationFragmentPlacementResult, ...] = Field(
        min_length=1, max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS
    )
    instance_receipts: tuple[IfcProductRealizationInstanceReceiptResult, ...] = Field(
        min_length=1, max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS
    )
    component_project_world_transforms: tuple[ProductRealizationComponentWorldTransform, ...] = (
        Field(min_length=1, max_length=64)
    )
    interface_project_world_frames: tuple[ProductRealizationInterfaceWorldFrame, ...] = Field(
        default=(), max_length=64
    )
    frame_check_status: Literal["PASS", "FAIL"] | None = Field(
        default=None, description=_preview_tool_docs.PREVIEW_FRAME_CONSTRAINT_STATUS_DESCRIPTION)
    frame_diagnostics: tuple[ProductRealizationFrameDiagnostic, ...] | None = Field(
        default=None, max_length=320
    )
    diagnostic_only: Literal[True] | None = None
    eligible_for_product_write: Literal[False] | None = None

    @field_validator("internal_parameters_json")
    @classmethod
    def require_canonical_internal_parameters(cls, value: str) -> str:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or canonical_json(decoded) != value:
            raise ValueError("internal_parameters_json must be one canonical JSON object")
        return value

    @model_validator(mode="after")
    def require_complete_retention_receipt(self):
        if (self.operation_alias is None) != (self.inspection_ref is None):
            raise ValueError("SOLVED retained Preview requires both operation_alias and inspection_ref")
        return self


class IfcProductRealizationSolvedResult(_IfcProductRealizationSolvedFacts):
    reference_geometries: tuple[IfcGeometryPreviewReferenceResult, ...] = Field(default=(), max_length=8)
    preview_mime_type: Literal["image/png"]
    preview_size_bytes: int = Field(strict=True, gt=0, le=16 * 1024 * 1024)
    presentation: IfcRenderedPreviewPresentationResult | None = None
    interface_overlays: tuple[GeometryRenderInterfaceOverlay, ...] = Field(
        default=(),
        max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    )
    derived_only: Literal[True] = True

    @model_serializer(mode="wrap")
    def omit_absent_render_facts(self, handler):
        result = handler(self)
        if self.presentation is None:
            result.pop("presentation", None)
        if not self.interface_overlays:
            result.pop("interface_overlays", None)
        return result


class IfcProductRealizationRetainedNoImageSolvedResult(_IfcProductRealizationSolvedFacts):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    inspection_ref: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    diagnostics: tuple[GeometryInspectionDiagnostic, ...] = Field(min_length=1)
    render_status: Literal["failed"] = "failed"

    @model_validator(mode="after")
    def require_render_failure(self):
        if not any(item.phase == "render" for item in self.diagnostics):
            raise ValueError("retained SOLVED without an image requires an actual render diagnostic")
        return self


class IfcProductRealizationAmbiguousResult(_IfcProductRealizationPreviewResultBase):
    status: Literal["AMBIGUOUS"]
    ambiguities: tuple[ProductRealizationAmbiguity, ...] = Field(min_length=1, max_length=64)


class IfcProductRealizationInfeasibleResult(_IfcProductRealizationPreviewResultBase):
    status: Literal["INFEASIBLE"]
    violations: tuple[ProductRealizationViolation, ...] = Field(min_length=1, max_length=64)


IfcProductRealizationPreviewOutcomeResult: TypeAlias = (
    IfcProductRealizationSolvedResult
    | IfcProductRealizationRetainedNoImageSolvedResult
    | IfcProductRealizationAmbiguousResult
    | IfcProductRealizationInfeasibleResult
    | PreviewCaseBatchResult
)


class IfcProductRealizationPreviewResult(DirectContract):
    __doc__ = _tool_docs.IFC_PRODUCT_REALIZATION_PREVIEW_RESULT_DESCRIPTION

    result: IfcProductRealizationPreviewOutcomeResult


PRODUCT_REALIZATION_PREVIEW_INPUT_ADAPTER = TypeAdapter(DirectIfcProductRealizationPreviewRequest)
PRODUCT_REALIZATION_PREVIEW_PROVIDER_INPUT_ADAPTER = TypeAdapter(
    DirectIfcProductRealizationPreviewProviderInput
)


def validate_product_realization_preview_input(
    value: object,
) -> DirectIfcProductRealizationPreviewRequest:
    return PRODUCT_REALIZATION_PREVIEW_INPUT_ADAPTER.validate_python(value)


def validate_product_realization_preview_provider_input(
    value: object,
) -> DirectIfcProductRealizationPreviewRequest:
    """Project a bounded Provider document onto the existing typed preview contract."""

    provider = PRODUCT_REALIZATION_PREVIEW_PROVIDER_INPUT_ADAPTER.validate_python(value)
    if isinstance(provider, DirectIfcProductRealizationCasesInput):
        return provider
    document, render_profile = parse_product_realization_invocation(
        provider.attachment_kind, provider.invocation_json, provider.preview_profile_json,
    )
    projected: dict[str, object] = {
        "direct_session_alias": provider.direct_session_alias,
        "mode": "single",
        "attachment_kind": provider.attachment_kind,
        "source": provider.source,
        "public_parameters": document.public_parameters,
        "product_origin": document.product_origin,
        "length_unit": document.length_unit,
        "render_profile": render_profile,
        "reference_geometries": provider.reference_geometries,
        "retain": provider.retain,
    }
    if isinstance(document, _ProductRealizationSelfGeometryInvocationDocument):
        projected["external_constraints"] = document.external_constraints
    try:
        return validate_product_realization_preview_input(projected)
    except ValidationError as exc:
        raise ProviderJsonDocumentError(
            provider_validation_message("invocation_json is invalid", exc, include_value_error_reason=True)
        ) from exc


def parse_product_realization_invocation(attachment_kind, invocation_json, preview_profile_json=None):
    """Strict, identity-free document decoding shared by both Provider entries."""
    render_profile = _decode_product_realization_preview_profile(preview_profile_json)
    decoded = decode_provider_json_object(
        invocation_json,
        label="invocation_json",
        maximum_utf8_bytes=_MAX_INVOCATION_DOCUMENT_BYTES,
    )
    document_type: type[_ProductRealizationInvocationDocumentBase]
    if attachment_kind == "self_geometry":
        document_type = _ProductRealizationSelfGeometryInvocationDocument
    elif attachment_kind == "standard_model":
        document_type = _ProductRealizationStandardModelInvocationDocument
    try:
        document = document_type.model_validate(decoded)
    except ValidationError as exc:
        raise ProviderJsonDocumentError(
            provider_validation_message(
                "invocation_json is invalid",
                exc,
                include_value_error_reason=True,
            )
        ) from exc

    return document, render_profile


def _decode_product_realization_preview_profile(
    value: str | None,
) -> GeometryPreviewProfile | None:
    if value is None:
        return None
    decoded = decode_provider_json_object(
        value,
        label="preview_profile_json",
        maximum_utf8_bytes=_MAX_PREVIEW_PROFILE_DOCUMENT_BYTES,
    )
    presentation = decoded.get("presentation")
    if isinstance(presentation, dict):
        overlay = presentation.get("overlay")
        if isinstance(overlay, dict) and "temporary_interfaces" in overlay:
            raise ProviderJsonDocumentError(
                "Product Realization preview_profile_json must not contain temporary_interfaces"
            )
    try:
        return GeometryPreviewProfile.model_validate(decoded)
    except ValidationError as exc:
        raise ProviderJsonDocumentError(
            provider_validation_message(
                "preview_profile_json is invalid",
                exc,
                include_value_error_reason=True,
            )
        ) from exc


def product_realization_parameter_values(
    values: dict[str, object],
) -> tuple[IfcProductFamilyPublicParameterResult, ...]:
    """Project parameters with bounded JSON leaves for structured readback."""

    projected: list[IfcProductFamilyPublicParameterResult] = []
    for key in sorted(values):
        value = values[key]
        if isinstance(value, bool):
            projected.append(
                IfcProductFamilyBooleanParameterValue(
                    parameter_kind="boolean", parameter_key=key, value=value
                )
            )
        elif isinstance(value, int):
            projected.append(
                IfcProductFamilyIntegerParameterValue(
                    parameter_kind="integer", parameter_key=key, value=value
                )
            )
        elif isinstance(value, float):
            projected.append(
                IfcProductFamilyNumberParameterValue(
                    parameter_kind="number", parameter_key=key, value=value
                )
            )
        elif isinstance(value, str):
            projected.append(
                IfcProductFamilyChoiceParameterValue(
                    parameter_kind="choice", parameter_key=key, value=value
                )
            )
        elif isinstance(value, (list, dict)):
            projected.append(IfcProductFamilyStructuredParameterResult(
                parameter_kind="array" if isinstance(value, list) else "object",
                parameter_key=key, value_json=canonical_json(value),
            ))
        else:
            raise ValueError("ProductFamily parameters must use supported structured or scalar values")
    return tuple(projected)


def product_realization_invocation_schemas() -> dict[str, dict[str, object]]:
    """Expose each existing closed document without introducing a second DTO."""
    return {
        "self_geometry": provider_reference_schema(
            _ProductRealizationSelfGeometryInvocationDocument,
            mode="validation",
        ),
        "standard_model": provider_reference_schema(
            _ProductRealizationStandardModelInvocationDocument,
            mode="validation",
        ),
    }


__all__ = [name for name in globals() if name.startswith(("DirectIfc", "IfcProduct"))] + [
    "product_realization_invocation_schemas",
    "PRODUCT_REALIZATION_PREVIEW_INPUT_ADAPTER",
    "PRODUCT_REALIZATION_PREVIEW_PROVIDER_INPUT_ADAPTER",
    "product_realization_parameter_values",
    "validate_product_realization_preview_input",
    "validate_product_realization_preview_provider_input",
]
