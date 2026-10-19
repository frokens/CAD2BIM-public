"""Build private four-table reports for the existing IFC publication owner."""

from __future__ import annotations

from copy import deepcopy

import pyarrow as pa

from CAD2BIM_backend.domain.data_runtime import (
    ProductApplyReportPublication,
    IfcProductsApplyOperationDetail,
)
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity, PreparedIfcRevision
from CAD2BIM_backend.domain.product_apply import ProductApplySummary
from CAD2BIM_backend.ports.storage import StoragePort


def _schema(fields):
    return pa.schema([pa.field(name, kind, nullable=nullable) for name, kind, nullable in fields])


S, I, F = pa.string(), pa.int64(), pa.float64()
REPORT_SCHEMAS = {
    "products": _schema([
        ("input_row_index", I, False), ("product_key", S, True),
        ("solve_status", S, False), ("build_status", S, False), ("commit_status", S, False),
        ("root_global_id", S, True), ("normalized_parameters_json", S, True),
        ("solution_json", S, True), ("error_code", S, True), ("error_phase", S, True),
    ]),
    "components": _schema([
        ("product_key", S, False), ("component_key", S, False), ("parent_component_key", S, True),
        ("ifc_class", S, False), ("project_world_transform_json", S, False),
        ("build_status", S, False), ("commit_status", S, False), ("global_id", S, True),
    ]),
    "interfaces": _schema([
        ("product_key", S, False), ("interface_key", S, False), ("component_key", S, False),
        ("component_global_id", S, True),
        ("fragment_key", S, False), ("model_interface_key", S, False), ("usage", S, False),
        ("origin_x_m", F, False), ("origin_y_m", F, False), ("origin_z_m", F, False),
        ("axis_x", F, False), ("axis_y", F, False), ("axis_z", F, False),
        ("reference_direction_json", S, True), ("profile_json", S, True), ("commit_status", S, False),
    ]),
    "checks": _schema([
        ("check_index", I, False), ("input_row_index", I, True), ("product_key", S, True),
        ("component_key", S, True), ("fragment_key", S, True), ("interface_key", S, True),
        ("peer_interface_key", S, True), ("phase", S, False), ("check_kind", S, False),
        ("status", S, False), ("metric", S, True), ("residual", F, True),
        ("tolerance", F, True), ("unit", S, True), ("field_path", S, True),
        ("actual_json", S, True), ("expected_json", S, True), ("error_code", S, True),
        ("message", S, True), ("error_origin", S, True), ("recommended_action", S, True),
        ("resource_version_id", S, True), ("diagnostic_json", S, True),
    ]),
}
REPORT_KEYS = {"products": ("input_row_index",), "components": ("product_key", "component_key"),
               "interfaces": ("product_key", "interface_key"), "checks": ("check_index",)}


def empty_report_rows():
    return {name: [] for name in REPORT_SCHEMAS}


def add_check(rows, *, phase, check_kind, status, **values):
    allowed = set(REPORT_SCHEMAS["checks"].names) - {"check_index", "phase", "check_kind", "status"}
    if set(values) - allowed:
        raise ValueError("unknown report diagnostic field")
    rows["checks"].append({"check_index": len(rows["checks"]), "phase": phase,
                           "check_kind": check_kind, "status": status, **values})


def report_for_commit(rows, bindings, *, committed):
    result = deepcopy(rows)
    by_component = {(item.product_key, item.component_key): item.global_id for item in bindings}
    roots = {item.product_key: item.global_id for item in bindings if item.parent_component_key is None}
    state = "COMMITTED" if committed else "NOT_COMMITTED"
    for row in result["products"]:
        row["commit_status"] = state
        row["root_global_id"] = roots.get(row["product_key"]) if committed else None
    for row in result["components"]:
        row["commit_status"] = state
        row["global_id"] = by_component.get((row["product_key"], row["component_key"])) if committed else None
    for row in result["interfaces"]:
        row["commit_status"] = state
        row["component_global_id"] = by_component[(row["product_key"], row["component_key"])] if committed else None
    return result


async def stage_product_apply_report(
    storage: StoragePort, scope: IfcRunIdentity, prepared: PreparedIfcRevision,
    *, detail: IfcProductsApplyOperationDetail, summary: ProductApplySummary, rows,
) -> ProductApplyReportPublication:
    from CAD2BIM_backend.application.product_report import stage_product_report
    package, members, artifacts = await stage_product_report(storage, scope, prepared,
        summary=summary, rows=rows, schemas=REPORT_SCHEMAS, keys=REPORT_KEYS,
        name="Product Apply report", asset_kind="product_apply_report",
        package_summary="Complete strict-call diagnostics and committed product identities.",
        description=("# Product Apply report\n\n"
            "## Tables and associations\n\n"
            "products preserves each original input_row_index and its readable product_key, including rejected or unexecuted rows. "
            "components joins products by product_key; (product_key, component_key) identifies a component row. "
            "interfaces joins that component by the same two fields and is keyed by (product_key, interface_key). "
            "checks is keyed by check_index and uses input_row_index/product_key and applicable component_key/interface_key "
            "to locate the recorded diagnostic; call-level checks may have no input row. Inspect statuses and actual recorded "
            "residuals/tolerances rather than treating a missing check as PASS.\n\n"
            "## Interface observations and saved identity\n\n"
            "Interface origins are project-world metres; axes and reference directions are project-world unit vectors. "
            "profile_json records the numerical interface profile, with dimensions interpreted by their declared field units. "
            "component_global_id is populated only for COMMITTED observations from this Apply's actual compiler binding; "
            "NOT_COMMITTED observations have null component_global_id even if candidate numerical frames were observed. "
            "A named interface is not an IfcPort, a measured physical end face, or proof of a connection to another product. "
            "These immutable observations describe this invocation and do not update when the model is later edited.\n\n"
            "## Input, Family and revision sources\n\n"
            "The original Operation detail.input_binding identifies the exact input Package/member/payload; "
            "detail.family_version_ref identifies the one exact Family used for this Apply. Preserve the original "
            "Operation when navigating its inputs, report and result. The Operation fixes its base revision, and its successful "
            "committed revision/result metadata identifies the historical saved output. Neither that revision number nor "
            "a historical report selects a later CURRENT model. Consult the original Operation after interruption; "
            "an unreadable response or unfinished stage does not establish zero submission.\n"),
        manifest_fields={"family_version_id": detail.request.family_version_id,
            "input": detail.input_binding.model_dump(mode="json"),
            "ifc_revision_id": prepared.after_revision_id if summary.outcome == "committed" else None})
    return ProductApplyReportPublication(package=package, members=members, artifacts=artifacts, summary=summary)
