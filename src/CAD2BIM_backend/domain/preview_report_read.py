"""Closed read selections and public pages over one immutable Preview report."""
from datetime import datetime
from typing import Literal, TypeAlias
from pydantic import Field, StrictInt
from CAD2BIM_backend.domain.preview_cases import PreviewCaseContract


class _PreviewTextSelection(PreviewCaseContract):
    mode: Literal["preview_report"]
    operation_ref: str = Field(min_length=1, max_length=16_384)
    start_character: StrictInt = Field(default=0, ge=0)
    character_count: StrictInt = Field(default=4096, ge=1, le=16_384)


class PreviewCaseTextSelection(_PreviewTextSelection):
    content_kind: Literal["case_request", "case_outcome"]
    case_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class PreviewReportTextSelection(_PreviewTextSelection):
    content_kind: Literal["dependencies", "coverage_gaps", "cross_case_diagnostics", "placement_input", "observation_input"]


PreviewTextSelection: TypeAlias = PreviewCaseTextSelection | PreviewReportTextSelection


class PreviewReadOperation(PreviewCaseContract):
    operation_ref: str
    operation_alias: str | None
    sequence: int
    tool_name: Literal["ifc_geometry_preview", "ifc_product_realization_preview"]
    outcome: Literal["succeeded", "failed"]
    started_at: datetime
    finished_at: datetime
    error_code: str | None = None
    error_phase: str | None = None
    error_message: str | None = None


class PreviewContentDescriptor(PreviewCaseContract):
    content_kind: Literal["case_request", "case_outcome", "dependencies", "coverage_gaps", "cross_case_diagnostics", "placement_input", "observation_input"]
    total_characters: int
    total_utf8_bytes: int


class PreviewCaseSummary(PreviewCaseContract):
    case_key: str
    execution_status: Literal["completed", "technical_error", "not_run"]
    execution_source: Literal["executed", "reused", "none"]
    status: Literal["BUILT", "REJECTED", "SOLVED", "AMBIGUOUS", "INFEASIBLE"] | None
    expectation_check: Literal["pass", "fail", "not_checked"]
    external_target_comparison: Literal["PASS", "FAIL", "NOT_COMPARED"] | None
    inspection_ref: str | None
    render_status: Literal["not_requested", "rendered", "failed", "no_geometry"]
    diagnostic_count: int
    contents: tuple[PreviewContentDescriptor, ...]


class PreviewReportSummary(PreviewCaseContract):
    detail_kind: Literal["preview_case_report"] = "preview_case_report"
    source_kind: Literal["geometry_sample", "family_realization"]
    source_ref: str
    completion: Literal["complete", "interrupted"]
    checks_passed: bool
    case_count: int
    cases: tuple[PreviewCaseSummary, ...]
    contents: tuple[PreviewContentDescriptor, ...]
    nominal_scenario_key: str | None
    complete: bool
    next_after_case_key: str | None


class PreviewReportText(PreviewContentDescriptor):
    detail_kind: Literal["preview_report_text"] = "preview_report_text"
    case_key: str | None
    start_character: int
    returned_characters: int
    text: str
    complete: bool
    next_character: int | None


class PreviewOperationRead(PreviewCaseContract):
    operation: PreviewReadOperation
    detail: PreviewReportSummary | PreviewReportText
