"""Evaluate existing realized facts without running author code again."""

from math import isclose

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometryFormat
from CAD2BIM_backend.domain.preview_cases import FamilyPreviewCase, PreviewCaseDiagnostic
from CAD2BIM_backend.domain.product_realization import ProductRealizationStatus


def evaluate_family_case(case, result):
    diagnostics = []
    if result.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED:
        diagnostics.append(PreviewCaseDiagnostic(code="PROPOSAL_CONTRACT_REJECTED", phase="output_validation",
            message=result.proposal.rejection.message))
    if result.proposal.status is ProductRealizationStatus.SOLVED and result.frame_check_status == "FAIL":
        targets = {item.interface_key: item.condition for item in case.invocation.external_constraints}
        facts = {item.interface_key: item for item in result.interface_project_world_frames}
        for check in result.external_condition_checks:
            evaluation = check.evaluation
            if evaluation.satisfied:
                continue
            condition = evaluation.condition
            failures = []
            for name, residual, tolerance in (
                ("position", evaluation.position_residual_m, condition.position_tolerance_m),
                ("segment_extent", evaluation.line_extent_residual_m, condition.position_tolerance_m),
                ("direction", evaluation.direction_residual_rad, condition.angular_tolerance_rad),
                ("roll", evaluation.roll_residual_rad, condition.angular_tolerance_rad),
            ):
                if residual is not None and residual > tolerance:
                    failures.append(f"{name}: residual={residual:g}, tolerance={tolerance:g}")
            actual = facts.get(check.interface_key)
            if evaluation.profile_satisfied is False:
                actual_profile = None if actual is None or actual.profile is None else actual.profile.model_dump()
                failures.append(f"profile: requested={condition.profile.profile.model_dump()}, actual={actual_profile}")
            binding = ("" if actual is None else
                f"; binding={actual.source.fragment_key}/{actual.source.model_interface_key}")
            target = targets[check.interface_key]
            diagnostics.append(PreviewCaseDiagnostic(code="EXTERNAL_TARGET_FAILED", phase="output_validation",
                message=f"External target {target.kind} on interface={check.interface_key}{binding}; " + "; ".join(failures)))
        if any(not item.evaluation.satisfied for item in result.frame_diagnostics) or not diagnostics:
            diagnostics.append(PreviewCaseDiagnostic(code="FRAME_CHECK_FAILED", phase="output_validation",
                message="The actual solved result failed its independent frame checks; see frame_diagnostics for the bound interfaces and residuals."))
    if case.expectation is None:
        return "not_checked", tuple(diagnostics)
    expected = case.expectation
    if result.proposal.status is not expected.expected_status:
        diagnostics.append(PreviewCaseDiagnostic(code="EXPECTED_STATUS_MISMATCH", phase="output_validation",
            message=f"Expected {expected.expected_status.value}; actual {result.proposal.status.value}."))
    if result.proposal.status is not ProductRealizationStatus.SOLVED:
        reasons = ({result.proposal.rejection.violation_key}
            if result.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED else
            {item.violation_key for item in result.proposal.violations}
            if result.proposal.status is ProductRealizationStatus.INFEASIBLE
            else {item.ambiguity_key for item in result.proposal.ambiguities})
        if not set(expected.reason_keys).issubset(reasons):
            diagnostics.append(PreviewCaseDiagnostic(code="EXPECTED_REASON_MISMATCH", phase="output_validation",
                message="The actual business reasons do not include every expected stable reason key."))
    return ("fail" if diagnostics else "pass"), tuple(diagnostics)


def case_check_coverage(cases):
    if any(case.expectation is None for case in cases):
        return ("Some Preview observations were not compared with explicit expectations.",)
    return ()


def _same_construction_value(left, right):
    """Compare construction data with the existing interface numeric tolerance.

    Names, counts and structure remain exact. This comparison accepts
    computational float noise; it does not replace installation constraints.
    """
    if (type(left) in (int, float) and type(right) in (int, float)
            and (isinstance(left, float) or isinstance(right, float))):
        return isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_construction_value(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _same_construction_value(a, b) for a, b in zip(left, right)
        )
    return type(left) is type(right) and left == right


def compare_standard_constructions(results):
    """A changed origin may change only the backend-owned root transform.

    Compare business facts, not BREP serialization: independent equivalent OCCT
    builds are permitted to have different byte layouts.
    """
    prior_results, measurements, diagnostics = {}, {}, []
    for item in results:
        case, result = item.case, item.realization
        if (not isinstance(case, FamilyPreviewCase) or case.invocation.realization_kind != "standard_model"
                or result is None or result.proposal.status is not ProductRealizationStatus.SOLVED):
            continue
        key = canonical_json(result.normalized_public_parameters)
        construction = (tuple(item.model_dump() for item in result.proposal.components),
            tuple(item.model_dump() for item in result.proposal.fragments),
            tuple(receipt.model_dump(exclude={"geometry_sha256", "geometry_summary"})
                  for receipt in result.instance_receipts))
        conflict = None
        by_fragment = measurements.setdefault(key, {})
        for receipt in result.instance_receipts:
            if receipt.geometry_summary is not None:
                summary = receipt.geometry_summary
                measured = summary.model_dump(exclude={"topology": {"triangle_count"}}
                    if summary.format is CanonicalGeometryFormat.OCCT_BREP_V1 else None)
                prior, prior_key = by_fragment.setdefault(receipt.fragment_key,
                    (measured, case.case_key))
                if not _same_construction_value(prior, measured):
                    conflict = prior_key
        if key in prior_results and not _same_construction_value(prior_results[key][0], construction):
            conflict = prior_results[key][1]
        if conflict is not None:
            diagnostics.append(PreviewCaseDiagnostic(code="STANDARD_MODEL_ORIGIN_DEPENDENCE",
                phase="output_validation", message=f"Standard construction changed between cases {conflict} and {case.case_key} at equal parameters."))
        prior_results[key] = construction, case.case_key
    return tuple(diagnostics)
