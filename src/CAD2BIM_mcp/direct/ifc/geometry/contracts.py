"""Closed Provider contracts for the Direct geometry-resource capability family."""

from __future__ import annotations

from CAD2BIM_mcp.direct.ifc.preview_cases import GeometryPreviewSource, PreviewCaseBatchResult, parse_cases, parse_render

from CAD2BIM_mcp.direct.geometry_preview_contracts import (
    IfcGeometryPreviewReferences,
    IfcPreviewDisplayQualityWarningResult,
    IfcPreviewPanelResult,
)

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D

from datetime import datetime
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, FiniteFloat, TypeAdapter, ValidationError, field_validator, model_validator

from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionCoordinates, GeometryInspectionSelection,
    GeometryInspectionDiagnostic, GeometrySectionDisplay,
    require_inspection_coordinate_selection,
)
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionObservation
from CAD2BIM_backend.internal_authoring_contracts import (
    GeometryResourceVersionQueryTarget, GeometryInspectionQueryTarget, FamilyStandardModelQueryTarget,
)

from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryPreviewProfile,
    GeometryPreviewPipesConnectionContext,
    GeometrySectionDiagnostic, GeometrySectionRenderFailure, GeometrySectionResolvedPlane,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract, NonNegativeInt64
from CAD2BIM_mcp.direct.source_view import (
    FullSourceView,
    LinesSourceView,
    SearchSourceView,
)
from CAD2BIM_mcp.direct.tool_support import (
    ProviderJsonDocumentError,
    decode_provider_json_object,
    provider_validation_message,
)
from CAD2BIM_mcp.tool_docs.preview import (
    GEOMETRY_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION,
    PREVIEW_REFERENCE_FIELD_DESCRIPTION, PREVIEW_RETAIN_DESCRIPTION,
    GEOMETRY_PREVIEW_SINGLE_SOURCE_DESCRIPTION, GEOMETRY_PREVIEW_PARAMETERS_DESCRIPTION,
    GEOMETRY_PREVIEW_CASE_SOURCE_DESCRIPTION, GEOMETRY_PREVIEW_CASES_DESCRIPTION,
    GEOMETRY_PREVIEW_RENDER_DESCRIPTION,
)
from CAD2BIM_mcp.tool_docs import ifc_geometry as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_SAFE_REFERENCE_PATTERN = r"^cur\..+$"
_CURSOR_PATTERN = r"^cur\..+$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SOURCE_PATH_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}$"
_MAX_SOURCE_BYTES = 65_536
_MAX_EDIT_BYTES = 16_384
_MAX_PARAMETER_JSON_BYTES = 16_384
_MAX_PREVIEW_PROFILE_JSON_BYTES = 65_536

IfcGeometryResourceKind: TypeAlias = Literal["mapped_model", "modeling_function"]
IfcGeometryOwnerScope: TypeAlias = Literal["system", "tenant", "task"]
IfcGeometryFormat: TypeAlias = Literal["occt_brep_v1", "indexed_triangle_mesh_v1"]


def _utf8_bounded(value: str, *, maximum: int, label: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    return value


def _safe_source_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix():
        raise ValueError("source_path must be a normalized relative POSIX path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source_path must not contain empty, dot, or parent segments")
    return value


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON number {token} is not allowed")


def _json_object_text(value: str, *, maximum: int, label: str) -> str:
    value = _utf8_bounded(value, maximum=maximum, label=label)
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _validate_geometry_preview_profile_json(value: str | None) -> None:
    if value is None:
        return
    decoded = decode_provider_json_object(
        value,
        label="preview_profile_json",
        maximum_utf8_bytes=_MAX_PREVIEW_PROFILE_JSON_BYTES,
    )
    try:
        GeometryPreviewProfile.model_validate(decoded)
    except ValidationError as exc:
        raise ProviderJsonDocumentError(
            provider_validation_message(
                "preview_profile_json is invalid",
                exc,
                include_value_error_reason=True,
            )
        ) from exc


def decode_geometry_preview_profile_json(value: str | None) -> GeometryPreviewProfile | None:
    """Decode the bounded Provider document into the one shared Domain profile."""

    if value is None:
        return None
    decoded = decode_provider_json_object(
        value,
        label="preview_profile_json",
        maximum_utf8_bytes=_MAX_PREVIEW_PROFILE_JSON_BYTES,
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


class _DirectGeometryInput(DirectContract):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )


class IfcGeometryResourceListFirstInput(DirectContract):
    resource_kinds: tuple[IfcGeometryResourceKind, ...] = Field(
        default=(),
        max_length=2,
        description=(
            _tool_docs.DIRECT_IFC_GEOMETRY_RESOURCE_LIST_FIRST_INPUT_RESOURCE_KINDS_DESCRIPTION
        ),
    )
    page_size: int = Field(
        default=50,
        strict=True,
        ge=1,
        le=100,
        description=_tool_docs.DIRECT_IFC_GEOMETRY_RESOURCE_LIST_FIRST_INPUT_PAGE_SIZE_DESCRIPTION,
    )

    @field_validator("resource_kinds")
    @classmethod
    def require_unique_resource_kinds(
        cls, value: tuple[IfcGeometryResourceKind, ...]
    ) -> tuple[IfcGeometryResourceKind, ...]:
        if len(value) != len(set(value)):
            raise ValueError("resource_kinds must not contain duplicates")
        return value


class DirectIfcGeometryResourceListFirstInput(IfcGeometryResourceListFirstInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryResourceListCursorInput(DirectContract):
    cursor: str = Field(
        pattern=_CURSOR_PATTERN,
        max_length=16_384,
        description=(_tool_docs.DIRECT_IFC_GEOMETRY_RESOURCE_LIST_CURSOR_INPUT_CURSOR_DESCRIPTION),
    )


class DirectIfcGeometryResourceListCursorInput(IfcGeometryResourceListCursorInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


DirectIfcGeometryResourceListInput: TypeAlias = (
    DirectIfcGeometryResourceListFirstInput | DirectIfcGeometryResourceListCursorInput
)


class IfcGeometryResourceRootTarget(DirectContract):
    target_kind: Literal["resource_root"] = Field(
        description=_tool_docs.IFC_GEOMETRY_RESOURCE_ROOT_TARGET_TARGET_KIND_DESCRIPTION
    )
    resource_alias: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN,
        max_length=16_384,
        description=_tool_docs.IFC_GEOMETRY_RESOURCE_ROOT_TARGET_RESOURCE_ALIAS_DESCRIPTION,
    )


class IfcGeometryResourceVersionTarget(DirectContract):
    target_kind: Literal["resource_version"] = Field(
        description=_tool_docs.IFC_GEOMETRY_RESOURCE_VERSION_TARGET_TARGET_KIND_DESCRIPTION
    )
    resource_version_alias: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN,
        max_length=16_384,
        description=(
            _tool_docs.IFC_GEOMETRY_RESOURCE_VERSION_TARGET_RESOURCE_VERSION_ALIAS_DESCRIPTION
        ),
    )


from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftInfoTarget
from CAD2BIM_backend.domain.authoring_draft_content import GeometryDraftInfo

IfcGeometryResourceInfoTarget: TypeAlias = (
    IfcGeometryResourceRootTarget | IfcGeometryResourceVersionTarget | AuthoringDraftInfoTarget
)


class IfcGeometryResourceInfoInput(DirectContract):
    target: IfcGeometryResourceInfoTarget = Field(
        description=_tool_docs.DIRECT_IFC_GEOMETRY_RESOURCE_INFO_INPUT_TARGET_DESCRIPTION
    )


class DirectIfcGeometryResourceInfoInput(IfcGeometryResourceInfoInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class _GeometryVersionSourceReadBase(DirectContract):
    resource_version_alias: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN,
        max_length=16_384,
        description=(
            _tool_docs.GEOMETRY_VERSION_SOURCE_READ_BASE_RESOURCE_VERSION_ALIAS_DESCRIPTION
        ),
    )
    source_path: str = Field(
        pattern=_SOURCE_PATH_PATTERN,
        max_length=255,
        description=(_tool_docs.GEOMETRY_VERSION_SOURCE_READ_BASE_SOURCE_PATH_DESCRIPTION),
    )

    _source_path = field_validator("source_path")(_safe_source_path)


class IfcGeometryResourceSourceFullInput(_GeometryVersionSourceReadBase, FullSourceView):
    pass


class DirectIfcGeometryResourceSourceFullInput(IfcGeometryResourceSourceFullInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryResourceSourceLinesInput(_GeometryVersionSourceReadBase, LinesSourceView):
    pass


class DirectIfcGeometryResourceSourceLinesInput(IfcGeometryResourceSourceLinesInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryResourceSourceSearchInput(_GeometryVersionSourceReadBase, SearchSourceView):
    pass


class DirectIfcGeometryResourceSourceSearchInput(IfcGeometryResourceSourceSearchInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


DirectIfcGeometryResourceSourceReadInput: TypeAlias = (
    DirectIfcGeometryResourceSourceFullInput
    | DirectIfcGeometryResourceSourceLinesInput
    | DirectIfcGeometryResourceSourceSearchInput
)


class IfcGeometrySinglePreviewInput(DirectContract):
    mode: Literal["single"]
    source: GeometryPreviewSource = Field(description=GEOMETRY_PREVIEW_SINGLE_SOURCE_DESCRIPTION)
    sample_parameters_json: str = Field(default="{}", min_length=2, max_length=_MAX_PARAMETER_JSON_BYTES, description=GEOMETRY_PREVIEW_PARAMETERS_DESCRIPTION)
    preview_profile_json: str | None = Field(default=None, min_length=2, max_length=_MAX_PREVIEW_PROFILE_JSON_BYTES, description=GEOMETRY_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION)
    datum_transform: RigidTransform3D | None = Field(default=None, description=_tool_docs.GEOMETRY_PREVIEW_DATUM_DESCRIPTION)
    reference_geometries: IfcGeometryPreviewReferences = Field(default=(), description=PREVIEW_REFERENCE_FIELD_DESCRIPTION)
    retain: bool = Field(default=False, strict=True, description=PREVIEW_RETAIN_DESCRIPTION)

    @field_validator("sample_parameters_json")
    @classmethod
    def validate_parameters(cls, value):
        return _json_object_text(value, maximum=_MAX_PARAMETER_JSON_BYTES, label="sample_parameters_json")

    @field_validator("preview_profile_json")
    @classmethod
    def validate_profile(cls, value):
        _validate_geometry_preview_profile_json(value)
        return value


class DirectIfcGeometrySinglePreviewInput(IfcGeometrySinglePreviewInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryCasesPreviewInput(DirectContract):
    mode: Literal["cases"]
    source: GeometryPreviewSource = Field(description=GEOMETRY_PREVIEW_CASE_SOURCE_DESCRIPTION)
    cases_json: str = Field(min_length=2, max_length=1_048_576, description=GEOMETRY_PREVIEW_CASES_DESCRIPTION)
    render_json: str | None = Field(default=None, min_length=2, max_length=65_536, description=GEOMETRY_PREVIEW_RENDER_DESCRIPTION)
    datum_transform: RigidTransform3D | None = Field(default=None, description=_tool_docs.GEOMETRY_PREVIEW_DATUM_DESCRIPTION)
    reference_geometries: IfcGeometryPreviewReferences = Field(default=(), description=PREVIEW_REFERENCE_FIELD_DESCRIPTION)
    retain: bool = Field(default=False, strict=True, description=PREVIEW_RETAIN_DESCRIPTION)

    @field_validator("cases_json")
    @classmethod
    def validate_cases(cls, value):
        parse_cases(value, case_kind="geometry")
        return value

    @field_validator("render_json")
    @classmethod
    def validate_render(cls, value):
        parse_render(value)
        return value


class DirectIfcGeometryCasesPreviewInput(IfcGeometryCasesPreviewInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryInspectionPreviewInput(DirectContract):
    mode: Literal["inspection"]
    inspection_ref: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384,
        description=_tool_docs.GEOMETRY_INSPECTION_REF_DESCRIPTION,
    )
    selection: GeometryInspectionSelection = Field(
        description=_tool_docs.GEOMETRY_INSPECTION_SELECTION_DESCRIPTION,
    )
    coordinate_space: GeometryInspectionCoordinates = Field(
        description=_tool_docs.GEOMETRY_INSPECTION_COORDINATES_DESCRIPTION,
    )
    observation: GeometryInspectionObservation = Field(
        description=_tool_docs.GEOMETRY_INSPECTION_VIEW_DESCRIPTION,
    )

    @model_validator(mode="after")
    def require_exact_fragment_coordinates(self):
        require_inspection_coordinate_selection(self.selection, self.coordinate_space)
        return self


class DirectIfcGeometryInspectionPreviewInput(IfcGeometryInspectionPreviewInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcFamilyStandardModelPreviewInput(DirectContract):
    mode: Literal["family_standard_model"]
    family_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    observation: GeometryInspectionObservation


class DirectIfcFamilyStandardModelPreviewInput(IfcFamilyStandardModelPreviewInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


DirectIfcGeometryPreviewInput: TypeAlias = Annotated[
    DirectIfcGeometrySinglePreviewInput | DirectIfcGeometryCasesPreviewInput | DirectIfcGeometryInspectionPreviewInput
    | DirectIfcFamilyStandardModelPreviewInput,
    Field(discriminator="mode"),
]


class IfcGeometrySourceEdit(DirectContract):
    old_text: str = Field(
        min_length=1,
        max_length=_MAX_EDIT_BYTES,
        description=_tool_docs.IFC_GEOMETRY_SOURCE_EDIT_OLD_TEXT_DESCRIPTION,
    )
    new_text: str = Field(
        default="",
        max_length=_MAX_EDIT_BYTES,
        description=_tool_docs.IFC_GEOMETRY_SOURCE_EDIT_NEW_TEXT_DESCRIPTION,
    )

    @field_validator("old_text", "new_text")
    @classmethod
    def validate_edit_text(cls, value: str, info) -> str:
        return _utf8_bounded(value, maximum=_MAX_EDIT_BYTES, label=info.field_name)


IfcGeometryResourceVersionQueryTarget = GeometryResourceVersionQueryTarget
IfcGeometryInspectionQueryTarget = GeometryInspectionQueryTarget
IfcFamilyStandardModelQueryTarget = FamilyStandardModelQueryTarget


IfcGeometryQueryFreshTarget: TypeAlias = (
    IfcGeometryResourceVersionQueryTarget | IfcGeometryInspectionQueryTarget | IfcFamilyStandardModelQueryTarget
)


class IfcGeometryQueryFreshInput(DirectContract):
    mode: Literal["fresh"] = Field(
        description=_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_FRESH_INPUT_MODE_DESCRIPTION
    )
    target: IfcGeometryQueryFreshTarget = Field(
        description=(_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_FRESH_INPUT_TARGET_DESCRIPTION)
    )
    code: str = Field(
        min_length=1,
        max_length=_MAX_SOURCE_BYTES,
        description=(_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_FRESH_INPUT_CODE_DESCRIPTION),
    )
    page_size: int = Field(
        default=100,
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_FRESH_INPUT_PAGE_SIZE_DESCRIPTION,
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _utf8_bounded(value, maximum=_MAX_SOURCE_BYTES, label="query code")


class DirectIfcGeometryQueryFreshInput(IfcGeometryQueryFreshInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryQueryRepairInput(DirectContract):
    mode: Literal["repair"] = Field(
        description=(_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_REPAIR_INPUT_MODE_DESCRIPTION)
    )
    parent_attempt_alias: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN,
        max_length=16_384,
        description=_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_REPAIR_INPUT_PARENT_ATTEMPT_ALIAS_DESCRIPTION,
    )
    edits: tuple[IfcGeometrySourceEdit, ...] = Field(
        min_length=1,
        max_length=16,
        description=_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_REPAIR_INPUT_EDITS_DESCRIPTION,
    )
    page_size: int = Field(
        default=100,
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_REPAIR_INPUT_PAGE_SIZE_DESCRIPTION,
    )


class DirectIfcGeometryQueryRepairInput(IfcGeometryQueryRepairInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryQueryCursorInput(DirectContract):
    mode: Literal["cursor"] = Field(
        description=_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_CURSOR_INPUT_MODE_DESCRIPTION
    )
    cursor: str = Field(
        pattern=_CURSOR_PATTERN,
        max_length=16_384,
        description=_tool_docs.DIRECT_IFC_GEOMETRY_QUERY_CURSOR_INPUT_CURSOR_DESCRIPTION,
    )


class DirectIfcGeometryQueryCursorInput(IfcGeometryQueryCursorInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


DirectIfcGeometryQueryPythonInput: TypeAlias = (
    DirectIfcGeometryQueryFreshInput
    | DirectIfcGeometryQueryRepairInput
    | DirectIfcGeometryQueryCursorInput
)


class IfcGeometryResourceCard(DirectContract):
    resource_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    resource_kind: IfcGeometryResourceKind
    owner_scope: IfcGeometryOwnerScope
    display_name: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=2048)
    latest_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    latest_version_no: int = Field(strict=True, ge=1)
    created_at: datetime


class IfcGeometryResourceListResult(DirectContract):
    resources: tuple[IfcGeometryResourceCard, ...] = Field(max_length=100)
    complete: bool
    next_cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)


class IfcGeometryResourceVersionCard(DirectContract):
    resource_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    version_no: int = Field(strict=True, ge=1)
    origin_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", max_length=64)
    geometry_format: IfcGeometryFormat | None
    has_source: bool
    has_preview: bool = Field(
        description=(_tool_docs.IFC_GEOMETRY_RESOURCE_VERSION_CARD_HAS_PREVIEW_DESCRIPTION)
    )
    created_at: datetime


class IfcGeometryResourceRootInfoResult(DirectContract):
    target_kind: Literal["resource_root"]
    resource_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    resource_kind: IfcGeometryResourceKind
    owner_scope: IfcGeometryOwnerScope
    display_name: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=4096)
    versions: tuple[IfcGeometryResourceVersionCard, ...] = Field(max_length=100)


class IfcGeometryBoundingBoxResult(DirectContract):
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]


class IfcGeometryTopologyResult(DirectContract):
    solid_count: NonNegativeInt64
    shell_count: NonNegativeInt64
    face_count: NonNegativeInt64
    edge_count: NonNegativeInt64
    vertex_count: NonNegativeInt64
    triangle_count: NonNegativeInt64 | None = Field(description=_tool_docs.GEOMETRY_TOPOLOGY_TRIANGLE_COUNT_DESCRIPTION)


class IfcGeometrySourceFileResult(DirectContract):
    source_path: str = Field(pattern=_SOURCE_PATH_PATTERN, max_length=255)
    size_bytes: int = Field(strict=True, ge=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    text_available: bool = Field(
        description=(_tool_docs.IFC_GEOMETRY_SOURCE_FILE_RESULT_TEXT_AVAILABLE_DESCRIPTION)
    )

    _source_path = field_validator("source_path")(_safe_source_path)


class IfcGeometryResourceInputInfo(DirectContract):
    input_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    resource_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)


from CAD2BIM_mcp.direct.contract_base import OperationNavigation


class IfcGeometryResourceVersionInfoResult(DirectContract):
    publication: OperationNavigation | None = None
    target_kind: Literal["resource_version"]
    resource_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    resource_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    resource_kind: IfcGeometryResourceKind
    owner_scope: IfcGeometryOwnerScope
    display_name: str = Field(min_length=1, max_length=255)
    usage_instructions: str | None = Field(
        default=None,
        max_length=8_192,
        description=_tool_docs.VERSION_USAGE_INSTRUCTIONS_INFO_DESCRIPTION,
    )
    version_no: int = Field(strict=True, ge=1)
    origin_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", max_length=64)
    geometry_format: IfcGeometryFormat | None
    unit: Literal["METRE"]
    local_basis: Literal["right_handed"]
    geometry_inputs: tuple[IfcGeometryResourceInputInfo, ...] = Field(default=(), max_length=8,
        description=_tool_docs.GEOMETRY_RESOURCE_INPUTS_DESCRIPTION)
    interfaces: GeometryModelInterfaces
    bbox: IfcGeometryBoundingBoxResult | None
    topology: IfcGeometryTopologyResult | None
    source_available: bool
    source_files: tuple[IfcGeometrySourceFileResult, ...] = Field(max_length=64)
    preview_available: bool = Field(
        description=(
            _tool_docs.IFC_GEOMETRY_RESOURCE_VERSION_INFO_RESULT_PREVIEW_AVAILABLE_DESCRIPTION
        )
    )
    parameter_schema_json: str | None = Field(default=None, max_length=65_536)
    created_at: datetime


class IfcGeometryResourceInfoResult(DirectContract):
    info: IfcGeometryResourceRootInfoResult | IfcGeometryResourceVersionInfoResult | GeometryDraftInfo = Field(
        description=_tool_docs.IFC_GEOMETRY_RESOURCE_INFO_RESULT_INFO_DESCRIPTION
    )


class IfcGeometryResourceSourceReadResult(DirectContract):
    resource_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    source_path: str = Field(pattern=_SOURCE_PATH_PATTERN, max_length=255)
    view_mode: Literal["full", "lines", "search"]
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    total_lines: NonNegativeInt64
    text: str = Field(max_length=_MAX_SOURCE_BYTES)
    returned_start_line: int | None = Field(default=None, strict=True, ge=1)
    returned_line_count: NonNegativeInt64
    match_count: NonNegativeInt64
    truncated: bool

    _source_path = field_validator("source_path")(_safe_source_path)


class IfcGeometryInspectionImageResult(DirectContract):
    mime_type: Literal["image/png"] = "image/png"
    size_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    width_px: int = Field(ge=64, le=2048)
    height_px: int = Field(ge=64, le=2048)
    display_quality_warnings: tuple[IfcPreviewDisplayQualityWarningResult, ...] = ()
    panels: tuple[IfcPreviewPanelResult, ...] = ()
    connection_context: GeometryPreviewPipesConnectionContext | None = None
    connection_context_diagnostics: tuple[str, ...] = ()


class IfcGeometrySectionCurveResult(DirectContract):
    curve_type: str = Field(min_length=1, max_length=32)
    length_m: float = Field(ge=0, allow_inf_nan=False)
    radius_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    major_radius_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    minor_radius_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    center_uv_m: tuple[FiniteFloat, FiniteFloat] | None = None


class IfcGeometrySectionBoundaryResult(DirectContract):
    boundary_index: int = Field(ge=0)
    role: Literal["outer", "hole", "unknown"]
    closed: bool
    length_m: float = Field(ge=0, allow_inf_nan=False)
    curves: tuple[IfcGeometrySectionCurveResult, ...]


class IfcGeometrySectionRegionResult(DirectContract):
    region_index: int = Field(ge=0)
    area_m2: float = Field(ge=0, allow_inf_nan=False)
    boundaries: tuple[IfcGeometrySectionBoundaryResult, ...]


class IfcGeometrySectionObjectResult(DirectContract):
    object_index: int = Field(ge=0)
    fragment_key: str | None = None
    component_key: str | None = None
    binding_key: str | None = None
    source_shape_kind: str = Field(min_length=1, max_length=32)
    status: Literal["computed", "empty", "degenerate", "failed", "unsupported"]
    material_regions: tuple[IfcGeometrySectionRegionResult, ...] = ()
    material_area_m2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    intersection_edge_count: int = Field(default=0, ge=0)
    intersection_vertex_count: int = Field(default=0, ge=0)
    intersection_curves: tuple[IfcGeometrySectionCurveResult, ...] = ()
    intersection_points_uv_m: tuple[tuple[FiniteFloat, FiniteFloat], ...] = ()
    diagnostics: tuple[GeometrySectionDiagnostic, ...] = ()


class IfcGeometrySectionMeasurementResult(DirectContract):
    section_index: int = Field(ge=0, le=3)
    offset_m: float = Field(allow_inf_nan=False)
    plane: GeometrySectionResolvedPlane
    status: Literal["computed", "empty", "degenerate", "failed", "unsupported"]
    objects: tuple[IfcGeometrySectionObjectResult, ...]
    material_area_sum_m2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    area_semantics: Literal["sum_of_object_material_regions_not_union"]
    projected_bounds_uv_m: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat] | None = Field(
        default=None, description=_tool_docs.GEOMETRY_SECTION_BOUNDS_DESCRIPTION,
    )
    diagnostics: tuple[GeometrySectionDiagnostic, ...] = ()


class IfcGeometrySectionImageResult(IfcGeometryInspectionImageResult):
    section_index: int = Field(ge=0, le=3)
    resolved_display: GeometrySectionDisplay
    display_clipped: bool
    diagnostics: tuple[GeometrySectionDiagnostic, ...] = ()


class _IfcGeometryInspectionPreviewBase(DirectContract):
    operation_ref: str | None = Field(default=None, min_length=1, max_length=16_384)
    case_key: str | None = None
    mode: Literal["inspection"] = "inspection"
    inspection_ref: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    operation_alias: str | None = Field(
        default=None, pattern=r"^op_[0-9]{3,}$", max_length=32,
        description=_tool_docs.GEOMETRY_INSPECTION_OPERATION_DESCRIPTION,
    )
    source_kind: Literal["geometry_sample", "family_realization"]
    source_version_ref: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    result_coordinate_space: Literal["geometry_local", "project_world"]
    selection: GeometryInspectionSelection
    coordinate_space: GeometryInspectionCoordinates
    diagnostics: tuple[GeometryInspectionDiagnostic, ...] = ()
    derived_only: Literal[True] = True

    @model_validator(mode="after")
    def require_exact_fragment_coordinates(self):
        require_inspection_coordinate_selection(self.selection, self.coordinate_space)
        return self


class IfcGeometryInspectionModelPreviewResult(_IfcGeometryInspectionPreviewBase):
    observation_kind: Literal["model"] = "model"
    image: IfcGeometryInspectionImageResult | None = None

    @model_validator(mode="after")
    def require_image_or_render_diagnostic(self):
        if self.image is None and not any(item.phase == "render" for item in self.diagnostics):
            raise ValueError("a model observation without an image requires an actual render diagnostic")
        return self


class IfcGeometryInspectionSectionPreviewResult(_IfcGeometryInspectionPreviewBase):
    observation_kind: Literal["section"] = "section"
    measurements: tuple[IfcGeometrySectionMeasurementResult, ...] = Field(min_length=1, max_length=4)
    images: tuple[IfcGeometrySectionImageResult, ...] = Field(default=(), max_length=4)
    render_errors: tuple[GeometrySectionRenderFailure, ...] = Field(default=(), max_length=4)


class _IfcFamilyStandardModelPreviewBase(DirectContract):
    mode: Literal["family_standard_model"] = "family_standard_model"
    family_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    public_parameters_json: str
    result_coordinate_space: Literal["geometry_local"] = "geometry_local"
    unit: Literal["METRE"] = "METRE"
    derived_only: Literal[True] = True
    diagnostics: tuple[GeometryInspectionDiagnostic, ...] = ()


class IfcFamilyStandardModelPreviewResult(_IfcFamilyStandardModelPreviewBase):
    observation_kind: Literal["model"] = "model"
    image: IfcGeometryInspectionImageResult | None = None


class IfcFamilyStandardSectionPreviewResult(_IfcFamilyStandardModelPreviewBase):
    observation_kind: Literal["section"] = "section"
    measurements: tuple[IfcGeometrySectionMeasurementResult, ...] = Field(min_length=1, max_length=4)
    images: tuple[IfcGeometrySectionImageResult, ...] = Field(default=(), max_length=4)
    render_errors: tuple[GeometrySectionRenderFailure, ...] = Field(default=(), max_length=4)


IfcGeometryPreviewOutcomeResult: TypeAlias = (
    PreviewCaseBatchResult | IfcGeometryInspectionModelPreviewResult
    | IfcGeometryInspectionSectionPreviewResult
    | IfcFamilyStandardModelPreviewResult | IfcFamilyStandardSectionPreviewResult
)


class IfcGeometryResourceVersionQueryResultTarget(DirectContract):
    target_kind: Literal["geometry_resource_version"]
    resource_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)


IfcGeometryQueryResultTarget: TypeAlias = (
    IfcGeometryResourceVersionQueryResultTarget | IfcGeometryInspectionQueryTarget | IfcFamilyStandardModelQueryTarget
)


class IfcGeometryQueryPythonResult(DirectContract):
    attempt_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    parent_attempt_alias: str | None = Field(
        default=None, pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384
    )
    target: IfcGeometryQueryResultTarget = Field(
        description=_tool_docs.IFC_GEOMETRY_QUERY_PYTHON_RESULT_TARGET_DESCRIPTION
    )
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    status: Literal["succeeded"]
    summary: str = Field(min_length=1, max_length=4096)
    result_json: str = Field(
        max_length=65_536,
        description=_tool_docs.IFC_GEOMETRY_QUERY_PYTHON_RESULT_RESULT_JSON_DESCRIPTION,
    )
    next_cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)


GEOMETRY_RESOURCE_LIST_INPUT_ADAPTER = TypeAdapter(DirectIfcGeometryResourceListInput)
GEOMETRY_RESOURCE_INFO_RESULT_ADAPTER = TypeAdapter(IfcGeometryResourceInfoResult)
GEOMETRY_RESOURCE_SOURCE_READ_INPUT_ADAPTER = TypeAdapter(DirectIfcGeometryResourceSourceReadInput)
GEOMETRY_PREVIEW_INPUT_ADAPTER = TypeAdapter(DirectIfcGeometryPreviewInput)
GEOMETRY_QUERY_INPUT_ADAPTER = TypeAdapter(DirectIfcGeometryQueryPythonInput)


def validate_geometry_resource_list_input(value: object) -> DirectIfcGeometryResourceListInput:
    return GEOMETRY_RESOURCE_LIST_INPUT_ADAPTER.validate_python(value)


def validate_geometry_resource_source_read_input(
    value: object,
) -> DirectIfcGeometryResourceSourceReadInput:
    return GEOMETRY_RESOURCE_SOURCE_READ_INPUT_ADAPTER.validate_python(value)


def validate_geometry_preview_input(value: object) -> DirectIfcGeometryPreviewInput:
    return GEOMETRY_PREVIEW_INPUT_ADAPTER.validate_python(value)


def validate_geometry_query_input(value: object) -> DirectIfcGeometryQueryPythonInput:
    return GEOMETRY_QUERY_INPUT_ADAPTER.validate_python(value)


__all__ = [name for name in globals() if name.startswith(("DirectIfc", "IfcGeometry"))] + [
    "GEOMETRY_QUERY_INPUT_ADAPTER",
    "GEOMETRY_PREVIEW_INPUT_ADAPTER",
    "GEOMETRY_RESOURCE_INFO_RESULT_ADAPTER",
    "GEOMETRY_RESOURCE_LIST_INPUT_ADAPTER",
    "GEOMETRY_RESOURCE_SOURCE_READ_INPUT_ADAPTER",
    "validate_geometry_query_input",
    "decode_geometry_preview_profile_json",
    "validate_geometry_preview_input",
    "validate_geometry_resource_list_input",
    "validate_geometry_resource_source_read_input",
]
