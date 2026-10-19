"""Provider-visible usage text for the data transform capability family."""

import json

from CAD2BIM_backend.domain.data_transform import (
    MAX_TRANSFORM_CODE_BYTES as _MAX_TRANSFORM_CODE_BYTES,
    DataTransformWorkerPolicy as _DataTransformWorkerPolicy,
    PythonArrowWorkerPolicy as _PythonArrowWorkerPolicy,
)
from CAD2BIM_backend.domain.runtime_execution import (
    DATA_PYTHON_EXECUTION as _DATA_PYTHON_EXECUTION,
    DATA_SQL_EXECUTION as _DATA_SQL_EXECUTION,
)
from CAD2BIM_backend.infrastructure.data_transform.capabilities import (
    PYTHON_ARROW_APPROVED_LIBRARIES,
    PYTHON_ARROW_SAFE_STDLIB,
)
from CAD2BIM_backend.infrastructure.ifc_query.adapter import IFC_TRANSFORM_BUDGET as _IFC_TRANSFORM_BUDGET
from CAD2BIM_backend.ports.ifc_query_worker import MAX_IFC_INPUT_BYTES as _MAX_IFC_INPUT_BYTES
from CAD2BIM_mcp.tool_docs.common import (
    IFC_INSTALLATION_EVIDENCE_GUIDANCE,
    runtime_budget_description as _runtime_budget_description,
)
from CAD2BIM_mcp.tool_docs.ifc_query import IFC_READONLY_PROGRAM_CONTRACT, IFC_SEMANTIC_EXTRACTION_GUIDANCE

RESOLVED_TRANSFORM_INPUT_BINDING_DESCRIPTION = (
    "Read-only Operation projection of the already-resolved exact member keys."
)

IFC_SOURCE_DESCRIPTION = (
    "Select current to pin the current IFC once for this Python create. Only this branch exposes "
    "the real process-private IfcOpenShell model through ctx.model(); no paths or revision IDs are accepted."
)
IFC_INPUTS_DESCRIPTION = (
    "Zero to 16 exact Package/Payload bindings. Empty inputs is valid only with ifc_source=current "
    "and execution.engine=python_arrow; otherwise bind at least one Package."
)
IFC_REVISION_SUMMARY_DESCRIPTION = (
    "Readable revision number of the exact IFC source pinned by the producing Operation; "
    "historical provenance only, not a selector for the current model or proof of freshness."
)
IFC_DERIVE_CONTRACT = (
    "Python create may declare ifc_source=current and zero to 16 Package inputs. The Host fixes "
    "one exact CURRENT snapshot; ctx.model() returns its real process-private IfcOpenShell model. "
    "Normal reads, attributes, Pset/QTO, materials, placement, spatial/system and port/connection "
    "investigation are supported. Do not mutate IFC, control transactions, open/save files or use "
    "native path exports; detected mutation discards all candidate outputs. Write only declared "
    "tables/JSON through ctx.write_table/write_json. SQL rejects IFC and Python without ifc_source "
    "has no model. Source lineage is Host-generated and business data cannot override it. "
    "repair inherits the original create's exact IFC; a different CURRENT at admission or publication "
    "rejects new repair with no new Package: use create against current. A completed original "
    "request replays its saved result even after CURRENT changes. Package-only historical analysis "
    "does not require CURRENT equality."
)


DATA_EXAMPLE_PACKAGE_DESCRIPTION_MARKDOWN = """# Selected records
## Summary
Records selected from one exact input Payload.
## Intended use
Review the selected records and their bounded summary.
## Contents
`selected` is Parquet and `summary` is JSON.
## Method
Filter `records` by the supplied minimum length.
## Scope and completeness
Includes only rows present in the selected immutable input Payload.
## Known limitations
The threshold is an input value and the result is not an IFC decision.
"""

DATA_TRANSFORM_DUCKDB_PACKAGE_DESCRIPTION_MARKDOWN = """# Selected records
## Summary
Records selected from one exact input Payload.
## Intended use
Review the selected records.
## Contents
`selected` is Parquet.
## Method
Filter `records` by the supplied minimum length.
## Scope and completeness
Includes only rows present in the selected immutable input Payload.
## Known limitations
The threshold is an input value and the result is not an IFC decision.
"""

DATA_TRANSFORM_PYTHON_SOURCE_EXAMPLE = """import pyarrow.compute as pc

def transform(ctx):
    records = ctx.table("records")
    keep = pc.greater_equal(records.column("length_m"), ctx.param("minimum_length_m"))
    selected = records.filter(keep)
    ctx.write_table("selected", selected)
    ctx.write_json("summary", {"selected_rows": selected.num_rows})
"""

DATA_TRANSFORM_DUCKDB_SOURCE_EXAMPLE = """CREATE TEMP TABLE selected AS
SELECT entity_key, length_m
FROM records
WHERE length_m >= $minimum_length_m
ORDER BY entity_key;
"""

_DATA_EXAMPLE_INPUTS = [
    {"input_name": "records", "package_alias": "data_001", "payload_name": "records"}
]
_DATA_EXAMPLE_PARAMETERS = [{"kind": "number", "name": "minimum_length_m", "value": 0.5}]
_DATA_EXAMPLE_PARQUET_OUTPUT = {
    "payload_name": "selected",
    "format": "parquet",
    "description": "Records at or above the selected length",
    "logical_key_fields": ["entity_key"],
}
DATA_TRANSFORM_PYTHON_CREATE_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "create",
        "inputs": _DATA_EXAMPLE_INPUTS,
        "inline_tables": [],
        "parameters": _DATA_EXAMPLE_PARAMETERS,
        "execution": {"engine": "python_arrow", "code": DATA_TRANSFORM_PYTHON_SOURCE_EXAMPLE},
        "outputs": {"action": "set", "value": [
            _DATA_EXAMPLE_PARQUET_OUTPUT,
            {
                "payload_name": "summary",
                "format": "json",
                "description": "Bounded selected-row summary",
                "json_kind": "document",
                "schema_json": json.dumps(
                    {"action": "set", "value": {
                        "type": "object",
                        "properties": {"selected_rows": {"type": "integer", "minimum": 0}},
                        "required": ["selected_rows"],
                        "additionalProperties": False,
                    }},
                    separators=(",", ":"),
                ),
            },
        ]},
        "output_package": {
            "name": "Selected records",
            "description_markdown": {"action": "set", "value": DATA_EXAMPLE_PACKAGE_DESCRIPTION_MARKDOWN},
        },
    },
    separators=(",", ":"),
)
DATA_TRANSFORM_DUCKDB_CREATE_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "create",
        "inputs": _DATA_EXAMPLE_INPUTS,
        "inline_tables": [],
        "parameters": _DATA_EXAMPLE_PARAMETERS,
        "execution": {"engine": "duckdb_sql", "code": DATA_TRANSFORM_DUCKDB_SOURCE_EXAMPLE},
        "outputs": {"action": "set", "value": [_DATA_EXAMPLE_PARQUET_OUTPUT]},
        "output_package": {
            "name": "Selected records",
            "description_markdown": {"action": "set", "value": DATA_TRANSFORM_DUCKDB_PACKAGE_DESCRIPTION_MARKDOWN},
        },
    },
    separators=(",", ":"),
)

DATA_TRANSFORM_CREATE_USAGE_EXAMPLES = (
    "Complete python_arrow create example: "
    + DATA_TRANSFORM_PYTHON_CREATE_EXAMPLE
    + " Complete duckdb_sql create example: "
    + DATA_TRANSFORM_DUCKDB_CREATE_EXAMPLE
    + " Replace direct_001, data_001, records, and referenced column names with exact values "
    "returned by runtime_open_direct_session and the Package catalog. inline_tables and "
    "parameters default to []; they are written explicitly here to show their location. Each "
    "parameter object is one of string, integer, number, boolean, string_list, integer_list, "
    "number_list, or boolean_list and is read by its name through $name in SQL or ctx.param(name) "
    "in Python."
)

_DATA_TRANSFORM_MINIMAL_PACKAGE_DESCRIPTION_MARKDOWN = """# Selected records
## Summary
Selected records.
## Intended use
Review the selected rows.
## Contents
`selected` is Parquet.
## Method
Copied from one exact input.
## Scope and completeness
Includes the selected input rows.
## Known limitations
This is not an IFC decision.
"""
_DATA_TRANSFORM_MINIMAL_OUTPUT = {
    "payload_name": "selected",
    "format": "parquet",
    "description": "Selected records",
    "logical_key_fields": ["entity_key"],
}
_DATA_TRANSFORM_MINIMAL_CREATE_BASE = {
    "direct_session_alias": "direct_001",
    "mode": "create",
    "inputs": _DATA_EXAMPLE_INPUTS,
    "outputs": {"action": "set", "value": [_DATA_TRANSFORM_MINIMAL_OUTPUT]},
    "output_package": {
        "name": "Selected records",
        "description_markdown": {"action": "set", "value": _DATA_TRANSFORM_MINIMAL_PACKAGE_DESCRIPTION_MARKDOWN},
    },
}
DATA_TRANSFORM_DUCKDB_MINIMAL_CREATE_EXAMPLE = json.dumps(
    {
        **_DATA_TRANSFORM_MINIMAL_CREATE_BASE,
        "execution": {
            "engine": "duckdb_sql",
            "code": (
                "CREATE TEMP TABLE selected AS SELECT * FROM records "
                "ORDER BY entity_key;"
            ),
        },
    },
    separators=(",", ":"),
)
DATA_TRANSFORM_PYTHON_MINIMAL_CREATE_EXAMPLE = json.dumps(
    {
        **_DATA_TRANSFORM_MINIMAL_CREATE_BASE,
        "execution": {
            "engine": "python_arrow",
            "code": (
                "def transform(ctx):\n"
                "    ctx.write_table('selected', ctx.table('records'))\n"
            ),
        },
    },
    separators=(",", ":"),
)
DATA_TRANSFORM_REPAIR_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "repair",
        "source": {
            "source_kind": "failed_operation",
            "operation_alias": "op_001",
        },
        "edits": [{"old_text": "bad exact text", "new_text": "correct exact text"}],
    },
    separators=(",", ":"),
)

DATA_TRANSFORM_RESULT_DESCRIPTION = (
    "Backward-compatible success response for an exact one-output request."
)
MULTI_OUTPUT_DATA_TRANSFORM_RESULT_DESCRIPTION = (
    "Closed success response for an exact request with two to 16 outputs."
)
DATA_REPAIR_TRANSFORM_RESULT_DESCRIPTION = (
    "Success response for one repaired transform with one declared output."
)
MULTI_OUTPUT_DATA_REPAIR_TRANSFORM_RESULT_DESCRIPTION = (
    "Success response for one repaired transform with multiple declared outputs."
)


DIRECT_PACKAGE_LIST_INPUT_BEFORE_SEQUENCE_DESCRIPTION = (
    "Sequence boundary for Package directory continuation. Omit or use null on the "
    "first page; then submit next_before_sequence unchanged to read sequence values "
    "strictly below it."
)

DIRECT_PACKAGE_LIST_INPUT_PAGE_SIZE_DESCRIPTION = (
    "Maximum Package cards requested on this page; default 20 and maximum 50."
)

DIRECT_OPERATION_LIST_INPUT_BEFORE_SEQUENCE_DESCRIPTION = (
    "Sequence boundary for Operation directory continuation. Omit or use null on the "
    "first page; then submit next_before_sequence unchanged to read sequence values "
    "strictly below it."
)

DIRECT_OPERATION_LIST_INPUT_PAGE_SIZE_DESCRIPTION = (
    "Maximum Operation cards requested on this page; default 20 and maximum 50."
)

DIRECT_OPERATION_LIST_INPUT_OPERATION_KIND_DESCRIPTION = (
    "Optional exact Operation-kind filter. Omit to read every supported terminal kind; "
    "when present, keyset pagination and completion apply only to that kind."
)

DIRECT_OPERATION_GET_INPUT_OPERATION_ALIAS_DESCRIPTION = (
    "Exact terminal Operation alias selected from operation_list."
)

TRANSFORM_INPUT_BINDING_INPUT_NAME_DESCRIPTION = (
    "Local read-only DuckDB relation name used by the submitted SQL."
)

TRANSFORM_INPUT_BINDING_PACKAGE_ALIAS_DESCRIPTION = (
    "Exact source_l0 or derived Package alias from data_package_list."
)

TRANSFORM_INPUT_BINDING_PAYLOAD_NAME_DESCRIPTION = (
    "Exact Parquet payload name returned by data_describe_package."
)

PARAMETER_BASE_NAME_DESCRIPTION = "DuckDB named value parameter without the leading dollar sign."

STRING_PARAMETER_KIND_DESCRIPTION = "Bind one UTF-8 string value."

STRING_PARAMETER_VALUE_DESCRIPTION = "String value of at most 4,096 UTF-8 bytes."

INTEGER_PARAMETER_KIND_DESCRIPTION = "Bind one strict signed 64-bit integer."

INTEGER_PARAMETER_VALUE_DESCRIPTION = "Strict signed 64-bit integer value."

NUMBER_PARAMETER_KIND_DESCRIPTION = "Bind one finite float64-compatible number."

NUMBER_PARAMETER_VALUE_DESCRIPTION = "Finite float64-compatible numeric value."

BOOLEAN_PARAMETER_KIND_DESCRIPTION = "Bind one JSON boolean."

BOOLEAN_PARAMETER_VALUE_DESCRIPTION = "Strict JSON boolean value."

STRING_LIST_PARAMETER_KIND_DESCRIPTION = "Bind an ordered list of UTF-8 strings."

STRING_LIST_PARAMETER_VALUE_DESCRIPTION = (
    "One to 100 strings; each string is at most 4,096 UTF-8 bytes."
)

INTEGER_LIST_PARAMETER_KIND_DESCRIPTION = "Bind an ordered list of strict signed 64-bit integers."

INTEGER_LIST_PARAMETER_VALUE_DESCRIPTION = (
    "Ordered list of one to 100 strict signed 64-bit integers."
)

NUMBER_LIST_PARAMETER_KIND_DESCRIPTION = "Bind an ordered list of finite numbers."

NUMBER_LIST_PARAMETER_VALUE_DESCRIPTION = (
    "Ordered list of one to 100 finite float64-compatible numbers."
)

BOOLEAN_LIST_PARAMETER_KIND_DESCRIPTION = "Bind an ordered list of JSON booleans."

BOOLEAN_LIST_PARAMETER_VALUE_DESCRIPTION = "Ordered list of one to 100 strict JSON boolean values."

DUCK_DB_SQL_EXECUTION_ENGINE_DESCRIPTION = "Execute the DuckDB SQL script mode."

DUCK_DB_SQL_EXECUTION_CODE_DESCRIPTION = (
    "Private multi-statement DuckDB SQL script, at most 65,536 UTF-8 bytes. Base "
    "relations come only from inputs, values use named parameters, and each declared "
    "output must exist as a same-named final TEMP TABLE or TEMP VIEW."
)

PARQUET_OUTPUT_DECLARATION_PAYLOAD_NAME_DESCRIPTION = (
    "Parquet payload name and exact same-named final TEMP TABLE or TEMP VIEW to publish."
)

PARQUET_OUTPUT_DECLARATION_FORMAT_DESCRIPTION = "Publish this declared output as Parquet."

PARQUET_OUTPUT_DECLARATION_DESCRIPTION_DESCRIPTION = (
    "Short human-readable purpose of this output payload."
)

PARQUET_OUTPUT_DECLARATION_LOGICAL_KEY_FIELDS_DESCRIPTION = (
    "One to 16 unique, case-sensitive result column names forming the stable logical key."
)

OUTPUT_PACKAGE_DECLARATION_NAME_DESCRIPTION = (
    "Stable human-readable name for the new immutable derived Package."
)

OUTPUT_PACKAGE_DECLARATION_DESCRIPTION_MARKDOWN_DESCRIPTION = (
    "Exact Package description Markdown, at most 65,536 UTF-8 bytes. Pure ASCII is "
    "about 65,536 characters and common Chinese text is theoretically about 21,845 "
    "characters; the complete encoded Markdown byte count is authoritative. It must "
    "contain exactly one H1, followed by these non-empty H2 sections in this exact "
    "order: Summary, Intended use, Contents, Method, Scope and completeness, Known "
    "limitations. Summary must be the first H2."
)

DIRECT_DATA_TRANSFORM_INPUT_INPUTS_DESCRIPTION = (
    "One to 16 exact read-only Package Payload bindings. Each input_name binds exactly "
    "one package_alias plus payload_name."
)

DIRECT_DATA_TRANSFORM_INPUT_PARAMETERS_DESCRIPTION = (
    "Zero to 32 typed runtime values from the user, Requirement/CAD context, a query, "
    "or Package facts; they are not output schemas or IFC contracts."
)

DIRECT_DATA_TRANSFORM_INPUT_EXECUTION_DESCRIPTION = (
    "DuckDB SQL engine and exact private multi-statement source."
)

DIRECT_DATA_TRANSFORM_INPUT_OUTPUTS_DESCRIPTION = (
    "One to 16 predeclared Parquet outputs in publication order. Each payload_name is "
    "also the required final TEMP relation name."
)

DIRECT_DATA_TRANSFORM_INPUT_OUTPUT_PACKAGE_DESCRIPTION = (
    "Name and exact description.md for the new derived Package."
)

PACKAGE_TRANSFORMATION_CODE_REFERENCE_REFERENCE_KIND_DESCRIPTION = (
    "Read code through a succeeded derived Package."
)

PACKAGE_TRANSFORMATION_CODE_REFERENCE_PACKAGE_ALIAS_DESCRIPTION = (
    "Derived Package alias selected from data_package_list."
)

OPERATION_TRANSFORMATION_CODE_REFERENCE_REFERENCE_KIND_DESCRIPTION = (
    "Read code through a terminal Data Transform Operation."
)

OPERATION_TRANSFORMATION_CODE_REFERENCE_OPERATION_ALIAS_DESCRIPTION = (
    "Data Transform Operation alias selected from operation_list."
)

DIRECT_TRANSFORMATION_CODE_INPUT_REFERENCE_DESCRIPTION = (
    "One complete package or operation reference; the two shapes are exclusive."
)

DATA_TRANSFORM_OPERATION_DETAIL_REPAIR_PARENT_OPERATION_ALIAS_DESCRIPTION = (
    "Exact producing or failed parent Operation selected by data_derive mode=repair; "
    "null for a mode=create request."
)

DATA_REPAIR_TRANSFORM_RESULT_REPAIR_PARENT_OPERATION_ALIAS_DESCRIPTION = (
    "Exact producing Operation resolved from the selected succeeded Package, or the "
    "selected failed parent Operation."
)

INPUTS_DUCK_DB_SQL_EXECUTION_ENGINE_DESCRIPTION = (
    "Select the private multi-statement DuckDB SQL engine."
)

INPUTS_DUCK_DB_SQL_EXECUTION_CODE_DESCRIPTION = (
    f"Exact DuckDB SQL source, at most {_MAX_TRANSFORM_CODE_BYTES:,} UTF-8 bytes; each declared output is "
    "read from its same-named final TEMP relation."
)

PYTHON_ARROW_EXECUTION_ENGINE_DESCRIPTION = "Select the isolated Python/Arrow transform engine."

PYTHON_ARROW_EXECUTION_CODE_DESCRIPTION = (
    f"Exact Python source defining def transform(ctx), at most {_MAX_TRANSFORM_CODE_BYTES:,} UTF-8 bytes; "
    "outputs are submitted only through ctx.write_table or ctx.write_json. Imported "
    "module aliases are ordinary Python names; do not reuse aliases such as pa, np, "
    "pd, pc, pl, or nx for local rows, ports, strings, or scalars."
)

INPUTS_TRANSFORM_INPUT_BINDING_INPUT_NAME_DESCRIPTION = (
    "Local read-only input name used as a DuckDB relation or with Python ctx.table, "
    "ctx.batches, or ctx.json."
)

INPUTS_TRANSFORM_INPUT_BINDING_PAYLOAD_NAME_DESCRIPTION = (
    "Exact Parquet or JSON Payload name returned by data_describe_package."
)

INLINE_STRING_VALUE_KIND_DESCRIPTION = "Select a UTF-8 string cell."

INLINE_STRING_VALUE_VALUE_DESCRIPTION = "Exact UTF-8 string cell value."

INLINE_INTEGER_VALUE_KIND_DESCRIPTION = "Select a strict signed int64 cell."

INLINE_INTEGER_VALUE_VALUE_DESCRIPTION = "Strict signed 64-bit integer; JSON booleans are rejected."

INLINE_NUMBER_VALUE_KIND_DESCRIPTION = "Select a finite float64-compatible cell."

INLINE_BOOLEAN_VALUE_KIND_DESCRIPTION = "Select a strict JSON boolean cell."

INLINE_NULL_VALUE_KIND_DESCRIPTION = "Select null; legal only when the matching column is nullable."

INLINE_COLUMN_NAME_DESCRIPTION = "Unique case-sensitive column name in this inline table."

INLINE_COLUMN_TYPE_DESCRIPTION = (
    "Exact scalar kind required for every non-null cell in this column."
)

INLINE_COLUMN_NULLABLE_DESCRIPTION = "Whether this column accepts a null cell."

INLINE_ROW_VALUES_DESCRIPTION = "Ordered closed typed cells matching the declared columns exactly."

INLINE_TABLE_INPUT_NAME_DESCRIPTION = (
    "Request-local read-only relation name shared with the Package input_name "
    "namespace; it creates no Package or Alias."
)

INLINE_TABLE_COLUMNS_DESCRIPTION = "One to 32 closed scalar column declarations."

INLINE_TABLE_ROWS_DESCRIPTION = "Zero to 500 rows; every row has exactly one typed cell per column."

INPUTS_PARQUET_OUTPUT_DECLARATION_PAYLOAD_NAME_DESCRIPTION = (
    "Unique derived Parquet Payload name; SQL provides the same-named final TEMP "
    "relation and Python calls ctx.write_table with this name."
)

INPUTS_PARQUET_OUTPUT_DECLARATION_DESCRIPTION_DESCRIPTION = (
    "Short human-readable purpose of this Parquet Payload."
)

INPUTS_PARQUET_OUTPUT_DECLARATION_LOGICAL_KEY_FIELDS_DESCRIPTION = (
    "One to 16 unique case-sensitive output columns forming a non-null logical key."
)

JSON_OUTPUT_DECLARATION_PAYLOAD_NAME_DESCRIPTION = "Unique name of the derived JSON Payload."

JSON_OUTPUT_DECLARATION_FORMAT_DESCRIPTION = "Publish this declared output as JSON."

JSON_OUTPUT_DECLARATION_DESCRIPTION_DESCRIPTION = (
    "Short human-readable purpose of this JSON Payload."
)

JSON_OUTPUT_DECLARATION_JSON_KIND_DESCRIPTION = (
    "One logical document or a top-level homogeneous records array."
)

JSON_OUTPUT_DECLARATION_SCHEMA_JSON_DESCRIPTION = (
    "Complete JSON Schema Draft 2020-12 text, at most 65,536 UTF-8 bytes. The Host "
    "parses the schema, rejects unsafe references, and validates the complete output."
)

DIRECT_TRANSFORM_ROOT_MODE_DESCRIPTION = (
    "Create a new immutable derived Package from a complete transform contract."
)

DIRECT_TRANSFORM_ROOT_INPUTS_DESCRIPTION = (
    "One to 16 exact read-only Package Payload bindings selected from the Package "
    "catalog and description; Data Transform does not allow inline-only input."
)

DIRECT_TRANSFORM_ROOT_INLINE_TABLES_DESCRIPTION = (
    "Zero to eight one-off typed relations, each limited to 32 columns and 500 rows; "
    "all inline tables together are limited to 1 MiB canonical JSON."
)

DIRECT_TRANSFORM_ROOT_PARAMETERS_DESCRIPTION = (
    "Zero to 32 small typed runtime values sourced from the user, Requirement/CAD "
    "context, a query, or Package facts; they are not schemas or IFC contracts."
)

DIRECT_TRANSFORM_ROOT_OUTPUTS_DESCRIPTION = (
    "One to 16 predeclared Parquet or JSON outputs in publication order; missing, "
    "duplicate, or undeclared writes make the whole publication fail."
)

DIRECT_TRANSFORM_ROOT_OUTPUT_PACKAGE_DESCRIPTION = (
    "Name and exact bounded description.md for the new immutable Package."
)

DIRECT_DUCK_DB_TRANSFORM_INPUT_EXECUTION_DESCRIPTION = (
    "Complete DuckDB SQL execution branch and exact source."
)

DIRECT_PYTHON_ARROW_TRANSFORM_INPUT_EXECUTION_DESCRIPTION = (
    "Complete Python/Arrow execution branch and exact transform(ctx) source."
)

FULL_CODE_READ_VIEW_MODE_DESCRIPTION = (
    "Return the complete exact source. Omit view_mode for the backward-compatible "
    "full-source behavior."
)

LINES_CODE_READ_VIEW_MODE_DESCRIPTION = "Return one bounded exact 1-based line range."

LINES_CODE_READ_START_LINE_DESCRIPTION = (
    "First exact source line to return, using 1-based numbering."
)

LINES_CODE_READ_LINE_COUNT_DESCRIPTION = "Exact source lines requested; minimum 1 and maximum 200."

SEARCH_CODE_READ_VIEW_MODE_DESCRIPTION = (
    "Search the exact source for a case-sensitive literal substring."
)

SEARCH_CODE_READ_TEXT_DESCRIPTION = (
    "Non-empty case-sensitive literal text to find, at most 65,536 UTF-8 bytes; "
    "regex and fuzzy matching are unsupported."
)

SEARCH_CODE_READ_CONTEXT_LINES_DESCRIPTION = (
    "Complete context lines before and after each match; default 3, maximum 20."
)

SEARCH_CODE_READ_MAX_MATCHES_DESCRIPTION = "Complete matches requested; default 20 and maximum 50."

DIRECT_PACKAGE_CODE_FULL_INPUT_REFERENCE_DESCRIPTION = (
    "Complete succeeded derived-Package source reference."
)

DIRECT_OPERATION_CODE_FULL_INPUT_REFERENCE_DESCRIPTION = (
    "Complete terminal-Operation source reference."
)

DIRECT_TRANSFORM_SOURCE_EDIT_OLD_TEXT_DESCRIPTION = (
    "Exact case-sensitive source text expected to occur exactly once in the selected "
    "Operation source; at most 16,384 UTF-8 bytes."
)

DIRECT_TRANSFORM_SOURCE_EDIT_NEW_TEXT_DESCRIPTION = (
    "Exact replacement text, at most 16,384 UTF-8 bytes; use an empty string to delete "
    "the matched old_text."
)

FAILED_OPERATION_REPAIR_SOURCE_SOURCE_KIND_DESCRIPTION = (
    "Select a visible terminal failed Data Transform Operation."
)

FAILED_OPERATION_REPAIR_SOURCE_OPERATION_ALIAS_DESCRIPTION = (
    "Visible failed Data Transform Operation alias returned by the failing ToolError or "
    "rediscovered with operation_list; operation_get may be used for complete diagnostics "
    "and inherited replay-contract inspection."
)

SUCCEEDED_PACKAGE_REPAIR_SOURCE_SOURCE_KIND_DESCRIPTION = (
    "Select a visible succeeded derived Package and its exact producing Operation."
)

SUCCEEDED_PACKAGE_REPAIR_SOURCE_PACKAGE_ALIAS_DESCRIPTION = (
    "Visible succeeded derived Package alias selected from data_package_list. The "
    "server resolves its exact producing Data Transform Operation; source_l0 and "
    "cad_compute_measurements Packages are unsupported."
)

DIRECT_REPAIR_TRANSFORM_BASE_MODE_DESCRIPTION = (
    "Repair one exact failed or succeeded Data Transform source and rerun it."
)

DIRECT_REPAIR_TRANSFORM_BASE_EDITS_DESCRIPTION = (
    "Optional one to 16 exact, non-overlapping old_text/new_text edits applied atomically against "
    "the same parent source."
)

DIRECT_FAILED_OPERATION_REPAIR_TRANSFORM_INPUT_SOURCE_DESCRIPTION = (
    "Complete failed-Operation source branch."
)

DIRECT_SUCCEEDED_PACKAGE_REPAIR_TRANSFORM_INPUT_SOURCE_DESCRIPTION = (
    "Complete succeeded-Package source branch."
)

DATA_TRANSFORM_INPUTS_DESCRIPTION = (
    "One to 16 exact read-only Package Payload bindings. Different entries may "
    "bind Payloads from different source_l0 or derived Packages."
)

DATA_TRANSFORM_EXECUTION_DESCRIPTION = (
    "Private DuckDB SQL or controlled-local Python/Arrow execution branch."
)

DATA_TRANSFORM_OUTPUTS_DESCRIPTION = (
    "One to 16 predeclared Parquet or JSON outputs in publication order."
)

DATA_TRANSFORM_OUTPUT_PACKAGE_DESCRIPTION = "Name and exact description.md for the derived Package."

DATA_TRANSFORM_PARAMETERS_DESCRIPTION = (
    "Zero to 32 typed runtime values. Different parameter objects are separate "
    "named bindings; list value order is preserved."
)

DATA_TRANSFORM_INLINE_TABLES_DESCRIPTION = (
    "Zero to eight small typed request-scoped tables; at most 32 columns and "
    "500 rows per table, with a 1 MiB aggregate canonical-JSON limit."
)

DATA_TRANSFORM_DUCKDB_RESOURCE_URI = "cad2bim://contracts/data_derive/duckdb_sql"
DATA_TRANSFORM_PYTHON_RESOURCE_URI = "cad2bim://contracts/data_derive/python_arrow"
DATA_TRANSFORM_REPAIR_RESOURCE_URI = "cad2bim://contracts/data_derive/repair"

DATA_TRANSFORM_DUCKDB_RESOURCE_DESCRIPTION = (
    "Read program_api for SQL relations, parameters, final TEMP outputs, and diagnostics; "
    "request_contract for exact inputs; publication_contract for output and atomicity rules; "
    "runtime_contract for Worker and admission limits; tool_result for the MCP response boundary; "
    "and examples for source plus a complete create request."
)
DATA_TRANSFORM_PYTHON_RESOURCE_DESCRIPTION = (
    "Read context_api for transform(ctx) reads, writes, return behavior, and restrictions; "
    "request_contract for exact inputs; publication_contract for output and atomicity rules; "
    "runtime_contract for Worker and admission limits; tool_result for the MCP response boundary; "
    "and examples for source plus a complete create request."
)
DATA_TRANSFORM_REPAIR_RESOURCE_DESCRIPTION = (
    "Read usage for when repair applies; source_and_edit_contract for source discovery, exact SHA, "
    "edits, and inheritance; failure_boundary for pre-execution rejection versus prepared failure; "
    "tool_result for the MCP response boundary; and complete_repair_request for the request shape."
)

_DATA_TRANSFORM_COMMON_CREATE_CONTRACT = (
    "Use outputs={action:set,value:[complete declarations]} and output_package.description_markdown={action:set,value:Markdown}. "
    "JSON outputs keep schema_json as a string containing {action:set,value:object-or-boolean-Schema}. "
    "A create request synchronously reads exact immutable sources and "
    "atomically publishes one new derived Data Package. Obtain package_alias from "
    "data_package_list and exact payload names and schemas from data_describe_package. Each "
    "input_name binds one Parquet relation or JSON value. Optional inline_tables are request-only "
    "typed relations, not Packages: at most eight, 32 columns and 500 rows each, and 1 MiB "
    "canonical JSON total. Without IFC, bind 1..16 Package inputs. Only Python with "
    "ifc_source=current permits zero Package inputs. Parameters are small "
    "sourced values, not schemas, reusable tables, tolerance defaults, or IFC contracts. Package "
    "input names, inline-table names, and output payload names share one request-local namespace."
)

_DATA_TRANSFORM_OUTPUT_CONTRACT = (
    "Declare 1..16 Parquet or JSON outputs. The resolved JSON schema_json is full Draft 2020-12 text, "
    "including boolean schemas, arrays, combinations and local references. Remote references are forbidden; records requires array/items. "
    f"Source is at most {_MAX_TRANSFORM_CODE_BYTES:,} UTF-8 bytes; schema_json and Package Markdown "
    "are each at most 65,536 UTF-8 bytes. Use code_read lines/search when source exceeds the "
    "65,536-byte full-view limit; parameters "
    "are at most 32 KiB canonical JSON. Package Markdown has exactly one H1 followed by non-empty "
    "Summary, Intended use, Contents, Method, Scope and completeness, and Known limitations H2 "
    "sections in that order. Summary is the first H2. All outputs together are at most 2,000,000 "
    "rows, 512 MiB Arrow, and 256 MiB artifacts; each Payload is at most 100 MiB. Missing, "
    "duplicate, undeclared, invalid, colliding, or over-budget outputs publish nothing. Parquet "
    "and JSON publication is atomic. When republishing one Payload, use a distinct input name, "
    "such as ports_in, and keep ports only as the output name. "
    "When preparing downstream modeling inputs, retain source record keys, role-assignment "
    "basis, derivation rules, and approved-default provenance in the derived Package payloads "
    "and explain them in Method. Keep measured, derived, defaulted, and unresolved values "
    "distinguishable. Project the selected model's declared geometric and assembly parameters "
    "from those records."
)

_DATA_TRANSFORM_TOOL_RESULT_CONTRACT = (
    "The MCP tool result is separate from the submitted program API. Success returns data_NNN "
    "and op_NNN. A single-output request has output; a multi-output request has outputs in "
    "declaration order, complete Parquet/JSON result branches, and no primary output. Use "
    "data_describe_package, operation_get, and code_list/code_read for follow-up. The exact JSON "
    "shape remains the data_derive outputSchema returned by list_tools."
)

_DATA_TRANSFORM_DUCKDB_PROGRAM_CONTRACT = (
    "execution.engine=duckdb_sql runs private multi-statement DuckDB SQL. Relations use declared "
    "input_name values, parameters use $name, and each declared output must be a same-named final "
    "TEMP TABLE or TEMP VIEW. Every declared input must be referenced; scalar-subquery references "
    "count, while unused bindings are rejected with their bounded names. Quote reserved output "
    'aliases, for example AS "name". Parser and binder failures return bounded line/token or '
    "candidate-column detail. BIGNUM, UHUGEINT, and TIME WITH TIME ZONE/TIMETZ are not "
    "Arrow-faithful and must be CAST AS VARCHAR, including inside complex values. Host/network "
    "access, external extensions, product databases, IFC access, and external mutation are "
    "unsupported. The SQL program submits outputs through its final TEMP relations; it does not "
    "return the MCP tool-result object."
)

_DATA_TRANSFORM_PYTHON_PROGRAM_CONTRACT = (
    "execution.engine=python_arrow runs exact source containing one top-level def transform(ctx). "
    "transform(ctx) must return None. Inputs are read-only. "
    "ctx.table(name) reads an Arrow input as pyarrow.Table; ctx.batches(name) returns a "
    "fresh RecordBatchReader from the beginning on each call; ctx.json(name) reads a JSON input; "
    "ctx.param(name) reads a typed parameter; ctx.log(message) emits bounded diagnostics that do "
    "not become Package data or the default MCP response. Submit each declared Parquet output once through "
    "ctx.write_table(name, value), where value is only pyarrow.Table, RecordBatch, or "
    "RecordBatchReader. Submit each declared JSON output once through ctx.write_json(name, value); "
    "the Host validates it against declared schema_json. transform(ctx) submits outputs through "
    "these methods; it does not return the MCP tool-result object. Pandas and Polars require "
    "explicit Arrow conversion. Current libraries are "
    f"{', '.join(PYTHON_ARROW_APPROVED_LIBRARIES)}. The builtin set is restricted: locals, globals, "
    "getattr, hasattr, eval, and exec are unavailable. Imported aliases are mutable Python names; "
    "do not reuse pa, np, pd, pc, pl, or nx for local values. Shapely predicates and indexes are "
    "2D and cannot prove a 3D connection. pip install, network/URL access, host paths, product "
    "databases, credentials, persistent files, arbitrary subprocess effects, undeclared IFC access, input "
    "mutation, and undeclared outputs are unsupported. Runtime exceptions return the innermost "
    "user-code line and a bounded exact source excerpt when available."
)

_DATA_TRANSFORM_REPAIR_CONTRACT = (
    "For a terminal failed Operation, take operation_alias from ToolError, inspect operation_get, "
    "then call code_list with code_kind=data_transform_attempt. Select the card whose owner_alias "
    "matches that Operation and pass its opaque code_locator to code_read. Repair with "
    "source_kind=failed_operation. To revise a succeeded derived Package, use "
    "source_kind=succeeded_package; the server resolves its exact producing Operation. Preflight "
    "failures before an Operation exists have no operation_alias. Code edits are optional 1..16 "
    "case-sensitive old_text/new_text edits; the immutable parent resolves the exact source without a caller SHA. Each text is at most 16,384 "
    f"UTF-8 bytes, all edits total at most 32,768 bytes, and repaired source at most {_MAX_TRANSFORM_CODE_BYTES:,} bytes. "
    "code_read full view remains limited to 65,536 UTF-8 bytes; use lines or search for larger source. "
    "Missing, duplicate, overlapping, or over-budget patches publish no new Operation or Package. "
    "Repair inherits inputs, execution engine, parameters, inline tables and execution budgets. "
    "Omit outputs/output_package to inherit them. output_package may contain only a replacement name "
    "or description_markdown with set/edit. outputs accepts set with a complete list, or edit with set/remove "
    "by payload_name. Every set entry is complete; schema_json alone may contain edit against that same existing JSON output's "
    "saved text. A new JSON output requires schema_json set. At least one effective change is required. "
    "Read an unavailable parent description or Schema with operation_get mode=transform_input, operation_alias, "
    "input_kind=package_description or output_schema with payload_name, and view full/lines/search. Only one text is returned; "
    "use a smaller view if JSON framing exceeds the 64 KiB response budget. "
    "Every repair runs a new Operation and may publish a new Package, including metadata-only repair. Old Packages remain immutable."
)

_DUCKDB_WORKER_POLICY = _DataTransformWorkerPolicy()
_PYTHON_WORKER_POLICY = _PythonArrowWorkerPolicy()
_DATA_TRANSFORM_DUCKDB_RUNTIME_CONTRACT = (
    f"The DuckDB child allows {_DUCKDB_WORKER_POLICY.input_row_limit:,} input rows, "
    f"{_DUCKDB_WORKER_POLICY.input_arrow_bytes // (1024 * 1024)} MiB input/output Arrow, "
    f"{_DUCKDB_WORKER_POLICY.duckdb_memory_bytes // (1024 * 1024)} MiB DuckDB memory, "
    f"{_DUCKDB_WORKER_POLICY.rss_bytes // (1024**3)} GiB RSS, "
    f"{_DUCKDB_WORKER_POLICY.scratch_bytes // (1024**3)} GiB scratch, "
    f"{_DUCKDB_WORKER_POLICY.cpu_seconds} CPU seconds, "
    f"{_DUCKDB_WORKER_POLICY.wall_seconds:g} worker-wall seconds, "
    f"{_DUCKDB_WORKER_POLICY.threads} threads, and "
    f"{_DUCKDB_WORKER_POLICY.max_concurrent_workers} concurrent workers. "
    + _runtime_budget_description(_DATA_SQL_EXECUTION, "SQL (two slots)").strip()
)
_DATA_TRANSFORM_PYTHON_RUNTIME_CONTRACT = (
    f"Without ifc_source, the Python child allows {_PYTHON_WORKER_POLICY.input_row_limit:,} input rows, "
    f"{_PYTHON_WORKER_POLICY.input_stored_parquet_bytes // (1024 * 1024)} MiB stored input "
    f"Parquet, {_PYTHON_WORKER_POLICY.input_arrow_bytes // (1024 * 1024)} MiB input/output "
    f"Arrow, {_PYTHON_WORKER_POLICY.rss_bytes // (1024**3)} GiB RSS, "
    f"{_PYTHON_WORKER_POLICY.scratch_bytes // (1024**3)} GiB scratch, "
    f"{_PYTHON_WORKER_POLICY.cpu_seconds} CPU seconds, "
    f"{_PYTHON_WORKER_POLICY.wall_seconds:g} worker-wall seconds, "
    f"{_PYTHON_WORKER_POLICY.threads} threads, and "
    f"{_PYTHON_WORKER_POLICY.max_concurrent_workers} concurrent worker. "
    + _runtime_budget_description(
        _DATA_PYTHON_EXECUTION, "shared native Python/geometry/IFC (one slot)"
    ).strip()
)

DATA_TRANSFORM_DUCKDB_DOCUMENT = {
    "usage": (
        "Read this document before writing a non-trivial duckdb_sql create request. "
        "It is not required for python_arrow or repair-only work."
    ),
    "request_contract": _DATA_TRANSFORM_COMMON_CREATE_CONTRACT,
    "program_api": _DATA_TRANSFORM_DUCKDB_PROGRAM_CONTRACT,
    "publication_contract": _DATA_TRANSFORM_OUTPUT_CONTRACT,
    "runtime_contract": _DATA_TRANSFORM_DUCKDB_RUNTIME_CONTRACT,
    "tool_result": _DATA_TRANSFORM_TOOL_RESULT_CONTRACT,
    "examples": {
        "source": DATA_TRANSFORM_DUCKDB_SOURCE_EXAMPLE,
        "complete_create_request": json.loads(DATA_TRANSFORM_DUCKDB_CREATE_EXAMPLE),
    },
}

DATA_DERIVE_IFC_SOURCE_EXAMPLE = '''import pyarrow as pa

def transform(ctx):
    products = ctx.model().by_type("IfcProduct")
    keys = [product.GlobalId for product in products]
    ctx.write_table("selected", pa.table({"entity_key": pa.array(keys, type=pa.string())}))
    ctx.write_json("summary", {"selected_rows": len(keys)})
'''
DATA_DERIVE_IFC_CREATE_EXAMPLE = json.loads(DATA_TRANSFORM_PYTHON_CREATE_EXAMPLE)
DATA_DERIVE_IFC_CREATE_EXAMPLE.update({
    "inputs": [], "parameters": [], "inline_tables": [], "ifc_source": "current",
    "execution": {"engine": "python_arrow", "code": DATA_DERIVE_IFC_SOURCE_EXAMPLE},
    "output_package": {"name": "IFC product inventory", "description_markdown": {
        "action": "set", "value": "# IFC product inventory\n## Summary\nProduct GlobalIds from one fixed IFC snapshot.\n## Intended use\nReview the inventory; future writes must recheck targets.\n## Contents\n`selected` holds product keys; `summary` records the count.\n## Method\nRead IfcProduct GlobalIds through ctx.model().\n## Scope and completeness\nAll IfcProduct objects in the pinned snapshot.\n## Known limitations\nThis is historical selection data, not authorization to modify objects."
    }},
})
DATA_DERIVE_IFC_CREATE_EXAMPLE["outputs"]["value"][0]["description"] = "Product GlobalIds in the fixed IFC snapshot"

DATA_TRANSFORM_PYTHON_DOCUMENT = {
    "usage": (
        "Read this document before writing a non-trivial python_arrow create request. "
        "It is not required for duckdb_sql or repair-only work."
    ),
    "request_contract": _DATA_TRANSFORM_COMMON_CREATE_CONTRACT,
    "context_api": _DATA_TRANSFORM_PYTHON_PROGRAM_CONTRACT,
    "ifc_source_contract": IFC_DERIVE_CONTRACT,
    "ifc_readonly_contract": IFC_READONLY_PROGRAM_CONTRACT,
    "ifc_semantic_extraction": IFC_SEMANTIC_EXTRACTION_GUIDANCE,
    "ifc_installation_evidence": IFC_INSTALLATION_EVIDENCE_GUIDANCE,
    "ifc_imports": (
        "With ifc_source=current, imports include IfcOpenShell, the Data computation libraries "
        f"{', '.join(PYTHON_ARROW_APPROVED_LIBRARIES)}, and safe standard library roots "
        f"{', '.join(PYTHON_ARROW_SAFE_STDLIB)}. Query keeps its own import boundary; "
        "these imports do not authorize filesystem, network, process, or IFC writes."
    ),
    "publication_contract": _DATA_TRANSFORM_OUTPUT_CONTRACT,
    "runtime_contract": _DATA_TRANSFORM_PYTHON_RUNTIME_CONTRACT,
    "ifc_runtime_contract": (
        f"With ifc_source=current, the exact IFC may be up to {_MAX_IFC_INPUT_BYTES // (1024**2)} MiB. "
        "Package input and declared Arrow/JSON output limits remain those of the Data Python runtime. "
        f"The IFC child uses {_IFC_TRANSFORM_BUDGET.wall_seconds:g} worker-wall seconds, "
        f"{_IFC_TRANSFORM_BUDGET.cpu_seconds:g} CPU seconds, "
        f"{_IFC_TRANSFORM_BUDGET.rss_bytes // (1024**3)} GiB RSS, "
        f"{_IFC_TRANSFORM_BUDGET.scratch_bytes // (1024**3)} GiB scratch, "
        f"{_IFC_TRANSFORM_BUDGET.open_file_descriptors} file descriptors. Controlled libraries "
        f"are configured for {_IFC_TRANSFORM_BUDGET.threads} threads; after trusted library "
        f"readiness, at most {_IFC_TRANSFORM_BUDGET.threads} additional native threads may be created. "
        "This is not a total-process thread count. Trusted initialization uses the same OS, "
        "RSS, CPU, wall and file-descriptor limits. "
        "Parsing, execution and mutation audits share this one Worker budget; "
        "the enclosing Data Python call includes output validation and publication."
    ),
    "tool_result": _DATA_TRANSFORM_TOOL_RESULT_CONTRACT,
    "examples": {
        "source": DATA_TRANSFORM_PYTHON_SOURCE_EXAMPLE,
        "complete_create_request": json.loads(DATA_TRANSFORM_PYTHON_CREATE_EXAMPLE),
        "complete_ifc_create_request": DATA_DERIVE_IFC_CREATE_EXAMPLE,
    },
}

DATA_TRANSFORM_REPAIR_DOCUMENT = {
    "usage": (
        "Read this document only to repair a failed transform or revise the producing transform "
        "of a succeeded derived Package. Ordinary create does not require it."
    ),
    "source_and_edit_contract": _DATA_TRANSFORM_REPAIR_CONTRACT,
    "ifc_source_contract": IFC_DERIVE_CONTRACT,
    "failure_boundary": (
        "A repair rejected before execution preparation creates no new Operation or Package. "
        "A prepared execution failure records a new terminal Operation and publishes no Package. "
        "Execution remains synchronous and terminal; there is no Job or polling."
    ),
    "tool_result": _DATA_TRANSFORM_TOOL_RESULT_CONTRACT,
    "complete_repair_request": json.loads(DATA_TRANSFORM_REPAIR_EXAMPLE),
}

DATA_TRANSFORM_DESCRIPTION = (
    "Derive one immutable Package by extraction, joins or computation, or repair a previous derive. "
    + IFC_DERIVE_CONTRACT
    + " "
    + _DATA_TRANSFORM_COMMON_CREATE_CONTRACT
    + " Choose execution.engine=duckdb_sql for private multi-statement SQL. Its program reads "
    "input_name relations and $name parameters and creates one same-named final TEMP relation per "
    "declared output. Minimal complete SQL create request: "
    + DATA_TRANSFORM_DUCKDB_MINIMAL_CREATE_EXAMPLE
    + " Choose execution.engine=python_arrow for isolated Python/Arrow. Define exactly "
    "def transform(ctx), read declared values through ctx.table/batches/json/param, and publish "
    "only declared outputs through ctx.write_table/write_json. Minimal complete Python create "
    "request: "
    + DATA_TRANSFORM_PYTHON_MINIMAL_CREATE_EXAMPLE
    + " Replace aliases, payload names, columns, and program logic with exact session and Package "
    "facts. Before a non-trivial create, read only the selected engine document: "
    + DATA_TRANSFORM_DUCKDB_RESOURCE_URI
    + " or "
    + DATA_TRANSFORM_PYTHON_RESOURCE_URI
    + ". These resources contain the complete engine API, limits, output publication contract, "
    "diagnostics, and detailed examples. For mode=repair, choose exactly one failed_operation or "
    "succeeded_package source. For a failed execution, take operation_alias from ToolError, inspect "
    "operation_get, then use code_list with code_kind=data_transform_attempt and code_read to obtain "
    "the exact source before applying bounded literal edits. Code edits may be omitted when changing "
    "outputs or Package metadata. Omitted fields inherit; outputs uses set/edit by payload_name, description_markdown uses set/edit, "
    "and schema_json contains action JSON for a full Schema set or literal edits. operation_get transform_input reads one saved "
    "description or output Schema with bounded full/lines/search views. Each repair re-executes and creates a new Operation. Package Markdown requires "
    "exactly one H1 and the ordered H2 sections documented by the output_package field; Summary is "
    "the first H2. Read "
    + DATA_TRANSFORM_REPAIR_RESOURCE_URI
    + " for source discovery, replay inheritance, edit limits, and a complete repair request; an "
    "ordinary create does not require the repair document. There is no Job or polling. Publication "
    "is atomic, and failures expose no partial Package. Results are computed facts or candidates, "
    "not approved business or IFC decisions."
)


__all__ = [
    "DATA_EXAMPLE_PACKAGE_DESCRIPTION_MARKDOWN",
    "DATA_TRANSFORM_DUCKDB_CREATE_EXAMPLE",
    "DATA_TRANSFORM_DUCKDB_DOCUMENT",
    "DATA_TRANSFORM_DUCKDB_MINIMAL_CREATE_EXAMPLE",
    "DATA_TRANSFORM_DUCKDB_RESOURCE_DESCRIPTION",
    "DATA_TRANSFORM_DUCKDB_RESOURCE_URI",
    "DATA_TRANSFORM_DUCKDB_PACKAGE_DESCRIPTION_MARKDOWN",
    "DATA_TRANSFORM_DUCKDB_SOURCE_EXAMPLE",
    "DATA_TRANSFORM_PYTHON_CREATE_EXAMPLE",
    "DATA_TRANSFORM_PYTHON_DOCUMENT",
    "DATA_TRANSFORM_PYTHON_MINIMAL_CREATE_EXAMPLE",
    "DATA_TRANSFORM_PYTHON_RESOURCE_DESCRIPTION",
    "DATA_TRANSFORM_PYTHON_RESOURCE_URI",
    "DATA_TRANSFORM_PYTHON_SOURCE_EXAMPLE",
    "DATA_TRANSFORM_REPAIR_DOCUMENT",
    "DATA_TRANSFORM_REPAIR_EXAMPLE",
    "DATA_TRANSFORM_REPAIR_RESOURCE_DESCRIPTION",
    "DATA_TRANSFORM_REPAIR_RESOURCE_URI",
]
