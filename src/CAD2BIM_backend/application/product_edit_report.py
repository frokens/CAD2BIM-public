"""Private three-table Edit reports, registered only by the existing IFC owner."""
from copy import deepcopy
import pyarrow as pa

from CAD2BIM_backend.application.product_apply_report import REPORT_SCHEMAS as APPLY_SCHEMAS
from CAD2BIM_backend.application.product_report import stage_product_report
from CAD2BIM_backend.domain.data_runtime import ProductEditReportPublication

S, I, B = pa.string(), pa.int64(), pa.bool_()


def schema(fields):
    return pa.schema([pa.field(name, kind, nullable=nullable) for name, kind, nullable in fields])


EDIT_REPORT_SCHEMAS = {
    "products": schema([("input_row_index", I, False),
        *((name, S, True) for name in ("product_key", "target_global_id", "action", "family_version_ref")),
        *((name, S, False) for name in ("precheck_status", "solve_status", "execution_status", "commit_status")),
        *((name, S, True) for name in ("result_global_id", "before_ifc_class", "after_ifc_class",
                                      "normalized_parameters_json", "solution_json"))]),
    "changes": schema([("change_index", I, False), ("input_row_index", I, False),
        *((name, S, False) for name in ("change_kind", "global_id", "ifc_class")),
        *((name, S, True) for name in ("component_key", "parent_component_key", "related_global_id",
            "relation_class", "relation_global_id", "relation_role", "relation_name")),
        ("relation_removed", B, True), ("before_placement_json", S, True), ("after_placement_json", S, True)]),
    "checks": pa.schema([*APPLY_SCHEMAS["checks"], pa.field("target_global_id", S), pa.field("related_global_id", S)]),
    "interfaces": pa.schema([pa.field("input_row_index", I, False), *APPLY_SCHEMAS["interfaces"]]),
}
EDIT_REPORT_KEYS = {"products": ("input_row_index",), "changes": ("change_index",),
                    "checks": ("check_index",), "interfaces": ("input_row_index", "interface_key")}


def empty_edit_report():
    return {name: [] for name in EDIT_REPORT_SCHEMAS}


def edit_check(rows, *, phase, check_kind, status, **values):
    allowed = set(EDIT_REPORT_SCHEMAS["checks"].names) - {"check_index", "phase", "check_kind", "status"}
    if set(values) - allowed:
        raise ValueError("unknown Edit check field")
    rows["checks"].append({"check_index": len(rows["checks"]), "phase": phase,
                           "check_kind": check_kind, "status": status, **values})


def terminal_edit_rows(rows, *, committed, bindings=()):
    result = deepcopy(rows)
    by_component = {(item.input_row_index, item.product_key, item.component_key): item.global_id for item in bindings}
    if not committed:
        result["changes"] = []
    for product in result["products"]:
        product["commit_status"] = "COMMITTED" if committed else "NOT_COMMITTED"
        if not committed:
            product.update(result_global_id=None, after_ifc_class=None)
    for interface in result["interfaces"]:
        interface["commit_status"] = "COMMITTED" if committed else "NOT_COMMITTED"
        interface["component_global_id"] = by_component[(interface["input_row_index"], interface["product_key"],
            interface["component_key"])] if committed else None
    if not result["interfaces"]:
        result.pop("interfaces")
    return result


async def stage_product_edit_report(storage, scope, prepared, *, detail, summary, rows, committed):
    schemas = {name: EDIT_REPORT_SCHEMAS[name] for name in rows}
    package, members, artifacts = await stage_product_report(storage, scope, prepared,
        summary=summary, rows=rows, schemas=schemas, keys=EDIT_REPORT_KEYS,
        name="Product Edit report", asset_kind="product_edit_report",
        package_summary="Complete input-row edit coverage, actual committed changes and recorded checks.",
        description=("# Product Edit report\n\n"
            "## Tables and associations\n\n"
            "products has one row per original input_row_index, retaining readable product_key, target_global_id and action, "
            "including unexecuted/unknown stages. changes is keyed by change_index and joins products by input_row_index; "
            "it contains only confirmed committed identity/relation effects and is empty for an uncommitted result. "
            "checks is keyed by check_index and uses original input_row_index plus applicable target/component/interface "
            "identifiers; call-level checks may have no row. interfaces, when present, is keyed by "
            "(input_row_index, interface_key) and joins the original product row by input_row_index/product_key. "
            "Its component_key locates the new Family component within that replacement; changes supplies actual "
            "component_key/parent_component_key identity effects. Old and new children do not share identity merely "
            "because their component_key is the same.\n\n"
            "## Interface observations and saved identity\n\n"
            "interfaces contains only numerical observations actually obtained for replace; placement/delete do not "
            "create new interface observations. Origins are project-world metres; axes and reference directions are "
            "project-world unit vectors, and profile_json records the numerical section with its declared field units. "
            "component_global_id comes from this Edit effect's binding for the original input_row_index/product_key/component_key "
            "only when COMMITTED; it is null when NOT_COMMITTED. A retained replacement root keeps its GUID, whereas "
            "new children use their actual new GUIDs. These are named interfaces, not IfcPort entities, physical end-face "
            "measurements or proof of external connections. This immutable report does not update after later edits.\n\n"
            "## Input, Family and revision sources\n\n"
            "The original Operation detail.input_binding identifies the exact input Package/member/payload. For each replace, "
            "the original physical input row supplies its exact family_version_ref and complete Family parameters/conditions; "
            "products retains the row and reference for navigation. placement/delete have no replacement Family. "
            "The original Operation fixes the base revision; successful committed output metadata describes that historical "
            "revision, not a new CURRENT precondition. Use Data describe/query and the original Operation to navigate "
            "inputs/results. An interruption or unreadable report alone does not establish whether a write committed.\n"),
        manifest_fields={"input": detail.input_binding.model_dump(mode="json"),
            "base_ifc_revision_id": prepared.before.revision_id,
            "ifc_revision_id": prepared.after_revision_id if committed else None,
            "committed": committed})
    return ProductEditReportPublication(package=package, members=members, artifacts=artifacts,
                                        summary=summary, committed=committed)
