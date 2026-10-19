"""Provider schema projections that FastMCP cannot infer exactly."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from fastmcp.utilities.json_schema import dereference_refs
from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode


def provider_input_array_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Express fixed homogeneous tuples with items, without changing their domain.

    Some clients omit prefixItems and synthesize string items. Length bounds plus
    one identical item schema express these vectors/matrices without that loss.
    Visit schema positions only; defaults and examples are user data.
    """
    result = dict(schema)
    for keyword in ("properties", "$defs", "patternProperties"):
        if keyword in result:
            result[keyword] = {key: provider_input_array_schema(value)
                               for key, value in result[keyword].items()}
    for keyword in ("items", "additionalProperties", "propertyNames"):
        if isinstance(result.get(keyword), dict):
            result[keyword] = provider_input_array_schema(result[keyword])
    for keyword in ("anyOf", "oneOf", "prefixItems"):
        if keyword in result:
            result[keyword] = [provider_input_array_schema(value) if isinstance(value, dict) else value
                               for value in result[keyword]]
    prefix = result.get("prefixItems")
    if prefix:
        if (result.get("maxItems") != len(prefix)
                or not all(item == prefix[0] for item in prefix)
                or result.get("items", False) is not False):
            raise ValueError("Provider input tuples require a fixed homogeneous item schema")
        result["items"] = prefix[0]
        del result["prefixItems"]
    return result


class _SchemaWithoutGeneratedTitles(GenerateJsonSchema):
    """Omit inferred field/model labels; keep explicit metadata and enum labels."""

    def field_title_should_be_set(self, schema) -> bool:
        return False

    def model_schema(self, schema):
        result = super().model_schema(schema)
        config = schema["cls"].model_config
        if not any(config.get(key) is not None for key in (
            "title", "model_title_generator", "json_schema_extra",
        )):
            result.pop("title", None)
        return result


def provider_reference_schema(
    type_: Any, *, mode: JsonSchemaMode = "validation",
) -> dict[str, Any]:
    """Generate a static reference from its DTO without inferred title labels.

    Retain the requested validation/serialization semantics, local definitions,
    references and root type. Resource owners compose this Schema with their
    prose and examples; no input or output structural projection is applied.
    """
    return TypeAdapter(type_).json_schema(
        mode=mode, schema_generator=_SchemaWithoutGeneratedTitles,
    )


def share_provider_reference_types(
    schemas: list[dict], *, common_uri: str,
    preview_uri: str | None = None, primary_indexes: set[int] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Factor identical named DTO definitions across actual static Schema roots.

    The complete reachable definition graph participates in identity, including
    annotations and constraints. Names preserve business type identity; cycles
    remain references. This projection never visits instance-valued annotations.
    """
    def children(node):
        for key in ("properties", "$defs", "patternProperties", "dependentSchemas"):
            yield from node.get(key, {}).values()
        for key in ("items", "additionalProperties", "propertyNames", "contains",
                    "additionalItems", "unevaluatedProperties", "unevaluatedItems",
                    "not", "if", "then", "else"):
            if isinstance(node.get(key), dict):
                yield node[key]
        for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
            yield from node.get(key, ())

    def references(node):
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            yield node["$ref"]
        yield from node.get("discriminator", {}).get("mapping", {}).values()
        for child in children(node):
            yield from references(child)

    def definition_name(ref, definitions):
        prefix = "#/$defs/"
        if not isinstance(ref, str) or not ref.startswith(prefix):
            raise ValueError("Static contract sharing requires local definition references")
        name = ref[len(prefix):].replace("~1", "/").replace("~0", "~")
        if name not in definitions:
            raise ValueError("Static contract definition reference is unresolved")
        return name

    def pointer(name):
        return "#/$defs/" + name.replace("~", "~0").replace("/", "~1")

    groups = {}
    for index, schema in enumerate(schemas):
        definitions = schema.get("$defs", {})
        for ref in references(schema):
            definition_name(ref, definitions)
        for name, definition in definitions.items():
            reachable, pending = {}, [name]
            while pending:
                dependency = pending.pop()
                if dependency in reachable:
                    continue
                reachable[dependency] = definitions[dependency]
                pending.extend(definition_name(ref, definitions)
                               for ref in references(definitions[dependency]))
            identity = json.dumps(reachable, sort_keys=True, ensure_ascii=False,
                                  allow_nan=False, separators=(",", ":"))
            groups.setdefault((name, identity), []).append(index)

    shared = {key: indexes for key, indexes in groups.items() if len(indexes) > 1}
    targets, owners = {}, {}
    for (name, identity), indexes in sorted(shared.items()):
        collisions = sum(other_name == name for other_name, _ in shared)
        shared_name = name if collisions == 1 else name + "_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        if shared_name in owners:
            raise ValueError("Shared static definition name collision")
        owners[shared_name] = (indexes[0], name)
        for index in indexes:
            targets[index, name] = shared_name

    # Primary (authoring/program) contracts must not pull in unrelated Preview
    # cases, rendering or result types. Every shared definition has one owner.
    definition_uris = {
        target: (common_uri if preview_uri is None or primary_indexes is None
                 or any(index in primary_indexes for index, name in targets
                        if targets[index, name] == target) else preview_uri)
        for target in owners
    }

    def rewrite(node, index, *, owner_uri=None):
        result = deepcopy(node)
        if not isinstance(result, dict):
            return result
        definitions = schemas[index].get("$defs", {})
        def reference(ref):
            name = definition_name(ref, definitions)
            target = targets.get((index, name))
            if owner_uri and target is None:
                raise ValueError("Shared static definition dependency is not shared")
            if not target:
                return ref
            uri = definition_uris[target]
            if owner_uri == common_uri and uri != common_uri:
                raise ValueError("Primary static types must be transitively closed")
            return ("" if owner_uri == uri else uri) + pointer(target)
        if "$ref" in result:
            result["$ref"] = reference(result["$ref"])
        if "discriminator" in result and "mapping" in result["discriminator"]:
            result["discriminator"]["mapping"] = {
                key: reference(ref) for key, ref in result["discriminator"]["mapping"].items()
            }
        for key in ("properties", "$defs", "patternProperties", "dependentSchemas"):
            if key in result:
                result[key] = {name: rewrite(child, index, owner_uri=owner_uri)
                               for name, child in result[key].items()}
        for key in ("items", "additionalProperties", "propertyNames", "contains",
                    "additionalItems", "unevaluatedProperties", "unevaluatedItems",
                    "not", "if", "then", "else"):
            if isinstance(result.get(key), dict):
                result[key] = rewrite(result[key], index, owner_uri=owner_uri)
        for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
            if key in result:
                result[key] = [rewrite(child, index, owner_uri=owner_uri) for child in result[key]]
        return result

    documents = {
        uri: {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": uri,
              "$defs": {name: rewrite(schemas[index]["$defs"][original], index, owner_uri=uri)
                        for name, (index, original) in owners.items() if definition_uris[name] == uri}}
        for uri in dict.fromkeys((common_uri, *definition_uris.values()))
    }
    projected = []
    for index, schema in enumerate(schemas):
        result = rewrite(schema, index)
        if "$defs" in result:
            result["$defs"] = {name: value for name, value in result["$defs"].items()
                               if (index, name) not in targets}
            if not result["$defs"]:
                del result["$defs"]
        projected.append(result)
    return projected, documents


def _input_schema_children(schema):
    """Yield only JSON Schema locations, never instance-valued annotations."""
    for keyword in ("properties", "$defs", "patternProperties"):
        for key, child in schema.get(keyword, {}).items():
            if isinstance(child, dict):
                yield keyword, key, child
    for keyword in ("items", "additionalProperties", "propertyNames"):
        if isinstance(schema.get(keyword), dict):
            yield keyword, None, schema[keyword]
    for keyword in ("anyOf", "oneOf", "prefixItems"):
        for index, child in enumerate(schema.get(keyword, ())):
            if isinstance(child, dict):
                yield keyword, index, child


def _input_refs_can_expand(schema):
    """Conservatively bound the existing FastMCP dereferencer's input domain."""
    definitions = schema.get("$defs", {})

    def data_has_ref(value):
        if isinstance(value, dict):
            return "$ref" in value or any(data_has_ref(item) for item in value.values())
        return isinstance(value, list) and any(data_has_ref(item) for item in value)

    def visit(node, active=()):
        if any(key in node for key in ("$id", "$anchor", "$dynamicRef", "$dynamicAnchor")):
            return False
        supported_containers = {
            "properties", "$defs", "patternProperties", "items", "additionalProperties", "propertyNames",
            "anyOf", "oneOf", "prefixItems", "required", "type", "discriminator",
            "default", "examples", "enum", "const",
        }
        # FastMCP/jsonref also walks keywords outside our supported Schema
        # locations. Do not pass those containers to it at all: e.g. contains
        # may hold a const object whose literal $ref must never be resolved.
        if any(isinstance(value, (dict, list)) and key not in supported_containers
               for key, value in node.items()):
            return False
        if any(isinstance(node.get(key), list) for key in ("items", "additionalProperties", "propertyNames")):
            return False
        discriminator = node.get("discriminator")
        if discriminator is not None:
            if not isinstance(discriminator, dict) or set(discriminator) - {"propertyName", "mapping"}:
                return False
            if not isinstance(discriminator.get("propertyName"), str):
                return False
            mapping = discriminator.get("mapping", {})
            if not isinstance(mapping, dict) or any(
                not isinstance(value, str) or value not in {f"#/$defs/{name}" for name in definitions}
                for value in mapping.values()
            ):
                return False
        # jsonref traverses instance annotations too. Such documents must remain
        # untouched rather than interpreting their literal $ref values as Schema.
        if any(data_has_ref(node[key]) for key in ("default", "examples", "enum", "const") if key in node):
            return False
        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                return False
            name = ref.removeprefix("#/$defs/")
            if "/" in name or "~" in name or name not in definitions or ref in active:
                return False
            target = definitions[name]
            if not isinstance(target, dict):
                return False
            # FastMCP merges siblings by replacement. Only annotations and an
            # already-identical type assertion are safe under that operation.
            annotations = {"$ref", "$defs", "title", "description", "default", "examples", "$comment", "deprecated", "readOnly", "writeOnly"}
            if set(node) - annotations - {"type"}:
                return False
            if "type" in node and node["type"] != target.get("type"):
                return False
            if not visit(target, (*active, ref)):
                return False
        return all(visit(child, active) for _, _, child in _input_schema_children(node))

    return visit(schema)


def share_provider_input_nodes(schema: dict[str, Any]) -> dict[str, Any]:
    """Reuse complete identical nodes in the final, already-expanded tool input.

    Existing reference/scoping dialects remain untouched. Generated definitions
    may refer to other local definitions; each accepted step strictly reduces the
    complete compact JSON size, including all definition and reference overhead.
    No annotations or instance data are traversed as Schema.
    """
    containers = {
        "properties", "$defs", "patternProperties", "items", "additionalProperties", "propertyNames",
        "anyOf", "oneOf", "prefixItems", "required", "type", "default", "examples", "enum", "const",
    }

    def supported(node):
        if any(key in node for key in (
            "$ref", "$defs", "$id", "id", "$schema", "$anchor", "$dynamicRef", "$dynamicAnchor",
            "$recursiveRef", "$recursiveAnchor", "discriminator",
        )):
            return False
        if any(isinstance(value, (dict, list)) and key not in containers for key, value in node.items()):
            return False
        if any(isinstance(node.get(key), list) for key in ("items", "additionalProperties", "propertyNames")):
            return False
        return all(supported(child) for _, _, child in _input_schema_children(node))

    current = deepcopy(schema)
    if not supported(current):
        return current

    def encoded(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))

    current_size = len(encoded(current).encode("utf-8"))
    while True:
        groups, identities = {}, {}

        def collect(node, *, root=False):
            identity = encoded(node)
            identities[id(node)] = identity
            if not root and "$ref" not in node:
                if identity not in groups:
                    groups[identity] = [node, 0, len(identity.encode("utf-8"))]
                groups[identity][1] += 1
            for _, _, child in _input_schema_children(node):
                collect(child)

        collect(current, root=True)
        name = f"Shared{len(current.get('$defs', {})) + 1}"
        reference = {"$ref": f"#/$defs/{name}"}
        reference_size = len(encoded(reference).encode("utf-8"))
        definition_overhead = len(encoded(name)) + 1 + (1 if "$defs" in current else len(',"$defs":{}'))
        candidates = sorted(
            ((count - 1) * size - count * reference_size - definition_overhead, identity)
            for identity, (_, count, size) in groups.items() if count > 1
        )
        selected = None
        for saving, identity in reversed(candidates):
            if saving <= 0:
                break

            def replace(node, *, root=False):
                if not root and identities[id(node)] == identity:
                    return dict(reference)
                result = dict(node)
                for keyword, key, child in _input_schema_children(node):
                    value = replace(child)
                    if key is None:
                        result[keyword] = value
                    else:
                        if result[keyword] is node[keyword]:
                            result[keyword] = node[keyword].copy()
                        result[keyword][key] = value
                return result

            candidate = replace(current, root=True)
            candidate.setdefault("$defs", {})[name] = deepcopy(groups[identity][0])
            size = len(encoded(candidate).encode("utf-8"))
            if size < current_size:
                selected = candidate, size
                break
        if selected is None:
            return current
        current, current_size = selected


def _merge_closed_required_variants(left, right, union, *, differing_key=None):
    """Factor one required field, preserving even overlapping oneOf branches."""
    allowed = {
        "type", "additionalProperties", "properties", "required", "title",
        "description", "default", "examples", "$comment", "deprecated", "readOnly", "writeOnly",
    }
    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    if set(left) - allowed or set(right) - allowed:
        return None
    if left.get("type") != "object" or left.get("additionalProperties") is not False:
        return None
    if {k: v for k, v in left.items() if k != "properties"} != {
        k: v for k, v in right.items() if k != "properties"
    }:
        return None
    first, second = left.get("properties", {}), right.get("properties", {})
    if first.keys() != second.keys():
        return None
    different = [key for key in first if first[key] != second[key]]
    if differing_key is None:
        if len(different) != 1:
            return None
        key = different[0]
    else:
        key = differing_key
        if key not in first or set(different) - {key}:
            return None
    if key not in left.get("required", ()):
        return None
    return {**left, "properties": {**first, key: {union: [first[key], second[key]]}}}


def _required_string_tags_disjoint(left, right):
    """A sufficient disjointness witness, not an arbitrary Schema solver."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False

    def values(node):
        if not isinstance(node, dict) or node.get("type") != "string":
            return None
        result = [node["const"]] if "const" in node else node.get("enum")
        return set(result) if isinstance(result, list) and result and all(isinstance(v, str) for v in result) else None

    for key in set(left.get("required", ())) & set(right.get("required", ())):
        first = values(left.get("properties", {}).get(key))
        second = values(right.get("properties", {}).get(key))
        if first is not None and second is not None and first.isdisjoint(second):
            return True
    return False


def compact_provider_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project only proven closed-object unions; preserve the original input DTO.

    Call after the registrar establishes its final union/query-cursor layout.
    Titles are never inferred here: their provenance belongs to the generator.
    Unsupported or cyclic reference documents are returned unchanged.
    """
    if not _input_refs_can_expand(schema):
        return deepcopy(schema)
    expanded = dereference_refs(schema)

    def compact(node):
        result = deepcopy(node)
        for keyword, key, child in _input_schema_children(node):
            if key is None:
                result[keyword] = compact(child)
            else:
                result[keyword][key] = compact(child)
        for union in ("anyOf", "oneOf"):
            if union not in result or "discriminator" in result:
                continue
            branches = result[union]
            # Revisit after each reduction because nested factoring can expose
            # another strictly equivalent pair. Never deduplicate branches.
            while True:
                match = None
                for right in range(1, len(branches)):
                    for left in range(right):
                        merged = _merge_closed_required_variants(branches[left], branches[right], union)
                        if merged is not None:
                            key = next(key for key in branches[left]["properties"]
                                       if branches[left]["properties"][key] != branches[right]["properties"][key])
                            group = [index for index, branch in enumerate(branches)
                                     if _merge_closed_required_variants(branches[left], branch, union, differing_key=key) is not None]
                            # Exactly-one is not associative. Factor all matching
                            # shapes together; any remaining branch must be
                            # provably disjoint from every member of this group.
                            if union == "oneOf" and any(
                                not _required_string_tags_disjoint(branches[index], branch)
                                for index in group for other, branch in enumerate(branches)
                                if other not in group
                            ):
                                continue
                            merged["properties"][key] = {union: [branches[index]["properties"][key] for index in group]}
                            match = group, compact(merged)
                            break
                    if match is not None:
                        break
                if match is None:
                    break
                group, merged = match
                branches = [merged if index == group[0] else branch
                            for index, branch in enumerate(branches)
                            if index == group[0] or index not in group]
            result[union] = branches
        return result

    return compact(expanded)


def _merge_closed_string_variants(left, right):
    """Merge only one disjoint required tag; never form cross-field products."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    if left.get("type") != "object" or left.get("additionalProperties") is not False:
        return None
    if {k: v for k, v in left.items() if k != "properties"} != {
        k: v for k, v in right.items() if k != "properties"
    }:
        return None
    a, b = left.get("properties", {}), right.get("properties", {})
    if a.keys() != b.keys():
        return None
    different = [key for key in a if a[key] != b[key]]
    if len(different) != 1 or different[0] not in left.get("required", ()):
        return None
    key = different[0]
    first, second = a[key], b[key]
    if not isinstance(first, dict) or not isinstance(second, dict):
        return None
    common = {k: v for k, v in first.items() if k not in {"const", "enum"}}
    if common.get("type") != "string" or common != {
        k: v for k, v in second.items() if k not in {"const", "enum"}
    }:
        return None

    def values(node):
        if ("const" in node) == ("enum" in node):
            return None
        result = [node["const"]] if "const" in node else node["enum"]
        if not isinstance(result, list) or not result or not all(isinstance(v, str) for v in result):
            return None
        return result

    first_values, second_values = values(first), values(second)
    if first_values is None or second_values is None or set(first_values) & set(second_values):
        return None
    return {
        **left,
        "properties": {**a, key: {**common, "enum": [*first_values, *second_values]}},
    }


def _compact_closed_unions(schema: dict[str, Any]) -> dict[str, Any]:
    """Visit Schema positions only, leaving defaults/examples/enum data untouched."""

    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    for keyword in ("properties", "$defs", "patternProperties"):
        if keyword in result:
            result[keyword] = {key: _compact_closed_unions(value) for key, value in result[keyword].items()}
    for keyword in ("items", "additionalProperties", "propertyNames"):
        if isinstance(result.get(keyword), dict):
            result[keyword] = _compact_closed_unions(result[keyword])
    for keyword in ("anyOf", "oneOf", "prefixItems"):
        if keyword not in result:
            continue
        branches = [_compact_closed_unions(branch) if isinstance(branch, dict) else branch for branch in result[keyword]]
        if keyword == "prefixItems":
            result[keyword] = branches
            continue
        compacted = []
        for branch in branches:
            for index, previous in enumerate(compacted):
                merged = _merge_closed_string_variants(previous, branch)
                if merged is not None:
                    compacted[index] = merged
                    break
            else:
                compacted.append(branch)
        result[keyword] = compacted
    return result


def provider_output_schema(
    result_type: Any, *, compact: bool = False, preserve_references: bool = False,
) -> dict[str, Any]:
    """Generate from the authoritative result type, with optional lossless compaction.

    Use FastMCP's existing dereferencer before union comparison: its default
    list-tools middleware would expand references anyway. Compaction therefore
    survives that final projection without changing the input-schema pipeline.
    Static result documents can preserve their local definitions instead: they
    are read as JSON resources, outside the tool-list dereferencing middleware.
    """

    schema = provider_reference_schema(result_type, mode="serialization")
    schema["type"] = "object"
    if not compact:
        return schema
    return _compact_closed_unions(schema if preserve_references else dereference_refs(schema))


def _object_union_output_schema(result_type: Any, *, compact: bool = False) -> dict[str, Any]:
    """Keep a discriminated union at the MCP top level without a ``result`` wrapper."""

    # MCP requires an object output schema. The union branches are already
    # object models; adding the root type preserves the exact top-level DTO.
    return provider_output_schema(result_type, compact=compact)


def _apply_query_cursor_xor_schema(tool: Any) -> None:
    """Expose the runtime query/cursor exclusivity in the provider input Schema."""

    schema = dict(tool.parameters)
    properties = schema["properties"]
    common_properties = {
        name: value for name, value in properties.items() if name not in {"query", "cursor"}
    }
    common_required = [
        name for name in schema.get("required", ()) if name not in {"query", "cursor"}
    ]

    def variant(name: str, *, null: bool) -> dict[str, Any]:
        property_schema = properties[name]
        selected = [
            candidate
            for candidate in property_schema["anyOf"]
            if (candidate.get("type") == "null") is null
        ]
        if len(selected) == 1:
            selected_schema = selected[0]
        else:
            selected_schema = {"anyOf": selected}
        annotations = {
            key: value for key, value in property_schema.items() if key not in {"anyOf", "default"}
        }
        return {**selected_schema, **annotations}

    object_type = schema.get("type", "object")
    additional_properties = schema.get("additionalProperties", True)
    branches = [
        {
            "type": object_type,
            "additionalProperties": additional_properties,
            "properties": {
                **common_properties,
                "query": variant("query", null=False),
                "cursor": variant("cursor", null=True),
            },
            "required": [*common_required, "query"],
        },
        {
            "type": object_type,
            "additionalProperties": additional_properties,
            "properties": {
                **common_properties,
                "query": variant("query", null=True),
                "cursor": variant("cursor", null=False),
            },
            "required": [*common_required, "cursor"],
        },
    ]
    root_schema = {
        key: value
        for key, value in schema.items()
        if key not in {"additionalProperties", "properties", "required", "oneOf"}
    }
    root_schema["oneOf"] = branches
    tool.parameters = root_schema


def _apply_complete_query_cursor_xor_schema(tool: Any) -> None:
    """Expose two closed branches that do not define the unselected field."""

    schema = dict(tool.parameters)
    properties = schema["properties"]
    common_properties = {
        name: value for name, value in properties.items() if name not in {"query", "cursor"}
    }
    common_required = [
        name for name in schema.get("required", ()) if name not in {"query", "cursor"}
    ]

    def selected_property(name: str) -> dict[str, Any]:
        property_schema = properties[name]
        variants = [
            candidate for candidate in property_schema["anyOf"] if candidate.get("type") != "null"
        ]
        selected = variants[0] if len(variants) == 1 else {"anyOf": variants}
        annotations = {
            key: value for key, value in property_schema.items() if key not in {"anyOf", "default"}
        }
        return {**selected, **annotations}

    object_type = schema.get("type", "object")
    additional_properties = schema.get("additionalProperties", True)
    branches = []
    for selected in ("query", "cursor"):
        branches.append(
            {
                "type": object_type,
                "additionalProperties": additional_properties,
                "properties": {
                    **common_properties,
                    selected: selected_property(selected),
                },
                "required": [*common_required, selected],
            }
        )
    root_schema = {
        key: value
        for key, value in schema.items()
        if key not in {"additionalProperties", "properties", "required", "oneOf"}
    }
    root_schema["oneOf"] = branches
    tool.parameters = root_schema


__all__ = [
    "_apply_complete_query_cursor_xor_schema",
    "_apply_query_cursor_xor_schema",
    "_object_union_output_schema",
    "provider_output_schema",
    "provider_reference_schema",
    "compact_provider_input_schema",
]
