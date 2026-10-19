"""Public Preview facts, shared before Direct/HTTP pagination and serialization."""
import json


def external_target_comparison(realization):
    checks = realization.external_condition_checks
    return ("NOT_COMPARED" if not checks else "PASS" if all(item.evaluation.satisfied for item in checks) else "FAIL")


def project_case_outcome(result):
    projected = result.model_dump(mode="json", exclude={
        "case": True, "inspection_artifact": True, "geometry_sha256": True,
        "realization": {"binding_digest": True, "source_bundle_sha256": True,
                        "instance_receipts": {"__all__": {"geometry_sha256"}}},
    })
    if result.realization is not None:
        projected["realization"]["external_target_comparison"] = external_target_comparison(result.realization)
    return projected


def project_case_outcome_json(result):
    return json.dumps(project_case_outcome(result), ensure_ascii=False, separators=(",", ":"))


def public_response_bytes(value):
    # Match the reference middleware's final TextContent JSON framing/escaping.
    return len(json.dumps(value.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"))


def preview_response_budget_error(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("detail"), dict):
        return None
    if payload["detail"].get("detail_kind") not in {"preview_case_report", "preview_report_text"}:
        return None
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 64 * 1024:
        return None
    operation = payload["operation"]
    return ("OPERATION_GET_RESPONSE_TOO_LARGE; phase=output_validation; retryable=false; "
            f"operation_status={operation['outcome']}; operation_ref={operation['operation_ref']}; "
            "message=The original Preview public navigation exceeds 64 KiB. Preserve this Operation; "
            "read preview_report with an exact content_kind/case_key and start_character.")
