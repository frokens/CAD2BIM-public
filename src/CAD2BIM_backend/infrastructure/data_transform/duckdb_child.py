"""Private child-process entry point for one M2 DuckDB script."""

from __future__ import annotations

import json
import os
import re
import resource
import sys
import threading
import time
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from CAD2BIM_backend.domain.data_transform import truncate_transform_error_message
from CAD2BIM_backend.ports.data_transform import DUCKDB_LOGICAL_TYPE_METADATA_KEY


class _PreflightError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _StartedWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, phase: str) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase


_NAMED_PARAMETER = re.compile(r"\$[a-z][a-z0-9_]{0,62}", re.IGNORECASE)
_SQL_IDENTIFIER = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)'
_CREATE_TEMP = re.compile(
    rf"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP|TEMPORARY)\s+"
    rf"(?:TABLE|VIEW|MACRO)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_SQL_IDENTIFIER})",
    re.IGNORECASE | re.DOTALL,
)
_MUTATION_TARGETS = {
    "INSERT": re.compile(rf"^\s*INSERT\s+INTO\s+(?P<name>{_SQL_IDENTIFIER})", re.IGNORECASE),
    "UPDATE": re.compile(rf"^\s*UPDATE\s+(?P<name>{_SQL_IDENTIFIER})", re.IGNORECASE),
    "DELETE": re.compile(rf"^\s*DELETE\s+FROM\s+(?P<name>{_SQL_IDENTIFIER})", re.IGNORECASE),
    "MERGE_INTO": re.compile(rf"^\s*MERGE\s+INTO\s+(?P<name>{_SQL_IDENTIFIER})", re.IGNORECASE),
    "DROP": re.compile(
        rf"^\s*DROP\s+(?:TABLE|VIEW|MACRO)\s+(?:IF\s+EXISTS\s+)?(?P<name>{_SQL_IDENTIFIER})",
        re.IGNORECASE,
    ),
    "ALTER": re.compile(
        rf"^\s*ALTER\s+(?:TABLE|VIEW|MACRO)\s+(?P<name>{_SQL_IDENTIFIER})",
        re.IGNORECASE,
    ),
}
_FORBIDDEN_STATEMENT_TYPES = {
    "ATTACH",
    "CALL",
    "COPY",
    "EXPORT",
    "IMPORT",
    "LOAD",
    "SET",
}
_UNSAFE_DIAGNOSTIC_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _bounded_duckdb_bind_message(exc: duckdb.Error) -> str:
    """Keep DuckDB's actionable Binder detail without exposing unbounded diagnostics."""

    detail = _UNSAFE_DIAGNOSTIC_CONTROL.sub("", str(exc)).strip()
    if not detail:
        return "DuckDB could not bind the SQL"
    return truncate_transform_error_message(
        f"DuckDB could not bind the SQL: {detail}",
        fallback="DuckDB could not bind the SQL",
    )


def _bounded_duckdb_parse_message(exc: duckdb.Error) -> str:
    """Keep the parser line/token hint while preserving the public error bound."""

    detail = _UNSAFE_DIAGNOSTIC_CONTROL.sub("", str(exc)).strip()
    if not detail:
        return "DuckDB could not parse the SQL"
    return truncate_transform_error_message(
        f"DuckDB could not parse the SQL: {detail}",
        fallback="DuckDB could not parse the SQL",
    )


def _parameter_free_query(code: str) -> str:
    """Replace value placeholders only for DuckDB's table-name introspector.

    DuckDB 1.5 cannot prepare ``get_table_names``. The original parsed statement
    and its exact named-parameter set remain authoritative for execution.
    """

    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(code):
        if quote == "line":
            end = code.find("\n", index)
            if end < 0:
                output.append(code[index:])
                break
            output.append(code[index : end + 1])
            index = end + 1
            quote = None
            continue
        if quote == "block":
            end = code.find("*/", index)
            if end < 0:
                output.append(code[index:])
                break
            output.append(code[index : end + 2])
            index = end + 2
            quote = None
            continue
        if quote in {"'", '"'}:
            character = code[index]
            output.append(character)
            index += 1
            if character == quote:
                if index < len(code) and code[index] == quote:
                    output.append(code[index])
                    index += 1
                else:
                    quote = None
            continue
        if code.startswith("--", index):
            output.append("--")
            index += 2
            quote = "line"
            continue
        if code.startswith("/*", index):
            output.append("/*")
            index += 2
            quote = "block"
            continue
        if code[index] in {"'", '"'}:
            quote = code[index]
            output.append(code[index])
            index += 1
            continue
        match = _NAMED_PARAMETER.match(code, index)
        if match is not None:
            output.append("NULL")
            index = match.end()
            continue
        output.append(code[index])
        index += 1
    return "".join(output)


def _emit(event: str, **fields: Any) -> None:
    value = json.dumps(
        {"event": event, **fields},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    sys.stdout.write(value + "\n")
    sys.stdout.flush()


def _apply_resource_limits(policy: dict[str, Any]) -> None:
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (int(policy["cpu_seconds"]), int(policy["cpu_seconds"])),
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (
            int(policy["open_file_descriptors"]),
            int(policy["open_file_descriptors"]),
        ),
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (int(policy["scratch_bytes"]), int(policy["scratch_bytes"])),
    )


def _connection(policy: dict[str, Any]) -> duckdb.DuckDBPyConnection:
    scratch = Path.cwd() / "duckdb-tmp"
    settings = {
        "enable_external_access": "false",
        "allow_community_extensions": "false",
        "allow_unsigned_extensions": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "python_enable_replacements": "false",
        "python_scan_all_frames": "false",
        "threads": str(policy["threads"]),
        "memory_limit": f"{policy['duckdb_memory_bytes']}B",
        "temp_directory": str(scratch),
        "max_temp_directory_size": f"{policy['scratch_bytes']}B",
    }
    connection = duckdb.connect(":memory:", config=settings)
    connection.execute("SET lock_configuration=true")
    return connection


_SYSTEM_RELATION_PREFIXES = ("DUCKDB_", "PRAGMA_", "SQLITE_")
_SYSTEM_FUNCTION_PREFIXES = (
    "CURRENT_",
    "DUCKDB_",
    "GET_CURRENT_",
    "PRAGMA_",
    "SQLITE_",
)
_SYSTEM_SCHEMAS = {"INFORMATION_SCHEMA", "PG_CATALOG"}
_DYNAMIC_SQL_FUNCTIONS = {
    "CHECKPOINT",
    "DISABLE_PROFILING",
    "ENABLE_PROFILING",
    "FORCE_CHECKPOINT",
    "JSON_DESERIALIZE_SQL",
    "JSON_EXECUTE_SERIALIZED_SQL",
    "JSON_SERIALIZE_PLAN",
    "JSON_SERIALIZE_SQL",
    "QUERY",
    "QUERY_TABLE",
}


def _token_text(code: str, tokens: list[tuple[int, Any]], index: int) -> str:
    start = tokens[index][0]
    end = tokens[index + 1][0] if index + 1 < len(tokens) else len(code)
    return code[start:end].strip()


def _identifier_name(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('""', '"')
    return value.upper()


def _unquote_identifier(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _identifier_key(value: str) -> str:
    """Match DuckDB's case-insensitive identifier comparison."""

    return _unquote_identifier(value).casefold()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _uses_system_function(code: str) -> bool:
    """Use DuckDB's lexer to reject configuration/system function capabilities."""

    tokens = duckdb.tokenize(code)
    for index, (_start, token_kind) in enumerate(tokens[:-1]):
        if token_kind not in {duckdb.token_type.identifier, duckdb.token_type.keyword}:
            continue
        if _token_text(code, tokens, index + 1) != "(":
            continue
        name = _identifier_name(_token_text(code, tokens, index))
        if name in _DYNAMIC_SQL_FUNCTIONS or name.startswith(_SYSTEM_FUNCTION_PREFIXES):
            return True
        if index >= 2 and _token_text(code, tokens, index - 1) == ".":
            schema = _identifier_name(_token_text(code, tokens, index - 2))
            if schema in _SYSTEM_SCHEMAS:
                return True
    return False


def _contains_system_token(value: Any) -> bool:
    if isinstance(value, str):
        return value.upper().startswith(_SYSTEM_RELATION_PREFIXES)
    if isinstance(value, list):
        return any(_contains_system_token(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_system_token(item) for item in value.values())
    return False


def _contains_system_relation(plan: Any) -> bool:
    if isinstance(plan, list):
        return any(_contains_system_relation(item) for item in plan)
    if not isinstance(plan, dict):
        return False
    if _contains_system_token(plan.get("name", "")):
        return True
    if _contains_system_token(plan.get("extra_info", {})):
        return True
    return _contains_system_relation(plan.get("children", []))


def _plan_relation_names(plan: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(plan, list):
        for item in plan:
            names.update(_plan_relation_names(item))
        return names
    if not isinstance(plan, dict):
        return names
    extra_info = plan.get("extra_info", {})
    if isinstance(extra_info, dict):
        table = extra_info.get("Table")
        if isinstance(table, str) and table:
            names.add(_identifier_key(table.rsplit(".", 1)[-1]))
    names.update(_plan_relation_names(plan.get("children", [])))
    return names


def _statement_type(statement: Any) -> str:
    return str(statement.type).rsplit(".", 1)[-1]


def _statement_target(statement: Any) -> str | None:
    statement_type = _statement_type(statement)
    pattern = _MUTATION_TARGETS.get(statement_type)
    if pattern is None:
        return None
    match = pattern.match(str(statement.query))
    if match is None:
        raise _PreflightError(
            "TRANSFORM_SQL_NOT_ALLOWED",
            "DuckDB mutation target could not be safely resolved",
        )
    return _unquote_identifier(match.group("name"))


def _is_temporary_relation(connection: duckdb.DuckDBPyConnection, name: str) -> bool:
    rows = connection.execute(
        """
        SELECT relation_name
        FROM (
            SELECT table_name AS relation_name, temporary FROM duckdb_tables()
            UNION ALL
            SELECT view_name AS relation_name, temporary FROM duckdb_views()
        ) AS relations
        WHERE temporary
        """,
    ).fetchall()
    key = _identifier_key(name)
    return sum(_identifier_key(str(row[0])) == key for row in rows) == 1


def _preflight_syntax(
    connection: duckdb.DuckDBPyConnection,
    code: str,
    parameters: dict[str, Any],
    result_mode: str,
) -> list[Any]:
    try:
        statements = connection.extract_statements(code)
    except duckdb.Error as exc:
        raise _PreflightError(
            "TRANSFORM_SQL_INVALID", _bounded_duckdb_parse_message(exc)
        ) from exc
    if not statements:
        raise _PreflightError(
            "TRANSFORM_SQL_INVALID", "DuckDB SQL must contain at least one statement"
        )
    if result_mode == "legacy_final_result" and len(statements) != 1:
        raise _PreflightError(
            "TRANSFORM_SQL_INVALID", "Legacy result mode requires exactly one statement"
        )
    if result_mode == "legacy_final_result" and statements[0].type != duckdb.StatementType.SELECT:
        raise _PreflightError(
            "TRANSFORM_SQL_NOT_ALLOWED",
            "Legacy result mode accepts one SELECT or WITH ... SELECT",
        )
    if result_mode == "query_final_result" and (
        duckdb.ExpectedResultType.QUERY_RESULT not in statements[-1].expected_result_type
    ):
        raise _PreflightError(
            "TRANSFORM_SQL_INVALID",
            "Query result mode requires a final statement that returns rows",
        )
    named_parameters = {name for statement in statements for name in statement.named_parameters}
    if named_parameters != set(parameters):
        raise _PreflightError(
            "TRANSFORM_PARAMETER_MISMATCH",
            "SQL named parameters do not match the declared parameters",
        )
    if _uses_system_function(code):
        raise _PreflightError(
            "TRANSFORM_SQL_NOT_ALLOWED",
            "DuckDB configuration and system functions are not available to transform SQL",
        )
    for statement in statements:
        statement_type = _statement_type(statement)
        query = str(statement.query)
        if statement_type in _FORBIDDEN_STATEMENT_TYPES or query.lstrip().upper().startswith(
            "PRAGMA"
        ):
            raise _PreflightError(
                "TRANSFORM_SQL_NOT_ALLOWED",
                "SQL requests an external, extension, or persistent capability",
            )
        if statement_type == "CREATE" and _CREATE_TEMP.match(query) is None:
            raise _PreflightError(
                "TRANSFORM_SQL_NOT_ALLOWED",
                "M2b CREATE is limited to private TEMP TABLE, VIEW, or MACRO objects",
            )
    return statements


def _preflight_statement_plan(
    connection: duckdb.DuckDBPyConnection,
    statement: Any,
    parameters: dict[str, Any],
) -> set[str]:
    statement_parameters = {name: parameters[name] for name in statement.named_parameters}
    try:
        # Relation-use validation needs the bound scans before DuckDB can fold a
        # filtered scalar subquery to EMPTY_RESULT using table statistics.
        connection.execute("PRAGMA disable_optimizer")
        try:
            row = connection.execute(
                "EXPLAIN (FORMAT JSON) " + str(statement.query),
                statement_parameters,
            ).fetchone()
        finally:
            connection.execute("PRAGMA enable_optimizer")
        plan = json.loads(str(row[1])) if row is not None else []
    except duckdb.PermissionException as exc:
        raise _PreflightError(
            "TRANSFORM_SQL_NOT_ALLOWED", "SQL requests disabled external access"
        ) from exc
    except duckdb.Error as exc:
        raise _PreflightError(
            "TRANSFORM_SQL_INVALID", _bounded_duckdb_bind_message(exc)
        ) from exc
    if _contains_system_relation(plan):
        raise _PreflightError(
            "TRANSFORM_SQL_NOT_ALLOWED", "DuckDB system relations are not transform inputs"
        )
    return _plan_relation_names(plan)


def _interrupt_on_request(
    connection: duckdb.DuckDBPyConnection,
    interrupt_fd: int,
) -> None:
    try:
        if os.read(interrupt_fd, 1):
            connection.interrupt()
    except OSError:
        return


def _arrow_schema_with_logical_types(
    schema: pa.Schema,
    description: list[tuple[Any, ...]] | None,
) -> pa.Schema:
    if description is None or len(description) != len(schema):
        raise _StartedWorkerError(
            "TRANSFORM_WORKER_FAILED",
            "DuckDB result metadata is inconsistent",
            "execution",
        )
    fields: list[pa.Field] = []
    for field, column in zip(schema, description, strict=True):
        metadata = dict(field.metadata or {})
        metadata[DUCKDB_LOGICAL_TYPE_METADATA_KEY] = str(column[1]).encode("utf-8")
        fields.append(field.with_metadata(metadata))
    return pa.schema(fields, metadata=schema.metadata)


_NESTED_LOGICAL_TYPE_IDS = {"array", "list", "map", "struct", "union"}
_UNSAFE_LOGICAL_TYPE_NAMES = {
    "bignum": "BIGNUM",
    "time with time zone": "TIME WITH TIME ZONE",
    "timetz": "TIMETZ",
    "uhugeint": "UHUGEINT",
}


def _unsafe_logical_type(logical_type: Any) -> str | None:
    logical_type_id = str(logical_type.id).casefold()
    if logical_type_id in _UNSAFE_LOGICAL_TYPE_NAMES:
        return _UNSAFE_LOGICAL_TYPE_NAMES[logical_type_id]
    if logical_type_id not in _NESTED_LOGICAL_TYPE_IDS:
        return None
    for _field_name, child_type in logical_type.children:
        if unsafe := _unsafe_logical_type(child_type):
            return unsafe
    return None


def _write_cursor_result(
    cursor: duckdb.DuckDBPyConnection,
    descriptor: int,
    policy: dict[str, Any],
) -> None:
    description = list(cursor.description or [])
    reader = cursor.fetch_record_batch(int(policy["output_batch_rows"]))
    schema = _arrow_schema_with_logical_types(reader.schema, description)
    if not 1 <= len(schema) <= int(policy["max_output_columns"]):
        raise _StartedWorkerError(
            "TRANSFORM_OUTPUT_INVALID",
            "A DuckDB output must contain 1..512 columns",
            "output_validation",
        )
    unsafe_types = {
        unsafe for column in description if (unsafe := _unsafe_logical_type(column[1])) is not None
    }
    if unsafe_types:
        reader.close()
        raise _StartedWorkerError(
            "TRANSFORM_OUTPUT_INVALID",
            "DuckDB output contains Arrow-unsafe logical type(s) "
            f"{', '.join(sorted(unsafe_types))}; CAST each problem field AS VARCHAR",
            "output_validation",
        )
    try:
        with os.fdopen(descriptor, "wb", buffering=0) as raw_output:
            with pa.ipc.new_stream(raw_output, schema) as writer:
                for batch in reader:
                    writer.write_batch(pa.RecordBatch.from_arrays(batch.columns, schema=schema))
    finally:
        reader.close()


def _created_name(statement: Any) -> str | None:
    if _statement_type(statement) != "CREATE":
        return None
    match = _CREATE_TEMP.match(str(statement.query))
    if match is None:
        return None
    return _unquote_identifier(match.group("name"))


def _static_relation_names(
    connection: duckdb.DuckDBPyConnection,
    statement: Any,
) -> set[str]:
    query = _parameter_free_query(str(statement.query))
    candidates = [query]
    if _statement_type(statement) == "CREATE" and re.search(r"\bVIEW\b", query, re.IGNORECASE):
        created = _CREATE_TEMP.match(query)
        if created is not None:
            tail = query[created.end() :]
            separator = re.search(r"\bAS\b", tail, re.IGNORECASE)
            if separator is not None:
                candidates.append(tail[separator.end() :])
    result: set[str] = set()
    for candidate in candidates:
        try:
            result.update(_identifier_key(name) for name in connection.get_table_names(candidate))
        except duckdb.PermissionException:
            raise
        except duckdb.Error:
            continue
    return result


def _run(request: dict[str, Any]) -> None:
    policy = dict(request["policy"])
    _apply_resource_limits(policy)
    os.umask(0o077)
    connection = _connection(policy)
    input_readers: list[pa.RecordBatchReader] = []
    execution_started = False
    try:
        parameters = {str(item["name"]): item["value"] for item in request.get("parameters", [])}
        result_mode = str(request.get("result_mode") or "legacy_final_result")
        statements = _preflight_syntax(
            connection,
            str(request["code"]),
            parameters,
            result_mode,
        )
        legacy_transform = (
            result_mode == "named_relations"
            and len(statements) == 1
            and statements[0].type == duckdb.StatementType.SELECT
        )
        legacy_relation_names: set[str] | None = None
        static_relation_names: set[str] = set()
        for statement in statements:
            try:
                static_relation_names.update(_static_relation_names(connection, statement))
            except duckdb.PermissionException as exc:
                raise _PreflightError(
                    "TRANSFORM_SQL_NOT_ALLOWED", "SQL requests disabled external access"
                ) from exc
            except duckdb.Error:
                # Later statements can reference TEMP relations that do not exist
                # until earlier statements have executed. They are rebound below.
                pass
        if result_mode == "legacy_final_result" or legacy_transform:
            try:
                legacy_relation_names = {
                    _identifier_key(name)
                    for name in connection.get_table_names(
                        _parameter_free_query(str(statements[0].query))
                    )
                }
            except duckdb.PermissionException as exc:
                raise _PreflightError(
                    "TRANSFORM_SQL_NOT_ALLOWED", "SQL requests disabled external access"
                ) from exc
            except duckdb.Error as exc:
                raise _PreflightError(
                    "TRANSFORM_SQL_INVALID", "DuckDB could not inspect the SQL"
                ) from exc
        for ordinal, item in enumerate(request["inputs"]):
            raw = os.fdopen(int(item["fd"]), "rb", buffering=0)
            reader = pa.ipc.open_stream(raw)
            input_readers.append(reader)
            raw_name = f"__cad2bim_input_{ordinal:02d}"
            input_name = str(item["input_name"])
            input_format = str(item.get("input_format") or "parquet")
            json_kind = item.get("json_kind")
            projection = "*"
            if input_format == "json":
                if json_kind == "document":
                    projection = "CAST(json_value AS JSON) AS json_value"
                elif json_kind == "records":
                    projection = "record_index, CAST(json_value AS JSON) AS json_value"
                else:
                    raise _PreflightError(
                        "TRANSFORM_SQL_NOT_ALLOWED",
                        "A JSON input relation requires document or records metadata",
                    )
            connection.register(raw_name, reader)
            try:
                connection.execute(
                    f"CREATE TEMP TABLE {_quote_identifier(input_name)} AS "
                    f"SELECT {projection} FROM {_quote_identifier(raw_name)}"
                )
            finally:
                connection.unregister(raw_name)
                reader.close()

        input_name_by_key = {
            _identifier_key(str(item["input_name"])): str(item["input_name"])
            for item in request["inputs"]
        }
        input_names = set(input_name_by_key)
        for statement in statements:
            created_name = _created_name(statement)
            if created_name is not None and _identifier_key(created_name) in input_names:
                raise _PreflightError(
                    "TRANSFORM_SQL_NOT_ALLOWED",
                    "SQL cannot replace a declared read-only input relation",
                )
        if result_mode == "legacy_final_result" or legacy_transform:
            _preflight_statement_plan(connection, statements[0], parameters)
            if legacy_relation_names != input_names:
                raise _PreflightError(
                    "TRANSFORM_SQL_NOT_ALLOWED",
                    "SQL base relations must exactly match the declared inputs",
                )
        _emit("preflight_ok")

        interrupt_fd = int(request["interrupt_fd"])
        threading.Thread(
            target=_interrupt_on_request,
            args=(connection, interrupt_fd),
            daemon=True,
        ).start()

        _emit("started")
        execution_started = True
        try:
            started = time.monotonic()
            used_inputs: set[str] = (
                set(input_names)
                if result_mode == "legacy_final_result" or legacy_transform
                else input_names.intersection(static_relation_names)
            )
            final_cursor: duckdb.DuckDBPyConnection | None = None
            for index, statement in enumerate(statements):
                target = _statement_target(statement)
                if target is not None and (
                    _identifier_key(target) in input_names
                    or not _is_temporary_relation(connection, target)
                ):
                    raise _StartedWorkerError(
                        "TRANSFORM_SQL_NOT_ALLOWED",
                        "DDL and DML may affect only private TEMP relations",
                        "execution",
                    )
                try:
                    plan_relation_names = _preflight_statement_plan(
                        connection, statement, parameters
                    )
                except _PreflightError as exc:
                    raise _StartedWorkerError(exc.code, str(exc), "execution") from exc
                used_inputs.update(input_names.intersection(plan_relation_names))
                try:
                    used_inputs.update(
                        input_names.intersection(_static_relation_names(connection, statement))
                    )
                except duckdb.Error:
                    pass
                statement_parameters = {
                    name: parameters[name] for name in statement.named_parameters
                }
                cursor = connection.execute(statement, statement_parameters)
                if (
                    result_mode in {"legacy_final_result", "query_final_result"}
                    and index == len(statements) - 1
                ) or legacy_transform:
                    final_cursor = cursor
            if used_inputs != input_names:
                unused_inputs = [
                    input_name_by_key[key] for key in sorted(input_names - used_inputs)
                ]
                raise _StartedWorkerError(
                    "TRANSFORM_SQL_NOT_ALLOWED",
                    "SQL base relations must use every declared input; unused input_name(s): "
                    + ", ".join(unused_inputs),
                    "output_validation",
                )

            output_specs = list(request["outputs"])
            if result_mode in {"legacy_final_result", "query_final_result"} or legacy_transform:
                if final_cursor is None or len(output_specs) != 1:
                    raise _StartedWorkerError(
                        "TRANSFORM_WORKER_FAILED",
                        "DuckDB result output protocol is inconsistent",
                        "execution",
                    )
                _write_cursor_result(final_cursor, int(output_specs[0]["fd"]), policy)
            else:
                for output in output_specs:
                    output_name = str(output["payload_name"])
                    if not _is_temporary_relation(connection, output_name):
                        raise _StartedWorkerError(
                            "TRANSFORM_OUTPUT_RELATION_NOT_FOUND",
                            f"Declared output relation {output_name!r} was not found as a TEMP TABLE or VIEW",
                            "output_validation",
                        )
                    cursor = connection.execute(f"SELECT * FROM {_quote_identifier(output_name)}")
                    _write_cursor_result(cursor, int(output["fd"]), policy)
            _emit("done", execution_ms=max(0, int((time.monotonic() - started) * 1000)))
        except _StartedWorkerError:
            raise
        except BaseException as exc:
            raise _StartedWorkerError(
                "TRANSFORM_EXECUTION_FAILED",
                "DuckDB execution failed",
                "execution",
            ) from exc
    finally:
        try:
            connection.close()
        except BaseException as exc:
            if execution_started:
                raise _StartedWorkerError(
                    "TRANSFORM_WORKER_FAILED",
                    "DuckDB worker failed while closing its private database",
                    "execution",
                ) from exc
            raise


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read())
        _run(request)
    except _PreflightError as exc:
        _emit("error", code=exc.code, phase="preflight", message=str(exc), started=False)
    except _StartedWorkerError as exc:
        # Never serialize a traceback, SQL text, path, or inherited process detail.
        sys.stderr.write(type(exc).__name__[:120])
        sys.stderr.flush()
        _emit(
            "error",
            code=exc.code,
            phase=exc.phase,
            message=str(exc),
            started=True,
        )
    except BaseException as exc:
        sys.stderr.write(type(exc).__name__[:120])
        sys.stderr.flush()
        _emit(
            "error",
            code="TRANSFORM_SERVICE_UNAVAILABLE",
            phase="preflight",
            message="DuckDB worker infrastructure is temporarily unavailable",
            started=False,
        )


if __name__ == "__main__":
    main()
