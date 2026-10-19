"""Bounded Provider documents and compact projections for shared Preview cases."""

import json

from typing import Literal

from pydantic import Field, TypeAdapter

from CAD2BIM_backend.domain.preview_cases import (
    GeometryPreviewSource as GeometryPreviewSource, FamilyPreviewSource as FamilyPreviewSource,
    GeometryCasesInput, FamilyCasesInput, PreviewCasesInput, PreviewCaseRender, PreviewCaseDiagnostic, PreviewCaseBatchOutcome,
    PreviewCaseResult,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.direct.tool_support import decode_provider_json_object
from CAD2BIM_mcp.tool_docs import preview as _tool_docs


CASES_MAX_BYTES = 1_048_576


def parse_cases(value: str, *, case_kind: Literal["geometry", "family"]) -> PreviewCasesInput:
    return TypeAdapter(GeometryCasesInput if case_kind == "geometry" else FamilyCasesInput).validate_python(decode_provider_json_object(
        value, label="cases_json", maximum_utf8_bytes=CASES_MAX_BYTES,
    ))


def parse_render(value: str | None) -> PreviewCaseRender | None:
    return None if value is None else TypeAdapter(PreviewCaseRender).validate_python(decode_provider_json_object(
        value, label="render_json", maximum_utf8_bytes=65_536,
    ))


class PreviewCaseResultCard(DirectContract):
    case_key: str
    execution_status: Literal["completed", "technical_error", "not_run"] = Field(description=_tool_docs.PREVIEW_EXECUTION_STATUS_DESCRIPTION)
    execution_source: Literal["executed", "reused", "none"] = Field(description=_tool_docs.PREVIEW_EXECUTION_SOURCE_DESCRIPTION)
    status: Literal["BUILT", "REJECTED", "SOLVED", "AMBIGUOUS", "INFEASIBLE"] | None = None
    expectation_check: Literal["pass", "fail", "not_checked"] = Field(description=_tool_docs.PREVIEW_EXPECTATION_DESCRIPTION)
    external_target_comparison: Literal["PASS", "FAIL", "NOT_COMPARED"] | None = Field(
        default=None, description=_tool_docs.PREVIEW_CASE_EXTERNAL_TARGET_COMPARISON_DESCRIPTION
    )
    inspection_ref: str | None = Field(default=None, description=_tool_docs.PREVIEW_SAVED_REF_DESCRIPTION)
    render_status: Literal["not_requested", "rendered", "failed", "no_geometry"]
    diagnostics: tuple[PreviewCaseDiagnostic, ...] = ()


class PreviewDependencyReference(DirectContract):
    source_ref: str


class PreviewCaseBatchResult(DirectContract):
    result_kind: Literal["case_report"] = "case_report"
    source_ref: str
    dependencies: tuple[PreviewDependencyReference, ...] = ()
    operation_alias: str | None = None
    completion: Literal["complete", "interrupted"] = Field(description=_tool_docs.PREVIEW_COMPLETION_DESCRIPTION)
    checks_passed: bool = Field(description=_tool_docs.PREVIEW_CHECKS_DESCRIPTION)
    cases: tuple[PreviewCaseResultCard, ...]
    rendered_case_keys: tuple[str, ...] = Field(default=(), description=_tool_docs.PREVIEW_IMAGE_KEYS_DESCRIPTION)
    coverage_gaps: tuple[str, ...] = Field(default=(), description=_tool_docs.PREVIEW_COVERAGE_DESCRIPTION)
    cross_case_diagnostics: tuple[PreviewCaseDiagnostic, ...] = ()


from CAD2BIM_backend.application.preview_report_projection import (
    external_target_comparison, project_case_outcome_json,
)


def project_case_batch(outcome: PreviewCaseBatchOutcome) -> PreviewCaseBatchResult:
    report = outcome.report
    return PreviewCaseBatchResult(
        source_ref=report.source.source_ref,
        dependencies=tuple(PreviewDependencyReference(source_ref=item.source_ref)
                           for item in report.dependencies),
        operation_alias=outcome.operation_alias, completion=report.completion,
        checks_passed=report.checks_passed,
        cases=tuple(PreviewCaseResultCard(
            case_key=item.case.case_key, execution_status=item.execution_status,
            execution_source=item.execution_source,
            status=item.geometry_status if item.realization is None else item.realization.proposal.status.value,
            expectation_check=item.expectation_check, inspection_ref=item.inspection_ref,
            external_target_comparison=(None if item.realization is None else external_target_comparison(item.realization)),
            render_status=item.render_status, diagnostics=item.diagnostics,
        ) for item in report.cases),
        coverage_gaps=report.coverage_gaps, cross_case_diagnostics=report.cross_case_diagnostics,
        rendered_case_keys=tuple(key for key, _ in outcome.images),
    )

