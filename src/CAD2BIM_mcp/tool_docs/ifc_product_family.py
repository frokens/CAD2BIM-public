"""Provider-visible usage text for IFC ProductFamily tools."""

DIRECT_IFC_PRODUCT_FAMILY_SCOPED_INPUT_DESCRIPTION = (
    "Direct entry adds its Session scope to the shared public contract."
)
IFC_PRODUCT_FAMILY_REJECTION_DESCRIPTION = (
    "Scoped identity and validation issues for a record excluded from discovery."
)
DIRECT_IFC_PRODUCT_REALIZATION_SCOPED_SINGLE_INPUT_DESCRIPTION = (
    "Direct scope on the shared public document contract."
)
DIRECT_IFC_PRODUCT_REALIZATION_SCOPED_CASES_INPUT_DESCRIPTION = (
    "Direct scope on the shared public case document contract."
)
PRODUCT_REALIZATION_EXTERNAL_TARGET_COMPARISON_DESCRIPTION = (
    "Derived from actual external_condition_checks. NOT_COMPARED means no external target comparison was completed; frame PASS alone does not establish external attachment or physical contact."
)

from CAD2BIM_mcp.tool_docs.common import runtime_budget_description as _runtime_budget_description
from CAD2BIM_backend.domain.runtime_execution import (
    PRODUCT_PREVIEW_EXECUTION as _PRODUCT_PREVIEW_EXECUTION,
)
from CAD2BIM_backend.domain.product_realization import (
    MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS as _SOURCE_MEMBERS,
    MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES as _SOURCE_MEMBER_BYTES,
    MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES as _SOURCE_TOTAL_BYTES,
    PRODUCT_REALIZATION_WALL_SECONDS as _CASE_WALL_SECONDS,
    PRODUCT_REALIZATION_CPU_SECONDS as _CASE_CPU_SECONDS,
)

import json
from CAD2BIM_mcp.tool_docs.preview import (
    PREVIEW_OBSERVATION_BOUNDARY,
    PREVIEW_RETAIN_DESCRIPTION,
)

from CAD2BIM_mcp.tool_docs.common import (
    READ_BATCH_DESCRIPTION,
    PRODUCT_SUPPORT_SCOPE_NAVIGATION,
    VERSION_USAGE_INSTRUCTIONS_CREATE_DESCRIPTION as VERSION_USAGE_INSTRUCTIONS_CREATE_DESCRIPTION,
    VERSION_USAGE_INSTRUCTIONS_MODIFY_DESCRIPTION as VERSION_USAGE_INSTRUCTIONS_MODIFY_DESCRIPTION,
    VERSION_USAGE_INSTRUCTIONS_INFO_DESCRIPTION as VERSION_USAGE_INSTRUCTIONS_INFO_DESCRIPTION,
)


PRODUCT_PREVIEW_SOLIDIFY_DESCRIPTION = PREVIEW_RETAIN_DESCRIPTION
PRODUCT_FRAGMENT_BINDING_DESCRIPTION = "The exact declared Geometry binding selected for this fragment by the Family Program."
PRODUCT_PREVIEW_INSPECTION_REF_DESCRIPTION = "Exact retained build evidence for Geometry Preview and Query; copy unchanged. It grants no product-write or acceptance authority."
PRODUCT_FAMILY_DRAFT_DEFINITION_DESCRIPTION = (
    "Once-encoded JSON: action=set with value containing the complete family_contract and geometry_bindings; "
    "update also accepts action=edit with business-key set/remove collections. Omit to inherit. Each binding "
    "selects an exact published Geometry version or Geometry draft. Read create_definition_input_schema or "
    "update_definition_input_schema at cad2bim://contracts/ifc_product_family_write/definition_json."
)
PRODUCT_FAMILY_DRAFT_SOURCE_CHANGES_DESCRIPTION = (
    "One closed JSON object with changes for atomic add_member, literal_edit, replace_member or "
    "delete_member changes. Read current text with Code; expected_edit_token protects the draft generation. Omit to inherit source. This does not "
    "replace the definition. See cad2bim://contracts/ifc_product_family_write/source_changes_json."
)
PRODUCT_FAMILY_DRAFT_SOURCE_FILES_DESCRIPTION = (
    f"Complete Python bundle with 1–{_SOURCE_MEMBERS} uniquely named relative .py members, including main.py. "
    f"At most {_SOURCE_MEMBER_BYTES:,} UTF-8 bytes per member and {_SOURCE_TOTAL_BYTES:,} UTF-8 bytes in total. "
    "main.py supplies realize(ctx) and standard_model(ctx); Write saves source, Preview executes instances, and Publish constructs the declared formal standard model."
)

PRODUCT_FAMILY_GEOMETRY_SOURCE_EXAMPLE = """import cadquery as cq

def build(ctx):
    length = ctx.param("length_m")
    diameter = ctx.param("diameter_m")
    inner_diameter = ctx.param("inner_diameter_m")
    return cq.Workplane("XY").circle(diameter / 2).circle(inner_diameter / 2).extrude(length)
"""
PRODUCT_FAMILY_GEOMETRY_INTERFACES_SOURCE_EXAMPLE = """def evaluate(ctx):
    length = ctx.param("length_m")
    diameter = ctx.param("diameter_m")
    ctx.define_interface(key="datum", origin_m=(0, 0, 0), axis=(0, 0, 1),
                         reference_direction=(1, 0, 0))
    ctx.define_interface(key="inlet", origin_m=(0, 0, 0), axis=(0, 0, -1),
                         reference_direction=(1, 0, 0),
                         profile={"kind": "circular", "diameter_m": diameter})
    ctx.define_interface(key="outlet", origin_m=(0, 0, length), axis=(0, 0, 1),
                         reference_direction=(1, 0, 0),
                         profile={"kind": "circular", "diameter_m": diameter})
"""
PRODUCT_FAMILY_GEOMETRY_PARAMETER_SCHEMA_EXAMPLE = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "length_m": {"type": "number", "exclusiveMinimum": 0.0, "maximum": 10.0},
        "diameter_m": {"type": "number", "exclusiveMinimum": 0.0, "maximum": 1.0},
        "inner_diameter_m": {"type": "number", "exclusiveMinimum": 0.0, "maximum": 1.0},
    },
    "required": ["length_m", "diameter_m", "inner_diameter_m"],
}
PRODUCT_FAMILY_GEOMETRY_VALIDATION_SAMPLE_EXAMPLE = {"length_m": 0.1, "diameter_m": 0.05, "inner_diameter_m": 0.04}
PRODUCT_FAMILY_GEOMETRY_CREATE_EXAMPLE = json.dumps({
    "direct_session_alias": "direct_001", "mode": "create",
    "display_name": "Hollow section with actual end interfaces",
    "usage_instructions": {"action": "set", "value": (
        "Illustrative straight hollow section. Changing length moves the outlet along local +Z; outer diameter controls both circular end profiles, while inner diameter controls the modeled hollow bore. These dimensions have no catalog-standard basis.\n\n"
        "The local datum is at the inlet, and the end interfaces point outwards in opposite axial directions. These interfaces describe the section itself; external connection has not been validated."
    )},
    "source": {"source_kind": "modeling_function",
               "source_files": [{"source_path": "main.py", "source": PRODUCT_FAMILY_GEOMETRY_SOURCE_EXAMPLE}, {"source_path": "interfaces.py", "source": PRODUCT_FAMILY_GEOMETRY_INTERFACES_SOURCE_EXAMPLE}],
               "parameter_schema_json": json.dumps({"action": "set", "value": PRODUCT_FAMILY_GEOMETRY_PARAMETER_SCHEMA_EXAMPLE})},
}, separators=(",", ":"))

PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE = """def realize(ctx):
    conditions = ctx.external_constraints()
    diameter = conditions[0]["condition"]["outer_diameter_m"] if conditions else 0.05
    return build_body(ctx, diameter)

def standard_model(ctx):
    # This explicit nominal dimension describes the standard model only.
    return build_body(ctx, 0.05)

def build_body(ctx, diameter):
    body_length, tail_length = ctx.param("segment_lengths_m")
    # The Family returns only a complete numeric solution. Backend builds both
    # exact Geometry fragments and derives their actual interfaces afterwards.
    tail_pose = {"translation_m": [0, 0, body_length]}
    return {
        "status": "SOLVED",
        "internal_parameters": {
            "body_length_m": body_length,
            "tail_length_m": tail_length,
            "diameter_m": diameter,
            "inner_diameter_m": ctx.param("section")["inner_diameter_m"],
        },
        "components": [{"component_key": "body"}, {"component_key": "tail", "local_transform": tail_pose}],
        "fragments": [
            {"fragment_key": "body_shape", "binding_key": "body_geometry",
             "local_transform": {}, "uniform_scale": 1.0},
            {"fragment_key": "tail_shape", "binding_key": "body_geometry",
             "local_transform": {}, "uniform_scale": 1.0},
        ],
    }
"""
PRODUCT_REALIZATION_EXTERNAL_CONDITION_EXAMPLE = {
    "kind": "fixed_point",
    "point_m": [0, 0, 0.3],
    "axis": [0, 0, -1],
    "direction_relation": "opposed",
    "outer_diameter_m": 0.05,
}
PRODUCT_REALIZATION_EXTERNAL_CONSTRAINT_EXAMPLE = {
    "interface_key": "outlet",
    "source_kind": "design_target",
    "condition": PRODUCT_REALIZATION_EXTERNAL_CONDITION_EXAMPLE,
}
PRODUCT_FAMILY_DEFINITION_DOCUMENT_EXAMPLE = {
    "family_contract": {
        "product_structure_kind": "semantic_assembly",
        "standard_model": {},
        "public_parameters": [
            {
                "parameter_key": "segment_lengths_m", "parameter_kind": "number_array",
                "length": 2, "unit": "metre", "minimum": 0.02, "maximum": 2.0,
            },
            {
                "parameter_key": "section", "parameter_kind": "object",
                "properties": [{
                    "parameter_key": "inner_diameter_m", "parameter_kind": "number",
                    "unit": "metre", "minimum": 0.01, "maximum": 1.0,
                }],
            },
        ],
        "solved_parameters": [
            {"parameter_key": "body_length_m", "parameter_kind": "number", "unit": "metre", "minimum": 0.02, "maximum": 2.0},
            {"parameter_key": "tail_length_m", "parameter_kind": "number", "unit": "metre", "minimum": 0.02, "maximum": 2.0},
            {"parameter_key": "diameter_m", "parameter_kind": "number", "unit": "metre", "minimum": 0.01, "maximum": 1.0},
            {"parameter_key": "inner_diameter_m", "parameter_kind": "number", "unit": "metre", "minimum": 0.01, "maximum": 1.0},
        ],
        "components": [
            {"component_key": "body", "ifc_class": "IfcPipeSegment"},
            {"component_key": "tail", "ifc_class": "IfcPipeFitting", "parent_component_key": "body",
             "local_transform_mode": "program"},
        ],
        "geometry_fragments": [{
            "fragment_key": "body_shape", "owner_component_key": "body", "binding_options": [{
                "binding_key": "body_geometry", "parameter_sources": {"default_source": "solved_parameter", "overrides": {
                "length_m": {"source_kind": "solved_parameter", "parameter_key": "body_length_m"},
                }},
            }],
        }, {
            "fragment_key": "tail_shape", "owner_component_key": "tail", "binding_options": [{
                "binding_key": "body_geometry", "parameter_sources": {"default_source": "solved_parameter", "overrides": {
                "length_m": {"source_kind": "solved_parameter", "parameter_key": "tail_length_m"},
                }},
            }],
        }],
        "interfaces": [{
            "interface_key": "inlet", "owner_component_key": "body", "interface_kind": "external",
            "source": {"source_kind": "model_interface", "fragment_key": "body_shape", "model_interface_key": "inlet"},
        }, {
            "interface_key": "body_joint", "owner_component_key": "body", "interface_kind": "internal",
            "source": {"source_kind": "model_interface", "fragment_key": "body_shape", "model_interface_key": "outlet"},
        }, {
            "interface_key": "tail_joint", "owner_component_key": "tail", "interface_kind": "internal",
            "source": {"source_kind": "model_interface", "fragment_key": "tail_shape", "model_interface_key": "inlet"},
        }, {
            "interface_key": "outlet", "owner_component_key": "tail", "interface_kind": "external",
            "source": {"source_kind": "model_interface", "fragment_key": "tail_shape", "model_interface_key": "outlet"},
            "external_constraint": {
                "required": False,
                "allowed_kinds": ["fixed_point"],
                "allowed_directions": ["opposed"],
                "profile_required": True,
                "maximum_position_tolerance_m": 0.000001,
                "maximum_angular_tolerance_rad": 0.000001,
            },
        }],
        "constraints": [{
            "constraint_key": "body_tail_joint", "constraint_kind": "interface_coincidence",
            "interface_a_key": "body_joint", "interface_b_key": "tail_joint",
            "position_tolerance_m": 0.000001, "angular_tolerance_rad": 0.000001,
            "frame_relation": {"direction_relation": "opposed", "orientation_requirement": "full_frame", "profile_requirement": "equal"},
        }],
    },
    "geometry_bindings": [{
        "binding_key": "body_geometry", "source": {"source_kind": "published", "resource_version_alias": "cur.geometry_version_ref_001"},
        "expected_resource_kind": "modeling_function", "geometry_purpose": "product_component",
    }],
}
PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE = [
        {"case_kind": "family", "case_key": "normal", "expectation": {"expected_status": "SOLVED"},
         "invocation": {"realization_kind": "self_geometry",
         "product_origin": [0, 0, 0], "length_unit": {"name": "METRE"},
         "public_parameters": {"segment_lengths_m": [0.1, 0.2], "section": {"inner_diameter_m": 0.04}},
         "external_constraints": [PRODUCT_REALIZATION_EXTERNAL_CONSTRAINT_EXAMPLE]}},
        {"case_kind": "family", "case_key": "different_dimensions", "expectation": {"expected_status": "SOLVED"},
         "invocation": {"realization_kind": "self_geometry",
         "product_origin": [0, 0, 0], "length_unit": {"name": "METRE"},
         "public_parameters": {"segment_lengths_m": [0.4, 0.5], "section": {"inner_diameter_m": 0.03}}}},
        {"case_kind": "family", "case_key": "standard_nominal", "expectation": {"expected_status": "SOLVED"},
         "invocation": {"realization_kind": "standard_model",
         "product_origin": [0, 0, 0], "length_unit": {"name": "METRE"},
         "public_parameters": {"segment_lengths_m": [0.1, 0.2], "section": {"inner_diameter_m": 0.04}}}},
        {"case_kind": "family", "case_key": "standard_placed", "expectation": {"expected_status": "SOLVED"},
         "invocation": {"realization_kind": "standard_model",
         "product_origin": [2000, 3000, 4000], "length_unit": {"name": "METRE", "prefix": "MILLI"},
         "public_parameters": {"segment_lengths_m": [0.1, 0.2], "section": {"inner_diameter_m": 0.04}}}},
    ]

PRODUCT_FAMILY_CREATE_EXAMPLE = json.dumps({
    "direct_session_alias": "direct_001", "mode": "create", "display_name": "Variable section assembly Family",
    "purpose": "Reuse one construction rule with structured segment lengths, a structured section and independently checked joints",
    "usage_instructions": {"action": "set", "value": (
        "Illustrative two-component hollow-section assembly along local +Z. The first segment length moves the body outlet and tail; the second changes the tail length and final outlet. section.inner_diameter_m controls the modeled bore only. An installation condition supplies the outer diameter once; the standard model and an unconstrained modeling observation use the explicit nominal outer diameter 0.05 m.\n\n"
        "The body and tail meet at opposed internal joint frames; the inlet and final outlet are the assembly's external interfaces. Their parameter-derived frames come from the independent evaluators. External connection has not been validated."
    )},
    "source": {"source_kind": "inline",
               "definition_json": json.dumps({"action": "set", "value": PRODUCT_FAMILY_DEFINITION_DOCUMENT_EXAMPLE}, separators=(",", ":")),
               "source_files": [{"source_path": "main.py", "source": PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE}]},
}, separators=(",", ":"))
PRODUCT_FAMILY_WRITE_TUTORIAL = {
    "usage": (
        "Requests below are tool arguments, not program returns. Replace ${...} with current-scope references "
        "or your text; expected_edit_token=1 illustrates the initial draft, so use the latest Write/Info token. "
        "create copies an exact published Family; create_inline supplies a complete illustrative two-component "
        "Family, not an engineering catalog standard. Replace its Geometry alias with a resource accepting "
        "length_m/diameter_m and exposing datum/inlet/outlet interfaces as used by the definition. A draft binding "
        "instead uses source={source_kind:'draft',draft_ref:<Geometry Write result>}. Discover the resource and "
        "read its contract before binding it. Each *_json field encodes its complete document once; arrays and "
        "objects inside remain native JSON values. Usage writing: cad2bim://contracts/common/usage#version_usage. "
        "Execution/publication rules: cad2bim://contracts/common/authoring. For this Family's observation and "
        "checked-case examples read cad2bim://contracts/ifc_product_realization_preview/tutorial."
    ),
    "create": {"direct_session_alias": "${DIRECT_SESSION_ALIAS}", "mode": "create",
               "source": {"source_kind": "published", "family_version_alias": "${CURRENT_FAMILY_VERSION_ALIAS}"}},
    "update": {"direct_session_alias": "${DIRECT_SESSION_ALIAS}", "mode": "update",
               "draft_ref": "${DRAFT_REF}", "expected_edit_token": 1,
               "usage_instructions": {"action": "edit", "edits": [{
                   "old_text": "Actual frames come from each build.",
                   "new_text": "The section diameter is limited to 0.5 metres. Actual frames come from each build."}]},
               "definition_json": json.dumps({"action": "edit", "family_contract": {"public_parameters": {"set": [{
                   "parameter_key": "section", "parameter_kind": "object", "properties": [{
                       "parameter_key": "diameter_m", "parameter_kind": "number", "unit": "metre", "minimum": 0.01, "maximum": 0.5}]}]}}})},
    "update_binding": {"direct_session_alias": "${DIRECT_SESSION_ALIAS}", "mode": "update",
               "draft_ref": "${DRAFT_REF}", "expected_edit_token": 1,
               "definition_json": json.dumps({"action": "edit", "geometry_bindings": {"set": [{
                   "binding_key": "body_geometry", "source": {"source_kind": "published", "resource_version_alias": "${CURRENT_GEOMETRY_VERSION_ALIAS}"},
                   "expected_resource_kind": "modeling_function", "geometry_purpose": "product_component"}]}})},
    "create_inline": json.loads(PRODUCT_FAMILY_CREATE_EXAMPLE),
}
PRODUCT_FAMILY_WRITE_TUTORIAL_RESOURCE_DESCRIPTION = (
    "Complete create/create_inline, binding-only update and joint usage/parameter update requests. Read usage for placeholder substitution and the "
    "inline Family's Geometry requirements. Preview and publication examples have their own Preview tutorial."
)


def _build_product_realization_preview_tutorial() -> dict[str, object]:
    invocation = {
        "public_parameters": PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[0]["invocation"]["public_parameters"],
        "product_origin": [0, 0, 0], "length_unit": {"name": "METRE"},
    }
    single = {
        "direct_session_alias": "${DIRECT_SESSION_ALIAS}", "mode": "single",
        "source": {"source_kind": "draft", "draft_ref": "${DRAFT_REF}"},
        "attachment_kind": "self_geometry", "invocation_json": json.dumps(invocation), "retain": True,
    }
    observed_case = {
        "case_kind": "family", "case_key": "normal", "expectation": {"expected_status": "SOLVED"},
        "invocation": {"realization_kind": "self_geometry",
                       **{key: value for key, value in invocation.items() if key != "schema"}},
        "reuse": {"reuse_kind": "case", "operation_alias": "${SINGLE_OPERATION_ALIAS}", "case_key": "observation"},
    }
    observed_cases = {
        "direct_session_alias": "${DIRECT_SESSION_ALIAS}", "mode": "cases", "source": single["source"],
        "cases_json": json.dumps({"mode": "inline", "items": [observed_case, *PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[2:]]}),
        "nominal_scenario_key": "standard_nominal", "render_json": json.dumps({"mode": "none"}), "retain": True,
    }
    return {
        "usage": (
            "These complete Direct tool requests use the illustrative Family from "
            "cad2bim://contracts/ifc_product_family_write/tutorial; another Family requires its own parameters "
            "and conditions. Replace every ${...} reference from actual responses. Each *_json value encodes "
            "one complete document. Lifecycle, applicability and publication closure rules: "
            "cad2bim://contracts/common/authoring. Images, actual-shape checks and result interpretation: "
            "cad2bim://contracts/common/preview."
        ),
        "preview_single": single,
        "input_purpose_examples": {
            "pure_point_target": {"interface_key": "datum", "condition": {
                "kind": "fixed_point", "point_m": [1, 2, 3], "direction_relation": "none"}},
            "directed_circular_target": PRODUCT_REALIZATION_EXTERNAL_CONSTRAINT_EXAMPLE,
            "computation_only": {
                "parameter_definition": {"parameter_key": "layout_input", "parameter_kind": "object", "properties": [{
                    "parameter_key": "offset_m", "parameter_kind": "number", "unit": "metre", "minimum": -10, "maximum": 10}]},
                "public_parameters": {"layout_input": {"offset_m": 0.2}},
                "external_constraints": [],
                "purpose": "Caller-confirmed layout offset used in computation; no independent external interface target is supplied.",
            },
        },
        "preview_saved_observation": observed_cases,
        "alternative_reuse": {"reuse_kind": "inspection", "inspection_ref": "${SINGLE_INSPECTION_REF}"},
        "preview_saved_inputs": {
            "direct_session_alias": "${DIRECT_SESSION_ALIAS}", "mode": "cases", "source": single["source"],
            "cases_json": json.dumps({"mode": "from_operation", "operation_alias": "${PREVIEW_OPERATION_ALIAS}",
                                      "changes": [{"change_kind": "reuse", "case_key": key}
                                          for key in ("normal", "standard_nominal", "standard_placed")]}),
            "nominal_scenario_key": "standard_nominal", "render_json": json.dumps({"mode": "none"}), "retain": True,
        },
        "publish": {
            "direct_session_alias": "${DIRECT_SESSION_ALIAS}", "root_kind": "family", "draft_ref": "${DRAFT_REF}",
            "expected_edit_token": 1,
            "preview_examples": [{"operation_alias": "${CHECKED_OPERATION_ALIAS}",
                          "case_keys": ["normal", "standard_nominal", "standard_placed"]}],
        },
        "example_sequence": (
            "Run preview_single, then put its Operation into SINGLE_OPERATION_ALIAS for preview_saved_observation. "
            "Alternatively replace only the decoded normal case's reuse object with alternative_reuse using the "
            "single's inspection_ref; encode the complete cases document again without changing the invocation. "
            "The same request can freshly execute all three cases by removing normal.reuse. standard_nominal and "
            "standard_placed intentionally have no reuse and execute the additional standard coverage. Put the new "
            "report into CHECKED_OPERATION_ALIAS only to attach optional preview_examples at Publish; omit that "
            "field to publish independently. Use the current edit token. "
            "For a standard single, replace attachment_kind with standard_model; this example has no external "
            "conditions to remove. To render cases, replace render_json with "
            "{\"mode\":\"selected\",\"case_keys\":[\"standard_nominal\"]} encoded once."
        ),
        "saved_input_changes": (
            "preview_saved_inputs uses the prior checked report as PREVIEW_OPERATION_ALIAS. from_operation inherits "
            "inputs and expectations, clearing old reuse fields. The three reuse changes select saved results "
            "without repeating their invocations. Unmarked cases execute; delete cases not needed now. To change "
            "an input or expectation, replace that complete case; applicability rules are in cad2bim://contracts/common/authoring#reuse."
        ),
        "publication_requirements": "cad2bim://contracts/common/authoring#family_publication",
    }


PRODUCT_REALIZATION_PREVIEW_TUTORIAL_RESOURCE_DESCRIPTION = (
    "Complete Family single/cases and Publish requests; example_sequence explains a retained observation, "
    "explicit result reuse and the alternative reuse object. saved_input_changes reuses inherited cases by key. "
    "Publication requirements: cad2bim://contracts/common/authoring#family_publication."
)
DIRECT_IFC_PRODUCT_REALIZATION_SOLVE_PROVIDER_INPUT_DESCRIPTION = "Closed transport converted to the typed preview input."
IFC_PRODUCT_REALIZATION_PREVIEW_RESULT_DESCRIPTION = (
    "Closed status-discriminated result in the first TextContent JSON result object."
)
PRODUCT_REALIZATION_STRUCTURED_PARAMETER_RESULT_DESCRIPTION = (
    "Canonical JSON array or object of normalized exact Family parameters; decode before passing as a typed value."
)
IFC_PRODUCT_REALIZATION_FRAGMENT_RESULT_GEOMETRY_PARAMETERS_JSON_DESCRIPTION = (
    "Canonical JSON object containing the complete Backend-validated Geometry parameters selected by the numeric Family solution."
)
PRODUCT_FAMILY_SOURCE_CHANGES_DOCUMENT_EXAMPLE = {
    "changes": [{
        "change_kind": "literal_edit",
        "source_path": "main.py",
        "replacements": [{"old_text": "BROKEN_TOKEN", "new_text": "WORKING_TOKEN"}],
    }],
}
PRODUCT_FAMILY_SOURCE_CHANGES_EXAMPLE = json.dumps(
    PRODUCT_FAMILY_SOURCE_CHANGES_DOCUMENT_EXAMPLE,
    separators=(",", ":"),
)
PRODUCT_FAMILY_DEFINITION_RESOURCE_DESCRIPTION = (
    "Read create_definition_input_schema for action=set/value creation, or update_definition_input_schema for "
    "set/edit updates. value contains family_contract and geometry_bindings. Edit collections use complete item "
    "DTOs in set and existing business keys in remove: parameter_key, component_key, fragment_key, interface_key, "
    "constraint_key and binding_key. Existing keys retain order; new keys append. Duplicate keys, set/remove "
    "conflicts and missing removals fail. Submit related changes together; complete merged references are checked. "
    "standard_model uses action=set/value or action=clear; product_structure_kind is a new scalar. Omit fields "
    "to inherit. Preview cases and nominal_scenario_key belong to Preview. The complete "
    "create_inline request is in cad2bim://contracts/ifc_product_family_write/tutorial. Follow shared referenced "
    "types only when unfamiliar or changed; retain their complete constraints. Dependency publication rules: "
    "cad2bim://contracts/common/authoring."
)
PRODUCT_FAMILY_SOURCE_CHANGES_RESOURCE_DESCRIPTION = (
    "Read for source_changes_json in mode=update: the closed root object contains changes. "
    "Choose add_member, literal_edit, replace_member or delete_member; apply all changes atomically to the current "
    "draft. literal_edit uses exact text from Code; replace_member/delete_member use that member's current digest. "
    "Omit source_changes_json to inherit the source. This changes source only; it is not a Preview cases document."
)
PRODUCT_FAMILY_PROGRAM_RESOURCE_DESCRIPTION = (
    "For writing Family programs or checking unfamiliar API/input/output behavior; unnecessary for "
    "metadata-only changes or ordinary instance use. Family program_api JSON. Read usage for realize(ctx), standard_model(ctx), and the complete "
    "pure-numeric context API, including Family-owned representation decisions. Each fragment selects "
    "one declared binding_options entry through the proposal's binding_key; the Backend constructs only "
    "that selection. Read the alternative-representation failure boundaries. "
    "return_schema is the proposal returned by either program entry, not an "
    "ifc_product_realization_preview result wrapper or reference solution. Geometry interfaces are "
    "Backend results and are deliberately absent from the author API."
)
PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_RESOURCE_DESCRIPTION = (
    "Read for mode=single with attachment_kind=self_geometry. Complete decoded invocation_json "
    "Schema only for attachment_kind=self_geometry. Begin at root "
    "public_parameters, product_origin, length_unit, and external_constraints, then follow "
    "$ref values into local $defs or cad2bim://contracts/common/types for the parameter JSON values and independent conditions. "
    "Declared public_parameters supplies computation data; external_constraints independently asserts bound output "
    "interface relations. Source retained_direction/pipe_outward do not define the product interface axis; "
    "take each direction role from its source and exact interface contract."
)
PRODUCT_REALIZATION_STANDARD_MODEL_INVOCATION_RESOURCE_DESCRIPTION = (
    "Read for mode=single with attachment_kind=standard_model. Complete decoded invocation_json "
    "Schema only for attachment_kind=standard_model. Begin at root "
    "public_parameters, required product_origin and required length_unit, then follow $ref "
    "values into local $defs or cad2bim://contracts/common/types. This branch has no external_constraints field."
)
PRODUCT_REALIZATION_CASES_RESOURCE_DESCRIPTION = (
    "Read for mode=cases. The decoded cases_json root selects mode=inline with items or mode=from_operation "
    "with operation_alias and optional add/replace/delete/reuse changes. reuse takes only case_key. At most 16 named Family cases; each invocation "
    "uses realization_kind, public_parameters, product_origin, length_unit and self_geometry-only external_constraints. "
    "The resource usage explains per-case execution limits. "
    "This is not the single invocation_json wrapper: no nested schema field or attachment_kind. expectation is "
    "optional for exploration; negative expectations require stable reason_keys. nominal_scenario_key is a tool "
    "argument outside cases_json. examples shows one case with an external condition, not complete branch coverage. "
    "Complete request and reuse variants: cad2bim://contracts/ifc_product_realization_preview/tutorial."
)
PRODUCT_REALIZATION_CASES_BUDGET_USAGE = (
    f"Each Family case has a {_CASE_WALL_SECONDS:g}-second wall limit and {_CASE_CPU_SECONDS:g}-second "
    "CPU limit, distinct from batch process/runtime limits. CPU includes Geometry construction and "
    "accumulates across native threads. Reducing the batch size does not raise a case limit; "
    "interrupted cases and remaining not_run items are not successful checks."
)
PRODUCT_REALIZATION_RENDER_RESOURCE_DESCRIPTION = (
    "Read for mode=cases render_json: decoded root mode=none or mode=selected with case_keys and optional profile. "
    "Omission means no case rendering, independently of retain. Profile and returned-image interpretation: "
    "cad2bim://contracts/common/preview."
)
PRODUCT_FAMILY_PARAMETER_RULES = (
    "Use number_array with exact length 0 to 128 for homogeneous numeric values; unit/minimum/maximum apply "
    "to every element. Values remain ordinary JSON arrays; no direction, interval order or unit conversion is inferred. "
    "array.items is an anonymous value contract without parameter_key, required or default_value. Object properties "
    "remain named members. All value kinds share the existing depth/node limits. Every solved numeric "
    "value, including number_array elements and nested numbers, requires explicit minimum and maximum; "
    "put number_array element bounds on the number_array definition itself. "
    "Missing-bound errors count affected declarations and list their paths; missing required values "
    "are listed together within one object. Diagnostic text is bounded, so long path lists may be clipped; "
    "correct the same rule across the relevant declarations before resubmitting. "
    "Parameter definitions default to required=true; providing default_value does not make them optional. "
    "For public parameters and nested fields, set required=false explicitly when omission should use a "
    "valid non-null typed default; omission does not represent an unknown value. A top-level solved "
    "parameter may set required=false without a default when only some declared binding options use it; "
    "the selected option still requires that parameter to be present. "
    "Omit that value when the option is not selected; supply it when selected. "
    "Use parameter_kind=tagged_object for a closed object selected by discriminator_key. Its branches are "
    "2 to 32 complete anonymous object contracts without parameter_key, required or default_value. "
    "Each branch includes that discriminator as a required choice with exactly one distinct string choice "
    "and no default. Supply the complete selected branch as the native object value in public_parameters, "
    "without fields from other branches. Branch alternatives share the object's depth; "
    "all branch nodes count toward the existing tree budget, and every solved numeric branch remains bounded. "
    "Public and solved parameters have independent namespaces: keys must be unique within each, but may "
    "have the same name and different values across them. A Geometry source_kind selects the namespace; "
    "values are not merged or overwritten across namespaces. "
    "parameter_sources contains optional default_source=public_parameter or solved_parameter and overrides. "
    "An override wins. Other exact Geometry parameters require same-name declarations in the selected namespace; "
    "missing declarations never fall back to another namespace or Geometry defaults. Without default_source, "
    "unmapped parameters freeze legal Geometry defaults as fixed sources; parameters without defaults need overrides. "
    "Write expands this shorthand against the exact binding, checks fixed values, saves complete overrides without "
    "default_source and compares normalized contents. Info reads that one saved definition. Source/usage-only edits "
    "inherit the frozen mapping. A changed Geometry definition requires an explicit fragment update when its mapping changes. "
)
PRODUCT_REALIZATION_PREVIEW_TUTORIAL = _build_product_realization_preview_tutorial()
PRODUCT_FAMILY_EXTERNAL_CONDITION_RULES = (
    "An interface external_constraint declares which requested conditions the interface accepts. Its required flag "
    "controls presence and defaults to true: omitting required makes that condition mandatory for every self_geometry "
    "invocation. Set required=false when callers may omit it; every supplied condition is still independently checked "
    "by Backend. allowed_kinds selects finite_segment, fixed_point or infinite_axis; allowed_directions and "
    "the existing profile/tolerance requirements constrain the selected condition. Choose inputs by purpose: "
    "public_parameters carries typed computation and layout data declared by the Family, including necessary host "
    "geometry facts with documented units, source and purpose. external_constraints independently asserts a "
    "position/direction/profile relation on its bound output interface. A supplied OD compares that output "
    "interface's circular profile; it is not a context-only host size. Family-produced targets are not independent "
    "evidence of the Family's own correctness. Source retained_direction points into the retained pipe and "
    "pipe_outward out of its endpoint; the product interface axis follows its exact interface contract. "
    "These roles are not interchangeable. direction_relation is always explicit. OD is optional for all three "
    "kinds unless the Family requires profile information; a supplied OD is always compared. "
    "An inner diameter may be a modeling parameter and is not a connection criterion. "
    "Preview checks the cases actually requested. A Preview case's "
    "expectation.expected_status asserts the status returned by the Program for that exact input; it does not make the "
    "Program return INFEASIBLE or convert a failed frame check into INFEASIBLE. An expected INFEASIBLE scenario must "
    "therefore have Program logic that returns INFEASIBLE with a violation for the relevant input or condition, "
    "and expectation.reason_keys must name the stable expected violation keys. AMBIGUOUS expectations similarly "
    "name ambiguity keys. Technical errors remain errors even in a negative case. "
)
PRODUCT_FAMILY_PROGRAM_API = (
    "Use only documented builtins (isinstance/type are unavailable); "
    "keep executable calculations inside realize/standard_model or helpers, not module initialization. "
    "Supply representative Preview cases for both entries. Preview executes and validates each complete "
    "proposal, including declared Geometry parameter sources. Write only saves the draft. "
    "A mapped asset stays canonical; return uniform_scale for its instance. "
    "INFEASIBLE identifies which declared support, business, geometry, or assembly constraint this input violates; "
    "it does not claim universal physical impossibility. A bounded detailed-candidate search miss may proceed "
    "to a declared alternative under the rules below; without a deliverable proposal, an unresolved search "
    "or numerical capacity is AMBIGUOUS. INFEASIBLE needs a violation_key/message; AMBIGUOUS needs an "
    "ambiguity_key/message. An execution/API error is not product infeasibility. "
    "No general full-domain proof is required. "
    + "Use Product Realization Preview for finite representative checks before Publish. "
    + "main.py exports realize(ctx) and standard_model(ctx), each returning the attached return_schema proposal, "
    "not a ProductRealizationResult wrapper. Family code is a pure numeric program: it may import controlled math "
    "and exact submitted relative helper modules, but it cannot import cadquery or OCP, instantiate Geometry, access "
    "shapes, receipts, measured interfaces, or query actual model geometry. Each invocation receives fresh author "
    "modules and detached JSON-like values. Reflection, dynamic execution, mutable shared state, files and network "
    "access are unavailable. "
    "ctx.contract() returns the complete current Family contract as a dictionary. ctx.param(name) returns one normalized "
    "public parameter; ctx.parameters() returns the normalized public parameter dictionary. Structured parameters are "
    "detached Python dict/list values. Parameter sources reference a complete top-level parameter; dotted keys or "
    "value_path selectors are unsupported. To source a structured value, the Geometry parameter schema must accept "
    "that whole value. Family and Geometry each retain their own validation authority. "
    "Select parameters using the bound Geometry's declared, numerically expressible constructibility "
    "conditions, including coupled limits; an in-range value alone does not establish constructibility. "
    "Numeric constraints and construction must use the same meanings for paths, sections and connection "
    "stations, with the rule defined by its owning contract rather than independently reinterpreted. "
    "Keep source and port identities separate from construction roles: when the resource permits "
    "choosing a different through path or branch, map the selected roles back to the original "
    "interface keys and constraints. Reassigning a construction role does not authorize moving "
    "source geometry or changing a connection. "
    "Conservative bounds can screen candidates, but an inconclusive bound does not prove a physical "
    "conflict; distinguish it from a violated declared constraint. Keep numerical solving independent of "
    "actual-shape checks performed after construction. "
    "For a resource that permits an alternative when a preferred layout is not found, declare the allowed representations and "
    "their trigger conditions in that resource's contract and usage. First evaluate candidates with bounded "
    "cost; only when the selected detailed layout is not found may a predeclared deterministic rule choose "
    "an alternative. This does not prove that detailed physical construction is impossible. Keep the current "
    "selection inside the numeric program when it depends on solving; callers need not predict that "
    "outcome by choosing a representation in advance. Declare every exact resource in geometry_bindings "
    "and every fragment's finite binding_options. Construct only the selected proposal. Keep the "
    "declared component, fragment and port identities; supply complete parameters without fabricated "
    "zero values or an additional execution framework. Selecting among independently defined Geometry "
    "resources belongs to Family solving, not a Geometry dispatcher. Do not wrap unrelated resource "
    "alternatives in one function merely to work around a binding restriction. Ordinary parameter "
    "branches within one Geometry's construction semantics remain valid. Every SOLVED fragment selects "
    "one declared binding_key; the Backend consumes only that option's parameter_sources and "
    "uniform_scale_source. A parameter tag alone does not switch resources. Preserve the declared business "
    "identity, connections, port identities and all hard constraints still required by the selected "
    "representation; port coordinates may change only as that resource permits. Shape simplification does "
    "not automatically waive occupancy, installation or functional requirements. Numerical-capacity limits, "
    "invalid inputs, source errors, Geometry failures and Codec failures must not be swallowed as an "
    "alternative selection. When using multiple representations, declare the selected representation and "
    "its applicable reason through existing solved_parameters and report them in internal_parameters, "
    "using resource-defined names; ordinary resources need no new mode/reason fields or global state. "
    "If no alternative is declared or its necessary constraints cannot be met, report the applicable "
    "INFEASIBLE, AMBIGUOUS or technical failure through the existing contract. An alternative is not "
    "guaranteed to succeed. "
    "ctx.product_origin() returns the Backend-normalized root origin as a three-number METRE tuple; input length_unit "
    "only controls conversion of that origin and does not rescale *_m conditions, metre-declared Family parameters, "
    "Geometry parameters, directions, rotations or scale. ctx.external_constraints() returns a tuple of dictionaries "
    "with interface_key, source_kind, evidence_reference and condition, using the exact invocation Schema. "
    "The condition.kind selects finite_segment {start_m,end_m,direction_relation}, "
    "fixed_point {point_m,direction_relation}, or infinite_axis {point_m,axis,direction_relation}. "
    "All three accept optional outer_diameter_m. fixed_point with none has no axis or reference_direction; "
    "same/opposed requires axis. reference_direction is available only for same/opposed, must be perpendicular "
    "to the target axis, and enables roll comparison. Missing keys in a closed branch are absent from this API, "
    "not null-filled placeholders. The solver reads those same targets that Backend checks; computation inputs "
    "are read from declared public_parameters. finite_segment checks the full closed "
    "segment and its supplied outer diameter, without an invented allowed subrange or external object identity. "
    "standard_model receives no external conditions. ctx.policy() returns the execution policy dictionary. "
    "The Family contract declares solved_parameters; the SOLVED proposal reports only their values in internal_parameters. "
    "Read public values with ctx.param; do not repeat public parameter keys in internal_parameters. "
    "Every SOLVED author fragment supplies fragment_key, selected binding_key, "
    "local_transform and positive finite "
    "uniform_scale. Authors omit geometry_parameters; supplying that retired field is rejected. Backend alone derives "
    "complete Geometry arguments from the exact mapping and normalized public/solved values, validates them against "
    "the selected Geometry Schema before constructing, and retains them in canonical results and receipts. "
    "A mapped_model receives no parameters, and its "
    "scale must match the declared source; modeling_function scale is exactly 1 and size belongs in declared parameters. "
    "Component and fragment local_transform use local_transform_mode=fixed by default and must equal "
    "fixed_local_transform (default identity). program mode requires an explicit complete rigid transform; missing "
    "placement is not inferred. Concretely, fixed parameter sources equal their declared value, public_parameter "
    "sources equal the normalized ctx.param(parameter_key), and solved_parameter sources equal "
    "internal_parameters[parameter_key]; fixed placements equal the declaration while program placements come from "
    "the complete proposal. Preview cases execute these same declarations and outputs. At the "
    "same normalized nominal parameters, standard_model must preserve the complete Family-local construction across "
    "identity and nonzero product_origin; Backend alone applies the origin translation. The root component remains "
    "identity relative to product_origin. The Program never "
    "returns a world-root transform, constraints verdict, installation anchor, receipt or handle. Backend constructs "
    "actual Geometry and computes the component/fragment transform chain, actual interfaces/profiles, internal pair "
    "checks and requested external condition checks. "
    "For checked connections, Family interfaces use source_kind=model_interface to reference the fragment's "
    "actual Geometry interface key; constraints declare the required interface relationships. For external "
    "pipe/equipment adaptation, declare the applicable external_constraint and supply matching conditions in "
    "self_geometry invocations. Undeclared connections are not inferred or checked. "
    + PRODUCT_FAMILY_EXTERNAL_CONDITION_RULES +
    "JSON Schema describes fields; "
    "semantic rules (complete sourced geometry, one identity component root and checked interfaces) remain enforced "
    "during execution. Preview compares each requested case with its explicit expectation, separately from publication. "
    + PRODUCT_FAMILY_PARAMETER_RULES
    + " Definition-based publication: cad2bim://contracts/common/authoring#family_publication."
    + PRODUCT_SUPPORT_SCOPE_NAVIGATION
)
_FAMILY_CONNECTION_AUTHORING_GUIDANCE = (
    "For connection checks, reference actual Geometry interfaces and declare the corresponding Family "
    "connection constraints; include external constraints for pipe/equipment adaptation. Undeclared "
    "connections are not inferred or checked. "
)

IFC_PRODUCT_FAMILY_WRITE_DESCRIPTION = (
    "Save a Family draft in the current authorized scope without execution or publication. Inline create supplies source_files and action=set "
    "definition_json/usage; published create copies an exact family_version_alias from list/info and may inherit "
    "name, purpose and valid usage. update uses draft_ref and the latest expected_edit_token from Write/Info. "
    "usage and definition_json choose set for complete values or edit for literal usage edits/business-key "
    "set/remove collections; omitted fields inherit. Submit related edits together; complete merged references "
    "are checked before saving. Preview checks actual solving. "
    + _FAMILY_CONNECTION_AUTHORING_GUIDANCE
    +
    "source_changes_json edits source members atomically after reading their current text with Code; expected_edit_token guards the draft generation. "
    "Returns draft_ref/edit_token; recover uncertain saves through operation_list/get. Complete create/update "
    "requests: cad2bim://contracts/ifc_product_family_write/tutorial. Definition schema: "
    "cad2bim://contracts/ifc_product_family_write/definition_json at create_definition_input_schema/update_definition_input_schema. Source changes: "
    "cad2bim://contracts/ifc_product_family_write/source_changes_json. Program ABI: "
    "cad2bim://contracts/ifc_product_family_write/program_api at usage/return_schema. Usage-writing guidance: "
    "cad2bim://contracts/common/usage#version_usage. Preview/publish lifecycle: cad2bim://contracts/common/authoring."
)
PRODUCT_REALIZATION_PUBLIC_PARAMETERS_INVOCATION_JSON = json.dumps({
    "public_parameters": PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[0]["invocation"]["public_parameters"],
    "product_origin": [0, 0, 0], "length_unit": {"name": "METRE"},
}, separators=(",", ":"))
PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_JSON = json.dumps({
    "public_parameters": PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[0]["invocation"]["public_parameters"],
    "product_origin": [2000, 3000, 4000], "length_unit": {"name": "METRE", "prefix": "MILLI"}, "external_constraints": [],
}, separators=(",", ":"))
PRODUCT_REALIZATION_SELF_GEOMETRY_CONDITION_EXAMPLE = json.dumps(
    {
        "public_parameters": PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[0]["invocation"]["public_parameters"],
        "product_origin": [0, 0, 0],
        "length_unit": {"name": "METRE"},
        "external_constraints": [PRODUCT_REALIZATION_EXTERNAL_CONSTRAINT_EXAMPLE],
    },
    separators=(",", ":"),
)
PRODUCT_REALIZATION_INVOCATION_JSON_FIELD_DESCRIPTION = (
    "Closed JSON object encoded once as a string. self_geometry accepts public_parameters, "
    "required product_origin, required length_unit and external_constraints; standard_model accepts public_parameters, "
    "required product_origin and required length_unit. length_unit is an IfcLengthUnit and converts only product_origin "
    "to internal metres. Fields already named *_m, metre-declared Family/Geometry parameters, directions, rotations and "
    "scale keep their existing meanings. public_parameters is a map from declared parameter names to complete JSON "
    "values; omitted values or {} follow the exact Family's defaults and required rules. Unknown keys and wrong "
    "types are rejected by that contract. "
    "Each external_constraints item has interface_key, source_kind (default design_target), optional "
    "evidence_reference and condition. condition.kind is finite_segment (start_m/end_m), fixed_point (point_m), "
    "or infinite_axis (point_m/axis). direction_relation is required for every kind: same, opposed or none. "
    "A fixed_point with none forbids axis and reference_direction; same/opposed requires axis. finite_segment "
    "and infinite_axis retain the axis defining their position even with none. All kinds allow optional "
    "outer_diameter_m, unless Family profile_required or connection_profile makes it mandatory. Supplied OD "
    "always compares the bound output interface's circular profile. reference_direction is allowed only for "
    "same/opposed and must be perpendicular to the target axis. Position and angular tolerances default to 1e-6. "
    "Points and diameters are metres. Declared typed public_parameters may supply computation/layout data; "
    "external_constraints separately asserts actual output interface relations. Source retained_direction, "
    "pipe_outward and a product interface axis are different roles: retained_direction points into the retained "
    "source pipe, pipe_outward points out of that pipe endpoint, and the product axis follows its exact "
    "Family/Geometry interface contract. Do not substitute one for another by field name; choose the condition "
    "axis and same/opposed/none relation for the bound output interface. Empty targets are reported "
    "as external_target_comparison=NOT_COMPARED; frame PASS does not prove physical contact. A modeled "
    "inner diameter does not participate in connection checking. "
    "An external_constraint with source_kind=observed_interface requires caller-supplied evidence_reference; "
    "this records provenance and is not backend verification of that external interface or connection. "
    "Take parameter/interface keys "
    "and legal types from exact Family/Geometry contracts; take parameter values, target points/axes and origin from "
    "caller-confirmed business input. Complete call example: "
    "cad2bim://contracts/ifc_product_realization_preview/tutorial at preview_single."
)
IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION = (
    "Execute a Family draft_ref from Write/Info or an exact published family_version_alias from list/info without "
    "writing IFC. single uses attachment_kind=self_geometry|standard_model and invocation_json; standard_model "
    "constructs the complete Family without external conditions. Read the chosen invocation schema at "
    "cad2bim://contracts/ifc_product_realization_preview/self_geometry or "
    "cad2bim://contracts/ifc_product_realization_preview/standard_model. cases accepts up to 16 named cases: "
    "cad2bim://contracts/ifc_product_realization_preview/cases_json defines inline/items and "
    "from_operation/changes. Rendering: cad2bim://contracts/ifc_product_realization_preview/render_json. "
    "retain=true saves reports and successful shapes; a retained single has no expectation check. "
    "Use inspection_ref for Geometry Preview/Query and operation_list/get for retained recovery. Complete "
    "single/cases/reuse/Publish requests: "
    "cad2bim://contracts/ifc_product_realization_preview/tutorial. Before selecting or reusing cases, read "
    "cad2bim://contracts/common/authoring. Image settings, result states and actual-shape checks: "
    "cad2bim://contracts/common/preview. Report the invocation, saved target, interfaces and conditions actually "
    "checked, and distinguish any subsequent Query from unperformed entity or connectivity checks. "
    "SOLVED or an image alone does not prove physical connectivity."
)




DIRECT_IFC_PRODUCT_FAMILY_LIST_FIRST_INPUT_OWNER_SCOPES_DESCRIPTION = (
    "Optional owner-scope OR-filter for the first page."
)

DIRECT_IFC_PRODUCT_FAMILY_LIST_FIRST_INPUT_PRODUCT_STRUCTURE_KINDS_DESCRIPTION = (
    "Optional product-structure-kind OR-filter for the first page."
)

DIRECT_IFC_PRODUCT_FAMILY_LIST_FIRST_INPUT_PURPOSE_CONTAINS_DESCRIPTION = (
    "Optional bounded case-insensitive purpose substring for the first page."
)

DIRECT_IFC_PRODUCT_FAMILY_LIST_FIRST_INPUT_PAGE_SIZE_DESCRIPTION = (
    "First-page family count; default 50 and maximum 100."
)

DIRECT_IFC_PRODUCT_FAMILY_LIST_CURSOR_INPUT_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by this tool; submit it unchanged and omit first-page filters."
)

IFC_PRODUCT_FAMILY_ROOT_TARGET_TARGET_KIND_DESCRIPTION = (
    "Read one stable family root and a bounded page of immutable versions."
)

IFC_PRODUCT_FAMILY_ROOT_TARGET_FAMILY_ALIAS_DESCRIPTION = (
    "Stable safe family alias selected from ifc_product_family_list."
)

IFC_PRODUCT_FAMILY_ROOT_TARGET_BEFORE_VERSION_NO_DESCRIPTION = (
    "Continuation boundary from next_before_version_no; omit for the newest version page."
)

IFC_PRODUCT_FAMILY_ROOT_TARGET_PAGE_SIZE_DESCRIPTION = (
    "Version-card count for this root page; default 20 and maximum 100."
)

IFC_PRODUCT_FAMILY_VERSION_TARGET_TARGET_KIND_DESCRIPTION = (
    "Read one exact immutable ready ProductFamilyVersion."
)

IFC_PRODUCT_FAMILY_VERSION_TARGET_FAMILY_VERSION_ALIAS_DESCRIPTION = (
    "Exact version alias returned by family list or root info."
)

DIRECT_IFC_PRODUCT_FAMILY_INFO_INPUT_TARGET_DESCRIPTION = (
    "Complete family_root or family_version target branch."
)

IFC_PRODUCT_FAMILY_CARD_LATEST_FAMILY_VERSION_ALIAS_DESCRIPTION = (
    "Exact latest version discovered at read time; copy this alias explicitly into a "
    "later exact-version operation instead of using the root as latest."
)

IFC_PRODUCT_FAMILY_VERSION_INFO_RESULT_FAMILY_CONTRACT_JSON_DESCRIPTION = (
    "Exact immutable family contract projected as canonical JSON text."
)

IFC_PRODUCT_FAMILY_INFO_RESULT_INFO_DESCRIPTION = (
    "Complete family_root or family_version result branch."
)

DIRECT_IFC_PRODUCT_REALIZATION_SOLVE_PROVIDER_INPUT_MODE_DESCRIPTION = (
    "Execute one Family realization and preview; retain controls persistence of its successful geometry."
)



DIRECT_IFC_PRODUCT_REALIZATION_PREVIEW_BASE_PUBLIC_PARAMETERS_DESCRIPTION = (
    "Map from declared parameter names to complete JSON values. Omitted values or {} follow the exact Family's "
    "defaults and required rules; unknown keys and wrong types are rejected."
)

IFC_PRODUCT_REALIZATION_INSTANCE_RECEIPT_RESULT_NORMALIZED_PARAMETERS_JSON_DESCRIPTION = (
    "Canonical JSON object of exact parameters used for this geometry instance."
)

IFC_PRODUCT_REALIZATION_INSTANCE_RECEIPT_RESULT_GEOMETRY_SUMMARY_JSON_DESCRIPTION = (
    "Canonical JSON of the closed GeometrySummary returned by the runtime."
)

IFC_PRODUCT_REALIZATION_PREVIEW_RESULT_BASE_FAMILY_VERSION_ALIAS_DESCRIPTION = (
    "The draft or exact published Family source frozen for this execution; this is not an inspection reference."
)

IFC_PRODUCT_REALIZATION_SOLVED_RESULT_INTERNAL_PARAMETERS_JSON_DESCRIPTION = (
    "Canonical JSON object of family-specific derived parameters."
)

IFC_PRODUCT_FAMILY_AUTHORING_SOURCE_FILE_SOURCE_PATH_DESCRIPTION = (
    "Normalized relative POSIX Python member path. The complete bundle must contain "
    "exactly one main.py; Preview executes realize(ctx) or standard_model(ctx) for the selected attachment kind."
)

IFC_PRODUCT_FAMILY_AUTHORING_SOURCE_FILE_SOURCE_DESCRIPTION = (
    f"Exact UTF-8 Python source, at most {_SOURCE_MEMBER_BYTES:,} bytes per member; "
    f"all members together must fit {_SOURCE_TOTAL_BYTES:,} bytes."
)


IFC_PRODUCT_FAMILY_GEOMETRY_PURPOSE_DESCRIPTION = (
    "Use product_component or shape_fragment for an exact Geometry binding mapped by declared Family fragments."
)














PRODUCT_FAMILY_LIST_FIRST_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "owner_scopes": ["task"],
        "product_structure_kinds": ["single_product"],
        "purpose_contains": "connector",
        "page_size": 50,
    },
    separators=(",", ":"),
)

PRODUCT_FAMILY_LIST_CURSOR_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "cursor": "cur.product_family_page_001",
    },
    separators=(",", ":"),
)

IFC_PRODUCT_FAMILY_LIST_DESCRIPTION = (
    "Discover ready ProductFamily roots visible to the current Direct scope. This tool has no "
    "mode field; the presence of cursor selects the closed continuation branch. First page: "
    "optionally filter by owner_scopes, product_structure_kinds, purpose_contains, and "
    "page_size. Complete first-page example (replace direct_001 with the opened session alias): "
    + PRODUCT_FAMILY_LIST_FIRST_EXAMPLE
    + " Continuation: provide direct_session_alias plus only next_cursor copied unchanged as "
    "cursor. Complete continuation shape (replace the illustrative cursor with the returned "
    "next_cursor): " + PRODUCT_FAMILY_LIST_CURSOR_EXAMPLE + " The cursor retains the exact "
    "filters and directory boundary, with no page number, OFFSET, custom sort, or edited "
    "filter support. Each card exposes safe root and exact latest-version aliases plus "
    "scope, purpose, structure kind, and digests. latest_family_version_alias is only a "
    "read-time discovery convenience: explicitly copy that exact alias into any later "
    "version operation; family_alias never means latest. This directory is read-only "
    "and exposes no database ID, code locator, authoring, realization preview, or IFC "
    "write operation. rejections reports current-contract failures with family_version_alias, version_no, "
    "code=PRODUCT_FAMILY_CONTRACT_INVALID, field_path and reason; a rejected reference only locates that record "
    "and cannot be executed or read as source. families contains only valid items. An empty families array "
    "can coexist with rejections and next_cursor: follow the cursor, which advances past the last scanned row. "
    "Technical failures remain tool errors rather than rejections." + READ_BATCH_DESCRIPTION
)

IFC_PRODUCT_FAMILY_INFO_DESCRIPTION = (
    "target_kind=draft uses draft_ref from Write and returns the current edit_token, purpose, "
    "instructions, contract_json, draft/published Geometry bindings and source file directory. "
    "Read source with code_list product_family_draft and code_read; pass edit_token as expected_edit_token to update. "
    "Read one ready ProductFamily by a closed target branch. family_root uses family_alias "
    "and returns a bounded immutable version directory; submit next_before_version_no as "
    "before_version_no with page_size to exhaust older versions. family_version uses one "
    "exact family_version_alias and returns its canonical family_contract_json, exact "
    "usage_instructions, geometry-version bindings, immutable asset metadata and byte digests. Validation scenarios, "
    "expected statuses, per-scenario outcomes, and reports belong to Preview and its saved Operation, "
    "not Info. Execution validates source integrity, exact dependencies, parameters and actual results "
    "under the current controlled runtime. Source, parameter or binding revisions edit a draft before Preview and Publish. Unsupported Family exact info, "
    "complete bundles, and dependent source discovery are rejected; offline-retained bytes are not an online Code route. Reuse "
    "requires a complete current create. There is no target=latest branch: copy an exact "
    "version alias from list or root info. Responses contain only scope-bound safe aliases, "
    "never database identities or paths. Use the complete exact usage_instructions with family_contract_json "
    "and each exact Geometry dependency to identify the actual component model, fixed/public/solved values, "
    "the confirmed business data and conversions needed for every input, product origin and interface identities, "
    "and parameter-derived interface checks before constructing an invocation. Function Geometry supplies interfaces.py:evaluate(ctx) before main.py:build(ctx); normal interface FAIL keeps diagnostics and skips build. The contract and dependency Schemas remain "
    "authoritative for typed bounds; never guess missing values. "
    "This tool has no code locator, source read, "
    "authoring, realization preview, family CURRENT, or IFC write behavior."
    + READ_BATCH_DESCRIPTION
)

__all__ = [
    "IFC_PRODUCT_FAMILY_WRITE_DESCRIPTION",
    "IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION",
    "PRODUCT_FAMILY_CREATE_EXAMPLE",
    "PRODUCT_FAMILY_DEFINITION_DOCUMENT_EXAMPLE",
    "PRODUCT_FAMILY_SOURCE_CHANGES_EXAMPLE",
    "PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE",
    "PRODUCT_REALIZATION_INVOCATION_JSON_FIELD_DESCRIPTION",
    "PRODUCT_REALIZATION_PUBLIC_PARAMETERS_INVOCATION_JSON",
    "PRODUCT_FAMILY_LIST_CURSOR_EXAMPLE",
    "PRODUCT_FAMILY_LIST_FIRST_EXAMPLE",
]


IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION += _runtime_budget_description(_PRODUCT_PREVIEW_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
