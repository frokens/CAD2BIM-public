"""Entry-neutral bounded diagnostics for structured validation failures."""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012


_MAX_VALIDATION_PATH_SEGMENTS = 12
_MAX_VALIDATION_MESSAGE_BYTES = 1_024
_MAX_VALIDATION_REASON_BYTES = 512


def _safe_validation_path_item(item: object) -> str:
    if isinstance(item, int):
        return str(item) if 0 <= item <= 999_999_999 else "<item>"
    if not isinstance(item, str) or not 1 <= len(item) <= 64:
        return "<field>"
    first, remainder = item[0], item[1:]
    if not (first == "_" or first.isascii() and first.isalpha()):
        return "<field>"
    if not all(
        character == "_" or character.isascii() and character.isalnum() for character in remainder
    ):
        return "<field>"
    return item


def _bounded_validation_message(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_VALIDATION_MESSAGE_BYTES:
        return value
    return encoded[:_MAX_VALIDATION_MESSAGE_BYTES].decode("utf-8", errors="ignore")


def _safe_value_error_reason(error: object) -> str | None:
    """Project one authored invariant without returning inputs or Pydantic internals."""

    if not isinstance(error, str) or not error.startswith("Value error, "):
        return None
    reason = " ".join(error.removeprefix("Value error, ").split())
    if not reason or any(
        marker in reason for marker in ("http://", "https://", "errors.pydantic.dev", "input_value")
    ):
        return None
    encoded = reason.encode("utf-8")
    if len(encoded) > _MAX_VALIDATION_REASON_BYTES:
        reason = encoded[:_MAX_VALIDATION_REASON_BYTES].decode("utf-8", errors="ignore")
    return reason


def _safe_json_schema_constraint(error: JsonSchemaValidationError) -> str | None:
    labels = {
        "exclusiveMaximum": "exclusive_maximum",
        "exclusiveMinimum": "exclusive_minimum",
        "maxItems": "max_items",
        "maxLength": "max_length_characters",
        "maximum": "maximum",
        "minItems": "min_items",
        "minLength": "min_length_characters",
        "minimum": "minimum",
    }
    label = labels.get(error.validator)
    value = error.validator_value
    if label is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    rendered = str(value)
    if len(rendered) > 64:
        return None
    return f"{label}={rendered}"


def select_json_schema_error(
    errors: Iterable[JsonSchemaValidationError],
    *,
    schema: dict | None = None,
) -> JsonSchemaValidationError | None:
    """Select a failure in an unambiguously tagged branch of the original tree.

    This only selects diagnostics: the complete schema validator still decides
    validity. Without a unique match of required literal properties, retain the
    union failure rather than guess a branch from its number of errors.
    """

    resolver = (
        Registry().resolver_with_root(Resource.from_contents(schema, default_specification=DRAFT202012))
        if schema is not None else None
    )
    error = next(iter(errors), None)
    while error is not None and error.validator in {"anyOf", "oneOf"}:
        if not isinstance(error.instance, dict):
            break
        matched = _matching_literal_branch(error.validator_value, error.instance, resolver)
        if matched is None:
            break
        selected = next(
            (child for child in error.context
             if child.schema_path and child.schema_path[0] == matched[0]),
            None,
        )
        if selected is None:
            break
        error = selected
    return error


def _matching_literal_branch(branches: Iterable, instance: dict, resolver) -> tuple[int, dict] | None:
    """Share exact required-literal selection; never rank branches by error count."""

    matches = []
    for index, branch in enumerate(branches):
        branch_resolver = resolver
        seen_refs = set()
        while isinstance(branch, dict) and "$ref" in branch:
            reference = branch["$ref"]
            if branch_resolver is None or reference in seen_refs:
                return None
            seen_refs.add(reference)
            try:
                resolved = branch_resolver.lookup(reference)
            except Unresolvable:
                return None
            branch, branch_resolver = resolved.contents, resolved.resolver
        if not isinstance(branch, dict):
            return None
        tags = _required_literal_constraints(branch, branch_resolver)
        if not tags:
            return None
        if all(_matches_literal_path(instance, path, literal) for path, literal in tags.items()):
            matches.append((index, branch))
    return matches[0] if len(matches) == 1 else None


def _required_literal_constraints(schema: dict, resolver, depth: int = 0) -> dict:
    if depth >= _MAX_VALIDATION_PATH_SEGMENTS:
        return {}
    tags = {}
    properties = schema.get("properties", {})
    for key in schema.get("required", ()):
        child = properties.get(key)
        if not isinstance(child, dict):
            continue
        child_resolver = resolver
        if "$ref" in child:
            if resolver is None:
                continue
            try:
                resolved = resolver.lookup(child["$ref"])
            except Unresolvable:
                continue
            child, child_resolver = resolved.contents, resolved.resolver
        if not isinstance(child, dict):
            continue
        if "const" in child:
            tags[(key,)] = child["const"]
        elif (
            isinstance(child.get("oneOf"), list)
            and child["oneOf"]
            and all(isinstance(choice, dict) and "const" in choice for choice in child["oneOf"])
        ):
            # A closed literal family is still an exact tag. This occurs when
            # Provider projection combines several equivalent input branches.
            tags[(key,)] = tuple(choice["const"] for choice in child["oneOf"])
        tags.update({
            (key, *path): literal
            for path, literal in _required_literal_constraints(child, child_resolver, depth + 1).items()
        })
    return tags


def _matches_literal_path(instance: dict, path: tuple, literal: object) -> bool:
    from jsonschema import Draft202012Validator

    value = instance
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    if isinstance(literal, tuple):
        return any(Draft202012Validator({"const": item}).is_valid(value) for item in literal)
    return Draft202012Validator({"const": literal}).is_valid(value)


def _json_schema_field_paths(exc: JsonSchemaValidationError) -> tuple[tuple, ...]:
    path = tuple(exc.absolute_path)
    if exc.validator == "required" and isinstance(exc.instance, dict):
        return tuple((*path, key) for key in exc.validator_value if key not in exc.instance)
    if (exc.validator == "additionalProperties" and exc.validator_value is False
        and isinstance(exc.schema, dict) and isinstance(exc.schema.get("properties"), dict)
        and "patternProperties" not in exc.schema and isinstance(exc.instance, dict)):
        return tuple((*path, key) for key in exc.instance if key not in exc.schema["properties"])
    return ()


def _json_schema_path(path: tuple) -> str:
    location_items = [
        _safe_validation_path_item(item)
        for item in path[:_MAX_VALIDATION_PATH_SEGMENTS]
    ]
    if len(path) > _MAX_VALIDATION_PATH_SEGMENTS:
        location_items.append("<truncated>")
    return ".".join(location_items) or "<root>"


def _json_schema_union_fields(exc: JsonSchemaValidationError) -> str:
    branches: dict[int, dict[str, list[str]]] = {}
    for child in exc.context:
        if not child.schema_path or not isinstance(child.schema_path[0], int):
            continue
        branch = branches.setdefault(child.schema_path[0], {})
        pending = [child]
        while pending:
            error = pending.pop()
            pending.extend(reversed(error.context))
            label = {"required": "missing", "additionalProperties": "extra"}.get(error.validator)
            if label is None:
                continue
            for path in _json_schema_field_paths(error):
                location = _json_schema_path(path)
                paths = branch.setdefault(label, [])
                if location not in paths:
                    paths.append(location)
    summaries = [
        f"{index}: " + "; ".join(
            f"{label}={','.join(fields[label])}" for label in ("missing", "extra") if label in fields
        )
        for index, fields in branches.items() if fields
    ]
    return "; branches=[" + " | ".join(summaries) + "]" if summaries else ""


def _query_page_size_correction(value: object) -> str | None:
    """Explain the current Data Query envelopes without accepting misplaced fields."""
    if not isinstance(value, dict):
        return None
    mode = value.get("mode")
    if mode is None:
        if "query" in value and "cursor" not in value and "parent_attempt_alias" not in value:
            mode = "fresh"
        elif "cursor" in value and "query" not in value:
            mode = "cursor"
    query = value.get("query")
    nested_page_size = isinstance(query, dict) and "page_size" in query
    if mode == "fresh" and "page_size" in value:
        return "mode=fresh: put page_size inside query as query.page_size"
    if mode == "repair" and nested_page_size:
        return "mode=repair: put page_size at the request root; repair does not accept query"
    if mode == "cursor" and ("page_size" in value or nested_page_size):
        return "mode=cursor: use the unchanged cursor; omit page_size and query"
    return None


def json_schema_validation_message(
    message: str, exc: JsonSchemaValidationError, *,
    query_pagination: bool = False, input_value: object = None,
) -> str:
    """Project bounded field paths from official errors without rendering inputs."""

    correction = _query_page_size_correction(input_value) if query_pagination else None
    if correction is not None:
        message = f"{message}; correction={correction}"
    fields = _json_schema_field_paths(exc)
    path = fields[0] if fields else tuple(exc.absolute_path)
    error_types = {
        "additionalProperties": "extra_forbidden",
        "anyOf": "union_validation",
        "const": "literal_error",
        "enum": "enum_error",
        "exclusiveMaximum": "less_than",
        "exclusiveMinimum": "greater_than",
        "maxItems": "too_long",
        "maxLength": "string_too_long",
        "maximum": "less_than_equal",
        "minItems": "too_short",
        "minLength": "string_too_short",
        "minimum": "greater_than_equal",
        "oneOf": "union_validation",
        "pattern": "string_pattern_mismatch",
        "required": "missing",
        "type": "json_type_mismatch",
    }
    error_type = error_types.get(exc.validator, "schema_validation")
    if exc.validator == "type":
        expected_types = (
            {exc.validator_value}
            if isinstance(exc.validator_value, str)
            else {item for item in exc.validator_value if isinstance(item, str) and item != "null"}
            if isinstance(exc.validator_value, list)
            else set()
        )
        type_errors = {
            "array": "list_type",
            "boolean": "bool_type",
            "integer": "int_type",
            "number": "float_type",
            "object": "dict_type",
            "string": "string_type",
        }
        if len(expected_types) == 1:
            error_type = type_errors.get(next(iter(expected_types)), error_type)
    constraint = _safe_json_schema_constraint(exc)
    constraint_suffix = f"; constraint={constraint}" if constraint is not None else ""
    branch_suffix = _json_schema_union_fields(exc) if exc.validator in {"anyOf", "oneOf"} else ""
    return _bounded_validation_message(
        f"{message}; invalid_path={_json_schema_path(path)}; error_type={error_type}{constraint_suffix}{branch_suffix}"
    )


def validation_error_message(
    message: str,
    exc: ValidationError,
    *,
    include_value_error_reason: bool = False,
    allowed_value_error_reasons: Collection[str] | None = None,
    input_schema: dict | None = None,
    input_value: object = None,
) -> str:
    """Return one bounded validation path and an explicitly permitted reason.

    ``allowed_value_error_reasons=None`` preserves the existing Direct Provider
    behavior when ``include_value_error_reason`` is true. Application consumers
    that persist or publicly project output failures should pass an exact
    allowlist. An input adapter may supply its schema and value to select a
    uniquely tagged model branch. The value is used only for selection and is
    never rendered; full Pydantic validation remains authoritative.
    """

    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    if not errors:
        return _bounded_validation_message(message)
    first = errors[0]
    if input_schema is not None and isinstance(input_value, dict):
        branches = input_schema.get("anyOf", input_schema.get("oneOf", ()))
        resolver = Registry().resolver_with_root(
            Resource.from_contents(input_schema, default_specification=DRAFT202012)
        )
        matched = _matching_literal_branch(branches, input_value, resolver)
        if matched is not None:
            title = matched[1].get("title")
            # Pydantic labels untagged model-union errors with the model name.
            # Custom titles or other schemas do not justify guessing that label.
            first = next((error for error in errors if error.get("loc", ())[:1] == (title,)), first)
    raw_location = first.get("loc", ())
    location_items = [
        _safe_validation_path_item(item) for item in raw_location[:_MAX_VALIDATION_PATH_SEGMENTS]
    ]
    if len(raw_location) > _MAX_VALIDATION_PATH_SEGMENTS:
        location_items.append("<truncated>")
    location = ".".join(location_items)
    raw_error_type = first.get("type", "validation_error")
    error_type = _safe_validation_path_item(raw_error_type)
    if error_type == "<field>":
        error_type = "validation_error"
    reason = (
        _safe_value_error_reason(first.get("msg"))
        if include_value_error_reason and error_type == "value_error"
        else None
    )
    if (
        reason is not None
        and allowed_value_error_reasons is not None
        and reason not in allowed_value_error_reasons
    ):
        reason = None
    suffix = f"; reason={reason}" if reason is not None else ""
    if not location:
        return _bounded_validation_message(f"{message}; error_type={error_type}{suffix}")
    return _bounded_validation_message(
        f"{message}; invalid_path={location}; error_type={error_type}{suffix}"
    )


__all__ = ["json_schema_validation_message", "select_json_schema_error", "validation_error_message"]
