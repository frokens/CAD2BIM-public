"""Provider-visible usage text for IFC geometry tools."""

from CAD2BIM_mcp.tool_docs.common import runtime_budget_description as _runtime_budget_description
from CAD2BIM_backend.domain.runtime_execution import (
    GEOMETRY_PREVIEW_EXECUTION as _GEOMETRY_PREVIEW_EXECUTION,
    GEOMETRY_QUERY_EXECUTION as _GEOMETRY_QUERY_EXECUTION,
)

import json

from CAD2BIM_backend.domain.geometry_interface import (
    MAX_GEOMETRY_INTERFACE_PROFILE_POINTS,
    MAX_GEOMETRY_MODEL_INTERFACES,
    MAX_GEOMETRY_MODEL_INTERFACES_BYTES,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
    MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES,
)
from CAD2BIM_mcp.tool_docs.preview import (
    PREVIEW_OBSERVATION_BOUNDARY,
)
from CAD2BIM_mcp.tool_docs.common import (
    READ_BATCH_DESCRIPTION,
    VERSION_USAGE_INSTRUCTIONS_NAVIGATION,
    VERSION_USAGE_INSTRUCTIONS_CREATE_DESCRIPTION as VERSION_USAGE_INSTRUCTIONS_CREATE_DESCRIPTION,
    VERSION_USAGE_INSTRUCTIONS_MODIFY_DESCRIPTION as VERSION_USAGE_INSTRUCTIONS_MODIFY_DESCRIPTION,
    VERSION_USAGE_INSTRUCTIONS_INFO_DESCRIPTION as VERSION_USAGE_INSTRUCTIONS_INFO_DESCRIPTION,
)

GEOMETRY_AUTHORING_PROGRAM_RESOURCE_URI = (
    "cad2bim://contracts/ifc_geometry_resource_write/program_api"
)


DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION = (
    "Direct entry adds its Session scope to the shared public contract."
)


GEOMETRY_TOPOLOGY_TRIANGLE_COUNT_DESCRIPTION = (
    "Triangle count for intrinsic mesh geometry; null for BREP."
)

GEOMETRY_PREVIEW_DATUM_DESCRIPTION = "Rigid placement in metres. Omit to observe resource-local coordinates with identity placement."
GEOMETRY_INSPECTION_REF_DESCRIPTION = "Exact retained result from a successful solidifying Preview or its Operation; copy unchanged. Observation does not rebuild the source."
GEOMETRY_INSPECTION_SELECTION_DESCRIPTION = "Select the whole result or an exact component_key/fragment_key from this result's context."
GEOMETRY_INSPECTION_COORDINATES_DESCRIPTION = "Result coordinates follow context.source.result_coordinate_space: a Geometry sample without datum uses geometry_local, a sample with datum and every Family result use project_world. fragment_local requires the exact selected fragment_key and uses the saved local geometry with receipt scale already applied. All distances are metres."
GEOMETRY_INSPECTION_VIEW_DESCRIPTION = "Choose a model view with the existing render profile, or actual material sections at an explicit plane and up to four normal offsets. Display extents only crop the picture; they do not change measured geometry."
GEOMETRY_SECTION_BOUNDS_DESCRIPTION = "u_min,u_max,v_min,v_max of the measured boundaries before image cropping."
GEOMETRY_INSPECTION_OPERATION_DESCRIPTION = "Original solidifying Operation when this response publishes a build. Observing an existing result does not create an Operation."


GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE = """import cadquery as cq

def build(ctx):
    length_m = ctx.param("length_m")
    width_m = ctx.param("width_m")
    height_m = ctx.param("height_m")
    return cq.Workplane("YZ").rect(width_m, height_m).extrude(length_m)
"""

GEOMETRY_INTERFACE_SOURCE_EXAMPLE = """def evaluate(ctx):
    length_m = ctx.param("length_m")
    width_m = ctx.param("width_m")
    height_m = ctx.param("height_m")
    ctx.define_interface(
        key="inlet",
        origin_m=(0.0, 0.0, 0.0),
        axis=(-1.0, 0.0, 0.0),
        reference_direction=(0.0, 0.0, 1.0),
        profile={"kind": "rectangle", "width_m": width_m, "height_m": height_m},
    )
    ctx.define_interface(
        key="outlet",
        origin_m=(length_m, 0.0, 0.0),
        axis=(1.0, 0.0, 0.0),
        reference_direction=(0.0, 0.0, 1.0),
        profile={"kind": "rectangle", "width_m": width_m, "height_m": height_m},
    )
"""

GEOMETRY_MODELING_FUNCTION_PARAMETER_SCHEMA_EXAMPLE = {
    "type": "object",
    "properties": {
        "length_m": {"type": "number", "exclusiveMinimum": 0.0, "maximum": 1000.0},
        "width_m": {"type": "number", "exclusiveMinimum": 0.0, "maximum": 1000.0},
        "height_m": {"type": "number", "exclusiveMinimum": 0.0, "maximum": 1000.0},
    },
    "required": ["length_m", "width_m", "height_m"],
    "additionalProperties": False,
}
GEOMETRY_MODELING_FUNCTION_VALIDATION_SAMPLE_EXAMPLE = {
    "length_m": 0.8,
    "width_m": 0.2,
    "height_m": 0.1,
}
GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE = json.dumps({
    "direct_session_alias": "direct_001", "mode": "create", "display_name": "Rectangular body",
    "usage_instructions": {"action": "set", "value": "Construct a local rectangular body in metres. Preview representative dimensions and inspect interfaces before publishing. This example does not create IFC connections."},
    "source": {"source_kind": "modeling_function", "source_files": [{"source_path": "main.py", "source": GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE}, {"source_path": "interfaces.py", "source": GEOMETRY_INTERFACE_SOURCE_EXAMPLE}],
               "parameter_schema_json": json.dumps({"action": "set", "value": GEOMETRY_MODELING_FUNCTION_PARAMETER_SCHEMA_EXAMPLE})},
}, separators=(",", ":"))
GEOMETRY_MODELING_FUNCTION_SHORT_CREATE_EXAMPLE = GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE

GEOMETRY_DRAFT_UPDATE_EXAMPLE = {
    "direct_session_alias": "direct_001", "mode": "update", "draft_ref": "<draft_ref>",
    "expected_edit_token": 1,
    "usage_instructions": {"action": "edit", "edits": [{"old_text": "representative dimensions", "new_text": "representative lengths, widths and heights"}]},
    "parameter_schema_json": json.dumps({"action": "edit", "set": {
        "length_m": {"schema": {"type": "number", "exclusiveMinimum": 0.0, "maximum": 500.0}, "required": True}}, "remove": []}),
    "source_changes": [{"change_kind": "literal_edit", "source_path": "main.py",
        "replacements": [{"old_text": 'return cq.Workplane("YZ").rect(width_m, height_m).extrude(length_m)',
                          "new_text": 'return cq.Workplane("YZ").rect(width_m, height_m).extrude(length_m).val()'}]}],
}

GEOMETRY_MODELING_FUNCTION_USAGE_EXAMPLE = (
    "Minimal complete modeling_function create (replace direct_001 with the alias returned by "
    "runtime_open_direct_session): "
    + GEOMETRY_MODELING_FUNCTION_SHORT_CREATE_EXAMPLE
    + " source.parameter_schema_json is a JSON string; parameters are supplied separately to Preview. "
    "Read program_api for controlled methods and interface declarations."
)

GEOMETRY_QUERY_SOURCE_EXAMPLE = """def query(ctx):
    shape = ctx.private_geometry()
    box = shape.BoundingBox()
    return {
        "xmin_m": box.xmin,
        "xmax_m": box.xmax,
        "solid_count": len(shape.Solids()),
    }
"""

GEOMETRY_QUERY_FRESH_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
        "target": {
            "target_kind": "geometry_resource_version",
            "resource_version_alias": "cur.geometry_version_ref_001",
        },
        "code": GEOMETRY_QUERY_SOURCE_EXAMPLE,
        "page_size": 100,
    },
    separators=(",", ":"),
)

GEOMETRY_QUERY_DISPLAY_DIAGNOSTICS_EXAMPLE = {
    "direct_session_alias": "direct_001", "mode": "fresh",
    "target": {"target_kind": "geometry_inspection", "inspection_ref": "cur.inspection_exact",
               "selection": {"selection_kind": "whole"},
               "coordinate_space": {"coordinate_space": "result"}},
    "code": "def query(ctx):\n    return ctx.display_diagnostics()\n",
}

GEOMETRY_QUERY_SECTION_SOURCE_EXAMPLE = """from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
import cadquery as cq

def query(ctx):
    shape = ctx.private_geometry()
    plane_point_m = (0.4, 0.0, 0.0)
    plane_normal = (1.0, 0.0, 0.0)
    section = BRepAlgoAPI_Section(
        shape.wrapped,
        gp_Pln(gp_Pnt(*plane_point_m), gp_Dir(*plane_normal)),
        False,
    )
    section.Build()
    edges = cq.Shape(section.Shape()).Edges()
    return {
        "section_edge_count": len(edges),
        "section_edge_lengths_m": [edge.Length() for edge in edges],
    }
"""

GEOMETRY_QUERY_SECTION_FRESH_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
        "target": {
            "target_kind": "geometry_resource_version",
            "resource_version_alias": "cur.geometry_version_ref_001",
        },
        "code": GEOMETRY_QUERY_SECTION_SOURCE_EXAMPLE,
        "page_size": 100,
    },
    separators=(",", ":"),
)

GEOMETRY_QUERY_BASIC_USAGE_EXAMPLE = (
    "Complete fresh BRep query example (copy an exact mapped-model version alias from "
    "ifc_geometry_resource_info and replace the illustrative alias): "
    + GEOMETRY_QUERY_FRESH_EXAMPLE
)

GEOMETRY_QUERY_USAGE_EXAMPLE = (
    GEOMETRY_QUERY_BASIC_USAGE_EXAMPLE
    + " Complete OCP plane-section example against the same exact mapped-model target: "
    + GEOMETRY_QUERY_SECTION_FRESH_EXAMPLE
    + " Choose plane_point_m and plane_normal from facts about the exact target; they are "
    "metre coordinates and a unit direction, not a server-selected section or a claimed "
    "product dimension."
)


IFC_GEOMETRY_PREVIEW_DESCRIPTION = (
    "Observe Geometry without requiring publication. source selects a draft_ref from Write/Info or "
    "an exact published resource_version_alias from Catalog. mode=single executes one parameter "
    "object in sample_parameters_json (default {}) with optional preview_profile_json. mode=cases "
    "checks up to 8 named inputs; read cad2bim://contracts/ifc_geometry_preview/cases_json for inline "
    "or saved-input requests, expectations and explicit execution reuse. render_json selects none "
    "or named successful cases; see cad2bim://contracts/ifc_geometry_preview/render_json. "
    "retain=true saves the report and successful geometry under an Operation; false is temporary. "
    "single is an unchecked observation; technical errors cannot pass as expected business rejection. "
    "BUILT and render failure are separate; confirmed display-boundary failures require same-target investigation "
    "and a supported correction before another Preview. "
    "mode=inspection observes a saved inspection_ref with selection, coordinate_space and observation "
    "for images or measured sections without rebuilding. Whole-result request: "
    '{"mode":"inspection","inspection_ref":"<inspection_ref>","selection":{"selection_kind":"whole"},'
    '"coordinate_space":{"coordinate_space":"result"},"observation":{"observation_kind":"model"}}. '
    "mode=family_standard_model takes a published family_version_alias and observation to read its "
    "formal whole model in local metres, with no new solve, saved copy or Operation. "
    "Add the current Direct session. Images use geometry and independently evaluated interfaces from the same normalized parameters and arrive as "
    "inline ImageContent; temporary_interfaces are display "
    "overlays, not physical connections. A successful build or image does not prove every shape requirement. "
    "Select finite representatives for main branches, transitions, support boundaries and rejection "
    "paths; reuse applicable results and stop when necessary cases and shape requirements are checked. "
    "Before selecting or reusing cases, read cad2bim://contracts/common/authoring. "
    "Complete reuse requests: cad2bim://contracts/ifc_geometry_preview/tutorial. "
    "Camera, material inspection and result interpretation: cad2bim://contracts/common/preview."
    + READ_BATCH_DESCRIPTION
)

GEOMETRY_PREVIEW_CASES_RESOURCE_DESCRIPTION = (
    "Geometry cases_json Schema: mode=inline supplies 1-8 named inputs; from_operation inherits "
    "saved inputs with add/replace/delete/reuse changes. reuse takes only case_key; applicability is in "
    "cad2bim://contracts/common/authoring#reuse."
)
GEOMETRY_PREVIEW_RENDER_RESOURCE_DESCRIPTION = (
    "render_json Schema: none skips images; selected takes case_keys and optional profile. "
    "Only successful selected cases render; retain independently controls saving."
)


GEOMETRY_RETAIN_REUSE_EXAMPLES = {
    "single": {"direct_session_alias": "direct_001", "mode": "single",
        "source": {"source_kind": "draft", "draft_ref": "<draft_ref>"},
        "sample_parameters_json": '{"length_m":0.8,"width_m":0.2,"height_m":0.1}', "retain": True},
    "cases": {"direct_session_alias": "direct_001", "mode": "cases",
        "source": {"source_kind": "draft", "draft_ref": "<draft_ref>"},
        "cases_json": json.dumps({"mode": "inline", "items": [{"case_kind": "geometry", "case_key": "nominal",
            "parameters": {"length_m": 0.8, "width_m": 0.2, "height_m": 0.1},
            "expectation": {"expected_status": "BUILT"},
            "reuse": {"reuse_kind": "inspection", "inspection_ref": "<inspection_ref>"}}]}), "retain": True},
    "alternative_reuse": {"reuse_kind": "case", "operation_alias": "op_001", "case_key": "observation"},
    "saved_cases": {"direct_session_alias": "direct_001", "mode": "cases",
        "source": {"source_kind": "draft", "draft_ref": "<draft_ref>"},
        "cases_json": json.dumps({"mode": "from_operation", "operation_alias": "op_002",
            "changes": [{"change_kind": "reuse", "case_key": "nominal"}]}), "retain": False},
    "publish": {"direct_session_alias": "direct_001", "root_kind": "geometry", "draft_ref": "<draft_ref>",
        "expected_edit_token": 1, "preview_examples": [{"operation_alias": "op_002", "case_keys": ["nominal"]}]},
}

GEOMETRY_PREVIEW_TUTORIAL_DESCRIPTION = (
    "Complete Geometry requests: retained single, explicit case reuse, saved-case continuation and publication. "
    "Replace example references and parameters with values for your resource."
)
GEOMETRY_PREVIEW_TUTORIAL = {
    "usage": "For the rectangular-body program_api example. Replace references and edit token from actual "
             "responses. alternative_reuse replaces only the cases item's reuse; op_001 is the original "
             "single report, op_002 the new checked report. saved_cases reuses nominal from op_002 without "
             "repeating its parameters. Omit the reuse selection to execute instead. General "
             "applicability/publication rules: cad2bim://contracts/common/authoring. These examples teach "
             "request structure, not engineering dimensions or a universal required case set.",
    **GEOMETRY_RETAIN_REUSE_EXAMPLES,
}


CONTROLLED_MODELING_PROGRAM_GUIDE = (
    "Geometry main.py:build(ctx) constructs only Shape; interfaces.py:evaluate(ctx) declares parameter-derived interfaces and returns None. "
    "The evaluator and reachable relative helpers use controlled pure math and ctx.param/define_interface, cannot import main, CadQuery/OCP, Shape or geometry_inputs. "
    "Mapped authoring build retains define_interface; published mapped manifest supplies fixed interfaces. Preview cases and later execution use the same controlled modeling "
    "rules. Family realize(ctx)/standard_model(ctx) are pure numeric entries described in the Family "
    "program_api; they cannot use CadQuery or shape handles. In Geometry build(ctx), "
    "construct the selected Geometry from its complete parameters. Product layout solving and selection "
    "among independently defined Geometry resources belong to Family, not to a Geometry wrapper that "
    "dispatches between unrelated models. Parameter-dependent construction within this Geometry's "
    "declared semantics remains valid; geometry_inputs may still supply assets needed by that construction. "
    "See cad2bim://contracts/ifc_product_family_write/program_api for declared binding_options "
    "and proposal binding_key selection. "
    "import cadquery as cq and import math expose controlled "
    "namespaces, not native modules, classes or functions. Exact submitted helper modules use relative imports. "
    "OCP imports, wrapped native objects, reflection, dynamic execution, class definitions, attribute assignment "
    "and arbitrary file/network access are unavailable. Use geometry_inputs and ctx.geometry(input_key) for "
    "fixed BREP instead of an OCP reader or embedded BREP bytes. Parameters and interface dictionaries are "
    "detached local values; local lists/dicts, helper functions, loops, comprehensions and synchronous "
    "min/max/sorted key callbacks remain available. A key callback must be an author function from this invocation "
    "and return basic snapshot values, not a native handle. Each invocation starts fresh author modules, helper defaults "
    "and an object arena. Shape, Workplane, Vector, Plane and topology values are opaque invocation-owned "
    "handles; operate through the declared methods and never retain them across invocations. "
    "Supported cq constructors are Vector, Plane, Workplane; Vertex.makeVertex; "
    "Edge.makeLine/makeThreePointArc/makeBezier/makeSpline/makeCircle; "
    "Wire.assembleEdges/makePolygon/makeCircle; Face.makeFromWires; Shell.makeShell; "
    "Solid.makeBox/makeCylinder/makeCone/makeSphere/makeTorus/makeLoft; Compound.makeCompound. "
    "Solid.revolve(outerWire, innerWires, angleDegrees, axisStart, axisEnd) accepts only a Wire and "
    "a list/tuple of inner Wires from this invocation, a finite angle, and distinct finite 3D axis points "
    "(Vector or list/tuple); the Face overload is unavailable. "
    "For example cq.Workplane('XY').circle(radius_m).extrude(length_m).val(), "
    "cq.Solid.makeCylinder(radius_m, length_m), cq.Plane(origin=(0,0,0), xDir=(1,0,0), normal=(0,0,1)), "
    "and cq.Compound.makeCompound([shape_a, shape_b]) use supported entries. Plane.XY and raw Shape casts "
    "are not entries. Workplane methods: moveTo, lineTo, polyline, threePointArc, spline, close, circle, rect, "
    "polygon, box, extrude, revolve, sweep, loft, union, cut, intersect, clean, translate, rotate, transformed, "
    "workplane, newObject, edges, faces, vertices, wires, solids, fillet, chamfer, val, vals. "
    "val may return a Shape or Vector handle; vals returns a list. A Vector or list is not a valid build return. "
    "Shape methods: copy, clean, cut, fuse, intersect, translate, rotate, scale, Center, BoundingBox, isValid, "
    "ShapeType, Area, Volume, distance, Edges, Faces, Solids, Shells, Wires, Vertices. Edge/Face expose geomType; "
    "Shape.fuse(*shapes, glue=True) is supported for operands proven to meet on coincident boundaries without "
    "other intersections requiring general Boolean solving. Compare saved shapes and cost within that domain; "
    "glue is not a default speed switch or a retry after Boolean failure. "
    "Edge/Wire expose Length; Edge exposes radius/startPoint/endPoint. Solid/Compound expose fillet/chamfer. "
    "Select fillet/chamfer edges from the actual target body; foreign-body edges are invalid even if dimensions "
    "match. Vector exposes x/y/z/Length, dot/cross/normalized/toTuple and controlled vector arithmetic; "
    "Plane exposes origin/xDir/yDir/zDir and rotated; BoundingBox exposes xmin/ymin/zmin/xmax/ymax/zmax, "
    "xlen/ylen/zlen, center and DiagonalLength. Native callbacks and all unlisted native operations are rejected. "
    "math provides pi/e/tau/inf/nan and acos/asin/atan/atan2/cos/sin/tan/sqrt/hypot/radians/degrees/dist/isclose/"
    "isfinite/isinf/isnan/floor/ceil/fabs/exp/log/log10/copysign/fmod/ulp; output dimensions and coordinates must "
    "still satisfy finite typed bounds. Missing declared input names raise KeyError. Capability violations or "
    "unknown native failures invalidate the invocation even if caught by author code; they are not an "
    "INFEASIBLE result. Read the source-located diagnostic, replace the unsupported operation with a declared "
    "one or submit a cause-related immutable source revision; do not retry unchanged or guess an API. "
    "Every invocation uses exact source and frozen dependencies with current capability, parameter, result and budget checks. "
    "Fixed mapped BREP consumption reads canonical geometry bytes and does not execute author source. "
    "For batch-oriented construction, match detail to the required appearance, placement, flow or manufacturing purpose. "
    "Prefer a direct extrusion or one axial revolved profile when it expresses the required shape. "
    "Avoid opening a void before fusion only to cut the identical void again afterwards: where the final subtraction H is required, "
    "compare ((A-H) union B)-H with (A union B)-H, preserving clearance, openings and validity. "
    "This set equivalence is not a universal faster-order guarantee; separately hollow parts can block a required passage when fused. "
    "Reuse unchanged topology measurements within one invocation, never native handles across invocations. "
    "Only omit hidden bores, fillets or other details when the task permits; describe approximations and blind recesses in usage_instructions. "
    "A visible opening or numeric interface does not prove an internal flow path. "
    "Compare a few representative inputs using available diagnostics and retained shapes; separate shared cold startup from per-instance build, Codec and IFC costs. "
    "For path construction, distinguish genuine turns from bounded coordinate roundoff; preserve endpoints and interfaces "
    "and check every skipped subchain against the retained segment instead of applying a global angle cutoff. "
    "Retain the original construction when an alternative is slower or introduces unresolved shape risk. "
    "Do not import timers/profilers or private runtime APIs into author code, invent unavailable timing data, remove necessary checks, "
    "or add a registration performance gate. Function interfaces remain independently parameter-derived before build. "
)

GEOMETRY_BUILD_CONTEXT_ABI = (
    "For either create kind, optional geometry_inputs binds up to 8 unique input_key names to "
    "exact resource_version_alias values returned by ifc_geometry_resource_list/info. Inputs must "
    "be fixed mapped_model BREP versions and total at most 32 MiB; modeling functions and meshes "
    "are not accepted. ctx.geometry(input_key) returns a controlled handle to an independent Shape copy in "
    "that input's metre local frame on every call; undeclared names raise KeyError. For example, "
    "geometry_inputs=[{input_key:body,resource_version_alias:<exact alias>}] allows "
    "return ctx.geometry('body') in build(ctx). No file paths or raw BREP bytes are supplied by "
    "code. Draft Preview freezes the selected source and dependencies for that execution; Publish "
    "makes them part of an immutable version. Draft updates inherit omitted dependencies unchanged. "
    "ctx.param(name) reads one value from the same exact normalized parameter object in evaluate and build "
    "and raises KeyError when that key is absent. The parameter schema is a closed root "
    "object and supports nested closed objects, homogeneous bounded arrays, and number, integer, "
    "string or boolean leaves. Each object requires properties, required and additionalProperties=false, "
    "with at most 64 properties. Each array requires items and integer minItems/maxItems satisfying "
    "0 <= minItems <= maxItems <= 128. The whole schema permits depth 4 (root at depth 0), "
    "256 nodes and 16,384 UTF-8 bytes. Every node may have title, description and default; "
    "scalar leaves may additionally have enum, minimum/maximum, exclusiveMinimum/exclusiveMaximum, "
    "multipleOf and minLength/maxLength. Only the root may have $schema; other keywords and "
    "types are unsupported. Defaults are validated and recursively inserted into missing object "
    "properties before independent interface evaluation and build; arrays retain their supplied length. "
    "A nested tagged object may instead use an anyOf wrapper with 2 to 32 complete closed-object branches. "
    "Each branch requires the same string tag field with a distinct singleton enum and no tag default; "
    "exactly one shared field must discriminate all branches. The wrapper permits only anyOf, title, "
    "description and default; branch objects have no default. Selected branches alone receive field defaults. "
    "Alternatives share instance depth, while all branch nodes count toward the existing node budget. "
    "A key without a supplied value or default remains absent. In interfaces.py:evaluate(ctx), or a fixed mapped authoring build, call "
    "the keyword-only method "
    "ctx.define_interface(key=..., origin_m=(x,y,z), axis=(nx,ny,nz), "
    "reference_direction=(ux,uy,uz), profile=...) to emit a model-local interface with the "
    "normalized parameters; positional arguments are invalid. key must match [a-z][a-z0-9_]{0,63}. "
    "origin_m is in metres; axis is local Z "
    "and reference_direction is local X. "
    "Both directions must be finite perpendicular unit vectors and define a right-handed frame. "
    "The parameter-derived frame/profile may be a nominal interface when the resource declares "
    "that meaning. Complete interface fields and successful pairing checks do not prove actual openings, "
    "material contact, body clearance or later installation; whether those properties are required is "
    "determined by the specific resource and consumer contracts. "
    "profile may be omitted or must be exactly {kind:circular,diameter_m:value}, "
    "{kind:rectangle,width_m:value,height_m:value}, or {kind:polygon,points_m:[[x,y],...]}; "
    "diameter_m, width_m, and height_m are positive metres no greater than 1,000. polygon points "
    "are metre coordinates in the interface-local XY plane and keep the author's origin. Supply "
    f"3-{MAX_GEOMETRY_INTERFACE_PROFILE_POINTS} distinct vertices without a repeated closing point; "
    "they must form a simple nonzero-area outer outline. One build may emit at "
    f"most {MAX_GEOMETRY_MODEL_INTERFACES} interfaces and {MAX_GEOMETRY_MODEL_INTERFACES_BYTES:,} "
    "canonical UTF-8 bytes in total per evaluator invocation. A per-invocation interface overflow is an invalid "
    "interface output; the output bundle retains its existing byte budget. Neither is a "
    "transient technical failure: do not retry unchanged source and inputs; reduce interface count "
    "or polygon detail, or reduce the validation samples, then submit corrected source or inputs. "
    "Keys must be unique "
    "within that evaluation; duplicate or invalid interfaces fail the interface stage. For connection or mating checks, "
    "declare the relevant interfaces in evaluate(ctx), or fixed mapped authoring build, with frames/profiles matching the intended connection "
    "geometry; document any nominal datum meaning. Omitting define_interface yields an empty set and is "
    "appropriate only when no interface checks are needed, such as purely decorative geometry."
)

GEOMETRY_BUILD_RETURN_CONTRACT = (
    "build(ctx) must return one invocation-owned controlled Shape handle or a cq.Workplane "
    "handle containing exactly one shape. Shape topology may be Vertex/Edge/Wire/Face/Shell/Solid/Compound; "
    "it is not restricted to one Solid. Raw native cq.Shape or OCP TopoDS_Shape values cannot be supplied. "
    "The return value is not JSON. Return the handle itself; do not serialize it or pre-tessellate it for "
    "display. The server validates and encodes the canonical geometry after build returns. "
    "Topology validity and interface checks establish their respective properties, not every aspect of "
    "the model's design intent. For targeted supplementary evidence, use the measurement guidance in "
    "cad2bim://contracts/ifc_geometry_query_python/program_api. "
    "Write only saves source and declarations. Preview executes this return contract per input, "
    "keeping geometry and evaluated interfaces from the same normalized parameters. retain=true saves the case inputs, outcomes "
    "and successful actual geometry; inspection_ref supports later Query and Preview without rebuilding. "
    "retain=false cleans temporary geometry after the call and creates no reusable evidence. "
    "Publish uses the definition to construct fixed Geometry or publish a parametric program. Preview "
    "instances are independent; one representative shape does not prove every supported parameter combination."
)


GEOMETRY_AUTHORING_EXECUTION_AND_DIAGNOSTIC_GUIDANCE = (
    "Native build and server-owned codec work run in separate supervised child processes. Each "
    "currently has its own 30-second wall, 30-second CPU, and 2 GiB RSS execution budget. These "
    "child execution limits are independent of the admission queue and outer execution budget in "
    "the tool description; admission does not replace either child limit. Return the controlled Shape handle "
    "and let the server-owned codec encode and later mesh it. Pre-tessellation in build(ctx) does "
    "not repair a later codec or Preview failure. A child timeout or resource-limit failure is "
    "terminal for that attempt, publishes no ready version, and should not be retried with "
    "unchanged source and inputs; inspect the preserved source and sampled resource diagnostic "
    "before a cause-related repair. "
    "Write success means saved=true and runtime_checked=false. Preview reports actual per-case results; "
    "retain=true saves evidence and successful shapes under its Operation. Inspect actual interfaces "
    "through Preview or saved-geometry Query. Input validation selects a branch from mode and source_kind. A leading branch "
    "label in invalid_path names that union variant rather than another JSON object; the remaining "
    "path identifies the invalid field."
)

GEOMETRY_AUTHORING_PROGRAM_USAGE = (
    "Write saves one mutable draft in the current authorized scope without executing Python or creating a published version. "
    "Create uses source_kind mapped_model/modeling_function with a complete main.py bundle, or published with an exact resource_version_alias. "
    'Inline create requires display_name, usage_instructions={"action":"set","value":"complete usage"}; '
    'modeling_function also requires source.parameter_schema_json containing {"action":"set","value":complete_schema}. '
    "Published create may omit display_name and usage_instructions: name comes from the selected resource root, usage from its exact version manifest. "
    "An explicit usage override at create uses set; missing or invalid inherited usage requires set. Test parameters belong to Preview single/cases. "
    "Update uses draft_ref and expected_edit_token; omitted fields inherit and explicit null is invalid. "
    'usage_instructions selects {"action":"set","value":"complete usage"} or {"action":"edit","edits":[{"old_text":"exact unique text","new_text":"replacement"}]}. '
    'parameter_schema_json contains {"action":"set","value":complete_schema} or {"action":"edit","set":{"parameter":{"schema":complete_parameter_schema,"required":true}},"remove":["old_parameter"]}. '
    "Each set entry replaces/adds one top-level parameter and explicitly sets its required membership; remove must name an existing parameter and removes its required membership. "
    "Untouched parameters and root metadata stay unchanged. Root defaults must still validate; use set to change root structure. "
    "The supported closed schema dialect excludes general $ref/$defs; final schema stays within 16 KiB, independent of action overhead. "
    "interfaces only applies to mapped_model, including explicit empty replacement; function interfaces come from interfaces.py:evaluate(ctx). "
    "source_changes applies literal_edit/add_member/replace_member/delete_member atomically under the draft expected_edit_token. "
    "Read current source through Code before editing. Preview checks the current frozen snapshot; retain=true saves instances for observation and reuse. "
    "The editable draft is not a version; later updates do not alter old published versions. "
    "Workflow, retention, input/result reuse and publication: cad2bim://contracts/common/authoring. "
    + CONTROLLED_MODELING_PROGRAM_GUIDE + GEOMETRY_BUILD_CONTEXT_ABI
)

GEOMETRY_AUTHORING_PROGRAM_RESOURCE_DESCRIPTION = (
    "Complete public build(ctx) and independent evaluate(ctx) programming reference for ifc_geometry_resource_write. "
    "Read usage for exact ctx methods, limits, units, dependency behavior, construction cost guidance, recovery, and interface "
    "evidence boundaries; "
    "interface_schema describes each emitted model interface; return_contract describes the "
    "native shape return; examples contains complete draft creation and bounded update requests; execution_diagnostics covers Preview runtime failures. "
    "This static JSON contains no session, project, or business data."
)

IFC_GEOMETRY_SOURCE_FILES_DESCRIPTION = (
    f"Create bundle: 1-{MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS} unique .py members, main.py defining build(ctx) and for modeling_function interfaces.py defining evaluate(ctx), "
    f"{MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES:,} UTF-8 bytes per member and {MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES:,} total. "
    "Coordinates are metres in a right-handed resource-local frame. Read "
    + GEOMETRY_AUTHORING_PROGRAM_RESOURCE_URI
    + " for controlled methods, relative imports, interfaces and Shape return."
)

_GEOMETRY_CONNECTION_AUTHORING_GUIDANCE = (
    "For geometry requiring connection or mating checks, declare the relevant interfaces with "
    "ctx.define_interface in interfaces.py:evaluate(ctx) for functions or build(ctx) for fixed mapped authoring; an evaluator with no interfaces still must exist and return None. "
)

IFC_GEOMETRY_RESOURCE_WRITE_DESCRIPTION = (
    "Save a Geometry draft in the current authorized scope. mode=create from mapped_model/modeling_function supplies display_name, usage_instructions action=set and complete source_files; "
    "functions require source.parameter_schema_json action=set. Published create supplies exact resource_version_alias and may inherit name and usage, or override either field (usage uses set). "
    "mode=update supplies draft_ref and expected_edit_token from the latest Write/Info plus changed fields only. "
    "Omitted fields inherit; explicit null is invalid. usage_instructions and parameter_schema_json use set for complete replacement or edit for targeted changes; "
    "parameter edits set/remove top-level named parameters with explicit required membership. source_changes edits the current bundle atomically. "
    "Full merged content is validated before saving; diagnose the reported field/edit and use Preview for runtime checks. "
    + _GEOMETRY_CONNECTION_AUTHORING_GUIDANCE
    +
    "For batch use, measure representative construction costs, check repeated Booleans and reusable data, and read program_api for conditional methods. "
    "Preserve required shape and state permitted approximations in usage. "
    "A saved draft has not executed or passed runtime checks; Preview checks inputs, while ifc_authoring_publish independently publishes the definition. "
    "Recover uncertain saves through operation_list/get before retrying; on a stale edit token, read current draft Info first. "
    "Complete create request and build(ctx) API: read examples.modeling_function_create and usage at "
    + GEOMETRY_AUTHORING_PROGRAM_RESOURCE_URI
    + VERSION_USAGE_INSTRUCTIONS_NAVIGATION
)


IFC_GEOMETRY_AUTHORING_SOURCE_FILE_SOURCE_PATH_DESCRIPTION = (
    "Normalized relative POSIX Python member path. The bundle must contain exactly one "
    "main.py entry file; imports remain relative to this private bundle."
)

GEOMETRY_AUTHORING_INPUT_KEY_DESCRIPTION = "Unique name chosen for ctx.geometry(input_key) in this build; lowercase letters, digits and underscores, starting with a letter."
GEOMETRY_AUTHORING_INPUT_VERSION_DESCRIPTION = "Exact visible fixed mapped_model BREP version alias from ifc_geometry_resource_list/info; root aliases, modeling functions and meshes are invalid."
GEOMETRY_AUTHORING_INPUTS_DESCRIPTION = "Optional named exact fixed-BREP inputs, at most 8 and 32 MiB combined. The server freezes versions and verifies bytes; ctx.geometry(key) returns an independent local-metre shape copy. Draft updates inherit these inputs when omitted."
GEOMETRY_DRAFT_INPUTS_DESCRIPTION = GEOMETRY_AUTHORING_INPUTS_DESCRIPTION + " Supplied list replaces all inputs; empty clears, explicit null is invalid."
GEOMETRY_RESOURCE_INPUTS_DESCRIPTION = "Frozen named fixed-BREP dependencies with exact aliases usable by info/query/preview or a new create. Empty for versions without declared inputs."

IFC_GEOMETRY_AUTHORING_SOURCE_FILE_SOURCE_DESCRIPTION = (
    f"Exact UTF-8 Python source for this member, at most {MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES:,} bytes."
)

GEOMETRY_DRAFT_SOURCE_CHANGES_DESCRIPTION = (
    "Atomic edits to the current draft bundle: add_member, replace_member, delete_member or literal_edit. "
    "Read current members through Code; expected_edit_token protects the selected draft generation. Use exact "
    "old_text matches for literal edits. Omit to inherit; explicit null is invalid. The resulting bundle retains "
    f"main.py and stays within {MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS} files, "
    f"{MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES:,} UTF-8 bytes each and {MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES:,} total."
)
GEOMETRY_DRAFT_PARAMETER_SCHEMA_DESCRIPTION = (
    'JSON text encoding one action: set/value holds the complete closed modeling_function schema; edit/set maps top-level parameter names to {"schema":complete_entry,"required":boolean}, with edit/remove naming existing parameters. '
    "Inline create is set-only; update accepts set/edit or omission to inherit, never null. Declare types, bounds, defaults and units shared by evaluate(ctx) and build(ctx); invalid for mapped_model. "
    "Unchanged root metadata stays; final content retains the original 16 KiB budget and restricted dialect. Root default conflicts require a complete set. Full supported schema and examples: "
    + GEOMETRY_AUTHORING_PROGRAM_RESOURCE_URI
)


DIRECT_IFC_GEOMETRY_RESOURCE_LIST_FIRST_INPUT_RESOURCE_KINDS_DESCRIPTION = (
    "Optional kind OR-filter. Omit or use an empty list to discover both mapped_model "
    "and modeling_function resources."
)

DIRECT_IFC_GEOMETRY_RESOURCE_LIST_FIRST_INPUT_PAGE_SIZE_DESCRIPTION = (
    "First-page resource count; default 50 and maximum 100."
)

DIRECT_IFC_GEOMETRY_RESOURCE_LIST_CURSOR_INPUT_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by this tool; submit it unchanged and omit first-page filters."
)

IFC_GEOMETRY_RESOURCE_ROOT_TARGET_TARGET_KIND_DESCRIPTION = (
    "Read one stable geometry-resource root and its bounded version cards."
)

IFC_GEOMETRY_RESOURCE_ROOT_TARGET_RESOURCE_ALIAS_DESCRIPTION = (
    "Stable safe resource alias selected from ifc_geometry_resource_list."
)

IFC_GEOMETRY_RESOURCE_VERSION_TARGET_TARGET_KIND_DESCRIPTION = (
    "Read one exact immutable successful geometry-resource version."
)

IFC_GEOMETRY_RESOURCE_VERSION_TARGET_RESOURCE_VERSION_ALIAS_DESCRIPTION = (
    "Exact successful version alias returned by geometry resource list or info."
)

DIRECT_IFC_GEOMETRY_RESOURCE_INFO_INPUT_TARGET_DESCRIPTION = (
    "Complete resource_root or resource_version target branch."
)

GEOMETRY_VERSION_SOURCE_READ_BASE_RESOURCE_VERSION_ALIAS_DESCRIPTION = (
    "Exact successful version alias from ifc_geometry_resource_info; that version must "
    "contain a source_bundle asset."
)

GEOMETRY_VERSION_SOURCE_READ_BASE_SOURCE_PATH_DESCRIPTION = (
    "Exact normalized relative source member path selected from resource version info; "
    "the server never guesses or concatenates multiple files."
)


IFC_GEOMETRY_SOURCE_EDIT_OLD_TEXT_DESCRIPTION = (
    "Exact source text expected once in the parent query source."
)

IFC_GEOMETRY_SOURCE_EDIT_NEW_TEXT_DESCRIPTION = (
    "Exact replacement text; use an empty string to delete old_text."
)

IFC_GEOMETRY_RESOURCE_VERSION_QUERY_TARGET_TARGET_KIND_DESCRIPTION = (
    "Query one exact immutable mapped-model Geometry Resource version."
)

IFC_GEOMETRY_RESOURCE_VERSION_QUERY_TARGET_RESOURCE_VERSION_ALIAS_DESCRIPTION = (
    "Exact successful mapped_model version selected from geometry resource info; not a "
    "modeling_function definition."
)

DIRECT_IFC_GEOMETRY_QUERY_FRESH_INPUT_MODE_DESCRIPTION = (
    "Reauthorize one closed exact target and save new query source."
)

DIRECT_IFC_GEOMETRY_QUERY_FRESH_INPUT_TARGET_DESCRIPTION = (
    "Select geometry_resource_version for one exact mapped_model. "
    "Select geometry_inspection for one saved inspection_ref with explicit selection and coordinate_space. "
    "A modeling_function definition and temporary Preview PNG bytes are not query targets."
)

GEOMETRY_QUERY_PROGRAM_RESOURCE_URI = (
    "cad2bim://contracts/ifc_geometry_query_python/program_api"
)

DIRECT_IFC_GEOMETRY_QUERY_FRESH_INPUT_CODE_DESCRIPTION = (
    "Read-only Python source defining query(ctx), at most 65,536 UTF-8 bytes. It investigates "
    "the selected exact private target in METRE coordinates and returns JSON-compatible data; "
    "it cannot publish or modify Geometry or IFC. Read "
    + GEOMETRY_QUERY_PROGRAM_RESOURCE_URI
    + " for the complete ctx API, BREP/mesh rules, native-shape versus JSON-return boundary, "
    "imports, limits, target schemas, and examples."
)

GEOMETRY_QUERY_EFFICIENCY_GUIDANCE = (
    "Inspect curve types before type-specific calls: radius() requires an analytic circle. "
    "Native curve parameters generally differ from arc length. Repeated arc-length inversion "
    "and global B-spline distance/extrema searches can be expensive; reuse same-query "
    "intermediates and use conservative candidate bounds when appropriate. Finite samples "
    "do not certify global distance, wall thickness or continuity. This limits the claim a sample supports; "
    "it does not require a global proof for every query. Check only properties required by the target. "
    "Choose measurements that observe the declared function and the actual construction risk, such as "
    "material retention at a relevant point, a section or a contact relationship. Reuse same-build or "
    "saved evidence and backend checks; do not add a measurement after every operation by default. "
    "Volume equality is not a general Boolean-operation invariant, and a volume computed from an "
    "invalid shape is not reliable physical evidence. "
    "A Query error calls for a corrected query, not a new product build; reuse the same saved target for "
    "supplementary measurement when available. Inspect resource_diagnostic "
    "to distinguish CPU/wall and RSS exhaustion before making a cause-related change. "
)

GEOMETRY_QUERY_PROGRAM_USAGE = (
    "Define query(ctx). ctx exposes summary(), private_geometry(), vertices(), triangles(), "
    "face_types(), edge_types(), and display_diagnostics(). Coordinates are METRE. A mapped_model uses its right-handed "
    "local frame. For BREP, private_geometry() returns a memoized detached cadquery.Shape computation "
    "copy and .wrapped exposes its TopoDS_Shape. Deriving from or changing that copy cannot change "
    "summary(), vertices(), triangles(), face_types(), or edge_types(), which describe the source. "
    "For indexed mesh use summary(), vertices(), and triangles(), not its private runtime type. "
    "A geometry_inspection target reuses actual saved Geometry/Family build bytes and existing "
    "context. Result coordinates follow context.source.result_coordinate_space: a sample "
    "without datum is geometry_local; a sample with datum and a Family result use project_world. "
    "source(), components(), fragments(), interfaces(), and checks() return private "
    "JSON copies of the original backend facts. "
    "An exact geometry_resource_version or family_standard_model target reads saved geometry "
    "without a saved inspection context; interfaces() and checks() may return null. This does "
    "not establish that a Family declares no interfaces. Read the exact Family's "
    "family_contract_json through ifc_product_family_info for declarations, or use a retained "
    "geometry_inspection target for "
    "actual saved interface frames. "
    "private_fragment(fragment_key, "
    "coordinate_space='result') returns a memoized detached cadquery.Shape for that exact "
    "fragment; fragment_local is also supported. Result placement applies only the recorded "
    "rigid transform: instance bytes already contain their receipt scale and must not be scaled "
    "again. sections(plane, offsets_m=(0.0,)) uses the query's saved object selection and "
    "coordinates. plane is {'origin_m':[x,y,z],'normal':[nx,ny,nz]}; normal is finite, nonzero "
    "and dimensionless. Up to four distinct finite metre offsets share source parsing and the "
    "same section core as Preview. The JSON contains geometry_sha256, selection, coordinates "
    "and measurements, with material regions, boundary/curve facts, actual planes and "
    "diagnostics; it contains no image or native Shape. Areas are sums of per-object material "
    "regions, not a fused union. Indexed mesh sections return explicit unsupported facts; "
    "existing mesh query methods retain their legal uses. Detached computations do not change "
    "original summary/context or later section facts. "
    "display_diagnostics() reuses the current selected display mesh and its same-meshing ordered pcurves, "
    "including the selected initial/detached-retry attempt. It reads the original saved input, not private_geometry() "
    "mutations. Whole remains the saved whole; component selections report each actual fragment with local face indices. "
    "The bounded JSON gives selection/coordinates, complete or incomplete coverage, object face/completed/unchecked counts, "
    "and abnormal or unchecked faces with surface_type, native area, bbox_m, selected attempt, area/retry facts, "
    "native_boundary checks/status/limitation and discrete_boundary status/crossing_segment_pairs/intersection_bbox_m. "
    "Crossing counts are strict interior segment pairs, not distinct spatial intersection counts. Endpoint touch and "
    "coincidence are not classified as crossing. Deep crossing checks support nonperiodic planar domains; other surfaces "
    "are explicitly unsupported, and unavailable same-meshing data is inconclusive with unknown counts. Native "
    "no_issue_reported and discrete no_crossing_reported do not certify continuous curves, material or business correctness. "
    "Explicit calls check all supported selected faces, independent of area warnings. Preview only confirms existing quality "
    "candidates. Confirmed boundary failures still return diagnostic facts here; vertices()/triangles() reject a mesh once "
    "its failure is confirmed. Repeated calls reuse one query-local mesh/check cache and return private JSON copies. "
    "Minimal diagnostic program: def query(ctx): return ctx.display_diagnostics(). Keep the same saved target, "
    "selection and coordinates; no I/O, internal imports or third-party geometry helpers are needed. "
    "Imports are limited to math, cadquery, and OCP. Module scope permits imports, function "
    "definitions, and literal constants only; put calculations inside query(ctx) or helpers. "
    "Top-level classes or executable statements, reflection including dir, private runtime "
    "attributes, and I/O are unsupported. Query source and the complete canonical JSON result are "
    "each limited to 65,536 UTF-8 bytes. Section results can include large boundary point arrays; "
    "return the selected areas, boundary counts, curve facts and diagnostics needed for your claim, "
    "not every point by default. QUERY_RESULT_BUDGET_EXCEEDED requires reducing the program return; "
    "smaller page_size does not reduce the complete stored-result limit. Keep the same saved target. "
    "Worker execution is supervised at 180 seconds wall, "
    "180 seconds CPU, 3 GiB RSS, 128 MiB private scratch, 8 threads, and 64 open file descriptors. "
    "The outer Direct read boundary includes the admission budget in the tool description plus 10 "
    "seconds for response projection. GEOMETRY_QUERY_RESOURCE_LIMIT is terminal and non-retryable: "
    "do not retry unchanged source or split one substantive measurement only to evade the limit. "
    "resource_diagnostic identifies the trigger, configured limits, effective wall deadline, "
    "sampled CPU/RSS and other peaks, and elapsed worker time when available. Sampled peaks are "
    "lower bounds; null means unknown, not zero. "
    + GEOMETRY_QUERY_EFFICIENCY_GUIDANCE
    + " Measurement completion and material inspection: cad2bim://contracts/common/preview#geometry_inspection."
)

GEOMETRY_QUERY_RETURN_CONTRACT = (
    "ctx.private_geometry() returns a native cadquery.Shape for BREP investigation, but query(ctx) "
    "must return JSON-compatible data. The server canonicalizes and persists that program return; "
    "only a top-level list is paged, while any other JSON value is one result. The tool response is "
    "an IfcGeometryQueryPythonResult wrapper with the durable attempt, source SHA, exact target, "
    "summary, canonical result_json, and optional next_cursor. Native Shape values are never the "
    "tool JSON return and cannot be published through this read-only query."
)

GEOMETRY_QUERY_PROGRAM_RESOURCE_DESCRIPTION = (
    "Complete ifc_geometry_query_python programming reference. Read usage for the exact ctx API, "
    "coordinate frames, execution limits, and investigation boundaries; target_schemas contains "
    "the generated mapped-model and retained-inspection target Schemas; return_contract separates the native "
    "BREP inspection value from the program and tool JSON returns; examples contains complete "
    "bounding-box and OCP plane-section requests."
)

DIRECT_IFC_GEOMETRY_QUERY_FRESH_INPUT_PAGE_SIZE_DESCRIPTION = (
    "Result records requested on the first page; default 100, maximum 200."
)

DIRECT_IFC_GEOMETRY_QUERY_REPAIR_INPUT_MODE_DESCRIPTION = (
    "Patch the exact source of one parent GeometryQueryAttempt while retaining that "
    "attempt's exact target."
)

DIRECT_IFC_GEOMETRY_QUERY_REPAIR_INPUT_PARENT_ATTEMPT_ALIAS_DESCRIPTION = (
    "Exact parent attempt alias returned by ifc_geometry_query_python."
)

DIRECT_IFC_GEOMETRY_QUERY_REPAIR_INPUT_EDITS_DESCRIPTION = (
    "One to 16 non-overlapping exact source replacements."
)

DIRECT_IFC_GEOMETRY_QUERY_REPAIR_INPUT_PAGE_SIZE_DESCRIPTION = (
    "Result records requested after repair; default 100, maximum 200."
)

DIRECT_IFC_GEOMETRY_QUERY_CURSOR_INPUT_MODE_DESCRIPTION = (
    "Continue the exact saved result set without rerunning Python."
)

DIRECT_IFC_GEOMETRY_QUERY_CURSOR_INPUT_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by this tool; submit it unchanged."
)

IFC_GEOMETRY_RESOURCE_VERSION_CARD_HAS_PREVIEW_DESCRIPTION = (
    "Whether ifc_geometry_preview can serve this exact version. Mapped models render "
    "their exact canonical geometry; modeling functions validate one submitted sample "
    "against the exact source bundle and parameter schema before rendering it."
)

IFC_GEOMETRY_SOURCE_FILE_RESULT_TEXT_AVAILABLE_DESCRIPTION = (
    "True only when this exact source member is valid bounded UTF-8 and supports "
    "ifc_geometry_resource_source_read full, lines, and search modes."
)

IFC_GEOMETRY_RESOURCE_VERSION_INFO_RESULT_PREVIEW_AVAILABLE_DESCRIPTION = (
    "Whether ifc_geometry_preview can serve this exact version. This is true for ready "
    "mapped models even without a separately stored PREVIEW asset, and for ready modeling "
    "functions with exact source and parameter schema but no stored sample geometry."
)

IFC_GEOMETRY_RESOURCE_INFO_RESULT_INFO_DESCRIPTION = (
    "Complete resource_root or resource_version result branch."
)

GEOMETRY_MAPPED_PREVIEW_DATUM_DESCRIPTION = (
    "Optional explicit local-geometry to project-world rigid datum, translation in metres. "
    "When supplied, renders the transformed canonical BREP; omit to render exact mapped "
    "geometry in its resource-local coordinates with identity placement."
)

GEOMETRY_FUNCTION_PREVIEW_DATUM_DESCRIPTION = (
    "Optional explicit local-geometry to project-world rigid datum, translation in metres; "
    "applied to the canonical sample and its model interfaces after parameter normalization."
)

GEOMETRY_PREVIEW_SAMPLE_PARAMETERS_RESULT_DESCRIPTION = (
    "Canonical JSON object of the exact normalized sample parameters, including defaults, "
    "at most 16 KiB UTF-8. Present for current dynamic samples, including an empty {}; absent for "
    "mapped models."
)

IFC_GEOMETRY_PREVIEW_RESULT_DERIVED_ONLY_DESCRIPTION = (
    "Preview is a rebuildable visual aid, never geometry authority."
)

IFC_GEOMETRY_QUERY_PYTHON_RESULT_TARGET_DESCRIPTION = (
    "Closed, honest exact-target projection for this durable query attempt."
)

IFC_GEOMETRY_QUERY_PYTHON_RESULT_RESULT_JSON_DESCRIPTION = (
    "Bounded canonical JSON text from the persisted successful result Artifact."
)

IFC_GEOMETRY_RESOURCE_LIST_DESCRIPTION = (
    "Discover ready reusable geometry roots visible to the current Direct scope. Only "
    "roots with at least one successful immutable version appear. SYSTEM resources are prepared "
    "through trusted Backend seed or curator exact-resource copy; this tool cannot seed, "
    "publish, promote, create, or repair "
    "resources, and "
    "an empty production SYSTEM catalog is valid. First page: provide optional "
    "resource_kinds and page_size. Continuation: provide only next_cursor unchanged; it "
    "retains the exact directory boundary and does not accept page numbers, OFFSET, custom "
    "sorting, or edited filters. Cards expose only safe aliases, kind, owner-scope label, "
    "display name, bounded summary, latest successful version alias, and time. Display "
    "names are not exact identity; use resource_alias or resource_version_alias in later "
    "tools. No database ID, ArtifactVersion, storage path, Task/Run identity, or other "
    "scope fact is returned." + READ_BATCH_DESCRIPTION
)

IFC_GEOMETRY_RESOURCE_INFO_DESCRIPTION = (
    "target_kind=draft uses draft_ref from Write and returns the current edit_token, source file "
    "directory and editable definition without executing code; read source via code_list geometry_draft "
    "and code_read, then pass edit_token as expected_edit_token for a guarded draft update. "
    "Read a ready geometry resource by one closed target branch. target_kind=resource_root "
    "uses resource_alias from ifc_geometry_resource_list and returns bounded successful "
    "version cards. target_kind=resource_version uses an exact resource_version_alias and "
    "returns its complete nonblank version-owned usage_instructions, METRE/right-handed local basis, "
    "geometry format, bbox and topology when a representative shape exists; these shape fields are null "
    "for a modeling_function published without a sample, while source and parameter schema remain available. "
    "It also returns source/preview availability, "
    "frozen geometry_inputs with reusable "
    "exact version aliases, and bounded modeling-function "
    "parameter schema text when applicable. Saved Preview Operations own reusable reports; "
    "each invocation checks its actual source, parameters, dependencies and result. The interfaces field reads "
    "the fixed version manifest; it may be empty for a modeling_function and is not the interface result "
    "for arbitrary parameters. Read evaluated sample interfaces from Geometry Preview or same-execution Family "
    "instance evidence. Use the exact instructions, schema, actual interfaces and frozen "
    "dependencies to identify the local shape, fixed or parameterized parts, confirmed source data and unit "
    "conversion for each input, local anchor/interface frames, and interface checks before preparing a build "
    "or Preview. The parameter schema remains authoritative for typed bounds; never guess dimensions, "
    "interfaces or external operations. SYSTEM entries are prepared through trusted Backend "
    "seed or curator exact-resource copy, never by this read. The response is "
    "exact ready "
    "Catalog information; it does not expose failed authoring attempts, database IDs, "
    "ArtifactVersion IDs, physical paths, or identities outside the current scope. This "
    "tool does not modify a resource, IFC, or query attempt." + READ_BATCH_DESCRIPTION
)

IFC_GEOMETRY_RESOURCE_SOURCE_READ_DESCRIPTION = (
    "Read the exact source_bundle of one successful geometry resource version. Select the "
    "resource_version_alias and one exact normalized source_path from the bounded "
    "source_files returned by ifc_geometry_resource_info; the server never guesses a file "
    "or concatenates a multi-file bundle. Use these text modes only when that member has "
    "text_available=true. A raw binary IFC/BRep member has text_available=false and "
    "full, lines, or search returns GEOMETRY_SOURCE_NOT_TEXT; investigate its normalized "
    "canonical geometry with ifc_geometry_query_python instead. full returns complete source "
    "when bounded; lines uses 1-based start_line plus line_count; search is case-sensitive "
    "exact literal matching with bounded context and does not support regex or fuzzy "
    "search. Every response identifies the whole selected source member by source_sha256. "
    "A mapped model "
    "may legitimately have no source_bundle, which returns a stable unavailable error; "
    "failed authoring source belongs to later authoring-read tools. This is an exact "
    "read-only view: it cannot patch source, create a version, publish a resource, or "
    "modify IFC, and it never returns an ArtifactVersion ID or host path." + READ_BATCH_DESCRIPTION
)

IFC_GEOMETRY_QUERY_PYTHON_DESCRIPTION = (
    "Run bounded read-only Python investigation against a private copy of one exact "
    "geometry target. Read "
    + GEOMETRY_QUERY_PROGRAM_RESOURCE_URI
    + " for the complete API, target Schema, native-versus-JSON return "
    "contract, limits, and detailed examples. fresh requires one complete target plus query(ctx) code. "
    "target_kind=geometry_resource_version requires resource_version_alias for one exact "
    "mapped_model version; modeling_function and one-time Preview images are not query targets. "
    "target_kind=geometry_inspection requires the exact inspection_ref from a solidifying "
    "Preview or its operation_list/get result, plus the closed selection and coordinate_space "
    "objects. It investigates that saved build without rerunning its source. "
    "target_kind=family_standard_model takes a published family_version_alias with a formal standard model; "
    "it queries that saved whole BREP in local metres without solving or requiring a Preview. "
    "Rejected targets create no repairable attempt. Once accepted, failures retain source for repair. query(ctx) may use "
    "the resource's read-only ctx methods; BREP private_geometry() is a native cadquery.Shape, "
    "while the program must return canonical JSON-compatible data. All coordinates are METRE. "
    "Use ctx.display_diagnostics() on the same saved target for display warnings or boundary failures. "
    "GEOMETRY_QUERY_RESOURCE_LIMIT is terminal and non-retryable; inspect its resource_diagnostic "
    "and make a cause-related change rather than retrying unchanged source. "
    + GEOMETRY_QUERY_BASIC_USAGE_EXAMPLE
    + " Only a top-level list is paged; any other JSON value is one result. "
    "repair requires parent_attempt_alias and exact edits; the backend verifies the immutable "
    "parent source bytes without a caller-supplied SHA, and "
    "always reuses the parent's exact target. cursor submits only the unchanged returned "
    "cursor and pages the saved top-level list without rerunning Python. Neither branch "
    "accepts a resource alias or internal target ID. Results identify "
    "the exact target, including unchanged inspection_ref/selection/coordinate_space across repair "
    "and cursor. Results and diagnostics return safe attempt aliases, never "
    "ArtifactVersion IDs or paths. private_geometry() memoizes a detached computation copy; "
    "local derivations and cache changes are allowed. The other ctx methods describe the "
    "source input. There is no geometry publication path: the exact target, Catalog, IFC "
    "CURRENT, database, and Storage remain unchanged. QueryAttempt/source/result audit "
    "records do not publish or modify geometry or IFC." + READ_BATCH_DESCRIPTION
)

__all__ = [
    "GEOMETRY_AUTHORING_EXECUTION_AND_DIAGNOSTIC_GUIDANCE",
    "GEOMETRY_AUTHORING_PROGRAM_RESOURCE_DESCRIPTION",
    "GEOMETRY_AUTHORING_PROGRAM_RESOURCE_URI",
    "GEOMETRY_AUTHORING_PROGRAM_USAGE",
    "GEOMETRY_BUILD_CONTEXT_ABI",
    "GEOMETRY_BUILD_RETURN_CONTRACT",
    "GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE",
    "GEOMETRY_MODELING_FUNCTION_SHORT_CREATE_EXAMPLE",
    "GEOMETRY_MODELING_FUNCTION_PARAMETER_SCHEMA_EXAMPLE",
    "GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE",
    "GEOMETRY_MODELING_FUNCTION_VALIDATION_SAMPLE_EXAMPLE",
    "GEOMETRY_QUERY_BASIC_USAGE_EXAMPLE",
    "GEOMETRY_QUERY_FRESH_EXAMPLE",
    "GEOMETRY_QUERY_PROGRAM_RESOURCE_DESCRIPTION",
    "GEOMETRY_QUERY_PROGRAM_RESOURCE_URI",
    "GEOMETRY_QUERY_PROGRAM_USAGE",
    "GEOMETRY_QUERY_RETURN_CONTRACT",
    "GEOMETRY_QUERY_SECTION_FRESH_EXAMPLE",
    "GEOMETRY_QUERY_SECTION_SOURCE_EXAMPLE",
    "GEOMETRY_QUERY_SOURCE_EXAMPLE",
    "IFC_GEOMETRY_PREVIEW_DESCRIPTION",
    "IFC_GEOMETRY_RESOURCE_WRITE_DESCRIPTION",
    "IFC_GEOMETRY_SOURCE_FILES_DESCRIPTION",
]


IFC_GEOMETRY_PREVIEW_DESCRIPTION += _runtime_budget_description(_GEOMETRY_PREVIEW_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
IFC_GEOMETRY_QUERY_PYTHON_DESCRIPTION += _runtime_budget_description(_GEOMETRY_QUERY_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
