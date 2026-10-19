"""Shared Provider-visible usage text for geometry preview profiles."""

PREVIEW_FRAME_CONSTRAINT_STATUS_DESCRIPTION = (
    "Numerical solved-interface result for the Family's declared internal constraints and "
    "invocation external conditions; it does not evaluate or change current adjacent pipes, "
    "physical end-face contact or IFC network connectivity."
)

PREVIEW_CASE_EXTERNAL_TARGET_COMPARISON_DESCRIPTION = (
    "Family only: comparison of supplied external targets with actual output interfaces. NOT_COMPARED means no such comparison completed; frame checks do not establish physical contact."
)

GEOMETRY_PREVIEW_REFERENCE_DATUM_DESCRIPTION = (
    "Explicit reference-local to preview-scene rigid transform; translation is in metres. "
    "Use identity to compare aligned sources. A placed Geometry or Family scene uses project-world coordinates."
)

PREVIEW_DISPLAY_QUALITY_WARNING_DESCRIPTION = (
    "Display limitation with the current object/selection and face attribution when confirmed; code/message only."
)

GEOMETRY_PREVIEW_PROFILE_MINIMAL_JSON = (
    '{"presentation":{"panels":[{"panel_key":"isometric","camera":'
    '{"camera_kind":"preset_fit","view":"isometric"}}],'
    '"overlay":{"overlay_kind":"clean"}}}'
)

GEOMETRY_PREVIEW_PROFILE_CONNECTION_PIPES_JSON = (
    '{"connection_context":{"mode":"pipes","items":[{"start_m":[0,0,0.3],'
    '"end_m":[0,0,0.5],"outer_diameter_m":0.05}]},"presentation":{"panels":['
    '{"panel_key":"isometric","camera":{"camera_kind":"preset_fit","view":"isometric"}}],'
    '"overlay":{"overlay_kind":"clean"}}}'
)
GEOMETRY_PREVIEW_PROFILE_CONNECTION_AUTO_JSON = (
    '{"connection_context":{"mode":"auto"},"presentation":{"panels":['
    '{"panel_key":"isometric","camera":{"camera_kind":"preset_fit","view":"isometric"}}],'
    '"overlay":{"overlay_kind":"clean"}}}'
)

GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_CLEAN_JSON = (
    '{"width_px":1280,"height_px":640,"presentation":{"panels":['
    '{"panel_key":"region","camera":{"camera_kind":"preset_region",'
    '"view":"front","target_m":[0.0,0.0,0.0],"vertical_extent_m":2.0}},'
    '{"panel_key":"custom","camera":{"camera_kind":"custom",'
    '"target_m":[0.0,0.0,0.0],"direction":[0.0,-1.0,0.0],'
    '"up":[0.0,0.0,1.0],"vertical_extent_m":2.0}}],'
    '"overlay":{"overlay_kind":"clean"}}}'
)

GEOMETRY_PREVIEW_PROFILE_INTERFACES_ALL_JSON = (
    '{"presentation":{"panels":[{"panel_key":"all_interfaces","camera":'
    '{"camera_kind":"preset_fit","view":"isometric"}}],"overlay":'
    '{"overlay_kind":"interfaces","focus":{"focus_kind":"all"}}}}'
)

# Replace both tokens with exact ``interface_overlays[].overlay_id`` strings from
# a response from the same exact inputs before submitting this JSON.
GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_INTERFACES_TEMPLATE = (
    '{"width_px":1280,"height_px":640,"presentation":{"panels":['
    '{"panel_key":"connection","camera":{"camera_kind":"interface_focus",'
    '"overlay_ids":["__FIRST_RESPONSE_OVERLAY_ID__","__SECOND_RESPONSE_OVERLAY_ID__"],'
    '"orientation":"side_x","vertical_extent_m":1.0}},'
    '{"panel_key":"context","camera":{"camera_kind":"preset_fit","view":"isometric"}}],'
    '"overlay":{"overlay_kind":"interfaces","focus":{"focus_kind":"interface_pair",'
    '"overlay_ids":["__FIRST_RESPONSE_OVERLAY_ID__",'
    '"__SECOND_RESPONSE_OVERLAY_ID__"]}}}}'
)

GEOMETRY_PREVIEW_PROFILE_USAGE = (
    "Display quality: presentation.display_quality_warnings, when present, accompanies a "
    "diagnostic PNG with a visible warning. Each code/message gives confirmed current object/selection, "
    "face, selected attempt and retry facts; face numbers apply only to that exact object and coordinates. "
    "Area difference alone is a display limitation and does not establish invalid geometry or business failure. "
    "Confirmed boundary crossing fails rendering while BUILT/SOLVED and an existing inspection_ref remain valid facts. "
    "Investigate a queryable saved object with ctx.display_diagnostics() using the same target/selection/coordinates; "
    "see cad2bim://contracts/ifc_geometry_query_python/program_api. Reference/background objects may require "
    "their own exact target; do not use the main target for another object's face. Inspect your construction "
    "and make a supported Geometry correction, then Preview the same inputs and required material/interfaces/shape. "
    "Do not change geometry solely to clear an area warning or retry confirmed crossing unchanged. "
    "Native no_issue_reported is not strict correctness proof. Failure to produce a safe image or exceeding "
    "runtime limits remains an error. "
    "preview_profile_json is a JSON string whose decoded top-level object is one closed "
    "GeometryPreviewProfile. In mode=inspection or family_standard_model, observation.render_profile "
    "is that native profile object, not a JSON-encoded string; keep camera/overlay fields inside it. "
    "Omit it for the default isometric 640x480 view. A profile "
    "has optional width_px, height_px and connection_context plus presentation={panels:[...],overlay:{...}}. "
    "connection_context is a closed object with mode=none (default), auto or pipes. pipes requires items, "
    "each with start_m, end_m and positive outer_diameter_m, all in metres in the current preview coordinate "
    "space; endpoints must differ. Explicit pipes display the supplied regions without snapping or trimming. "
    "connection_context cannot replace or combine with an existing Host-specific display layer. "
    "auto uses same-result or saved checked Family ports: for every condition kind, start at the actual port, "
    "follow its outward axis for three times the condition outer diameter. This short illustration is not "
    "clipped to finite_segment endpoints and does not extend the solving range. Missing ports, passed checks "
    "or condition OD skip that illustration with a diagnostic. Geometry-only previews have no automatic "
    "installation targets. The response reports effective "
    "connection_context items and connection_context_diagnostics for skipped context. Context participates "
    "in camera fitting and occlusion, and is excluded from the product geometry, queries and IFC writes. "
    "It requires no CAD pipe data, performs no cutting and does not verify physical connection. "
    "presentation.panels contains one to four ordered panels, each exactly "
    "{panel_key:...,camera:{camera_kind:...}}; panel_key values are unique, 1-64 characters, "
    "start with a lowercase ASCII letter, and contain only lowercase ASCII letters, digits, "
    "or underscores (^[a-z][a-z0-9_]{0,63}$). The camera object is "
    "discriminated by camera_kind. preset_fit requires view. preset_region requires view, "
    "target_m, and vertical_extent_m. custom requires target_m, direction, up, and "
    "vertical_extent_m. interface_focus requires one or two overlay_ids, orientation, and "
    "optionally vertical_extent_m. For both preset cameras, put view inside the camera object; "
    "view is one of isometric/front/back/right/left/top/bottom. target_m, direction, and up are "
    "three-number arrays; direction and up are perpendicular unit vectors. interface_focus "
    "orientation is side_x/side_y/axial/oblique. presentation.overlay is an object discriminated "
    "by overlay_kind, never the string clean or interfaces. The clean object is exactly "
    "{overlay_kind:clean}. The interfaces object has overlay_kind=interfaces and optional "
    "focus={focus_kind:all} or focus={focus_kind:interface_pair,overlay_ids:[id_a,id_b]}; the pair "
    "requires two distinct IDs. For interface_focus and interface_pair, copy exact strings from "
    "a response from the same exact inputs at interface_overlays[].overlay_id. Do not "
    "derive IDs from labels or interface keys. The complete object is at most 65,536 UTF-8 bytes "
    "and rejects duplicate keys, NaN, infinity, invalid Unicode, open fields, and invalid Domain "
    "values. A presentation still renders one shared PNG canvas with at most 2048x2048 "
    "pixels and 16 MiB. Camera direction points from camera to target and screen-right is "
    "direction cross up. Preset directions are FRONT=+Y, BACK=-Y, RIGHT=-X, LEFT=+X, TOP=-Z, "
    "BOTTOM=+Z; front/back/right/left use up=+Z, top/bottom use up=+Y, and isometric uses "
    "direction=(-1,-1,-1)/sqrt(3), up=(-1,-1,2)/sqrt(6). "
    "If interface_focus reports that its extent does not contain both frames, increase vertical_extent_m "
    "or omit that optional field for automatic framing. preset_region and custom require an extent: "
    "correct their target, extent and direction/up rather than dropping a required field. A degenerate "
    "viewport needs a nonzero valid view. Use Geometry Preview inspection on an already retained model "
    "to correct its picture without solving or building it again."
)

GEOMETRY_PREVIEW_PROFILE_EXAMPLES = (
    " Complete minimal presentation JSON: "
    + GEOMETRY_PREVIEW_PROFILE_MINIMAL_JSON
    + " Complete two-panel clean JSON covering preset_region and custom cameras: "
    + GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_CLEAN_JSON
    + " Complete interfaces/all-focus JSON: "
    + GEOMETRY_PREVIEW_PROFILE_INTERFACES_ALL_JSON
    + " Complete two-panel interfaces/interface-pair template covering interface_focus: "
    + GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_INTERFACES_TEMPLATE
    + " Complete explicit connection-pipe JSON: "
    + GEOMETRY_PREVIEW_PROFILE_CONNECTION_PIPES_JSON
    + " Complete automatic Family connection-context JSON: "
    + GEOMETRY_PREVIEW_PROFILE_CONNECTION_AUTO_JSON
    + " Replace every __FIRST_RESPONSE_OVERLAY_ID__ and __SECOND_RESPONSE_OVERLAY_ID__ token with "
    "two distinct exact interface_overlays[].overlay_id strings copied from a response for the "
    "same exact inputs before submitting the template."
)

GEOMETRY_PREVIEW_PROFILE_NAVIGATION = (
    " For complete Preview profile rules and typed examples, read "
    "cad2bim://contracts/common/preview#preview_profile before selecting camera or overlay options. "
    "Reuse the complete, still-applicable session copy. "
)

GEOMETRY_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION = (
    "JSON string encoding one closed GeometryPreviewProfile. Modern shape is "
    "{presentation:{panels:[{panel_key,camera:{camera_kind,...}}],"
    "overlay:{overlay_kind,...}},width_px?,height_px?,connection_context?}; preset cameras put view inside camera. "
    "connection_context has mode=none|auto|pipes: auto draws short outward Family port illustrations; "
    "pipes supplies display endpoints/OD, not CAD pipes. "
    "Geometry preview alone may use interfaces-overlay temporary_interfaces. Read "
    "cad2bim://contracts/common/preview#preview_profile for complete typed examples and copy focus IDs from a response for the same "
    "exact inputs."
)

PRODUCT_REALIZATION_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION = (
    "JSON string encoding one closed GeometryPreviewProfile. Modern shape is "
    "{presentation:{panels:[{panel_key,camera:{camera_kind,...}}],"
    "overlay:{overlay_kind,...}},width_px?,height_px?,connection_context?}; preset cameras put view inside camera. "
    "connection_context has mode=none|auto|pipes: auto draws short outward Family port illustrations; "
    "pipes supplies display endpoints/OD, not CAD pipes. "
    "Product Realization rejects any explicit temporary_interfaces field, including null or []. "
    "Read cad2bim://contracts/common/preview#preview_profile for complete typed examples and copy focus IDs from a response for "
    "the same exact inputs."
)


__all__ = [
    "FINITE_VALIDATION_GUIDANCE",
    "GEOMETRY_INSPECTION_COMPLETION_GUIDANCE",
    "GEOMETRY_PREVIEW_PROFILE_EXAMPLES",
    "GEOMETRY_PREVIEW_PROFILE_CONNECTION_PIPES_JSON",
    "GEOMETRY_PREVIEW_PROFILE_CONNECTION_AUTO_JSON",
    "GEOMETRY_PREVIEW_PROFILE_INTERFACES_ALL_JSON",
    "GEOMETRY_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION",
    "GEOMETRY_PREVIEW_PROFILE_MINIMAL_JSON",
    "GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_CLEAN_JSON",
    "GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_INTERFACES_TEMPLATE",
    "GEOMETRY_PREVIEW_PROFILE_USAGE",
    "PREVIEW_DISPLAY_QUALITY_WARNING_DESCRIPTION",
    "PRODUCT_REALIZATION_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION",
]
GEOMETRY_PREVIEW_REFERENCES_DESCRIPTION = (
    "Optional display-only reference layers, at most eight unique reference_key values and 32 MiB "
    "combined canonical source bytes. Discover exact mapped-model resource_version_alias values with "
    "ifc_geometry_resource_list/info; canonical BREP and indexed triangle mesh are supported. "
    "Supply each reference's explicit datum_transform into this preview's coordinate space. "
    "References render in amber and participate in camera fitting and occlusion. They are excluded "
    "from the primary geometry, Query target, ProductFamily solve and IFC writes. Exact aliases, "
    "formats and placements are returned in this call. "
    "Hidden, missing, non-mapped or oversized references fail before rendering; select valid visible "
    "fixed assets and correct placement before retrying."
    " For aligned comparison, pass reference_geometries=[{reference_key:'source', "
    "resource_version_alias:<exact alias copied from Catalog>,datum_transform:{}}]. "
    "datum_transform={} explicitly selects identity; a translated reference can use "
    "datum_transform={translation_m:[1.0,0.0,0.0]}."
)

PREVIEW_REFERENCE_FIELD_DESCRIPTION = (
    "Display-only exact fixed-model references with explicit placement; at most 8 unique keys and "
    "32 MiB combined source bytes. Excluded from primary geometry, solve, Query and IFC writes. "
    "Full rules/examples: cad2bim://contracts/common/preview#reference_geometries."
)
PREVIEW_RETAIN_DESCRIPTION = (
    "Save this report and successful geometry for Query, new pictures and applicable execution reuse. "
    "False releases temporary models. Saving does not compare an omitted expectation or publish a version."
)
PREVIEW_CASES_FIELD_DESCRIPTION = (
    "JSON string with root mode=inline or from_operation. For saved cases, changes supports add/replace/delete "
    "or {change_kind:'reuse',case_key:...}; unmarked cases execute. Applicability: cad2bim://contracts/common/authoring#reuse."
)
PREVIEW_RENDER_FIELD_DESCRIPTION = (
    "JSON string: mode=none (default), or mode=selected with case_keys and optional profile. "
    "Independent of retain; correlate actual images using returned rendered_case_keys."
)
GEOMETRY_PREVIEW_SINGLE_SOURCE_DESCRIPTION = "Exact editable draft from Write/Info or published Geometry from Catalog; no publication is required for draft observation."
GEOMETRY_PREVIEW_PARAMETERS_DESCRIPTION = "One JSON parameter object matching the selected Geometry contract; units and required fields come from its Info/parameter schema. This single observation has no expected-result assertion."
GEOMETRY_PREVIEW_CASE_SOURCE_DESCRIPTION = "Exact editable draft or published Geometry; every case uses this source. Reuse must match its execution content and dependencies."
GEOMETRY_PREVIEW_CASES_DESCRIPTION = PREVIEW_CASES_FIELD_DESCRIPTION + " Contract: cad2bim://contracts/ifc_geometry_preview/cases_json."
GEOMETRY_PREVIEW_RENDER_DESCRIPTION = PREVIEW_RENDER_FIELD_DESCRIPTION + " Contract: cad2bim://contracts/ifc_geometry_preview/render_json."
FAMILY_PREVIEW_SINGLE_SOURCE_DESCRIPTION = "Editable Family draft from Write/Info or exact published Family from Catalog. Draft solving and inspection do not require publication."
FAMILY_PREVIEW_CASE_SOURCE_DESCRIPTION = "Editable draft or exact published Family; all scenarios use this source and its complete Geometry bindings."
FAMILY_PREVIEW_CASES_DESCRIPTION = PREVIEW_CASES_FIELD_DESCRIPTION + " Contract: cad2bim://contracts/ifc_product_realization_preview/cases_json."
FAMILY_PREVIEW_RENDER_DESCRIPTION = PREVIEW_RENDER_FIELD_DESCRIPTION + " Contract: cad2bim://contracts/ifc_product_realization_preview/render_json."
FAMILY_PREVIEW_NOMINAL_DESCRIPTION = "Optional existing case key labeling this report's nominal case; it does not select Publish inputs or require extra cases."
PREVIEW_EXECUTION_STATUS_DESCRIPTION = "Execution completion, separate from domain status and expected-result comparison."
PREVIEW_EXECUTION_SOURCE_DESCRIPTION = "Whether this invocation executed the case, reused a saved execution, or obtained no result."
PREVIEW_EXPECTATION_DESCRIPTION = "Comparison against the caller's explicit expectation; omitted expectation, including a single observation, is not_checked."
PREVIEW_SAVED_REF_DESCRIPTION = "Exact saved geometry for Query or new pictures. Later draft edits do not change this model."
PREVIEW_COMPLETION_DESCRIPTION = "Batch completion; an interrupted report preserves available cases but is not a wholly passing batch."
PREVIEW_CHECKS_DESCRIPTION = "Whether this report's requested cases completed and passed explicit expectations without storage or cross-case failures or reported gaps. Rendering is separate; this does not prove the entire support domain."
PREVIEW_IMAGE_KEYS_DESCRIPTION = "Case keys corresponding to actual returned ImageContent blocks, in order."
PREVIEW_COVERAGE_DESCRIPTION = "Unchecked observations or incomplete saved-case recovery in this report; no publication representatives are required."
PREVIEW_OBSERVATION_BOUNDARY = (
    " A valid shape, SOLVED, an overall image or interface-frame checks do not prove hollow material, "
    "clearance or physical connectivity. Inspect risky joints/bends/transitions with local views and "
    "actual sections, including nearby planes if ambiguous. An unverified requirement cannot be closed "
    "by merely adding a limitation note."
)
PREVIEW_RESULT_GUIDANCE = (
    "Read the actual response first; result guides are optional explanations, not prerequisites. "
    "The first TextContent contains JSON; subsequent ImageContent blocks contain actual images, "
    "not model files. *_json fields are JSON strings to decode when their contents are needed. "
    "This guide explains shared meanings; it is not a complete output Schema. "
    "execution_status describes completed, technical_error or not_run; execution_source distinguishes "
    "executed, reused or none. Neither is the domain status BUILT/REJECTED/SOLVED/AMBIGUOUS/INFEASIBLE. "
    "expectation_check is pass/fail/not_checked; no expectation means not_checked, including single's "
    "observation. A business rejection can pass its explicit expected reason, but an exception, timeout "
    "or invalid program cannot be relabeled business rejection. coverage_gaps describes unchecked observations "
    "or incomplete saved-case recovery in this report. A focused batch can pass without successful, nominal "
    "or normal-construction representatives; its checks do not prove the entire support domain. "
    "Report only the invocations, saved targets, interfaces and external conditions actually checked, "
    "and name any subsequent Query's object and coordinate space. Unperformed entity or connectivity "
    "checks remain unverified; do not infer them from SOLVED, a frame check or an image. "
    "completion=interrupted preserves available completed cases; explicitly reuse applicable completed "
    "cases in a new report, never treat the interrupted report or not_run cases as wholly passed. "
    "render_status is not_requested/rendered/failed/no_geometry and is separate from execution and "
    "expectation. A failed picture leaves its retained inspection_ref available for Query. "
    "A runtime timeout does not establish invalid geometry: keep the saved model and address the reported execution limit; "
    "do not change Geometry or require Query solely because rendering timed out. Follow the "
    "reported cause: confirmed boundary crossing requires inspection and an authoring correction, "
    "not an unchanged redraw. Construction success alone does not make that result acceptable. "
    "rendered_case_keys identifies the actual returned pictures in order, not every requested case. "
    "After draft edits an inspection_ref still addresses its original saved model, never the new draft. "
    "Use that inspection_ref as a Geometry Query inspection target or Geometry Preview inspection "
    "source; an operation_alias locates the saved report through operation_get when needed. "
    "A report may retain rejected cases without any model. A one-time unsaved build supplies no "
    "new persistent model reference. Preview does not publish a resource or install a product. "
    "diagnostics identifies the failed phase, reason and available correction; preserve successful "
    "cases instead of repeating the whole batch. Display warnings or clipping limit what the image "
    "can establish even if the entity was built successfully. "
    "Family frame checks report independent position, direction, roll and profile facts; "
    "external_actual_connection_status remains NOT_CHECKED because Preview does not install IFC connections. "
    "Family observed_interface constraints require evidence_reference supplied by the caller as provenance; "
    "that text is not automatic external-object lookup, authorization or verified physical connection."
)

# Human-facing output guidance only. Exact result validation stays with the DTOs;
# author-written program return contracts remain precise in their own API pages.
PREVIEW_RESULT_DOCUMENTS = {
    "ifc_geometry_preview": {
        "purpose": "Interpret this Geometry Preview response; inspect its actual fields rather than constructing a result object.",
        "shared_semantics": "cad2bim://contracts/common/preview#preview_results",
        "single": {
            "meaning": "Observes one Geometry build. Read the actual geometry/interface facts and diagnostics; this mode does not assert an expected outcome.",
            "inspection_ref": "When retained, identifies this exact saved model for later Query or observation.",
        },
        "cases": {
            "shape": "result_kind identifies a batch; cases is an array keyed by case_key. Read each card before interpreting checks_passed.",
            "status": "BUILT means construction succeeded; REJECTED means a domain rejection. Execution, expectation and render states are separate; see shared_semantics.",
        },
        "saved_observation": {
            "mode": "inspection reads an existing inspection_ref; family_standard_model reads the published family_version_alias's saved standard model. Neither reruns its author program.",
            "coordinates": "result_coordinate_space and coordinate_space identify the frame. Respect returned units; section fields ending _m or _m2 are metres or square metres.",
            "model": "observation_kind=model provides image information; display_quality_warnings and panels describe image reliability and clipping.",
            "connection_context": "image.connection_context gives the pipe segments actually displayed; connection_context_diagnostics explains skipped automatic context. These are display facts and do not add geometry or prove a physical connection.",
            "section": "measurements contains section_index, offset_m, plane, status, objects and material areas. objects separates participating solids/fragments; material_regions and boundaries describe their actual section material, not a filled outer silhouette. curves and intersection points describe the cut.",
            "images": "Match section images by section_index. render_errors and diagnostics can coexist with valid measurements; missing pictures do not imply an empty section.",
        },
        "next_action": "Query the saved model for unmet requirements, or observe it again with another view/section. For batch state, image ordering and reuse limits use shared_semantics; no recursive output-type reading is required.",
    },
    "ifc_product_realization_preview": {
        "purpose": "Interpret this Family Preview response; the first JSON object's result holds either one solve outcome or a case batch. This is a field guide, not an output object to submit.",
        "shared_semantics": "cad2bim://contracts/common/preview#preview_results",
        "single": {
            "status": "SOLVED identifies a numerical solution. Declared interface constraints are then evaluated; a normal FAIL retains numerical facts and diagnostics without geometry. Construction and a retained inspection identify later completed work. AMBIGUOUS supplies choices requiring resolution; INFEASIBLE supplies violations of the declared conditions.",
            "inputs": "source_ref identifies the evaluated Family source; attachment_kind distinguishes self_geometry from standard_model. normalized_public_parameters is the effective public input, not another request to construct.",
            "parameters": "internal_parameters_json contains solved quantities; fragments describes selected bindings and geometry parameters. instance_receipts records the actual normalized parameters and geometry summaries used to build each fragment.",
            "placement": "Geometry numerical interfaces follow the Family component/fragment transforms to produce interface_project_world_frames. frame_check_status evaluates only declared Family internal relations and this invocation's external conditions; frame_diagnostics and external_condition_checks give the actual residuals. A finite_segment limits the selected station's feasible domain. Current adjacent objects, their endpoints and physical contact require a separate installation comparison.",
            "saved_model": "inspection_ref, when present, identifies the exact saved realization. Use Geometry Query or Geometry Preview inspection with that reference; rendering and retention semantics are shared.",
            "image": "presentation and interface_overlays explain the returned picture and markers. A SOLVED retained model may instead report render_status=failed with render diagnostics and no image.",
            "connection_context": "presentation.connection_context gives displayed pipe segments and connection_context_diagnostics explains skipped context. auto uses the same checked installation targets as solving; it does not create or verify an external object.",
        },
        "cases": {
            "shape": "result_kind identifies a batch; cases is an array of cards keyed by case_key, using SOLVED/AMBIGUOUS/INFEASIBLE domain statuses. The report does not repeat every full single-solve object.",
            "navigation": "For completion, checks_passed, execution_status, expectation_check, image ordering and exact reuse, read shared_semantics. Use a successful card's inspection_ref for further model work.",
        },
        "boundary": "Preview solving does not publish the Family or apply a product. Interface-frame agreement is not proof of physical connection; follow the shared inspection guidance for material and clearance requirements.",
    },
}

FINITE_VALIDATION_GUIDANCE = (
    "Preview covers only submitted or explicitly reused cases, not the full declared support domain. "
    "Choose finite representatives of required branches, boundaries and expected rejections; technical "
    "failures are not business rejections. BUILT or SOLVED alone does not prove material or connectivity. "
    "Case selection, construction and repair methods are covered by the optional "
    "cad2bim-component-authoring Skill; the call and reuse contracts remain on this page."
)

GEOMETRY_INSPECTION_COMPLETION_GUIDANCE = (
    "An overall image, a valid shape or successful interface-frame checks do not establish material, "
    "clearance or physical connectivity. Saved-body Query and observation verify only their measured "
    "scope; a few normal sections do not prove an entire flow path. Identify any unverified required "
    "property and missing capability instead of declaring completion."
)

SAVED_PREVIEW_OBSERVATION_USAGE = (
    "If only the original Operation is known, use operation_get with case_key to obtain its inspection_ref, "
    "then use ifc_geometry_preview mode=inspection to observe the same saved solid with another view or "
    "measured section, without another build or solve. An already known inspection_ref can be observed directly."
)
