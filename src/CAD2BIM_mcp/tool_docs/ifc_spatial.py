"""Provider-visible usage text for the ifc spatial capability family."""

from CAD2BIM_mcp.tool_docs.common import runtime_budget_description as _runtime_budget_description
from CAD2BIM_backend.domain.runtime_execution import (
    IFC_REVISION_INVOCATION_EXECUTION as _IFC_REVISION_INVOCATION_EXECUTION,
    IFC_REVISION_EXECUTION as _IFC_REVISION_EXECUTION,
)

IFC_SPATIAL_PATCH_INPUT_DESCRIPTION = (
    "Explicit direct-attribute replacements; clearing remains a separate operation field."
)

IFC_PROJECT_WORLD_PLACEMENT_INPUT_ORIGIN_DESCRIPTION = (
    "Target origin in the current project's public project-world coordinates."
)

IFC_PROJECT_WORLD_PLACEMENT_INPUT_AXIS_DESCRIPTION = (
    "Unit local Z direction in project-world coordinates."
)

IFC_PROJECT_WORLD_PLACEMENT_INPUT_REF_DIRECTION_DESCRIPTION = (
    "Unit local X direction, orthogonal to axis, in project-world coordinates."
)

IFC_PROJECT_PARENT_INPUT_KIND_DESCRIPTION = (
    "Aggregate the new or existing root Site directly under the unique Project."
)

IFC_EXISTING_SPATIAL_PARENT_INPUT_KIND_DESCRIPTION = (
    "Reference an existing spatial object from ifc_spatial_structure_list."
)

IFC_EXISTING_SPATIAL_PARENT_INPUT_GLOBAL_ID_DESCRIPTION = (
    "Exact IfcRoot.GlobalId of the existing parent spatial object."
)

IFC_CLIENT_REF_SPATIAL_PARENT_INPUT_KIND_DESCRIPTION = (
    "Reference a spatial object created elsewhere in this same atomic request."
)

IFC_CLIENT_REF_SPATIAL_PARENT_INPUT_CLIENT_REF_DESCRIPTION = (
    "Request-local create client_ref; it expires when this call returns."
)

CREATE_IFC_SPATIAL_OPERATION_INPUT_CLIENT_REF_DESCRIPTION = (
    "Unique request-local identity used only to connect creates in this batch."
)

CREATE_IFC_SITE_OPERATION_INPUT_REF_LATITUDE_DESCRIPTION = (
    "Optional approximate site latitude in decimal degrees."
)

CREATE_IFC_SITE_OPERATION_INPUT_REF_LONGITUDE_DESCRIPTION = (
    "Optional approximate site longitude in decimal degrees."
)

CREATE_IFC_SITE_OPERATION_INPUT_REF_ELEVATION_DESCRIPTION = (
    "Optional site elevation expressed in the current project length unit."
)

CREATE_IFC_SPACE_OPERATION_INPUT_PREDEFINED_TYPE_DESCRIPTION = (
    "Optional IFC4X3 IfcSpace.PredefinedType."
)

CREATE_IFC_SPACE_OPERATION_INPUT_ELEVATION_WITH_FLOORING_DESCRIPTION = (
    "Optional IfcSpace.ElevationWithFlooring expressed in the current project length unit."
)

SET_IFC_SPATIAL_PLACEMENT_OPERATION_INPUT_PRESERVE_DESCENDANT_PROJECT_WORLD_DESCRIPTION = (
    "False moves the full semantic subtree rigidly; true moves only the target and "
    "recomputes descendant relative placements to preserve their project-world poses."
)

IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_SPATIAL_CLASSES_DESCRIPTION = (
    "Optional class OR-filter; omit or use an empty list to inspect all classes."
)

IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_GLOBAL_IDS_DESCRIPTION = (
    "Optional exact GlobalId OR-filter; combine with other filters using AND."
)

IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_PARENT_GLOBAL_ID_DESCRIPTION = (
    "Optional exact direct-parent Project or spatial GlobalId filter."
)

IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_PAGE_SIZE_DESCRIPTION = (
    "Maximum spatial nodes in this page; default 64 and maximum 256."
)

DIRECT_IFC_SPATIAL_LIST_FIRST_PAGE_INPUT_SPATIAL_CLASSES_DESCRIPTION = (
    "Optional class OR-filter; omit to inspect all four spatial classes."
)

DIRECT_IFC_SPATIAL_LIST_FIRST_PAGE_INPUT_GLOBAL_IDS_DESCRIPTION = (
    "Optional exact GlobalId OR-filter; combines with other filters using AND."
)

DIRECT_IFC_SPATIAL_LIST_FIRST_PAGE_INPUT_PAGE_SIZE_DESCRIPTION = (
    "Maximum rows in the first page; default 64 and maximum 256."
)

DIRECT_IFC_SPATIAL_LIST_CONTINUATION_INPUT_CURSOR_DESCRIPTION = (
    "Opaque next_cursor from this tool; submit it unchanged and omit query."
)

IFC_SPATIAL_STRUCTURE_APPLY_DESCRIPTION = (
    "Apply one atomic, fully prevalidated spatial-structure write to the current durable "
    "IFC. The operations array supports exactly five explicit actions: create, update, "
    "reparent_preserve_world, set_project_world_placement, and delete_if_empty. It covers "
    "IfcSite, IfcBuilding, IfcBuildingStorey, and IfcSpace. All public positions and "
    "directions are project-world facts; the Backend computes and validates IFC relative "
    "IfcLocalPlacement chains. Placement tuples contain JSON numbers, not quoted numeric "
    'strings; for example project_world_placement={"origin":[0,0,0],'
    '"axis":[0,0,1],"ref_direction":[1,0,0]}. Use '
    "ifc_spatial_structure_list first to obtain the formal "
    "GlobalId of every existing spatial object. Within this one request, create operations "
    "use unique client_ref values and parent kind=client_ref may point forward or backward "
    "to any create in the same array; client_ref expires when the call returns and is never "
    "a durable alias. Existing parents use kind=existing plus GlobalId, while only a root "
    "Site may use kind=project. The whole request is validated before private-copy mutation: "
    "one invalid class, parent, reference, cycle, placement, conflicting operation, or "
    "delete blocker makes zero IFC modification. Reparenting preserves the target and full "
    "subtree project-world placement. Placement changes move the semantic subtree rigidly "
    "by default; set preserve_descendant_project_world=true to compensate descendants. "
    "delete_if_empty permanently removes only an empty target and has no force, cascade, "
    "hidden product migration, or ignored-reference mode. "
    "Create and update expose only direct spatial business attributes: common labels and "
    "CompositionType, optional decimal-degree approximate Site latitude/longitude, Site "
    "elevation in the current project length unit, and optional Space PredefinedType and "
    "elevation-with-flooring in that same length unit. They do not edit products, "
    "representations, PropertySets, quantities, systems, or containment. "
    "A blocked delete returns bounded relationship counts and sample GlobalIds. "
    "Successful non-empty calls create one "
    "critical Operation and one immutable IFC update, then return operation_alias, bounded "
    "applied receipts, and exact-reread affected spatial facts. The tool never accepts "
    "or returns model IDs, revisions, ArtifactVersion IDs, writer identities, relative "
    "placement entities, or STEP IDs. Submit this WRITE alone; if it depends on a read, wait "
    "for the returned read result before issuing it. Deterministic preflight failures after "
    "Operation preparation return a safe operation_alias and make zero IFC change. On "
    "IFC_CURRENT_CONFLICT, read the spatial hierarchy again before deciding a new write. "
    "On SYSTEM_PAUSED, stop writing and wait for backend recovery."
)

IFC_SPATIAL_STRUCTURE_LIST_DESCRIPTION = (
    "Inspect the current durable IFC spatial hierarchy without creating an Operation. "
    "When you know nothing about the model, submit only direct_session_alias. The first "
    "page may filter "
    "by spatial_classes, exact global_ids, and one direct parent_global_id; values within "
    "a list are OR alternatives and different filters combine with AND. Sorting is fixed "
    "by IFC class plus GlobalId. Each item returns formal GlobalId, class, direct business "
    "attributes, parent GlobalId/class, computed project-world placement, and bounded "
    "relationship/blocker counts. It does not return large related-product lists. page_size "
    "defaults to 64 and is capped at 256. Continue only by submitting the returned opaque "
    "cursor unchanged with the same Direct session and omitting all first-page filters; "
    "the cursor keeps all "
    "pages on the exact immutable IFC snapshot fixed by the first call even if CURRENT "
    "later advances. Cursors do not accept caller-edited filters, page numbers, OFFSET, "
    "custom sort, or revision selectors. If a cursor is invalid or expired, restart from "
    "the first page using the original first-page filters. Results never expose the current revision number, "
    "IfcModel or Artifact identities, writer state, relative placement internals, or STEP "
    "IDs. Multiple independent READ calls may be submitted together; do not include a WRITE "
    "in the same batch."
)

IFC_SPATIAL_STRUCTURE_APPLY_OPERATIONS_DESCRIPTION = (
    "One to 256 closed spatial actions committed as a single batch."
)

IFC_SPATIAL_STRUCTURE_LIST_SPATIAL_CLASSES_DESCRIPTION = (
    "Optional first-page spatial class filters. Omit every filter to discover all."
)

IFC_SPATIAL_STRUCTURE_LIST_PAGE_SIZE_DESCRIPTION = "First-page size; default 64, maximum 256."

IFC_SPATIAL_STRUCTURE_LIST_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by this tool; submit it unchanged with the same "
    "Direct session and omit every first-page filter."
)


IFC_SPATIAL_STRUCTURE_APPLY_DESCRIPTION += _runtime_budget_description(_IFC_REVISION_INVOCATION_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
IFC_SPATIAL_STRUCTURE_LIST_DESCRIPTION += _runtime_budget_description(_IFC_REVISION_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
