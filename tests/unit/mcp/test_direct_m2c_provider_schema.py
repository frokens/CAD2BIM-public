from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.data_transform import (
    DataTransformCommand,
    JsonOutputDeclaration as DomainJsonOutputDeclaration,
    PythonArrowExecution as DomainPythonArrowExecution,
    PythonArrowWorkerPolicy,
    PythonArrowWorkerRequest,
)
from CAD2BIM_mcp.direct.transform.inputs import (
    DirectDuckDbTransformInput,
    DirectDataTransformCreateInput,
    DirectPythonArrowTransformInput,
    InlineTable,
    JsonOutputDeclaration,
    to_domain_duckdb_transform_command,
    to_domain_data_transform_create_command,
    to_domain_python_arrow_transform_command,
    validate_direct_data_transform_create_input,
)


_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}


def _package_input() -> dict[str, object]:
    return {
        "input_name": "entities",
        "package_alias": "data_001",
        "payload_name": "entities",
    }


def _parquet_output(name: str = "selected") -> dict[str, object]:
    return {
        "payload_name": name,
        "format": "parquet",
        "description": "Selected facts",
        "logical_key_fields": ["entity_key"],
    }


def _json_output(name: str = "summary") -> dict[str, object]:
    return {
        "payload_name": name,
        "format": "json",
        "description": "Exact summary document",
        "json_kind": "document",
        "schema_json": json.dumps(
            {"action": "set", "value": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            }}
        ),
    }


def _output_package() -> dict[str, object]:
    return {
        "name": "selected_v1",
        "description_markdown": {"action": "set", "value": "# Selected\n## Summary\nSelected facts."},
    }


def _inline_table() -> dict[str, object]:
    return {
        "input_name": "trial_values",
        "columns": [
            {"name": "label", "type": "string", "nullable": False},
            {"name": "count", "type": "integer", "nullable": False},
            {"name": "ratio", "type": "number", "nullable": False},
            {"name": "enabled", "type": "boolean", "nullable": False},
            {"name": "comment", "type": "string", "nullable": True},
        ],
        "rows": [
            {
                "values": [
                    {"kind": "string", "value": "base"},
                    {"kind": "integer", "value": 1},
                    {"kind": "number", "value": 1.5},
                    {"kind": "boolean", "value": True},
                    {"kind": "null"},
                ]
            }
        ],
    }


def _duckdb_payload() -> dict[str, object]:
    return {
        "mode": "create",
        "direct_session_alias": "direct_001",
        "inputs": [_package_input()],
        "inline_tables": [_inline_table()],
        "parameters": [{"name": "minimum", "kind": "integer", "value": 1}],
        "execution": {
            "engine": "duckdb_sql",
            "code": "CREATE TEMP TABLE selected AS SELECT * FROM entities",
        },
        "outputs": {"action": "set", "value": [_parquet_output()]},
        "output_package": _output_package(),
    }


def _python_payload() -> dict[str, object]:
    return {
        "mode": "create",
        "direct_session_alias": "direct_001",
        "inputs": [_package_input()],
        "inline_tables": [_inline_table()],
        "parameters": [{"name": "minimum", "kind": "integer", "value": 1}],
        "execution": {
            "engine": "python_arrow",
            "code": (
                "def transform(ctx):\n"
                "    ctx.write_table('selected', ctx.table('entities'))\n"
                "    ctx.write_json('summary', {'count': 1})\n"
            ),
        },
        "outputs": {"action": "set", "value": [_parquet_output(), _json_output()]},
        "output_package": _output_package(),
    }


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def test_complete_root_branches_convert_to_entry_neutral_domain_commands() -> None:
    duckdb = DirectDuckDbTransformInput.model_validate(_duckdb_payload())
    duckdb_command = to_domain_duckdb_transform_command(duckdb)
    assert isinstance(duckdb_command, DataTransformCommand)
    assert duckdb_command.execution.engine == "duckdb_sql"
    assert duckdb_command.inline_tables[0].rows[0].values[-1].kind == "null"
    assert not hasattr(duckdb_command, "direct_session_alias")

    python = DirectPythonArrowTransformInput.model_validate(_python_payload())
    python_command = to_domain_python_arrow_transform_command(python)
    assert isinstance(python_command.execution, DomainPythonArrowExecution)
    assert [output.format for output in python_command.outputs] == ["parquet", "json"]
    assert isinstance(python_command.outputs[1], DomainJsonOutputDeclaration)
    assert to_domain_data_transform_create_command(python) == python_command

    assert isinstance(
        validate_direct_data_transform_create_input(_duckdb_payload()),
        DirectDuckDbTransformInput,
    )
    assert isinstance(
        validate_direct_data_transform_create_input(_python_payload()),
        DirectPythonArrowTransformInput,
    )


def test_create_rejects_missing_mode_instead_of_accepting_retired_input() -> None:
    old = _duckdb_payload()
    old.pop("inline_tables")
    old.pop("mode")
    with pytest.raises(ValidationError):
        validate_direct_data_transform_create_input(old)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["columns"].append(value["columns"][0]), "must be unique"),
        (lambda value: value["rows"][0]["values"].pop(), "same length"),
        (
            lambda value: value["rows"][0]["values"].__setitem__(
                1, {"kind": "string", "value": "wrong"}
            ),
            "must match",
        ),
        (
            lambda value: value["rows"][0]["values"].__setitem__(1, {"kind": "null"}),
            "nullable",
        ),
    ],
)
def test_inline_table_is_closed_and_matches_declared_columns(mutation, message: str) -> None:
    value = _inline_table()
    mutation(value)
    with pytest.raises(ValidationError, match=message):
        InlineTable.model_validate(value)


def test_inline_namespace_and_canonical_budget_are_enforced_for_both_engines() -> None:
    for payload in (_duckdb_payload(), _python_payload()):
        payload["inline_tables"][0]["input_name"] = "entities"
        model = (
            DirectDuckDbTransformInput
            if payload["execution"]["engine"] == "duckdb_sql"
            else DirectPythonArrowTransformInput
        )
        with pytest.raises(ValidationError, match="must not collide"):
            model.model_validate(payload)

    over_budget = _python_payload()
    over_budget["inline_tables"][0]["rows"][0]["values"][0]["value"] = "x" * 1_048_576
    with pytest.raises(ValidationError, match="1 MiB"):
        DirectPythonArrowTransformInput.model_validate(over_budget)


@pytest.mark.parametrize(
    "code",
    [
        "def transform(ctx)\n    pass",
        "def transform():\n    pass\n",
        "def transform(ctx, extra):\n    pass\n",
        "async def transform(ctx):\n    pass\n",
        "def other(ctx):\n    pass\n",
    ],
)
def test_python_source_reaches_backend_exactly_for_operation_backed_validation(
    code: str,
) -> None:
    payload = _python_payload()
    payload["execution"]["code"] = code
    model = DirectPythonArrowTransformInput.model_validate(payload)
    command = to_domain_python_arrow_transform_command(model)

    # Provider validation owns only the closed request shape and bounded exact
    # source text. Compilation and transform(ctx) entry-point validation happen
    # after an Operation exists, so their exact diagnostics and source remain
    # available to the repair workflow.
    assert model.execution.code == code
    assert command.execution.code == code


def test_json_schema_carrier_is_bounded_text_and_output_union_is_closed() -> None:
    assert JsonOutputDeclaration.model_validate(_json_output()).json_kind == "document"
    too_large = _json_output()
    too_large["schema_json"] = json.dumps({"action": "set", "value": {"description": "界" * 21_846}}, ensure_ascii=False)
    with pytest.raises(ValidationError, match="UTF-8"):
        JsonOutputDeclaration.model_validate(too_large)

    unknown = _json_output()
    unknown["arbitrary_value"] = {"open": ["object"]}
    with pytest.raises(ValidationError):
        JsonOutputDeclaration.model_validate(unknown)


@pytest.mark.parametrize("schema", [True, False, {"anyOf": [{"type": "object"}, {"type": "array"}]}])
def test_create_schema_set_keeps_full_data_dialect(schema: object) -> None:
    payload = _python_payload()
    payload["outputs"]["value"][1]["schema_json"] = json.dumps({"action": "set", "value": schema})
    command = to_domain_data_transform_create_command(validate_direct_data_transform_create_input(payload))
    assert json.loads(command.outputs[1].schema_json) == schema


def test_whole_output_set_rejects_schema_edit_and_retired_hash_input() -> None:
    from CAD2BIM_mcp.direct.transform.repair_inputs import validate_direct_repair_transform_input
    output = _json_output()
    output["schema_json"] = json.dumps({"action": "edit", "edits": [{"old_text": "integer", "new_text": "number"}]})
    base = {"mode": "repair", "direct_session_alias": "direct_001", "source": {"source_kind": "failed_operation", "operation_alias": "op_002"}}
    with pytest.raises(ValidationError, match="requires set"):
        validate_direct_repair_transform_input({**base, "outputs": {"action": "set", "value": [output]}})
    valid = {**base, "output_package": {"name": "Reviewed"}}
    assert validate_direct_repair_transform_input(valid).edits is None
    with pytest.raises(ValidationError):
        validate_direct_repair_transform_input({**valid, "expected_source_sha256": "0" * 64})


def test_python_worker_request_preserves_each_declared_output_kind_and_doc18_policy() -> None:
    command = to_domain_python_arrow_transform_command(
        DirectPythonArrowTransformInput.model_validate(_python_payload())
    )
    request = PythonArrowWorkerRequest.from_command(command)
    assert request.output_names == ("selected", "summary")
    assert [(item.payload_name, item.format, item.json_kind) for item in request.outputs] == [
        ("selected", "parquet", None),
        ("summary", "json", "document"),
    ]
    policy = PythonArrowWorkerPolicy()
    assert policy.total_call_seconds == 730
    assert policy.wall_seconds == 90
    assert policy.cpu_seconds == 180
    assert policy.rss_bytes == 4 * 1024**3
    assert policy.scratch_bytes == 4 * 1024**3
    assert policy.max_concurrent_workers == 1
    assert policy.capture_bytes == 16 * 1024


def test_provider_schema_has_complete_roots_closed_cells_and_no_unsupported_keywords() -> None:
    schema = TypeAdapter(DirectDataTransformCreateInput).json_schema()
    serialized = json.dumps(schema, sort_keys=True)
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
    assert '"additionalProperties": {}' not in serialized
    assert "Any" not in serialized

    assert len(schema["anyOf"]) == 3
    for root_name in ("DirectDuckDbTransformInput", "DirectPythonArrowTransformInput", "DirectIfcPythonArrowTransformInput"):
        root = schema["$defs"][root_name]
        assert root["additionalProperties"] is False
        assert set(root["properties"]) == {
            "mode",
            "direct_session_alias",
            "inputs",
            "inline_tables",
            "parameters",
            "execution",
            "outputs",
            "output_package",
        } | ({"ifc_source"} if root_name == "DirectIfcPythonArrowTransformInput" else set())
        assert all(
            property_schema.get("description") for property_schema in root["properties"].values()
        )

    inline_items = schema["$defs"]["InlineRow"]["properties"]["values"]["items"]
    cell_refs = {branch["$ref"].rsplit("/", 1)[-1] for branch in inline_items["anyOf"]}
    assert cell_refs == {
        "InlineStringValue",
        "InlineIntegerValue",
        "InlineNumberValue",
        "InlineBooleanValue",
        "InlineNullValue",
    }
    output_items = schema["$defs"]["TransformOutputsSet"]["properties"]["value"]["items"]
    output_refs = {branch["$ref"].rsplit("/", 1)[-1] for branch in output_items["oneOf"]}
    assert output_refs == {"ParquetOutputDeclaration", "JsonOutputInput"}


@pytest.mark.parametrize("package_count", [0, 2])
def test_derive_ifc_create_uses_same_domain_command_for_direct_and_runner(package_count) -> None:
    from CAD2BIM_mcp.runner.data_contracts import TransformInput

    payload = _python_payload()
    payload["ifc_source"] = "current"
    payload["inputs"] = [
        {"input_name": f"source_{i}", "package_alias": f"data_{i + 1:03d}", "payload_name": "items"}
        for i in range(package_count)
    ]
    direct = validate_direct_data_transform_create_input(payload)
    command = to_domain_data_transform_create_command(direct)
    runner_payload = {key: value for key, value in payload.items() if key != "direct_session_alias"}
    runner = TypeAdapter(TransformInput).validate_python(runner_payload)
    assert command.ifc_source == runner.ifc_source == "current"
    assert len(command.inputs) == len(runner.inputs) == package_count
    assert runner.model_dump(mode="json") == direct.model_dump(mode="json", exclude={"direct_session_alias"})


@pytest.mark.parametrize("engine,source", [("duckdb_sql", "current"), ("python_arrow", "historical"), ("python_arrow", None)])
def test_derive_rejects_sql_ifc_unknown_source_and_no_source(engine, source) -> None:
    from CAD2BIM_mcp.runner.data_contracts import TransformInput

    payload = _python_payload() if engine == "python_arrow" else _duckdb_payload()
    payload["inputs"] = []
    if source is not None:
        payload["ifc_source"] = source
    with pytest.raises(ValidationError):
        validate_direct_data_transform_create_input(payload)
    with pytest.raises(ValidationError):
        TypeAdapter(TransformInput).validate_python({key: value for key, value in payload.items() if key != "direct_session_alias"})
