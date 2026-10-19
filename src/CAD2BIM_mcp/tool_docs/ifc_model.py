"""Provider-visible usage text for the ifc model capability family."""

IFC_MODEL_INFO_RESULT_SIZE_BYTES_DESCRIPTION = (
    "Byte count of the exact persisted IFC file represented by this summary."
)
IFC_SOURCE_REVISION_REFERENCE_DESCRIPTION = "Opaque exact IFC revision read basis. Forward unchanged as base_revision_ref; never construct it from a revision number or a historical Apply summary."

from CAD2BIM_mcp.tool_docs.common import runtime_budget_description as _runtime_budget_description
from CAD2BIM_backend.domain.runtime_execution import (
    IFC_REVISION_INVOCATION_EXECUTION as _IFC_REVISION_INVOCATION_EXECUTION,
    IFC_REVISION_EXECUTION as _IFC_REVISION_EXECUTION,
)

IFC_PROJECT_PATCH_INPUT_DESCRIPTION = (
    "Only explicitly supplied, non-null Project business fields are changed."
)

IFC_LENGTH_UNIT_INPUT_NAME_DESCRIPTION = "IFC SI length-unit name; I0-B1 supports METRE only."

IFC_LENGTH_UNIT_INPUT_PREFIX_DESCRIPTION = (
    "Optional IFC SI prefix; for example MILLI makes existing project length values "
    "be interpreted as millimetres rather than metres."
)

IFC_PLANE_ANGLE_UNIT_INPUT_NAME_DESCRIPTION = (
    "IFC SI plane-angle unit name; I0-B1 supports RADIAN only."
)

IFC_PLANE_ANGLE_UNIT_INPUT_PREFIX_DESCRIPTION = (
    "Optional IFC SI prefix applied to existing plane-angle values."
)

IFC_PROJECT_UNIT_CHANGE_INPUT_LENGTH_UNIT_DESCRIPTION = (
    "Explicit new IfcProject length unit; omit to leave it unchanged."
)

IFC_PROJECT_UNIT_CHANGE_INPUT_PLANE_ANGLE_UNIT_DESCRIPTION = (
    "Explicit new IfcProject plane-angle unit; omit to leave it unchanged."
)

IFC_WORLD_COORDINATE_SYSTEM2_DINPUT_KIND_DESCRIPTION = (
    "Select an IfcAxis2Placement2D world coordinate system."
)

IFC_WORLD_COORDINATE_SYSTEM2_DINPUT_ORIGIN_DESCRIPTION = (
    "WCS origin [x, y] in the configured project length unit."
)

IFC_WORLD_COORDINATE_SYSTEM2_DINPUT_REF_DIRECTION_DESCRIPTION = (
    "Non-zero WCS local X-axis direction ratios [x, y]."
)

IFC_WORLD_COORDINATE_SYSTEM3_DINPUT_KIND_DESCRIPTION = (
    "Select an IfcAxis2Placement3D world coordinate system."
)

IFC_WORLD_COORDINATE_SYSTEM3_DINPUT_ORIGIN_DESCRIPTION = (
    "WCS origin [x, y, z] in the configured project length unit."
)

IFC_WORLD_COORDINATE_SYSTEM3_DINPUT_AXIS_DESCRIPTION = (
    "Non-zero WCS local Z-axis direction ratios [x, y, z]."
)

IFC_WORLD_COORDINATE_SYSTEM3_DINPUT_REF_DIRECTION_DESCRIPTION = (
    "Non-zero WCS approximate local X-axis direction ratios [x, y, z], not parallel to axis."
)

IFC_LOCAL_COORDINATE_REFERENCE_CHANGE_INPUT_MODE_DESCRIPTION = (
    "Remove projected CRS/MapConversion and use only project-local coordinates."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_MODE_DESCRIPTION = (
    "Create or replace one IfcProjectedCRS and IfcMapConversion."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_SOURCE_CONTEXT_IDENTIFIER_DESCRIPTION = (
    "Case-sensitive identifier of the Model/3D parent context used as "
    "IfcMapConversion.SourceCRS; it may already exist or be created in this command."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_CRS_NAME_DESCRIPTION = (
    "Explicit projected CRS name in EPSG:<positive integer> form."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_DESCRIPTION_DESCRIPTION = (
    "Optional IfcProjectedCRS.Description; omit for no description."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_GEODETIC_DATUM_DESCRIPTION = (
    "Optional IfcProjectedCRS.GeodeticDatum label."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_VERTICAL_DATUM_DESCRIPTION = (
    "Optional IfcProjectedCRS.VerticalDatum label."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_MAP_PROJECTION_DESCRIPTION = (
    "Optional IfcProjectedCRS.MapProjection label."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_MAP_ZONE_DESCRIPTION = (
    "Optional IfcProjectedCRS.MapZone label."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_MAP_UNIT_DESCRIPTION = (
    "Optional projected CRS map unit; null leaves IfcProjectedCRS.MapUnit unset."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_EASTINGS_DESCRIPTION = (
    "IfcMapConversion.Eastings translation in map units."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_NORTHINGS_DESCRIPTION = (
    "IfcMapConversion.Northings translation in map units."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_ORTHOGONAL_HEIGHT_DESCRIPTION = (
    "IfcMapConversion.OrthogonalHeight translation in map units."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_X_AXIS_ABSCISSA_DESCRIPTION = (
    "Non-zero map X-axis direction abscissa used with x_axis_ordinate."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_X_AXIS_ORDINATE_DESCRIPTION = (
    "Non-zero map X-axis direction ordinate used with x_axis_abscissa."
)

IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_SCALE_DESCRIPTION = (
    "Positive IfcMapConversion scale from project coordinates to map coordinates."
)

IFC_CONTEXT_OPERATION_INPUT_CONTEXT_IDENTIFIER_DESCRIPTION = (
    "Case-sensitive, model-wide unique ContextIdentifier; it is immutable after create "
    "and is the later product-representation reference key."
)

CREATE_PARENT_CONTEXT_INPUT_ACTION_DESCRIPTION = (
    "Create one new parent IfcGeometricRepresentationContext."
)

CREATE_PARENT_CONTEXT_INPUT_CONTEXT_TYPE_DESCRIPTION = (
    "Model for a 3D parent or Plan for a 2D parent."
)

CREATE_PARENT_CONTEXT_INPUT_COORDINATE_SPACE_DIMENSION_DESCRIPTION = (
    "Coordinate dimension; Model requires 3 and Plan requires 2."
)

CREATE_PARENT_CONTEXT_INPUT_PRECISION_DESCRIPTION = (
    "Positive geometric precision interpreted in the project length unit."
)

CREATE_PARENT_CONTEXT_INPUT_WORLD_COORDINATE_SYSTEM_DESCRIPTION = (
    "Complete parent-context WCS matching coordinate_space_dimension."
)

CREATE_PARENT_CONTEXT_INPUT_TRUE_NORTH_DESCRIPTION = (
    "Optional non-zero TrueNorth direction ratios in the parent WCS XY plane."
)

UPDATE_PARENT_CONTEXT_INPUT_ACTION_DESCRIPTION = (
    "Directly update explicitly supplied fields on one existing parent context."
)

UPDATE_PARENT_CONTEXT_INPUT_CONTEXT_TYPE_DESCRIPTION = (
    "Optional replacement ContextType; omit to preserve the current value."
)

UPDATE_PARENT_CONTEXT_INPUT_COORDINATE_SPACE_DIMENSION_DESCRIPTION = (
    "Optional replacement dimension; changing it also requires a matching complete WCS."
)

UPDATE_PARENT_CONTEXT_INPUT_PRECISION_DESCRIPTION = (
    "Optional replacement precision; omit to preserve the current value."
)

UPDATE_PARENT_CONTEXT_INPUT_WORLD_COORDINATE_SYSTEM_DESCRIPTION = (
    "Optional complete replacement WCS; omit to preserve the current WCS."
)

UPDATE_PARENT_CONTEXT_INPUT_TRUE_NORTH_DESCRIPTION = (
    "Replacement TrueNorth; explicit null removes it, while omission preserves it."
)

CREATE_SUBCONTEXT_INPUT_ACTION_DESCRIPTION = "Create one new IfcGeometricRepresentationSubContext."

CREATE_SUBCONTEXT_INPUT_CONTEXT_TYPE_DESCRIPTION = (
    "Subcontext ContextType, compatible with its selected parent."
)

CREATE_SUBCONTEXT_INPUT_PARENT_CONTEXT_IDENTIFIER_DESCRIPTION = (
    "Case-sensitive ContextIdentifier of the parent context; it may already exist or "
    "be created in this command."
)

CREATE_SUBCONTEXT_INPUT_TARGET_VIEW_DESCRIPTION = (
    "IFC TargetView describing the intended representation view. GRAPH_VIEW is used "
    "for graph/axis representations; MODEL_VIEW is used for ordinary 3D model "
    "representations such as Body or Box. Follow the current project Profile rather "
    "than treating MODEL_VIEW as a universal default."
)

CREATE_SUBCONTEXT_INPUT_TARGET_SCALE_DESCRIPTION = (
    "Optional positive target plot scale; null leaves TargetScale unset."
)

CREATE_SUBCONTEXT_INPUT_USER_DEFINED_TARGET_VIEW_DESCRIPTION = (
    "Required only when target_view is USERDEFINED."
)

UPDATE_SUBCONTEXT_INPUT_ACTION_DESCRIPTION = (
    "Directly update explicitly supplied fields on one existing subcontext."
)

UPDATE_SUBCONTEXT_INPUT_PARENT_CONTEXT_IDENTIFIER_DESCRIPTION = (
    "Optional replacement parent ContextIdentifier."
)

UPDATE_SUBCONTEXT_INPUT_TARGET_VIEW_DESCRIPTION = "Optional replacement TargetView."

UPDATE_SUBCONTEXT_INPUT_TARGET_SCALE_DESCRIPTION = (
    "Replacement TargetScale; explicit null removes it, omission preserves it."
)

UPDATE_SUBCONTEXT_INPUT_USER_DEFINED_TARGET_VIEW_DESCRIPTION = (
    "Replacement USERDEFINED label; explicit null removes it, omission preserves it."
)

IFC_MODEL_INITIALIZE_DESCRIPTION = (
    "Create the current Run's one durable IFC4X3_ADD2 model and its single IfcProject. "
    "Call this once only after runtime_open_direct_session for a new Run that has no IFC "
    "model. direct_session_alias must be that tool's returned alias; project_name is the "
    "required 1..255-character business name written to IfcProject.Name after trimming "
    "and must contain no NUL or control characters. The server owns the Project GlobalId, "
    "Operation identity, immutable revision and storage identities. This tool cannot "
    "rename, update, repair, reconfigure, reset, or replace a model, and it creates no "
    "Site, units, geometry context, products, or representations. Internal recovery of "
    "an identical call after response loss returns the original Operation and revision 1; "
    "a defensive call after an existing model returns IFC_MODEL_ALREADY_INITIALIZED. "
    "Technical uncertainty returns SYSTEM_PAUSED without exposing internal diagnostics."
)

IFC_MODEL_INFO_DESCRIPTION = (
    "Read the current Run's durable IFC model from database CURRENT pinned to one exact "
    "immutable revision and ArtifactVersion, then return only bounded business facts. "
    "size_bytes is the verified byte count of that stored IFC file, not a reserialization estimate. "
    "source_revision contains that same read's opaque revision_ref and local revision_no; forward revision_ref unchanged as an edit base_revision_ref when an input Package has no unique system IFC source. It does not select a newer CURRENT or expose internal revision IDs. "
    "When only an Apply's committed revision and size are needed, use its returned committed_revision instead; that original result remains fixed after later writes. "
    "direct_session_alias must come from runtime_open_direct_session. This read-only tool "
    "accepts no revision selector, creates no Operation, and never returns internal model, "
    "revision, ArtifactVersion, writer, storage, Task, or Run identities. If the Run has "
    "not been initialized it returns IFC_MODEL_NOT_INITIALIZED; call ifc_model_initialize "
    "once before retrying. geometry_basis is null until exact CURRENT has project length "
    "and plane-angle units plus one Project-linked Model/3D parent context; once complete, "
    "it reports those units, precision, coordinate-reference mode, and main context. "
    "Technical integrity uncertainty returns SYSTEM_PAUSED without internal diagnostics."
)

IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_DESCRIPTION = (
    "Create or directly modify only the explicitly submitted project units, geometric "
    "contexts, WCS/TrueNorth, precision, and local or projected CRS configuration in the "
    "current durable IFC model. direct_session_alias must come from "
    "runtime_open_direct_session. Submit at least one project_unit_change, "
    "coordinate_reference_change, or complete create/update operation in "
    "context_operations. Every context_operations item must use the discriminator field "
    "action (never operation): create_parent_context requires context_identifier, "
    "context_type, coordinate_space_dimension, precision, and a complete "
    "world_coordinate_system; create_subcontext requires context_identifier, "
    "context_type, parent_context_identifier, and target_view. The corresponding "
    "update_parent_context and update_subcontext actions require context_identifier plus "
    "at least one explicitly changed field. ContextIdentifier is the unique model-wide "
    "key. A complete 3D parent operation has this exact shape: "
    "{action:create_parent_context, context_identifier:Model, context_type:Model, "
    "coordinate_space_dimension:3, precision:0.00001, "
    "world_coordinate_system:{kind:3d, origin:[0,0,0], axis:[0,0,1], "
    "ref_direction:[1,0,0]}}. A Body child has this exact shape: "
    "{action:create_subcontext, context_identifier:Body, context_type:Model, "
    "parent_context_identifier:Model, target_view:MODEL_VIEW}. The example values show "
    "the submitted object shape only; choose identifiers, precision, WCS and views from "
    "the actual project policy. Create required "
    "parent contexts and subcontexts explicitly because product tools never create them "
    "implicitly. Identifiers are case-sensitive and cannot be renamed after creation. "
    "Before choosing shared subcontexts, inspect the downstream product representation "
    "strategies and required IFC outputs in the current Data Packages. A source "
    "geometry input role describes available input geometry; it is not by itself the "
    "complete set of target representation contexts. "
    "For a 3D parent context, world_coordinate_system.axis is the WCS local Z-axis and "
    "world_coordinate_system.ref_direction is its approximate local X-axis. Spatial or "
    "product placement-frame fields are separate facts; do not copy their axis or "
    "ref-direction values into the project WCS unless an authoritative project policy "
    "gives them those WCS meanings. "
    "Use an explicit project or Requirement precision when one is available; otherwise "
    "choose a legal project precision from the modeling scale and intended geometry and "
    "state that modeling-policy choice. Source coordinate quantization, topology or snap "
    "tolerances, and the smallest observed feature are separate facts and do not "
    "mechanically determine IFC geometric precision. "
    "This WRITE_ONE creates one critical Operation and one immutable revision on success, "
    "then returns only operation_alias and the exact reread model summary. It preserves "
    "every existing Placement, geometry, property, quantity, and "
    "business numeric value, so the new units, WCS, precision, or CRS reinterpret those "
    "values and can change physical size, world position, georeferencing, or tolerance. "
    "The tool never rescales, rotates, compensates, or traverses existing products. "
    "Read ifc_model_info and ifc_representation_context_list first when the command depends "
    "on current facts, then issue this write in a separate call. Deterministic context "
    "errors make zero IFC modification; technical uncertainty returns SYSTEM_PAUSED."
)

IFC_REPRESENTATION_CONTEXT_LIST_DESCRIPTION = (
    "List the current IFC model's bounded representation-context catalog from database "
    "CURRENT pinned to one exact immutable revision. The only input is the "
    "direct_session_alias returned by runtime_open_direct_session. Results are sorted by "
    "the model-wide unique ContextIdentifier and distinguish parent_context from "
    "subcontext, including bounded WCS/precision/TrueNorth or parent/TargetView facts and "
    "representation_count. Use an existing compatible ContextIdentifier in later product "
    "tools, or create/update one explicitly with ifc_model_configure_geometry_basis. This "
    "read-only tool accepts no revision selector, creates no Operation, and returns no "
    "revision, ArtifactVersion, STEP, database, writer, Task, or Run identity."
)

IFC_MODEL_INITIALIZE_PROJECT_NAME_DESCRIPTION = (
    "Business name for IfcProject.Name; trimmed value must be non-empty and "
    "contain no NUL or control characters."
)

IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_PROJECT_UNIT_CHANGE_DESCRIPTION = (
    "Optional explicit replacement for the project length unit, plane-angle "
    "unit, or both; omitted units remain unchanged."
)

IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_COORDINATE_REFERENCE_CHANGE_DESCRIPTION = (
    "Optional complete local or projected_crs branch; projected_crs binds its "
    "MapConversion to one Model/3D parent source_context_identifier that already "
    "exists or is created in the same command."
)

IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_CONTEXT_OPERATIONS_DESCRIPTION = (
    "Zero to 64 explicit create_parent_context, update_parent_context, "
    "create_subcontext, or update_subcontext operations. Derive required shared "
    "subcontexts from downstream representation strategies, not only from the "
    "roles present in source geometry inputs."
)

IFC_PROJECT_PATCH_INPUT_NAME_DESCRIPTION = (
    "Replacement non-empty IfcProject.Name; omit to preserve it."
)

IFC_PROJECT_PATCH_INPUT_DESCRIPTION_DESCRIPTION = (
    "Replacement IfcProject.Description; omit to preserve it and use clear_fields to remove it."
)

IFC_PROJECT_PATCH_INPUT_OBJECT_TYPE_DESCRIPTION = (
    "Replacement IfcProject.ObjectType; omit to preserve it and use clear_fields to remove it."
)

IFC_PROJECT_PATCH_INPUT_LONG_NAME_DESCRIPTION = (
    "Replacement IfcProject.LongName; omit to preserve it and use clear_fields to remove it."
)

IFC_PROJECT_PATCH_INPUT_PHASE_DESCRIPTION = (
    "Replacement IfcProject.Phase; omit to preserve it and use clear_fields to remove it."
)

IFC_PROJECT_RESULT_GLOBAL_ID_DESCRIPTION = "Stable server-created IfcProject.GlobalId."

IFC_PROJECT_RESULT_NAME_DESCRIPTION = "Current IfcProject.Name after exact saved-content reread."

IFC_PROJECT_RESULT_DESCRIPTION_DESCRIPTION = "Current optional IfcProject.Description."

IFC_PROJECT_RESULT_OBJECT_TYPE_DESCRIPTION = "Current optional IfcProject.ObjectType."

IFC_PROJECT_RESULT_LONG_NAME_DESCRIPTION = "Current optional IfcProject.LongName."

IFC_PROJECT_RESULT_PHASE_DESCRIPTION = "Current optional IfcProject.Phase."

IFC_PROJECT_UPDATE_RESULT_OPERATION_ALIAS_DESCRIPTION = (
    "Safe alias of the committed critical Operation."
)

IFC_PROJECT_UPDATE_RESULT_PROJECT_DESCRIPTION = (
    "The five readable Project business fields after exact saved-content reread."
)

IFC_PROJECT_UPDATE_DESCRIPTION = (
    "Modify only the current durable model's unique IfcProject business metadata after "
    "ifc_model_initialize has performed its create-only initialization. "
    "direct_session_alias must come from runtime_open_direct_session. patch accepts only "
    "explicit non-null name, description, object_type, long_name, and phase values; omit "
    "a field to preserve it. clear_fields accepts only description, object_type, "
    "long_name, and phase; name cannot be cleared. A field cannot be both patched and "
    "cleared, duplicate clear entries are invalid, and an empty or semantically "
    "unchanged request returns IFC_NO_CHANGES with zero IFC modification. This tool does "
    "not modify GlobalId, schema, units, representation contexts, OwnerHistory, spatial "
    "structure, products, geometry, properties, or quantities. It creates one critical "
    "Operation and one immutable model update on success, then returns only "
    "operation_alias and the exact-reread Project GlobalId plus its five business fields. "
    "It never accepts or returns model, revision, ArtifactVersion, storage, writer, Task, "
    "or Run identities; callers cannot submit, read, or infer revision numbers. "
    "If the write depends on current facts, read them first and wait for the returned result. "
    "Submit this WRITE alone; do not batch it with another tool call. Deterministic patch "
    "failures make zero IFC modification; technical "
    "uncertainty returns SYSTEM_PAUSED."
)

IFC_PROJECT_UPDATE_PATCH_DESCRIPTION = (
    "Closed, explicit non-null replacements for up to five Project business "
    "fields; an empty object is legal only when clear_fields is non-empty."
)

IFC_PROJECT_UPDATE_CLEAR_FIELDS_DESCRIPTION = (
    "Zero to four unique nullable Project fields to remove; name and all "
    "server-owned fields are illegal."
)


IFC_MODEL_INITIALIZE_DESCRIPTION += _runtime_budget_description(_IFC_REVISION_INVOCATION_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_DESCRIPTION += _runtime_budget_description(_IFC_REVISION_INVOCATION_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
IFC_PROJECT_UPDATE_DESCRIPTION += _runtime_budget_description(_IFC_REVISION_INVOCATION_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
IFC_MODEL_INFO_DESCRIPTION += _runtime_budget_description(_IFC_REVISION_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
IFC_REPRESENTATION_CONTEXT_LIST_DESCRIPTION += _runtime_budget_description(_IFC_REVISION_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
