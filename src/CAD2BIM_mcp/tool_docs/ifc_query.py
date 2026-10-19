"""Provider-visible usage text for the ifc query capability family."""

from CAD2BIM_mcp.tool_docs.common import runtime_budget_description as _runtime_budget_description
from CAD2BIM_backend.domain.runtime_execution import (
    IFC_QUERY_EXECUTION as _IFC_QUERY_EXECUTION,
)

import json

from CAD2BIM_mcp.tool_docs.common import READ_BATCH_DESCRIPTION, IFC_INSTALLATION_EVIDENCE_GUIDANCE


IFC_QUERY_SOURCE_EXAMPLE = """def query(ctx):
    projects = ctx.model().by_type("IfcProject")
    counts = {"IfcProject": 0}
    counts["IfcProject"] = len(projects)
    labels = ["unset"]
    labels[0] = projects[0].Name if projects else None
    discarded = [0, 1]
    del discarded[0]
    return {"counts": counts, "first_project_name": labels[0]}
"""

IFC_QUERY_FRESH_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
        "inputs": [],
        "code": IFC_QUERY_SOURCE_EXAMPLE,
        "page_size": 100,
    },
    separators=(",", ":"),
)

IFC_QUERY_USAGE_EXAMPLE = (
    "Complete IFC-only fresh example (replace direct_001 with the alias returned by "
    "runtime_open_direct_session): "
    + IFC_QUERY_FRESH_EXAMPLE
    + " inputs defaults to [] and page_size defaults to 100; both are shown so their "
    "placement in the outer tool object is explicit."
)

IFC_QUERY_PROGRAM_RESOURCE_URI = "cad2bim://contracts/ifc_query_python/program_api"


IFC_QUERY_EXACT_INPUT_INPUT_NAME_DESCRIPTION = (
    "Unique local read-only name used with ctx.table, ctx.batches, or ctx.json."
)

IFC_QUERY_EXACT_INPUT_PACKAGE_ALIAS_DESCRIPTION = (
    "Exact visible immutable Package alias selected from the current catalog."
)

IFC_QUERY_EXACT_INPUT_PAYLOAD_NAME_DESCRIPTION = (
    "Exact Parquet or JSON Payload name selected from the Package description."
)

IFC_QUERY_SOURCE_EDIT_OLD_TEXT_DESCRIPTION = (
    "Exact source text expected once in the parent attempt source."
)

IFC_QUERY_SOURCE_EDIT_NEW_TEXT_DESCRIPTION = (
    "Exact replacement text; use an empty string to delete old_text."
)

DIRECT_IFC_QUERY_FRESH_INPUT_MODE_DESCRIPTION = (
    "Fix the server-resolved CURRENT IFC and zero to 16 exact Package Payload inputs, "
    "then persist and execute new read-only query source."
)

DIRECT_IFC_QUERY_FRESH_INPUT_INPUTS_DESCRIPTION = (
    "Zero to 16 exact read-only Package Payload bindings. Each input_name binds one "
    "package_alias plus payload_name; omit for an IFC-only investigation."
)

DIRECT_IFC_QUERY_FRESH_INPUT_CODE_DESCRIPTION = (
    "Python source defining query(ctx), at most 65,536 UTF-8 bytes. The server persists this exact "
    "source before validation or execution. It reads the server-fixed CURRENT IFC and optional named "
    "exact Package Payload bindings, and must return JSON-compatible data without modifying IFC or "
    "publishing data. Read "
    + IFC_QUERY_PROGRAM_RESOURCE_URI
    + " for the complete ctx API, input-binding schema, imports, mutation boundary, return contract, "
    "and example."
)

DIRECT_IFC_QUERY_FRESH_INPUT_PAGE_SIZE_DESCRIPTION = (
    "Result records requested on the first page; default 100, maximum 200."
)

DIRECT_IFC_QUERY_REPAIR_INPUT_MODE_DESCRIPTION = (
    "Patch one parent attempt's exact persisted source while retaining its exact IFC "
    "revision and exact Package Payload inputs."
)

DIRECT_IFC_QUERY_REPAIR_INPUT_PARENT_ATTEMPT_ALIAS_DESCRIPTION = (
    "Exact visible parent alias returned by ifc_query_python."
)

DIRECT_IFC_QUERY_REPAIR_INPUT_EDITS_DESCRIPTION = (
    "One to 16 non-overlapping exact source replacements."
)

DIRECT_IFC_QUERY_REPAIR_INPUT_PAGE_SIZE_DESCRIPTION = (
    "Result records requested after repair; default 100, maximum 200."
)

DIRECT_IFC_QUERY_CURSOR_INPUT_MODE_DESCRIPTION = (
    "Continue the exact persisted result without rerunning Python."
)

DIRECT_IFC_QUERY_CURSOR_INPUT_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by this tool; submit it unchanged."
)

IFC_QUERY_PYTHON_RESULT_RESULT_JSON_DESCRIPTION = (
    "Bounded RFC 8785 canonical JSON array containing the current result page. "
    "A program returning an object or scalar produces a singleton array; "
    "a top-level list contributes its elements to the paged array."
)

IFC_READONLY_PROGRAM_CONTRACT = (
    "Use this fixed saved snapshot to compare affected installation objects, their world poses "
    "and required Body/relationship facts. Historical interface reports retain original coordinates "
    "after later placement; recover current evidence before deciding a coordinated write. "
    "ctx.model() is a real process-private IfcOpenShell model. Normal read APIs include "
    "by_type, by_id, by_guid, traverse, get_inverse, schema, and header inspection; these "
    "examples are not a per-function allowlist. Local dictionary and list index assignment or "
    "deletion is allowed for request-local aggregation; set.add() and list.remove() are also "
    "allowed on local containers. Attribute assignment, IFC "
    "mutation methods, low-level wrapper or transaction access, and private-object writes "
    "are rejected. An IFC entity index write or deletion is rejected during execution or "
    "by the private transaction mutation audit and never creates a revision. Do not open "
    "or save files, call model.write or arbitrary exports, or use reflection, network, "
    "subprocesses, host paths, database/Storage access, or IFC publication. Static checks "
    "reject known write and file operations; compiling successfully does not prove read-only "
    "execution. Runtime checks audit transaction mutations and compare the private model "
    "before and after execution. Detected changes, including recorded edits later restored, "
    "or failure of a required audit discard the whole result and all candidate outputs. "
    "Mutation reports IFC_QUERY_MUTATION_DETECTED. The private object has no durable IFC "
    "write or commit path. A code-validation diagnostic identifies the forbidden source "
    "position and AST operation when available."
)

IFC_SEMANTIC_EXTRACTION_GUIDANCE = (
    "Keep property absence, an explicitly present null value, and a usable value distinct. "
    "Retain the actual instance or Type source, raw value and actual unit, including an explicit "
    "property-unit override or the applicable project unit. Resolve inheritance using the task's "
    "declared priority; an explicit instance null is not an absent property and must not silently "
    "fall back to Type unless that rule permits it. Do not use truthiness to discard a valid zero. "
    "Discover actual property/set names and relevant relationships, then apply the task predicate "
    "to actual values across its required scope. A preselected name list, expected-count assertion "
    "or old IFC class is not a substitute for that filtering. Unknown or unsupported readings remain "
    "unknown rather than becoming zero, false, an invented value or a guessed unit. "
    "An occurrence's HasAssociations does not enumerate associations of its assigned Type. "
    "For effective material, inspect the actual occurrence and Type sources under the applicable "
    "semantics; absence of occurrence associations alone does not establish missing material."
)

IFC_QUERY_PROGRAM_USAGE = (
    "Define query(ctx). The server fixes "
    "CURRENT to one exact immutable revision before execution. Explicitly bound exact Parquet or JSON "
    "Package Payloads are available only through ctx.table(name), ctx.batches(name), and ctx.json(name); "
    "ctx.log(message) accepts bounded diagnostics. ctx.host_sections(product_global_id) reads actual "
    "straight IfcPipeSegment Body sections in metres along the original Axis and returns "
    "sections[].body_section_ref for direct CURRENT endpoint selection. It supports one Body with 1-64 "
    "disjoint two-point IfcSweptDiskSolid items, identical radii, and omitted StartParam/EndParam; "
    "unsupported or ambiguous geometry returns a diagnostic and never inferred material. "
    "Allowed import roots are collections, decimal, fractions, functools, ifcopenshell, itertools, "
    "json, math, operator, pyarrow, re, and statistics. "
    + IFC_READONLY_PROGRAM_CONTRACT
    + " Return JSON-compatible data: use Python list for arrays, string-key dictionaries for objects, "
    "and finite JSON scalar values. Tuples and native IFC entities are rejected, including when nested. "
    "get_info(recursive=True) converts entity references but can retain tuple aggregates, so it does "
    "not guarantee a JSON-safe result. Explicitly project primitives and lists, or convert an already "
    "serializable structure with json.loads(json.dumps(value)); do not stringify native objects. "
    + IFC_SEMANTIC_EXTRACTION_GUIDANCE + " "
    + IFC_INSTALLATION_EVIDENCE_GUIDANCE + " "
    "Result validation errors identify a bounded json_path, value_type, and reason without dumping values. "
    "Return data no larger than the fixed 65,536-byte "
    "server result budget. Each fresh or repair executes against the exact IFC with its normal "
    "read and mutation safeguards; a smaller page_size bounds output, not model initialization or "
    "query computation. Use Apply's committed_revision metadata for the original commit's size, "
    "ifc_model_info for latest model facts, and Data queries for report-only statistics. Avoid "
    "to_string().encode() solely to measure file size. Combine related IFC assertions when they "
    "need the same revision, and return aggregates plus bounded anomalies."
)

IFC_QUERY_RETURN_CONTRACT = (
    "query(ctx) returns JSON-compatible data, not an ifcopenshell model or the tool response object. "
    "The server canonicalizes and persists that program return. Only a top-level list is paged; any "
    "other JSON value is one result. The tool then returns an IfcQueryPythonResult wrapper containing "
    "attempt_alias, optional parent_attempt_alias, source_sha256, succeeded status, bounded summary, "
    "canonical result_json, and optional next_cursor. result_json always encodes the current result "
    "page as a JSON array: a returned object becomes [object], a scalar becomes [scalar], and a "
    "top-level list supplies the page elements. Decode this array before reading fields on its items. "
    "cursor reads the persisted list without rerunning "
    "Python. Native IFC objects and Package tables are read inputs and cannot be returned directly."
)

IFC_QUERY_PROGRAM_RESOURCE_DESCRIPTION = (
    "Complete ifc_query_python programming reference. Read usage for the exact CURRENT IFC and Package "
    "ctx API, allowed imports, host-section support, and read-only mutation boundary; "
    "input_binding_schema is the independent Direct IfcQueryExactInput root; return_contract separates "
    "the program JSON value from the IfcQueryPythonResult wrapper; examples contains the complete "
    "IFC-only fresh request."
)

IFC_QUERY_PYTHON_DESCRIPTION = (
    "Run bounded read-only Python investigation using ctx.model(), a real process-private "
    "IfcOpenShell object loaded from the current Run's IFC. Do not modify it or open/save files; "
    "known writes are rejected statically and runtime mutation checks discard results when model changes are detected. "
    "The object has no durable IFC write or commit path. In fresh mode the server fixes CURRENT to one server-only exact immutable "
    "revision, resolves zero to 16 explicitly named exact Package Payload bindings in the "
    "current scope, and persists the exact source before AST validation or Worker execution. Each "
    "binding supplies input_name, package_alias, and payload_name; omit inputs for an IFC-only query. "
    "Every fresh/repair pays exact model read and read-only safeguards; page_size only bounds output. "
    "Use Apply committed_revision for the original file size and Data queries for report-only statistics. "
    "Combine necessary checks against the same revision; after a timeout remove or change the diagnosed expensive operation before retrying. "
    "Read "
    + IFC_QUERY_PROGRAM_RESOURCE_URI
    + " for the complete ctx API, the independent input-binding Schema root, the read-only mutation "
    "boundary, and the program-versus-tool JSON return contract. Define query(ctx) and return "
    "JSON-compatible data. Only a top-level list is paged; another JSON value is one result. repair "
    "requires parent_attempt_alias and one to 16 exact edits; the backend verifies the "
    "immutable parent source bytes without a caller-supplied SHA. It "
    "reuses the parent's exact IFC revision and exact Package Payloads, never a newer "
    "CURRENT. cursor submits only the unchanged returned cursor, reads the exact persisted "
    "result, and does not rerun Python. Success returns only attempt_alias, optional parent "
    "alias, exact source SHA-256, the fixed succeeded status, bounded summary, canonical "
    "result_json, and next_cursor. result_json always encodes the current page as a JSON array, "
    "including a singleton array for a returned object or scalar. "
    "A source-persisted syntax, execution, budget, mutation, or output failure is a stable "
    "ToolError whose diagnostic includes the safe attempt_alias so its exact source can be "
    "read and repaired. Mutation of the private copy is rejected. This tool does not "
    "create an IFC revision, does not advance CURRENT, does not call the durable writer, "
    "does not create a critical "
    "IFC Operation, does not publish a Package, and does not expose a revision/model/"
    "Artifact identity, writer "
    "identity, internal Package identity, Storage location, or host path. It appends only "
    "the QueryAttempt and exact source/result audit facts required for repair, cursor, "
    "restart recovery, and response-loss reconciliation. "
    + IFC_QUERY_USAGE_EXAMPLE
    + READ_BATCH_DESCRIPTION
)


__all__ = [
    "IFC_QUERY_FRESH_EXAMPLE",
    "IFC_QUERY_PROGRAM_RESOURCE_DESCRIPTION",
    "IFC_QUERY_PROGRAM_RESOURCE_URI",
    "IFC_QUERY_PROGRAM_USAGE",
    "IFC_QUERY_PYTHON_DESCRIPTION",
    "IFC_QUERY_RETURN_CONTRACT",
    "IFC_QUERY_SOURCE_EXAMPLE",
]


IFC_QUERY_PYTHON_DESCRIPTION += _runtime_budget_description(_IFC_QUERY_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
