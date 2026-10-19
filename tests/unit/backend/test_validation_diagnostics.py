from __future__ import annotations

import math
from typing import Literal

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from CAD2BIM_backend.application.validation_support import (
    json_schema_validation_message as application_json_message,
    validation_error_message as application_pydantic_message,
)
from CAD2BIM_backend.domain.geometry_runtime import validate_geometry_function_inputs
from CAD2BIM_backend.domain.validation_diagnostics import (
    json_schema_validation_message,
    select_json_schema_error,
    validation_error_message,
)


def test_application_preserves_the_same_neutral_diagnostic_implementation() -> None:
    assert application_json_message is json_schema_validation_message
    assert application_pydantic_message is validation_error_message


@pytest.mark.parametrize("key,expected", [("replacements", "replacements"), ("secret/value", "<field>"), ("x" * 65, "<field>")])
def test_closed_object_extra_field_diagnostic_is_safe_and_specific(key, expected):
    schema = {"type":"object", "properties":{"edits":{"type":"array", "items":{
        "type":"object", "properties":{"source_path":{"type":"string"}}, "additionalProperties":False}}}}
    error = next(Draft202012Validator(schema).iter_errors({"edits":[{"source_path":"main.py", key:"PRIVATE_VALUE"}]}))
    message = json_schema_validation_message("invalid", error)
    assert f"invalid_path=edits.0.{expected}; error_type=extra_forbidden" in message
    assert "PRIVATE_VALUE" not in message and "secret/value" not in message


def test_pattern_properties_extra_diagnostic_preserves_conservative_path():
    schema = {"type":"object", "properties":{}, "patternProperties":{"^a":{"type":"string"}}, "additionalProperties":False}
    error = next(Draft202012Validator(schema).iter_errors({"extra":"PRIVATE_VALUE"}))
    assert "invalid_path=<root>; error_type=extra_forbidden" in json_schema_validation_message("invalid", error)


@pytest.mark.parametrize("value", [
    {"mode": "private_unknown_mode", "page_size": "PRIVATE_VALUE"},
    {"mode": "fresh", "query": {"page_size": "PRIVATE_VALUE"}},
    {"mode": "repair", "page_size": "PRIVATE_VALUE"},
    {"query": {}, "cursor": "PRIVATE_CURSOR", "page_size": "PRIVATE_VALUE"},
])
def test_query_pagination_correction_does_not_guess_other_errors_or_echo_values(value):
    schema = {"type": "object", "additionalProperties": False}
    error = next(Draft202012Validator(schema).iter_errors(value))
    original = json_schema_validation_message("Invalid", error)
    projected = json_schema_validation_message("Invalid", error,
        query_pagination=True, input_value=value)
    assert projected == original
    assert "PRIVATE" not in projected and "private_unknown_mode" not in projected


def test_query_pagination_correction_keeps_the_existing_utf8_budget_and_opt_in():
    value = {"mode": "fresh", "query": {}, "page_size": "PRIVATE_VALUE"}
    schema = {"type": "object", "additionalProperties": False}
    error = next(Draft202012Validator(schema).iter_errors(value))
    assert "correction=" not in json_schema_validation_message("Invalid", error, input_value=value)
    message = json_schema_validation_message("Invalid", error,
        query_pagination=True, input_value=value)
    assert "query.page_size" in message and "invalid_path=" in message
    assert "PRIVATE_VALUE" not in message
    assert len(json_schema_validation_message("界" * 400, error,
        query_pagination=True, input_value=value).encode()) <= 1_024


@pytest.mark.parametrize("first,second", [("query", "cursor"), ("document", "continuation")])
def test_untagged_closed_union_lists_each_branch_field_paths(first: str, second: str) -> None:
    schema = {"anyOf": [
        {"type": "object", "properties": {field: {"type": "object"}},
         "required": [field], "additionalProperties": False}
        for field in (first, second)
    ]}
    errors = tuple(Draft202012Validator(schema).iter_errors({
        "payload": "PRIVATE_VALUE", "select": ["PRIVATE_SOURCE"], "secret/path": "PRIVATE_PATH",
    }))
    error = select_json_schema_error(errors, schema=schema)
    assert error is errors[0]
    message = json_schema_validation_message("Invalid input", error)
    assert "invalid_path=<root>; error_type=union_validation" in message
    assert f"0: missing={first}; extra=payload,select,<field>" in message
    assert f"1: missing={second}; extra=payload,select,<field>" in message
    assert not any(value in message for value in ("PRIVATE_VALUE", "PRIVATE_SOURCE", "PRIVATE_PATH", "secret/path"))
    assert len(message.encode()) <= 1_024
    assert Draft202012Validator(schema).is_valid({first: {}})


def test_required_diagnostic_locates_missing_nested_field_and_union_paths() -> None:
    schema = {"type": "object", "properties": {"request": {"anyOf": [
        {"type": "object", "properties": {"query": {"type": "object", "properties": {
            "select": {"type": "array"}}, "required": ["select"], "additionalProperties": False}},
         "required": ["query"], "additionalProperties": False},
        {"type": "object", "properties": {"cursor": {"type": "string"}},
         "required": ["cursor"], "additionalProperties": False},
    ]}}}
    error = next(Draft202012Validator(schema).iter_errors({"request": {"query": {}}}))
    message = json_schema_validation_message("Invalid", error)
    assert "0: missing=request.query.select" in message
    assert "1: missing=request.cursor; extra=request.query" in message
    missing = error.context[0]
    assert "invalid_path=request.query.select; error_type=missing" in json_schema_validation_message("Invalid", missing)


@pytest.mark.parametrize(
    ("definition", "sample", "error_type", "constraint"),
    [
        ({"type": "number", "maximum": 1}, math.nextafter(1.0, math.inf), "less_than_equal", "maximum=1"),
        ({"type": "number", "minimum": 0}, -1, "greater_than_equal", "minimum=0"),
        ({"type": "number", "exclusiveMaximum": 1}, 1, "less_than", "exclusive_maximum=1"),
        ({"type": "string", "maxLength": 2}, "private_sample_value", "string_too_long", "max_length_characters=2"),
    ],
)
def test_geometry_sample_validation_retains_bounded_constraint_without_input(
    definition: dict, sample: object, error_type: str, constraint: str
) -> None:
    schema = {
        "type": "object", "properties": {"parameter": definition},
        "required": ["parameter"], "additionalProperties": False,
    }
    with pytest.raises(ValueError) as caught:
        validate_geometry_function_inputs(schema, ({"parameter": sample},))
    message = str(caught.value)
    assert "invalid_path=parameter" in message
    assert f"error_type={error_type}" in message
    assert f"constraint={constraint}" in message
    assert "private_sample_value" not in message
    assert "1.0000000000000002" not in message
    assert "input_value" not in message
    assert len(message.encode("utf-8")) <= 1_024


def test_geometry_sample_validation_keeps_exact_boundary_and_rejects_unknown_input() -> None:
    schema = {
        "type": "object", "properties": {"parameter": {"type": "number", "maximum": 1}},
        "required": ["parameter"], "additionalProperties": False,
    }
    validate_geometry_function_inputs(schema, ({"parameter": 1},))
    with pytest.raises(ValueError, match="extra_forbidden") as caught:
        validate_geometry_function_inputs(schema, ({"parameter": 1, "private_extra": "secret"},))
    assert "invalid_path=private_extra; error_type=extra_forbidden" in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("tags", [{}, {"kind": "private_wrong_tag"}, {"kind": "same"}])
def test_union_diagnostics_do_not_guess_missing_invalid_or_ambiguous_tags(tags: dict) -> None:
    branches = [
        {"type": "object", "properties": {"kind": {"const": "same"}, "value": {"maximum": n}},
         "required": ["kind", "value"]}
        for n in (1, 2)
    ]
    schema = {"oneOf": branches}
    errors = tuple(Draft202012Validator(schema).iter_errors({**tags, "value": 5}))
    error = select_json_schema_error(errors, schema=schema)
    assert error is errors[0]
    message = json_schema_validation_message("Invalid input", error)
    assert "invalid_path=<root>; error_type=union_validation" in message
    assert "private_wrong_tag" not in message


def test_union_diagnostics_keep_oneof_ambiguity_and_untagged_alternatives() -> None:
    for schema, value in [
        ({"oneOf": [{"const": 1}, {"type": "number"}]}, 1),
        ({"anyOf": [{"type": "number"}, {"type": "array"}]}, "private_value"),
    ]:
        errors = tuple(Draft202012Validator(schema).iter_errors(value))
        assert select_json_schema_error(errors, schema=schema) is errors[0]
        message = json_schema_validation_message("Invalid", errors[0])
        assert "error_type=union_validation" in message
        assert "branches=" not in message
        assert "private_value" not in message


class _MappedBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["mapped"]
    size: int


class _FunctionBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["function"]
    count: int


def test_pydantic_diagnostic_uses_unique_required_literal_and_hides_input() -> None:
    adapter = TypeAdapter(_MappedBranch | _FunctionBranch)
    value = {"kind": "function", "count": "private_value"}
    with pytest.raises(ValidationError) as caught:
        adapter.validate_python(value)
    message = validation_error_message("Invalid", caught.value,
        input_schema=adapter.json_schema(), input_value=value)
    assert "invalid_path=_FunctionBranch.count; error_type=int_parsing" in message
    assert "private_value" not in message and "input_value" not in message


@pytest.mark.parametrize("tag", [{}, {"kind": "private_invalid_tag"}])
def test_pydantic_diagnostic_does_not_guess_missing_or_invalid_tag(tag: dict) -> None:
    adapter = TypeAdapter(_MappedBranch | _FunctionBranch)
    value = {**tag, "count": "private_value"}
    with pytest.raises(ValidationError) as caught:
        adapter.validate_python(value)
    assert validation_error_message("Invalid", caught.value, input_schema=adapter.json_schema(), input_value=value) == validation_error_message("Invalid", caught.value)


def test_pydantic_diagnostic_does_not_select_an_ambiguous_or_untagged_schema() -> None:
    adapter = TypeAdapter(_MappedBranch | _FunctionBranch)
    value = {"kind": "function", "count": "private_value"}
    with pytest.raises(ValidationError) as caught:
        adapter.validate_python(value)
    for ambiguous in (True, False):
        schema = adapter.json_schema()
        mapped = schema["$defs"]["_MappedBranch"]
        if ambiguous:
            mapped["properties"]["kind"]["const"] = "function"
        else:
            mapped["required"].remove("kind")
        assert validation_error_message("Invalid", caught.value, input_schema=schema, input_value=value) == validation_error_message("Invalid", caught.value)
