"""Provider-visible usage text for the data query capability family."""

import json

from CAD2BIM_backend.domain.runtime_execution import (
    DATA_PYTHON_EXECUTION as _DATA_PYTHON_EXECUTION,
    DATA_SQL_EXECUTION as _DATA_SQL_EXECUTION,
)
from CAD2BIM_mcp.tool_docs.common import runtime_budget_description as _runtime_budget_description

QUERY_PYTHON_QUERY_DESCRIPTION = "One complete first-page Python query."


DATA_QUERY_SQL_SOURCE_EXAMPLE = """SELECT entity_key, length_m
FROM records
WHERE length_m >= $minimum_length_m
ORDER BY entity_key;
"""

DATA_QUERY_PYTHON_SOURCE_EXAMPLE = """import pyarrow as pa
import pyarrow.compute as pc

def query(ctx):
    records = ctx.table("records")
    keep = pc.greater_equal(records.column("length_m"), ctx.param("minimum_length_m"))
    selected = records.filter(keep)
    return pa.table({
        "entity_key": selected.column("entity_key"),
        "length_m": selected.column("length_m"),
    })
"""

_DATA_QUERY_EXAMPLE_INPUTS = [
    {"input_name": "records", "package_alias": "data_001", "payload_name": "records"}
]
_DATA_QUERY_EXAMPLE_PARAMETERS = [{"kind": "number", "name": "minimum_length_m", "value": 0.5}]
DATA_QUERY_SQL_FRESH_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
        "query": {
            "inputs": _DATA_QUERY_EXAMPLE_INPUTS,
            "parameters": _DATA_QUERY_EXAMPLE_PARAMETERS,
            "execution": {"engine": "duckdb_sql", "code": DATA_QUERY_SQL_SOURCE_EXAMPLE},
            "page_size": 100,
        },
    },
    separators=(",", ":"),
)
DATA_QUERY_PYTHON_FRESH_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
        "query": {
            "inputs": _DATA_QUERY_EXAMPLE_INPUTS,
            "inline_tables": [],
            "parameters": _DATA_QUERY_EXAMPLE_PARAMETERS,
            "code": DATA_QUERY_PYTHON_SOURCE_EXAMPLE,
            "page_size": 100,
        },
    },
    separators=(",", ":"),
)
DATA_QUERY_PYTHON_MINIMAL_FRESH_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
        "query": {
            "inputs": _DATA_QUERY_EXAMPLE_INPUTS,
            "code": 'def query(ctx):\n    return ctx.table("records")\n',
        },
    },
    separators=(",", ":"),
)
DATA_QUERY_PYTHON_REPAIR_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "mode": "repair",
        "parent_attempt_alias": "cur.parent_attempt",
        "edits": [
            {
                "old_text": 'ctx.table("wrong_name")',
                "new_text": 'ctx.table("records")',
            }
        ],
        "page_size": 100,
    },
    separators=(",", ":"),
)
DATA_QUERY_PYTHON_PROGRAM_RESOURCE_URI = (
    "cad2bim://contracts/data_query_python/program_api"
)
DATA_QUERY_PYTHON_PROGRAM_RESOURCE_DESCRIPTION = (
    "Python query contract navigation: usage selects temporary analysis and persistence routing; "
    "context_api defines read methods, libraries, and restrictions; program_return defines the "
    "Arrow return ABI; mcp_result_navigation identifies the later paged JSON result and its "
    "registered output Schema; "
    "budgets_and_failures lists limits, cursor lifecycle, and error codes; repair defines "
    "Attempt discovery, exact-source edits, and failure boundaries; examples contains the "
    "complete source, fresh request, and repair request."
)
DATA_QUERY_SQL_USAGE_EXAMPLE = (
    "Complete DuckDB first-page call: "
    + DATA_QUERY_SQL_FRESH_EXAMPLE
    + " Replace direct_001, data_001, records, and column names with exact values returned by "
    "runtime_open_direct_session and the Package catalog. mode defaults to fresh, page_size "
    "defaults to 100, and parameters default to []; they are explicit in this example to show "
    "their nesting. Each typed parameter is read by $name in SQL."
)

JSON_PAYLOAD_TOOL_QUERY_PACKAGE_ALIAS_DESCRIPTION = (
    "Immutable Package alias selected from data_package_list and confirmed to "
    "contain this JSON Payload with data_describe_package."
)

JSON_PAYLOAD_TOOL_QUERY_PAYLOAD_NAME_DESCRIPTION = (
    "Exact JSON Payload name returned by data_describe_package."
)

JSON_VALUE_TOOL_QUERY_MODE_DESCRIPTION = (
    "Return one exact node as complete JSON without pagination."
)

JSON_VALUE_TOOL_QUERY_POINTER_DESCRIPTION = (
    "RFC 6901 JSON Pointer to the exact node; use the empty string for the root. "
    "Maximum 4,096 UTF-8 bytes. JSONPath, wildcard, filter, and regex expressions "
    "are unsupported."
)

JSON_CHILDREN_TOOL_QUERY_MODE_DESCRIPTION = "Page the direct children of one object or array node."

JSON_CHILDREN_TOOL_QUERY_POINTER_DESCRIPTION = (
    "RFC 6901 JSON Pointer to an object or array; use the empty string for the root. "
    "Maximum 4,096 UTF-8 bytes."
)

JSON_CHILDREN_TOOL_QUERY_PAGE_SIZE_DESCRIPTION = (
    "Direct child items requested; default 50 and maximum 100."
)

JSON_RECORDS_TOOL_QUERY_MODE_DESCRIPTION = (
    "Page the top-level array of a Payload declared with json_kind=records."
)

JSON_RECORDS_TOOL_QUERY_PAGE_SIZE_DESCRIPTION = (
    "Complete records requested; default 100 and maximum 200."
)

DIRECT_JSON_VALUE_INPUT_QUERY_DESCRIPTION = (
    "Complete value-mode first-page query; this branch has no cursor field."
)

DIRECT_JSON_CHILDREN_INPUT_QUERY_DESCRIPTION = (
    "Complete children-mode first-page query; this branch has no cursor field."
)

DIRECT_JSON_RECORDS_INPUT_QUERY_DESCRIPTION = (
    "Complete records-mode first-page query; this branch has no cursor field."
)

DIRECT_JSON_CONTINUATION_INPUT_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by data_read_json. Submit it unchanged; this "
    "continuation branch has no query field."
)

QUERY_PYTHON_QUERY_INPUTS_DESCRIPTION = (
    "One to 16 exact read-only Package Payload bindings. Each input_name binds one "
    "package_alias plus payload_name discovered with the Package tools."
)

QUERY_PYTHON_QUERY_INLINE_TABLES_DESCRIPTION = (
    "Zero to eight small typed request-scoped tables; at most 32 columns and 500 "
    "rows per table, with a 1 MiB aggregate canonical-JSON limit."
)

QUERY_PYTHON_QUERY_PARAMETERS_DESCRIPTION = (
    "Zero to 32 typed runtime values from the user, Requirement/CAD context, a prior "
    "query, or Package facts; they are value bindings read with ctx.param."
)

QUERY_PYTHON_QUERY_CODE_DESCRIPTION = (
    "Python source defining exactly one top-level def query(ctx), at most 65,536 "
    "UTF-8 bytes. The function must return one PyArrow Table, RecordBatch, or "
    "RecordBatchReader and cannot publish outputs. Imported module aliases are "
    "ordinary Python names; do not reuse aliases such as pa, np, pd, pc, pl, or nx "
    "for local rows, ports, strings, or scalars."
)

QUERY_PYTHON_QUERY_PAGE_SIZE_DESCRIPTION = (
    "Rows requested on the first page; default 100 and maximum 200."
)

QUERY_PYTHON_FIRST_PAGE_INPUT_QUERY_DESCRIPTION = (
    "Complete first-page Python query; this branch has no cursor field."
)

QUERY_PYTHON_CONTINUATION_INPUT_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by data_query_python. Submit it unchanged in this "
    "continuation branch, which has no query or code field."
)

QUERY_FRESH_MODE_DESCRIPTION = (
    "Start one new first-page query from the complete query object in this branch."
)

QUERY_REPAIR_MODE_DESCRIPTION = (
    "Patch and rerun one terminal failed query Attempt using the exact persisted source."
)

QUERY_CURSOR_MODE_DESCRIPTION = (
    "Continue the immutable result snapshot selected by cursor without rerunning the program."
)

QUERY_REPAIR_PARENT_ATTEMPT_ALIAS_DESCRIPTION = (
    "Terminal failed same-engine query Attempt alias returned by the original tool failure. "
    "Confirm its Code Card with code_list before reading the exact source."
)

QUERY_REPAIR_EDITS_DESCRIPTION = (
    "One to 16 non-overlapping exact literal source edits. Every old_text must occur exactly "
    "once in the inspected parent source."
)

QUERY_REPAIR_OLD_TEXT_DESCRIPTION = (
    "Non-empty exact literal copied from the inspected parent source; maximum 16,384 UTF-8 bytes."
)

QUERY_REPAIR_NEW_TEXT_DESCRIPTION = (
    "Literal replacement text; empty deletes the old literal, maximum 16,384 UTF-8 bytes."
)

QUERY_REPAIR_PAGE_SIZE_DESCRIPTION = (
    "Rows requested from the repaired result's first page; default 100 and maximum 200."
)

QUERY_SQL_EXECUTION_ENGINE_DESCRIPTION = (
    "Execute the private multi-statement DuckDB SQL query mode."
)

QUERY_SQL_EXECUTION_CODE_DESCRIPTION = (
    "Private multi-statement DuckDB SQL script, at most 65,536 UTF-8 bytes. Base "
    "relations come only from inputs, values use named parameters, and the final "
    "statement must return the single row set to snapshot."
)

QUERY_SQL_QUERY_PARAMETERS_DESCRIPTION = (
    "Zero to 32 typed runtime values from the user, Requirement/CAD context, a prior "
    "query, or Package facts; they are named value bindings, not schemas."
)

QUERY_SQL_QUERY_EXECUTION_DESCRIPTION = (
    "Exact private DuckDB script. Its final statement must return the one row set to "
    "snapshot and page."
)

QUERY_SQL_FIRST_PAGE_INPUT_QUERY_DESCRIPTION = (
    "Complete first-page SQL query; this branch has no cursor field."
)

QUERY_SQL_CONTINUATION_INPUT_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by data_query_sql. Submit it unchanged in this "
    "continuation branch, which has no query field."
)

DATA_QUERY_SQL_DESCRIPTION = (
    "Run, repair, or continue an exploratory private multi-statement DuckDB query over exact immutable "
    "Parquet Payloads without publishing a Package or Operation. Obtain package_alias "
    "from data_package_list and payload names/schema from data_describe_package; "
    "each input_name binds one exact Package Payload. Every declared input must be "
    "referenced by the submitted SQL; references inside scalar subqueries count as use, "
    "while a truly unused binding is rejected with a bounded list of the unused "
    "input_name values. Typed parameters are small values "
    "from the user, Requirement/CAD context, prior queries, or Package facts. Quote "
    'DuckDB reserved words when they are output aliases, for example AS "name"; parse '
    "and bind failures return bounded DuckDB line/token or candidate-column detail. First call: "
    "provide the complete query branch and omit cursor. Continuation: provide only the "
    "returned cursor branch unchanged; SQL is not rerun. The final SQL statement must "
    "return one row set. Use explicit ORDER BY when semantic order must survive an "
    "independent rerun; the immutable snapshot guarantees stable pages only within this "
    "call lifecycle. Default page_size is 100 and maximum 200; byte limits may return "
    "fewer rows with next_cursor. Cursors do not accept page numbers, snapshot IDs, custom "
    "positions, or caller-modified OFFSET. The snapshot has a non-sliding 900-second TTL, "
    "128 MiB maximum, four snapshots per Direct scope, eight per process, and 512 MiB "
    "total process cache. Each response is at most 1 MiB, each complex encoded cell 256 "
    "KiB, and each row 512 KiB. JSON, LIST, ARRAY, STRUCT, and MAP values return as "
    "bounded json_text strings described by row_schema; DECIMAL, binary, temporal, and "
    "special floats use their declared response_encoding, never recursive response JSON. "
    "BIGNUM, UHUGEINT, and TIME WITH TIME ZONE/TIMETZ must be CAST AS VARCHAR before the "
    "Arrow boundary, including when nested; UNION must be explicitly projected to one "
    "stable member or cast to a stable type. "
    "The query shares data_derive's 2,000,000-row/512-MiB Arrow result limit, 512 MiB "
    "DuckDB memory, 2 GiB RSS/scratch, two threads, 90 CPU seconds, 45-second SQL wall "
    "limit and two-worker capacity. No pip install, "
    "network, host path, product database, credential, persistent file, extension, IFC "
    "access, or mutation outside private TEMP relations is supported. A failed durable "
    "Attempt returns attempt_alias. Use code_list with code_kind=data_query_sql_attempt "
    "and select the Code Card whose owner_alias matches it, then pass code_locator to "
    "code_read. Repair by calling this tool with mode=repair, that attempt_alias as "
    "parent_attempt_alias, code_read's exact source_sha256, and bounded literal edits. "
    "Snapshot overflow, "
    "expiry/eviction/restart, or an unreturnable value fails with "
    "QUERY_RESULT_SNAPSHOT_TOO_LARGE, QUERY_CURSOR_EXPIRED, or "
    "QUERY_RESULT_VALUE_TOO_LARGE. "
    + DATA_QUERY_SQL_USAGE_EXAMPLE
    + " For durable results or results consumed by later writes, use data_derive. "
    "Returned rows and statistics are computed facts, not approved business or IFC decisions."
)

DATA_QUERY_PYTHON_DESCRIPTION = (
    "Run, repair, or continue one temporary read-only Python/Arrow analysis over exact "
    "immutable Package Payloads. It publishes no Package or Critical Operation. Obtain "
    "package_alias from data_package_list and exact payload names and schemas from "
    "data_describe_package. First call: submit mode=fresh with one complete query. Minimal "
    "complete request: "
    + DATA_QUERY_PYTHON_MINIMAL_FRESH_EXAMPLE
    + " Replace its aliases, payload name, and program with current catalog facts. Code must "
    "define exactly def query(ctx). The read-only context exposes ctx.table, ctx.batches, "
    "ctx.json, ctx.param, and ctx.log and has no write methods. query(ctx) returns exactly one "
    "pyarrow.Table, RecordBatch, or RecordBatchReader; the tool then returns a paged JSON "
    "result, not that Arrow object. Convert Pandas or Polars explicitly to Arrow. Python "
    "builtins are allowlisted; getattr and hasattr are unavailable, so use direct access and "
    "vectorized APIs. Shapely predicates and indexes are 2D. Do not reuse imported aliases "
    "pa, np, pd, pc, pl, or nx for other values. pip install, network or URL access, arbitrary "
    "host paths, product databases, credentials, subprocesses, IFC access, and input mutation "
    "are unsupported. Continuation: submit mode=cursor with only the returned cursor and same "
    "Direct session; Python is not rerun. Inputs are 1..16 exact Payloads within 4,000,000 "
    "rows, 256 MiB stored, and 512 MiB Arrow. Code is at most 65,536 UTF-8 bytes. Results "
    "have 1..512 columns, at most 2,000,000 rows, and a complete snapshot at most 128 MiB; "
    "the non-sliding snapshot TTL is 900 seconds. page_size defaults to 100 and is at most "
    "200; responses are at most 1 MiB. The Worker has 90 seconds wall, 180 CPU seconds, "
    "4 GiB RSS/scratch, two threads, and one concurrent call; cursor reads have 15 seconds. "
    "Important failures include QUERY_PYTHON_INVALID, QUERY_INPUT_BUDGET_EXCEEDED, "
    "QUERY_RESOURCE_LIMIT, QUERY_TIMEOUT, QUERY_CAPACITY_BUSY, "
    "QUERY_RESULT_SNAPSHOT_TOO_LARGE, QUERY_RESULT_VALUE_TOO_LARGE, and "
    "QUERY_CURSOR_EXPIRED. Runtime errors include the innermost user-code line and bounded "
    "source excerpt when available. A failed durable Attempt returns attempt_alias. Use "
    "code_list with code_kind=data_query_python_attempt, select the card whose owner_alias "
    "matches it, and pass its code_locator to code_read. Repair with mode=repair, that alias "
    "as parent_attempt_alias and bounded literal edits. The backend verifies the immutable "
    "parent source bytes; callers do not resubmit its SHA. Read "
    + DATA_QUERY_PYTHON_PROGRAM_RESOURCE_URI
    + " for context_api, program_return, MCP result navigation, complete budgets and failures, "
    "repair rules, and detailed examples. Use data_derive when a result must "
    "persist, feed a later write or IFC tool, or contain multiple outputs. Returned values are "
    "computed facts, not approved CAD classifications or IFC decisions."
)

DATA_READ_JSON_DESCRIPTION = (
    "Read one immutable JSON Payload selected from data_package_list and "
    "data_describe_package. First call supplies exactly one mode: value returns one "
    "complete RFC 6901 node, children pages only the direct children of an object or "
    "array, and records pages the top-level array of a json_kind=records Payload. Use "
    "the empty pointer for the root; pointers are limited to 4,096 UTF-8 bytes. "
    "Continuation supplies only the returned opaque cursor unchanged. Children default "
    "to 50 and allow at most 100 items; records default to 100 and allow at most 200. "
    "The response is at most 1 MiB, and each exact value or record is at most 512 KiB; "
    "JSON is never truncated. JSONPath, wildcard, filter, regex, custom sort, OFFSET, "
    "page numbers, arbitrary files, and Context resources are unsupported. The hard "
    "timeout is 15 seconds. Returned JSON is Package data, not an approved business or "
    "IFC decision."
)

DATA_QUERY_ROWS_FIRST_EXAMPLE = json.dumps(
    {
        "direct_session_alias": "direct_001",
        "query": {
            "package_alias": "data_001",
            "payload_name": "records",
            "select": ["entity_key", "length_m"],
            "filters": [{"field": "length_m", "op": "gte", "value": 0.5}],
            "page_size": 2,
        },
    },
    separators=(",", ":"),
)
DATA_QUERY_ROWS_CONTINUATION_EXAMPLE = json.dumps(
    {"direct_session_alias": "direct_001", "cursor": "cur.returned"},
    separators=(",", ":"),
)

DATA_QUERY_ROWS_DESCRIPTION = (
    "Read bounded raw rows from one source_l0 or derived Parquet payload. Obtain "
    "package_alias from data_package_list "
    "and payload/field names from data_describe_package. First call: provide the same "
    "direct_session_alias with a complete query and omit cursor. package_alias, "
    "payload_name, select, filters and page_size belong inside query, never at the "
    "call root; query.select is required. Use an empty select list to return only the "
    "payload logical-key fields, or explicitly list every ordinary field to return. "
    "Selected values retain JSON scalars, lists and objects recursively; binary values "
    "at any depth use the binary_base64 object. Arrow MAP entries retain their order "
    "as arrays of key/value pairs. Logical keys and filter values remain scalar. "
    "Continuation: use the same direct_session_alias with the returned next_cursor "
    "unchanged as cursor and omit query. Filters are "
    "AND-only; in accepts 1..100 scalar OR alternatives. Supported ops are eq, in, lt, lte, gt, "
    "gte, is_null and is_not_null. Default page_size is 100, maximum 200; ordering is "
    "fixed by the payload logical key. No OR tree, regex, SQL, arbitrary expression, "
    "custom sort or OFFSET. Cursor binds the exact package, query, ordering, page size, "
    "tool and invocation scope. Cursor capacity is checked only when another page is "
    "required. If continuation state is too large, INVALID_ARGUMENT advises smaller "
    "IN batches or data_query_sql with the same filters. Hard timeout is 15 seconds "
    "with a 2,000,000-row/256-MiB "
    "scan budget; limits fail with QUERY_TIMEOUT or QUERY_BUDGET_EXCEEDED. Query and "
    "cursor are mutually exclusive. page_size only limits returned rows; logical-key "
    "fields must be scanned even when omitted from select. Budget errors report the "
    "exceeded dimension, actual scanned rows and decoded projection bytes, and limits; "
    "matching totals remain unknown on failure. If the scan still exceeds its budget, "
    "use data_query_sql with the same filters and only necessary result columns. "
    "Complete first-call example: " + DATA_QUERY_ROWS_FIRST_EXAMPLE + ". "
    "Continuation example: " + DATA_QUERY_ROWS_CONTINUATION_EXAMPLE + ". "
    "Replace example aliases, table and fields with discovered values, and cur.returned "
    "with the actual next_cursor; these examples are not pre-existing resources. "
    'Results are Package facts or computed candidates, not '
    "approved business or IFC decisions."
)

DATA_QUERY_SQL_QUERY_DESCRIPTION = (
    "Complete first-page SQL query. Supply this field only on the first call; "
    "the continuation branch has cursor instead."
)

DATA_QUERY_SQL_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by data_query_sql. Submit it unchanged with "
    "the same Direct session in the continuation branch."
)

DATA_QUERY_PYTHON_QUERY_DESCRIPTION = (
    "Complete first-page Python query. Supply this field only on the first "
    "call; the continuation branch has cursor instead."
)

DATA_QUERY_PYTHON_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by data_query_python. Submit it unchanged "
    "with the same Direct session and omit query."
)

DATA_READ_JSON_QUERY_DESCRIPTION = (
    "Complete value, children, or records first-page query. Omit this field "
    "when continuing with cursor."
)

DATA_READ_JSON_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by data_read_json. Submit it unchanged and "
    "omit query; page numbers and OFFSET are unsupported."
)

DATA_QUERY_ROWS_QUERY_DESCRIPTION = (
    "Complete first-page Parquet row query: package_alias, payload_name, select, "
    "filters and page_size all belong inside this object, not at the call root. "
    "Supply query with direct_session_alias only on the first call and omit cursor."
)

DATA_QUERY_ROWS_CURSOR_DESCRIPTION = (
    "Opaque next_cursor returned by this tool. Submit it unchanged with the "
    "same direct_session_alias and omit query and all query fields; "
    "OFFSET/page numbers are unsupported."
)


_DATA_QUERY_PYTHON_USAGE_CONTRACT = {
    "scope": (
        "Temporary read-only analysis over 1..16 exact immutable Package Payloads. Discover "
        "package_alias with data_package_list and payload names and schemas with "
        "data_describe_package. Package and inline inputs share a unique input_name namespace; "
        "the same exact Package Payload cannot be bound twice."
    ),
    "lifecycle": (
        "A fresh call executes Python once and snapshots the complete Arrow result. A cursor call "
        "submits the returned opaque cursor unchanged with the same Direct session; it reads the "
        "same immutable snapshot and does not rerun Python."
    ),
    "publication": (
        "This tool creates no Package or Critical Operation. Use data_derive when output must "
        "persist, feed a later write or IFC tool, or contain multiple published outputs."
    ),
    "evidence_boundary": (
        "Returned rows and statistics are computed facts, not approved CAD classifications or "
        "IFC decisions."
    ),
}

_DATA_QUERY_PYTHON_CONTEXT_API = {
    "entrypoint": "Source defines exactly one top-level def query(ctx).",
    "methods": {
        "ctx.table(name)": (
            "Return the declared Parquet or inline input as a read-only pyarrow.Table. A JSON "
            "binding must be read with ctx.json."
        ),
        "ctx.batches(name)": (
            "Return a fresh pyarrow.RecordBatchReader from the start on every call for the "
            "declared Parquet or inline input."
        ),
        "ctx.json(name)": "Return the validated Python JSON value for one declared JSON input.",
        "ctx.param(name)": "Return one declared typed parameter as its native Python value.",
        "ctx.log(message)": (
            "Append bounded text diagnostics. Logs are not Package data, business conclusions, "
            "or the MCP result."
        ),
    },
    "writes": "The query context has no write_table or write_json methods and cannot mutate inputs.",
    "libraries": (
        "Available analysis libraries include PyArrow, DuckDB, Pandas, Polars, NumPy, SciPy, "
        "Shapely, NetworkX, pyproj, RapidFuzz, Pydantic, and jsonschema. Pandas and Polars "
        "results require explicit conversion to Arrow. Shapely predicates and indexes are 2D."
    ),
    "python_restrictions": (
        "Builtins are allowlisted; reflection helpers such as getattr and hasattr are unavailable, "
        "so use direct access and vectorized library APIs. Imported aliases are ordinary mutable "
        "Python names; do not reuse pa, np, pd, pc, pl, or nx for rows, ports, strings, or scalars. "
        "pip install, network or URL access, arbitrary host paths, product databases, credentials, "
        "subprocesses, IFC access, and input mutation are unsupported."
    ),
    "diagnostics": (
        "Runtime exceptions include the innermost user-code line and exact bounded source excerpt "
        "when available; full tracebacks and private Worker paths are not returned."
    ),
}

_DATA_QUERY_PYTHON_PROGRAM_RETURN = {
    "accepted": [
        "pyarrow.Table",
        "pyarrow.RecordBatch",
        "pyarrow.RecordBatchReader",
    ],
    "contract": (
        "query(ctx) returns exactly one accepted Arrow object with 1..512 unique columns; zero "
        "rows are valid. Convert Pandas with pyarrow.Table.from_pandas(..., preserve_index=False) "
        "or Polars with frame.to_arrow() before returning."
    ),
    "boundary": (
        "This is the in-Worker program return ABI. It is not the MCP JSON result Schema; the Host "
        "validates, snapshots, encodes, and pages the Arrow value before producing the tool result."
    ),
}

_DATA_QUERY_PYTHON_MCP_RESULT_NAVIGATION = {
    "registered_tool": "data_query_python",
    "top_level_fields": [
        "row_schema",
        "rows",
        "page",
        "query_statistics",
    ],
    "boundary": (
        "This is the paged JSON MCP result produced after the Host validates, snapshots, encodes, "
        "and pages the Arrow program return. It is not the in-Worker return ABI."
    ),
    "discovery": (
        "If your client exposes MCP list_tools and outputSchema, data_query_python.outputSchema "
        "provides the complete registered result contract. Clients need not expose that API: "
        "use this resource's result field guidance and the actual tool response instead; do not "
        "invent a schema-reading tool. Submit page.next_cursor unchanged with mode=cursor to continue."
    ),
    "result_fields": {
        "row_schema": "Column names and types for interpreting this result's rows.",
        "rows": "The actual computed rows in this page, not a newly published Package.",
        "page": (
            "returned_count is this page's row count; remaining_count is the count still unread. "
            "complete=true means remaining_count=0 and next_cursor=null. Otherwise continue with "
            "the same Direct session and the exact non-null next_cursor; do not rerun the program."
        ),
        "query_statistics": "Execution statistics for the immutable query snapshot, not business conclusions.",
    },
}

_DATA_QUERY_PYTHON_BUDGETS_AND_FAILURES = {
    "request": (
        "Bind 1..16 exact Package inputs. Package plus inline data is limited to 4,000,000 rows, "
        "256 MiB stored input bytes, and 512 MiB Arrow input. Supply 0..8 inline tables, each at "
        "most 32 columns and 500 rows, within 1 MiB aggregate canonical JSON; 0..32 typed "
        "parameters within 32 KiB aggregate canonical JSON; and source within 65,536 UTF-8 bytes."
    ),
    "result_and_page": (
        "The Arrow result has 1..512 columns and at most 2,000,000 rows and must fit the 128 MiB "
        "complete snapshot limit. page_size defaults to 100 and ranges from 1..200. Each response "
        "is at most 1 MiB, each complex encoded cell 256 KiB, and each row 512 KiB; byte limits can "
        "return fewer rows than page_size."
    ),
    "snapshot_and_cursor": (
        "Snapshots have a non-sliding 900-second TTL, 128 MiB per snapshot, four per Direct scope, "
        "eight per process, and 512 MiB total process cache. Expiry, LRU eviction, or restart makes "
        "the cursor expire. Cursor reads have a 15-second MCP deadline."
    ),
    "worker": (
        "The shared native Python/geometry/IFC pool allows one concurrent call. Python is bounded "
        "by 90 seconds wall, 180 CPU seconds, 4 GiB RSS, 4 GiB scratch, and two threads."
        + _runtime_budget_description(
            _DATA_PYTHON_EXECUTION,
            "shared native Python/geometry/IFC (one slot)",
        )
    ),
    "errors": [
        "INVALID_ARGUMENT",
        "PAYLOAD_NOT_FOUND",
        "QUERY_PYTHON_INVALID",
        "QUERY_INPUT_BUDGET_EXCEEDED",
        "QUERY_EXECUTION_FAILED",
        "QUERY_RESOURCE_LIMIT",
        "QUERY_TIMEOUT",
        "QUERY_CAPACITY_BUSY",
        "QUERY_SERVICE_UNAVAILABLE",
        "QUERY_RESULT_SNAPSHOT_TOO_LARGE",
        "QUERY_RESULT_VALUE_TOO_LARGE",
        "CURSOR_INVALID",
        "CURSOR_TOOL_MISMATCH",
        "CURSOR_SCOPE_MISMATCH",
        "QUERY_CURSOR_EXPIRED",
    ],
    "retry": (
        "QUERY_CAPACITY_BUSY and QUERY_SERVICE_UNAVAILABLE are retryable only when execution did "
        "not start. Follow the returned retryable flag; other listed failures require correcting "
        "the request, program, input, result, or cursor."
    ),
}

_DATA_QUERY_PYTHON_REPAIR_CONTRACT = {
    "discover": (
        "A failed durable query Attempt returns attempt_alias. Call code_list with "
        "code_kind=data_query_python_attempt, select the Code Card whose owner_alias matches that "
        "attempt_alias, and pass its code_locator unchanged to code_read."
    ),
    "request": (
        "Call data_query_python with mode=repair, the failed alias as parent_attempt_alias, "
        "1..16 literal edits and the "
        "page_size for the repaired result. The terminal parent must use the python_arrow engine."
    ),
    "edits": (
        "Each old_text must be non-empty and occur exactly once in the inspected source. Edits must "
        "not overlap. Each old_text and new_text is at most 16,384 UTF-8 bytes, their aggregate is "
        "at most 32,768 bytes, and repaired source is non-empty and at most 65,536 bytes. "
        "Absent or repeated literals, overlaps and budget excess are rejected. The backend "
        "verifies the exact immutable parent source; no caller-supplied SHA is required."
    ),
    "inheritance": (
        "Repair inherits the parent's exact Package inputs, typed parameters, and inline tables. A "
        "successful repair creates a new immutable result snapshot; continuation uses its cursor."
    ),
    "failure_boundary": (
        "A repair rejected before execution preparation creates no new Attempt. Once execution is "
        "prepared, a failure records a new terminal Attempt and publishes no Package or Critical "
        "Operation."
    ),
}


def data_query_python_program_document() -> dict[str, object]:
    """Build the public Python query program document."""

    return {
        "usage": _DATA_QUERY_PYTHON_USAGE_CONTRACT,
        "context_api": _DATA_QUERY_PYTHON_CONTEXT_API,
        "program_return": _DATA_QUERY_PYTHON_PROGRAM_RETURN,
        "mcp_result_navigation": _DATA_QUERY_PYTHON_MCP_RESULT_NAVIGATION,
        "budgets_and_failures": _DATA_QUERY_PYTHON_BUDGETS_AND_FAILURES,
        "repair": _DATA_QUERY_PYTHON_REPAIR_CONTRACT,
        "examples": {
            "source": DATA_QUERY_PYTHON_SOURCE_EXAMPLE,
            "complete_fresh_request": json.loads(DATA_QUERY_PYTHON_FRESH_EXAMPLE),
            "complete_repair_request": json.loads(DATA_QUERY_PYTHON_REPAIR_EXAMPLE),
        },
    }


__all__ = [
    "DATA_QUERY_PYTHON_PROGRAM_RESOURCE_DESCRIPTION",
    "DATA_QUERY_PYTHON_PROGRAM_RESOURCE_URI",
    "DATA_QUERY_PYTHON_REPAIR_EXAMPLE",
    "DATA_QUERY_PYTHON_FRESH_EXAMPLE",
    "DATA_QUERY_PYTHON_MINIMAL_FRESH_EXAMPLE",
    "DATA_QUERY_PYTHON_SOURCE_EXAMPLE",
    "DATA_QUERY_SQL_FRESH_EXAMPLE",
    "DATA_QUERY_SQL_SOURCE_EXAMPLE",
    "DATA_QUERY_SQL_USAGE_EXAMPLE",
    "data_query_python_program_document",
]


DATA_QUERY_SQL_DESCRIPTION += _runtime_budget_description(_DATA_SQL_EXECUTION, 'SQL (two slots)')
DATA_QUERY_PYTHON_DESCRIPTION += _runtime_budget_description(_DATA_PYTHON_EXECUTION, 'shared native Python/geometry/IFC (one slot)')
