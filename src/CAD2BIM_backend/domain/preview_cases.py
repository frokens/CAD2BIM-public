"""Finite Preview cases and their exact saved execution evidence.

Cases are author-supplied expectations, not runtime inputs or publication facts.
Reports reference Inspection assets; they never duplicate native geometry bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from CAD2BIM_backend.domain.geometry_query import GeometryQueryArtifactRef
from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef
from CAD2BIM_backend.domain.geometry_runtime import GeometryPreview, GeometryPreviewProfile, GeometrySummary
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation, ProductRealizationResult,
    ProductRealizationStatus, ProductRealizationValidationScenario,
)

_KEY = r"^[a-z][a-z0-9_]{0,63}$"


class PreviewCaseContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DraftPreviewSource(PreviewCaseContract):
    source_kind: Literal["draft"]
    draft_ref: str = Field(min_length=1, max_length=16_384)


class PublishedGeometryPreviewSource(PreviewCaseContract):
    source_kind: Literal["published"]
    resource_version_alias: str = Field(min_length=1, max_length=16_384)


class PublishedFamilyPreviewSource(PreviewCaseContract):
    source_kind: Literal["published"]
    family_version_alias: str = Field(min_length=1, max_length=16_384)


GeometryPreviewSource = Annotated[DraftPreviewSource | PublishedGeometryPreviewSource, Field(discriminator="source_kind")]
FamilyPreviewSource = Annotated[DraftPreviewSource | PublishedFamilyPreviewSource, Field(discriminator="source_kind")]
class PreviewSourceSnapshot(PreviewCaseContract):
    """Identity frozen by the authorized resource owner before execution."""

    source_kind: Literal["geometry_sample", "family_realization"]
    source_type: Literal["published", "draft"]
    source_id: str = Field(min_length=1, max_length=80)
    source_ref: str = Field(min_length=1, max_length=16_384)
    snapshot_id: str = Field(min_length=1, max_length=80)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=255)
    draft_artifact: ArtifactVersionRef | None = None

    @model_validator(mode="after")
    def exact_draft(self):
        if (self.source_type == "draft") != (self.draft_artifact is not None):
            raise ValueError("only a draft source carries its exact content Artifact")
        if self.draft_artifact is not None and self.snapshot_id != self.draft_artifact.version_id:
            raise ValueError("draft snapshot must identify its exact content Artifact version")
        return self


class PreviewInspectionReuse(PreviewCaseContract):
    reuse_kind: Literal["inspection"] = "inspection"
    inspection_ref: str = Field(min_length=1, max_length=16_384)


class PreviewCaseReuse(PreviewCaseContract):
    reuse_kind: Literal["case"] = "case"
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$")
    case_key: str = Field(pattern=_KEY)


PreviewExecutionReuse = Annotated[PreviewInspectionReuse | PreviewCaseReuse, Field(discriminator="reuse_kind")]


class GeometryCaseExpectation(PreviewCaseContract):
    expected_status: Literal["BUILT", "REJECTED"] = "BUILT"
    reason_code: Literal["PARAMETERS_INVALID"] | None = None

    @model_validator(mode="after")
    def rejection_reason(self):
        if (self.expected_status == "REJECTED") != (self.reason_code is not None):
            raise ValueError("a Geometry rejection expectation requires its domain reason")
        return self


class FamilyCaseExpectation(PreviewCaseContract):
    expected_status: ProductRealizationStatus
    reason_keys: tuple[Annotated[str, Field(pattern=_KEY)], ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def rejection_reason(self):
        if self.expected_status is ProductRealizationStatus.SOLVED and self.reason_keys:
            raise ValueError("SOLVED does not have rejection or ambiguity reasons")
        if self.expected_status is not ProductRealizationStatus.SOLVED and not self.reason_keys:
            raise ValueError("a negative Family expectation requires stable business reason keys")
        if len(self.reason_keys) != len(set(self.reason_keys)):
            raise ValueError("expected reason keys must be unique")
        return self


class GeometryPreviewCase(PreviewCaseContract):
    case_kind: Literal["geometry"] = "geometry"
    case_key: str = Field(pattern=_KEY)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    expectation: GeometryCaseExpectation | None = None
    reuse: PreviewExecutionReuse | None = None


class FamilyPreviewCase(PreviewCaseContract):
    case_kind: Literal["family"] = "family"
    case_key: str = Field(pattern=_KEY)
    invocation: ProductRealizationInvocation
    expectation: FamilyCaseExpectation | None = None
    reuse: PreviewExecutionReuse | None = None

    def to_scenario(self) -> ProductRealizationValidationScenario:
        if self.expectation is None:
            raise ValueError("coverage requires an explicit case expectation")
        return ProductRealizationValidationScenario(**self.invocation.model_dump(mode="python"),
            scenario_key=self.case_key, expected_status=self.expectation.expected_status)


PreviewCase = Annotated[GeometryPreviewCase | FamilyPreviewCase, Field(discriminator="case_kind")]


_CaseT = TypeVar("_CaseT", bound=PreviewCase)


class PreviewCaseAdd(PreviewCaseContract, Generic[_CaseT]):
    change_kind: Literal["add"] = "add"
    case: _CaseT


class PreviewCaseReplace(PreviewCaseContract, Generic[_CaseT]):
    change_kind: Literal["replace"] = "replace"
    case: _CaseT


class PreviewCaseDelete(PreviewCaseContract):
    change_kind: Literal["delete"] = "delete"
    case_key: str = Field(pattern=_KEY)


class PreviewCaseReuseChange(PreviewCaseContract):
    change_kind: Literal["reuse"] = "reuse"
    case_key: str = Field(pattern=_KEY)


PreviewCaseChange = Annotated[PreviewCaseAdd | PreviewCaseReplace | PreviewCaseDelete | PreviewCaseReuseChange, Field(discriminator="change_kind")]


class InlinePreviewCases(PreviewCaseContract, Generic[_CaseT]):
    mode: Literal["inline"] = "inline"
    items: tuple[_CaseT, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({item.case_key for item in self.items}) != len(self.items):
            raise ValueError("case keys must be unique")
        if len(self.items) > 8 and any(isinstance(item, GeometryPreviewCase) for item in self.items):
            raise ValueError("Geometry Preview accepts at most 8 cases per batch")
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler.resolve_ref_schema(handler(core_schema))
        if cls.__pydantic_generic_metadata__.get("args") == (GeometryPreviewCase,):
            schema["properties"]["items"]["maxItems"] = 8
        return schema


class SavedPreviewCases(PreviewCaseContract, Generic[_CaseT]):
    mode: Literal["from_operation"] = "from_operation"
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$")
    changes: tuple[Annotated[PreviewCaseAdd[_CaseT] | PreviewCaseReplace[_CaseT] | PreviewCaseDelete | PreviewCaseReuseChange, Field(discriminator="change_kind")], ...] = Field(default=(), max_length=32)


PreviewCasesInput = Annotated[InlinePreviewCases | SavedPreviewCases, Field(discriminator="mode")]


GeometryCasesInput = Annotated[InlinePreviewCases[GeometryPreviewCase] | SavedPreviewCases[GeometryPreviewCase], Field(discriminator="mode")]
FamilyCasesInput = Annotated[InlinePreviewCases[FamilyPreviewCase] | SavedPreviewCases[FamilyPreviewCase], Field(discriminator="mode")]


class PreviewNoRender(PreviewCaseContract):
    mode: Literal["none"] = "none"


class PreviewSelectedRender(PreviewCaseContract):
    mode: Literal["selected"] = "selected"
    case_keys: tuple[Annotated[str, Field(pattern=_KEY)], ...] = Field(min_length=1, max_length=16)
    profile: GeometryPreviewProfile | None = None

    @model_validator(mode="after")
    def unique_keys(self):
        if len(set(self.case_keys)) != len(self.case_keys):
            raise ValueError("render case keys must be unique")
        return self


PreviewCaseRender = Annotated[PreviewNoRender | PreviewSelectedRender, Field(discriminator="mode")]


class PreviewCaseDiagnostic(PreviewCaseContract):
    code: str = Field(min_length=1, max_length=120)
    phase: Literal["input", "execution", "output_validation", "render", "storage", "interrupted"]
    message: str = Field(min_length=1, max_length=2000)


class PreviewCaseResult(PreviewCaseContract):
    case: PreviewCase
    execution_status: Literal["completed", "technical_error", "not_run"]
    execution_source: Literal["executed", "reused", "none"] = "executed"
    expectation_check: Literal["pass", "fail", "not_checked"]
    realization: ProductRealizationResult | None = None
    geometry_status: Literal["BUILT", "REJECTED"] | None = None
    geometry_summary: GeometrySummary | None = None
    geometry_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    geometry_interfaces: GeometryModelInterfaces = ()
    inspection_ref: str | None = None
    inspection_artifact: GeometryQueryArtifactRef | None = None
    render_status: Literal["not_requested", "rendered", "failed", "no_geometry"] = "not_requested"
    diagnostics: tuple[PreviewCaseDiagnostic, ...] = ()

    @model_validator(mode="after")
    def complete_case(self):
        if (self.inspection_ref is None) != (self.inspection_artifact is None):
            raise ValueError("retained geometry needs its exact Artifact and public reference")
        if self.execution_status != "completed":
            if self.expectation_check == "pass" or self.realization is not None or self.geometry_status is not None:
                raise ValueError("technical failures and unexecuted cases cannot claim business results")
        elif isinstance(self.case, FamilyPreviewCase):
            if self.realization is None or self.geometry_status is not None:
                raise ValueError("completed Family cases require their actual realization")
            if (self.realization.proposal.status is not ProductRealizationStatus.SOLVED or self.realization.execution_stage != "instantiated") and self.inspection_ref is not None:
                raise ValueError("non-SOLVED cases cannot carry a shape reference")
        elif self.geometry_status is None or self.realization is not None:
            raise ValueError("completed Geometry cases require their actual construction result")
        if self.geometry_status == "REJECTED" and self.inspection_ref is not None:
            raise ValueError("rejected Geometry cases cannot carry a shape reference")
        return self


class PreviewCaseReport(PreviewCaseContract):
    source: PreviewSourceSnapshot
    dependencies: tuple[PreviewSourceSnapshot, ...] = ()
    cases: tuple[PreviewCaseResult, ...] = Field(min_length=1, max_length=16)
    nominal_scenario_key: str | None = Field(default=None, pattern=_KEY)
    completion: Literal["complete", "interrupted"] = "complete"
    coverage_gaps: tuple[str, ...] = ()
    cross_case_diagnostics: tuple[PreviewCaseDiagnostic, ...] = ()

    @model_validator(mode="after")
    def coherent_cases(self):
        expected_kind = "family" if self.source.source_kind == "family_realization" else "geometry"
        keys = [item.case.case_key for item in self.cases]
        if len(keys) != len(set(keys)) or any(item.case.case_kind != expected_kind for item in self.cases):
            raise ValueError("report cases must be unique and match their source kind")
        if self.nominal_scenario_key is not None and self.nominal_scenario_key not in keys:
            raise ValueError("nominal case must be present in this report")
        if self.completion == "complete" and any(item.execution_status == "not_run" for item in self.cases):
            raise ValueError("a complete report cannot contain unexecuted cases")
        return self

    @property
    def checks_passed(self) -> bool:
        return (self.completion == "complete" and not self.coverage_gaps and not self.cross_case_diagnostics
            and all(item.execution_status == "completed" and item.expectation_check == "pass"
                and not any(value.phase == "storage" for value in item.diagnostics) for item in self.cases))


@dataclass(frozen=True, slots=True)
class PreviewCaseBatchOutcome:
    report: PreviewCaseReport
    operation_alias: str | None = None
    images: tuple[tuple[str, GeometryPreview], ...] = ()


def revise_preview_cases(original: tuple[PreviewCase, ...], changes: tuple[PreviewCaseChange, ...], *, operation_alias: str) -> tuple[PreviewCase, ...]:
    """Apply bounded domain edits; omission only inherits input, never a PASS."""
    items = {item.case_key: item.model_copy(update={"reuse": None}) for item in original}
    changed = set()
    for change in changes:
        key = change.case_key if isinstance(change, (PreviewCaseDelete, PreviewCaseReuseChange)) else change.case.case_key
        if key in changed:
            raise ValueError("a case key can be changed only once")
        changed.add(key)
        if isinstance(change, PreviewCaseAdd):
            if key in items:
                raise ValueError("added case already exists")
            items[key] = change.case
        else:
            if key not in items:
                raise ValueError("replaced, deleted or reused case does not exist")
            if isinstance(change, PreviewCaseDelete):
                del items[key]
            elif isinstance(change, PreviewCaseReuseChange):
                items[key] = items[key].model_copy(update={"reuse": PreviewCaseReuse(
                    operation_alias=operation_alias, case_key=key)})
            else:
                items[key] = change.case
    return InlinePreviewCases(items=tuple(items.values())).items
