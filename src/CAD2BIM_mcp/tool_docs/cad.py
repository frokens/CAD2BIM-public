"""Provider-visible usage text for the cad capability family."""

from CAD2BIM_mcp.tool_docs.common import runtime_budget_description as _runtime_budget_description
from CAD2BIM_backend.domain.runtime_execution import (
    CAD_ANALYSIS_EXECUTION as _CAD_ANALYSIS_EXECUTION,
    CAD_MEASUREMENT_EXECUTION as _CAD_MEASUREMENT_EXECUTION,
)

DIRECT_CAD_POINT_SCOPE_SCOPE_KEY_DESCRIPTION = (
    "Unique request-local POINT group key retained in measurements; it is not a product "
    "category, Package alias or server profile."
)

DIRECT_CAD_POINT_SCOPE_POINT_LAYERS_DESCRIPTION = (
    "One to 100 unique POINT Layer names discovered with cad_query_entities. Within this "
    "scope, the listed Layer names select a union; this is not an AND filter."
)

DIRECT_CAD_POINT_SCOPE_SEARCH_RADIUS_M_DESCRIPTION = (
    "Positive finite 3D search radius in metres for only this POINT scope, supplied from "
    "a Requirement, user approval, CAD fact or other verifiable source."
)

DIRECT_CAD_POINT_GROUP_SCOPE_KEY_DESCRIPTION = (
    "Unique request-local analyzer POINT group key retained in results; it is not a "
    "product category, Package alias or server profile."
)

DIRECT_CAD_POINT_GROUP_POINT_LAYERS_DESCRIPTION = (
    "One to 100 unique POINT Layer names discovered with cad_query_entities. The listed "
    "Layer names select a union; analyzer radii belong in each trial, not this group."
)

DIRECT_CAD_POINT_SEARCH_RADIUS_SCOPE_KEY_DESCRIPTION = (
    "Exact scope_key of one top-level point_groups item in this analyzer call."
)

DIRECT_CAD_POINT_SEARCH_RADIUS_SEARCH_RADIUS_M_DESCRIPTION = (
    "Positive finite 3D search radius in metres for this trial and POINT group, supplied "
    "from a Requirement, user approval, CAD fact or other verifiable source."
)

DIRECT_CAD_TOLERANCE_SPEC_ENDPOINT_QUANTIZATION_M_DESCRIPTION = (
    "Positive finite endpoint quantization cell size in metres; it creates deterministic "
    "round-ties-to-even keys and is not an endpoint merge approval."
)

DIRECT_CAD_TOLERANCE_SPEC_ENDPOINT_SNAP_CANDIDATE_M_DESCRIPTION = (
    "Positive finite 3D endpoint-pair candidate distance in metres."
)

DIRECT_CAD_TOLERANCE_SPEC_ENDPOINT_ON_PATH_CANDIDATE_M_DESCRIPTION = (
    "Positive finite 3D endpoint-to-other-path candidate distance in metres."
)

DIRECT_CAD_TOLERANCE_SPEC_HOST_ENDPOINT_GUARD_M_DESCRIPTION = (
    "Finite non-negative distance in metres used to report whether a host projection is "
    "inside the endpoint guard; it does not delete the candidate."
)

DIRECT_CAD_TOLERANCE_SPEC_POINT_TARGET_UNIQUENESS_MARGIN_M_DESCRIPTION = (
    "Finite non-negative metre gap used to report POINT ambiguity between genuinely "
    "different geometric targets."
)

DIRECT_CAD_TOLERANCE_SPEC_STRAIGHT_TURN_TOLERANCE_DEG_DESCRIPTION = (
    "Finite 0..180 degree threshold for reporting a curve turn as straight-like."
)

DIRECT_CAD_TOLERANCE_TRIAL_TRIAL_KEY_DESCRIPTION = (
    "Unique request-local key used to identify this complete explicit trial."
)

DIRECT_CAD_TOLERANCE_TRIAL_TOLERANCE_SPEC_DESCRIPTION = (
    "All six explicit tolerance values for this trial; no server profile is used."
)

DIRECT_CAD_TOLERANCE_TRIAL_POINT_SEARCH_RADII_DESCRIPTION = (
    "Required explicit radii for this trial. Its unique scope_key set must exactly match "
    "the analyzer call's top-level point_groups; use an empty array only when point_groups "
    "is empty."
)

DIRECT_CAD_MEASUREMENT_ROOT_PACKAGE_ALIAS_DESCRIPTION = (
    "Exact visible source_l0 Package alias selected from data_package_list; a derived "
    "Package is invalid, and each additional CAD drawing requires a separate call."
)

DIRECT_CAD_MEASUREMENT_ROOT_SOURCE_UNIT_TO_M_DESCRIPTION = (
    "Required positive finite multiplier from source drawing coordinates to metres, from "
    "a Requirement, user approval, CAD fact or other verifiable source; never inferred."
)

DIRECT_CAD_MEASUREMENT_ROOT_PATH_LAYERS_DESCRIPTION = (
    "One to 100 unique host-path Layer names discovered with cad_query_entities. The list "
    "selects their union and carries no business classification or implicit AND filter."
)

DIRECT_CAD_ANALYZE_TOLERANCE_CANDIDATES_INPUT_POINT_GROUPS_DESCRIPTION = (
    "Zero to 16 independent POINT Layer groups with no radius. Every explicit trial must "
    "supply one point_search_radii entry for each unique scope_key."
)

DIRECT_CAD_ANALYZE_TOLERANCE_CANDIDATES_INPUT_TRIALS_DESCRIPTION = (
    "Zero to eight complete, uniquely keyed explicit trials. Empty means derive bounded "
    "one-dimensional probes from observed geometry, not use hidden default tolerances."
)

DIRECT_CAD_COMPUTE_MEASUREMENTS_INPUT_POINT_SCOPES_DESCRIPTION = (
    "Zero to 16 independent POINT groups. Each scope has its own explicit Layer union and "
    "metre search radius; duplicate scope_key values are invalid."
)

DIRECT_CAD_COMPUTE_MEASUREMENTS_INPUT_TOLERANCE_SPEC_DESCRIPTION = (
    "Exactly one complete set of six explicit tolerance values; values are never resolved "
    "through a server alias or hidden profile."
)

DIRECT_CAD_COMPUTE_MEASUREMENTS_INPUT_OUTPUT_PACKAGE_DESCRIPTION = (
    "Name and exact description.md for the immutable derived measurement Package. Output "
    "Payload declarations are server-fixed and are not an input."
)

CAD_QUERY_ENTITIES_DESCRIPTION = (
    "Discover CAD entity facts in one source_l0 Package. Passing a derived Package fails "
    "with PACKAGE_KIND_MISMATCH; inspect derived data with data_describe_package and "
    "data_query_rows. rows mode lists compact entities and "
    "entity_key values; count_by groups by layer, entity_type, or layer_and_entity_type. "
    "Start unfiltered count_by queries to discover legal layer/type values, then place "
    "them under query.filters.layers/entity_types. Different filter fields are AND; "
    "values inside one array are OR. The default scope is top_level_modelspace. First "
    "call uses query only; continuation uses the returned cursor only and unchanged. "
    "Query and cursor are mutually exclusive. Default page_size is 100, maximum 200; "
    "no OR tree, regex, SQL, arbitrary expression, custom sort or OFFSET. Hard timeout "
    "is 15 seconds with a 2,000,000-row/256-MiB scan budget; limits fail with "
    "QUERY_TIMEOUT or QUERY_BUDGET_EXCEEDED. Counts are L0 CAD facts, not product "
    "classification, deduplication, or IFC decisions. Rows example: "
    '{"package_alias":"data_001","mode":"rows","filters":{'
    '"layers":["LAYER_FROM_COUNT_BY"]}}. Count example: {"package_alias":'
    '"data_001","mode":"count_by","count_by":"layer"}.'
)

CAD_GET_ENTITY_DESCRIPTION = (
    "Read one entity from a source_l0 Package selected by entity_key from "
    "cad_query_entities. Passing a derived Package fails with PACKAGE_KIND_MISMATCH; "
    "inspect derived data with data_describe_package and data_query_rows. Read it together with "
    "exactly one detail section: geometry_elements, xdata_tags, relationships, or "
    "raw_tags. Request other sections in separate calls. First call uses query only; "
    "continuation uses the returned cursor only and unchanged. Query and cursor are "
    "mutually exclusive. Default page_size is 100, maximum 200; ordering is fixed by "
    "the section logical key, with no page number, OFFSET, custom sort, or cross-section "
    "cursor reuse. Hard timeout is 15 seconds with a 2,000,000-row/256-MiB scan budget. "
    "The result exposes L0 source facts and does not make product or IFC decisions."
)

CAD_QUERY_XDATA_DESCRIPTION = (
    "Explore XDATA joined to owner CAD entities in one source_l0 Package. Passing a "
    "derived Package fails with PACKAGE_KIND_MISMATCH; inspect derived data with "
    "data_describe_package and data_query_rows. rows mode returns raw typed tags plus "
    "deterministic assignment_key/value convenience projections. These projection fields "
    "exist only in this tool response and are not stored in the immutable xdata_tags "
    "Payload. To reproduce them in a Package transform, group rows by entity_key, appid, "
    "and appid_group_ordinal: a non-marker group-code 1000 string key=value projects that "
    "key and string value; when the group has exactly one non-marker and no keyed value, "
    "the AppID is the key and its raw typed value is the value; otherwise the projection "
    "is null. profile mode discovers "
    "AppIDs, group codes, or assignment keys and reports bounded completeness/value "
    "statistics. Restrict owners only under query.entity_filters; in rows mode restrict "
    "XDATA under query.xdata_filters. Discover layer/type/entity keys with "
    "cad_query_entities and AppID/group-code/assignment-key values with an unfiltered "
    "profile before filtering. Different filter fields are AND; values inside one array "
    "are OR. First call uses query only; continuation uses the returned cursor only and "
    "unchanged. Query and cursor are mutually exclusive. Default page_size is 100, "
    "maximum 200; no OR tree, regex, SQL, arbitrary expression, custom sort or OFFSET. "
    "Hard timeout is 15 seconds with a 2,000,000-row/256-MiB scan budget; limits fail "
    "with QUERY_TIMEOUT or QUERY_BUDGET_EXCEEDED. Results preserve raw XDATA and do "
    'not assign business meaning or IFC classes. Rows example: {"package_alias":"data_001",'
    '"mode":"rows","entity_filters":{"entity_keys":["KEY_FROM_ENTITIES"]},'
    '"xdata_filters":{}}. Profile example: {"package_alias":"data_001",'
    '"mode":"profile","profile_dimension":"assignment_key",'
    '"entity_filters":{"layers":["LAYER_FROM_COUNT_BY"]}}.'
)

CAD_ANALYZE_TOLERANCE_CANDIDATES_PACKAGE_ALIAS_DESCRIPTION = (
    "Exact visible source_l0 Package alias selected from "
    "data_package_list; derived Packages are invalid."
)

CAD_ANALYZE_TOLERANCE_CANDIDATES_SOURCE_UNIT_TO_M_DESCRIPTION = (
    "Required positive finite source-coordinate to metre multiplier obtained "
    "from a Requirement, user approval, CAD fact or another verifiable source; "
    "the server never infers it."
)

CAD_ANALYZE_TOLERANCE_CANDIDATES_PATH_LAYERS_DESCRIPTION = (
    "Unique host-path Layer names discovered with cad_query_entities; the list "
    "selects their union and is not an AND filter."
)

CAD_ANALYZE_TOLERANCE_CANDIDATES_POINT_GROUPS_DESCRIPTION = (
    "Independent analyzer POINT groups; each group's point_layers select a "
    "union, and search radii are supplied separately by every explicit trial."
)

CAD_ANALYZE_TOLERANCE_CANDIDATES_TRIALS_DESCRIPTION = (
    "Zero to eight complete explicit trials, each with radii for exactly the "
    "point_groups keys; zero requests observed-geometry probes, not a hidden "
    "server default."
)

CAD_COMPUTE_MEASUREMENTS_TOLERANCE_SPEC_DESCRIPTION = (
    "One complete explicit six-value tolerance specification obtained from "
    "Requirements, user approvals, CAD facts or other verifiable sources; no "
    "server-side tolerance alias or default exists."
)

CAD_COMPUTE_MEASUREMENTS_OUTPUT_PACKAGE_DESCRIPTION = (
    "Name and exact description.md for the derived Package. The server always "
    "publishes the fixed ten Parquet Payloads; there is no outputs input."
)

CAD_COMPUTE_MEASUREMENTS_POINT_SCOPES_DESCRIPTION = (
    "Independent POINT Layer groups with explicit per-group radii."
)

CAD_QUERY_ENTITIES_QUERY_DESCRIPTION = (
    "Complete first-page entity rows or count_by query. Supply this field only "
    "on the first call; it is mutually exclusive with cursor."
)

CAD_QUERY_ENTITIES_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by this tool. Submit it unchanged with the "
    "same Direct session and omit query; OFFSET/page numbers are unsupported."
)

CAD_GET_ENTITY_DIRECT_SESSION_ALIAS_DESCRIPTION = "Direct session alias."

CAD_GET_ENTITY_QUERY_DESCRIPTION = "Complete first-page single-entity query; exclusive with cursor."

CAD_GET_ENTITY_CURSOR_DESCRIPTION = "Opaque next_cursor from this same tool; omit query when used."

CAD_QUERY_XDATA_QUERY_DESCRIPTION = (
    "Complete first-page XDATA rows or profile query. Supply this field only "
    "on the first call; it is mutually exclusive with cursor."
)

EQ_FILTER_FIELD_DESCRIPTION = "Payload field name returned by data_describe_package."

EQ_FILTER_OP_DESCRIPTION = "Equality comparison."

EQ_FILTER_VALUE_DESCRIPTION = "One scalar whose JSON type must match the described payload field."

IN_FILTER_OP_DESCRIPTION = "Match any value in one homogeneous array."

IN_FILTER_VALUE_DESCRIPTION = (
    "One to 100 values of one JSON type. Values are OR alternatives inside this "
    "condition; separate filter objects are AND conditions."
)

ORDERED_FILTER_OP_DESCRIPTION = (
    "Ordered comparison; boolean and binary payload fields are not orderable."
)

NULL_FILTER_OP_DESCRIPTION = "Null test. This filter shape has no value field."

DATA_ROWS_TOOL_QUERY_PACKAGE_ALIAS_DESCRIPTION = (
    "source_l0 or derived Package alias returned by data_package_list. "
    "source_l0 is produced by backend processing of a user upload; discover it in data_package_list."
)

DATA_ROWS_TOOL_QUERY_PAYLOAD_NAME_DESCRIPTION = (
    "Parquet payload name returned by data_describe_package."
)

DATA_ROWS_TOOL_QUERY_SELECT_DESCRIPTION = (
    "Required list of zero to 64 unique fields from the payload schema. An empty "
    "array returns only the payload logical-key fields. Selected list and object "
    "fields return their complete nested values; binary values use binary_base64 objects."
)

DATA_ROWS_TOOL_QUERY_FILTERS_DESCRIPTION = (
    "Zero to 32 filter objects; all filter objects are combined with AND."
)

DATA_ROWS_TOOL_QUERY_PAGE_SIZE_DESCRIPTION = (
    "Rows to return on the first page; default 100 and maximum 200."
)

ENTITY_TOOL_QUERY_BASE_PACKAGE_ALIAS_DESCRIPTION = (
    "source_l0 Package alias discovered with data_package_list after user upload and backend processing. "
    "A derived Package is invalid here; inspect it with data_describe_package and "
    "data_query_rows instead."
)

ENTITY_TOOL_QUERY_BASE_ENTITY_SCOPE_DESCRIPTION = (
    "Entity population to inspect. top_level_modelspace is the default; "
    "all_entities also includes child and structural records."
)

ENTITY_TOOL_QUERY_BASE_FILTERS_DESCRIPTION = (
    "Optional entity filters. Different fields are AND conditions; values inside "
    "one array are OR alternatives."
)

ENTITY_TOOL_QUERY_BASE_PAGE_SIZE_DESCRIPTION = (
    "Entity rows or count buckets per page; default 100 and maximum 200."
)

ENTITY_ROWS_TOOL_QUERY_MODE_DESCRIPTION = (
    "Return compact entity rows and entity_key values for deeper reads."
)

COUNT_BY_LAYER_TOOL_QUERY_MODE_DESCRIPTION = "Return deterministic count buckets."

COUNT_BY_LAYER_TOOL_QUERY_COUNT_BY_DESCRIPTION = "Group matching entities by CAD layer."

COUNT_BY_LAYER_TOOL_QUERY_INCLUDE_DECLARED_EMPTY_LAYERS_DESCRIPTION = (
    "Include layers declared in the DXF layer table even when their count is zero."
)

COUNT_BY_ENTITY_TYPE_TOOL_QUERY_COUNT_BY_DESCRIPTION = "Group matching entities by DXF entity type."

COUNT_BY_LAYER_AND_TYPE_TOOL_QUERY_COUNT_BY_DESCRIPTION = (
    "Group matching entities by CAD layer and DXF entity type."
)

CAD_ENTITY_TOOL_QUERY_ENTITY_KEY_DESCRIPTION = (
    "Exact package-internal entity_key returned by cad_query_entities rows mode."
)

CAD_ENTITY_TOOL_QUERY_SECTION_DESCRIPTION = (
    "Exactly one detail section: geometry_elements, xdata_tags, relationships, or raw_tags."
)

CAD_ENTITY_TOOL_QUERY_PAGE_SIZE_DESCRIPTION = (
    "Detail rows to return on the first page; default 100 and maximum 200."
)

PROVIDER_XDATA_FILTERS_APPIDS_DESCRIPTION = "AppIDs first discovered with profile_dimension=appid."

PROVIDER_XDATA_FILTERS_GROUP_CODES_DESCRIPTION = (
    "DXF XDATA group codes first discovered with profile_dimension=group_code."
)

PROVIDER_XDATA_FILTERS_ASSIGNMENT_KEYS_DESCRIPTION = (
    "Projected assignment keys first discovered with profile_dimension=assignment_key."
)

PROVIDER_XDATA_FILTERS_ASSIGNMENT_VALUES_DESCRIPTION = (
    "Zero to 100 unique typed assignment values. Choose exactly one kind for the "
    "entire array; mixed-kind arrays are invalid."
)

XDATA_TOOL_QUERY_BASE_ENTITY_FILTERS_DESCRIPTION = (
    "Filter the owner CAD entities before reading XDATA. Use layers/entity_types "
    "discovered by cad_query_entities or entity_keys returned by its rows mode."
)

XDATA_TOOL_QUERY_BASE_PAGE_SIZE_DESCRIPTION = (
    "XDATA rows or profile buckets per page; default 100 and maximum 200."
)

XDATA_ROWS_TOOL_QUERY_MODE_DESCRIPTION = (
    "Return bounded raw XDATA rows plus deterministic assignment projections."
)

XDATA_ROWS_TOOL_QUERY_XDATA_FILTERS_DESCRIPTION = (
    "Filter XDATA rows by discovered AppIDs, group codes, assignment keys or typed "
    "assignment values. Different fields are AND; values inside one array are OR."
)

APPID_PROFILE_TOOL_QUERY_MODE_DESCRIPTION = "Return XDATA discovery/statistical buckets."

APPID_PROFILE_TOOL_QUERY_PROFILE_DIMENSION_DESCRIPTION = "Discover and count XDATA application IDs."

APPID_PROFILE_TOOL_QUERY_INCLUDE_DECLARED_UNUSED_APPIDS_DESCRIPTION = (
    "Include AppIDs declared in the DXF table even when no matching entity uses them."
)

GROUP_CODE_PROFILE_TOOL_QUERY_PROFILE_DIMENSION_DESCRIPTION = (
    "Discover and count XDATA group codes."
)

ASSIGNMENT_KEY_PROFILE_TOOL_QUERY_PROFILE_DIMENSION_DESCRIPTION = (
    "Discover assignment keys and report completeness and value distributions."
)

ASSIGNMENT_KEY_PROFILE_TOOL_QUERY_ASSIGNMENT_KEYS_DESCRIPTION = (
    "Optional profile-bucket filter using keys first discovered by an unfiltered "
    "assignment_key profile; null means all keys."
)


CAD_ANALYZE_TOLERANCE_CANDIDATES_DESCRIPTION = (
    "Read one exact source_l0 CAD Package and return bounded coverage, distance/angle "
    "distributions, ambiguity, blocker, trial-sensitivity and recommendation evidence. Select "
    "package_alias from data_package_list and discover every path/POINT Layer first with "
    "cad_query_entities; source_unit_to_m must come from a Requirement, user approval, CAD fact "
    "or another verifiable source. Process multiple source_l0 drawings in separate calls. There "
    "are no filter objects or AND/OR expression tree: path_layers selects the listed path Layers, "
    "and each point_groups item independently selects its listed POINT Layers without a radius. "
    "Supply zero to eight complete explicit trials; every trial supplies point_search_radii with "
    "exactly the same scope_key set as point_groups. Zero trials asks the server for bounded "
    "deterministic probes from the observed distributions, not hidden defaults. Those probes may "
    "reflect whole-network spacing and are investigation anchors, not topology-safe tolerances; "
    "they are not compute-ready. Do not pass them unchanged to cad_compute_measurements: first "
    "run at least one sourced, domain-scale explicit trial and inspect edge-collapse and blocker "
    "diagnostics. Scan independent relation "
    "families or POINT groups separately when an all-group sensitivity call exceeds the shared "
    "budget. A recommendation may be null and is never an approved tolerance. The synchronous "
    "call returns no cursor, "
    "Package or Operation, "
    "and never returns full candidate rows. Input is limited to 2,000,000 L0 rows, 256 MiB "
    "Parquet and 512 MiB Arrow; the response is limited to 256 KiB and execution uses the "
    "admission budget below. Important failures include INVALID_ARGUMENT, PACKAGE_KIND_MISMATCH, "
    "CAD_LAYER_NOT_FOUND, CAD_PATH_SCOPE_EMPTY, CAD_MEASUREMENT_INPUT_BUDGET_EXCEEDED, "
    "CAPACITY_BUSY, TIMEOUT, RESOURCE_LIMIT, WORKER_FAILED and SERVICE_UNAVAILABLE. Errors are a "
    "single line of at most 2,000 UTF-8 bytes in CODE; phase=...; retryable=...; "
    "operation_alias=...?; message=... format. Results are geometric investigation facts, not "
    "business topology, CAD classification or IFC decisions."
)

CAD_COMPUTE_MEASUREMENTS_DESCRIPTION = (
    "Synchronously compute complete deterministic 3D L1 measurements for one exact source_l0 "
    "CAD Package and atomically publish one immutable derived Package. Select package_alias from "
    "data_package_list, discover all path/POINT Layer names first with cad_query_entities, "
    "and obtain source_unit_to_m and every tolerance value from a Requirement, user approval, CAD "
    "fact or another verifiable source. Process multiple source_l0 drawings in separate calls. "
    "There are no filter objects or AND/OR expression tree: path_layers selects the listed path "
    "Layers, while each point_scopes item is an independent POINT group. Submit exactly one "
    "complete tolerance_spec and output_package; there is deliberately no outputs declaration. "
    "The server always publishes atomic_edges, endpoints, endpoint_pairs, endpoint_on_path, "
    "planar_intersections, point_to_path, curve_turns, entity_bounds, local_frames and "
    "tolerance_diagnostics as ten Parquet Payloads. point_to_path preserves every raw edge "
    "candidate: raw_candidate_rank orders rows, while distinct_target_rank groups shared-node "
    "edges as one target; filtering distinct_target_rank=1 alone may still return several rows, "
    "so use raw_candidate_rank=1 only when one representative raw edge is explicitly intended. "
    "Nonzero bulge or any geometry/budget failure "
    "fails the whole synchronous call without partial publication. XY intersections retain Z "
    "facts and are not physical connections. The response reuses the multi-output Data Transform "
    "result. Input is limited to 2,000,000 L0 rows, 256 MiB Parquet and 512 MiB Arrow. The complete "
    "ten-Payload output is limited to 2,000,000 rows, 512 MiB Arrow and 256 MiB Parquet, with "
    "100 MiB per Payload and 2,000,000 generated candidates; exceeding any limit fails atomically "
    "rather than truncating candidates. The response is limited to 64 KiB and execution uses "
    "the admission budget below. Geometry failures include CAD_GEOMETRY_UNSUPPORTED, "
    "CAD_NONZERO_BULGE_UNSUPPORTED, CAD_GEOMETRY_NONFINITE, CAD_ZERO_LENGTH_EDGE, "
    "CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED and CAD_ENDPOINT_QUANTIZATION_COLLAPSE. Execution "
    "or publication failures include CAD_MEASUREMENT_CANDIDATE_BUDGET_EXCEEDED, CAPACITY_BUSY, "
    "TIMEOUT, RESOURCE_LIMIT, WORKER_FAILED, OUTPUT_INVALID, OUTPUT_BUDGET_EXCEEDED, "
    "SERVICE_UNAVAILABLE and PUBLICATION_UNCERTAIN. Errors are a single line of at most 2,000 "
    "UTF-8 bytes in CODE; phase=...; retryable=...; operation_alias=...?; message=... format. "
    "Rows are geometric facts, not topology nodes, product categories or IFC decisions."
)


CAD_ANALYZE_TOLERANCE_CANDIDATES_DESCRIPTION += _runtime_budget_description(_CAD_ANALYSIS_EXECUTION, 'CAD measurement (one slot)')
CAD_COMPUTE_MEASUREMENTS_DESCRIPTION += _runtime_budget_description(_CAD_MEASUREMENT_EXECUTION, 'CAD measurement (one slot)')
