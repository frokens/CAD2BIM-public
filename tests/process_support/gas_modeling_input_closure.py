"""Pure validator for the test-only gas modeling-input closure profile.

The validator deliberately has no repository, MCP, L0, Golden IFC, or IFC
dependency.  Passing it means that the supplied fourteen small logical tables
are internally closed under this experimental preflight profile; it does not
mean that an IFC file has been generated or that a public IFC tool contract has
been frozen.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

import pyarrow as pa

from tests.process_support.m4_resource_geometry import (
    ResourceGeometryError,
    plan_elbow,
    plan_tee,
)


@dataclass(frozen=True)
class ClosureIssue:
    code: str
    location: str
    message: str


@dataclass(frozen=True)
class ClosureValidationReport:
    issues: tuple[ClosureIssue, ...]

    @property
    def passed(self) -> bool:
        return not self.issues

    @property
    def issue_codes(self) -> frozenset[str]:
        return frozenset(issue.code for issue in self.issues)


def load_closure_profile(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("closure profile must be a YAML object")
    return value


def load_closure_tables(path: Path) -> dict[str, list[dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("closure fixture must be a JSON object")
    return value


def load_modeling_input_schema(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("modeling-input schema must be a JSON object")
    return value


def load_component_catalog(path: Path) -> dict[str, Any]:
    """Load the public experiment component catalog used by exact validation."""

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("component catalog must be a YAML object")
    return value


_ARROW_TYPES: dict[str, pa.DataType] = {
    "string": pa.string(),
    "int32": pa.int32(),
    "int64": pa.int64(),
    "float64": pa.float64(),
    "bool": pa.bool_(),
}


def arrow_schema_for_domain(schema: Mapping[str, Any], domain: str) -> pa.Schema:
    domains = schema.get("domains")
    if not isinstance(domains, Mapping) or domain not in domains:
        raise ValueError(f"unknown modeling-input domain {domain!r}")
    spec = domains[domain]
    if not isinstance(spec, Mapping) or not isinstance(spec.get("columns"), list):
        raise ValueError(f"domain {domain!r} has no column declaration")
    fields = []
    for column in spec["columns"]:
        if not isinstance(column, Mapping):
            raise ValueError(f"domain {domain!r} contains an invalid column declaration")
        arrow_type = _ARROW_TYPES.get(column.get("arrow_type"))
        if arrow_type is None:
            raise ValueError(
                f"domain {domain!r} column {column.get('name')!r} has an unsupported Arrow type"
            )
        fields.append(
            pa.field(
                column["name"],
                arrow_type,
                nullable=bool(column.get("nullable", False)),
            )
        )
    return pa.schema(fields)


def _non_empty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_modeling_input_closure(
    tables: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> ClosureValidationReport:
    """Validate tables without consulting any external source or expected count."""

    issues: list[ClosureIssue] = []

    def add(code: str, location: str, message: str) -> None:
        issues.append(ClosureIssue(code=code, location=location, message=message))

    domain_specs = profile.get("domains")
    if not isinstance(domain_specs, Mapping):
        return ClosureValidationReport(
            (ClosureIssue("PROFILE_INVALID", "profile.domains", "domains must be an object"),)
        )

    expected_domains = set(domain_specs)
    actual_domains = set(tables)
    for domain in sorted(expected_domains - actual_domains):
        add("DOMAIN_MISSING", domain, "required logical domain is absent")
    if not profile.get("allow_extra_domains", False):
        for domain in sorted(actual_domains - expected_domains):
            add("DOMAIN_UNDECLARED", domain, "logical domain is not declared by this profile")

    normalized: dict[str, list[Mapping[str, Any]]] = {}
    for domain in domain_specs:
        raw_rows = tables.get(domain, [])
        if not isinstance(raw_rows, list):
            add("DOMAIN_NOT_ROWS", domain, "domain value must be a list of row objects")
            normalized[domain] = []
            continue
        rows: list[Mapping[str, Any]] = []
        for index, row in enumerate(raw_rows):
            if not isinstance(row, Mapping):
                add("ROW_NOT_OBJECT", f"{domain}[{index}]", "row must be an object")
                continue
            rows.append(row)
        normalized[domain] = rows

    keys: dict[str, set[str]] = {}
    for domain, raw_spec in domain_specs.items():
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        rows = normalized[domain]
        minimum_rows = spec.get("minimum_rows", 0)
        if not isinstance(minimum_rows, int) or minimum_rows < 0:
            add("PROFILE_INVALID", f"profile.domains.{domain}.minimum_rows", "must be a non-negative integer")
            minimum_rows = 0
        if len(rows) < minimum_rows:
            add("MINIMUM_ROWS", domain, f"requires at least {minimum_rows} row(s)")

        primary_key = spec.get("primary_key")
        if not isinstance(primary_key, str) or not primary_key:
            add("PROFILE_INVALID", f"profile.domains.{domain}.primary_key", "must be a field name")
            keys[domain] = set()
            continue

        required_fields = tuple(spec.get("required_fields", ()))
        non_empty_fields = tuple(spec.get("non_empty_fields", ()))
        enum_fields = spec.get("enum_fields", {})
        finite_fields = tuple(spec.get("finite_fields", ()))
        unit_vectors = tuple(spec.get("unit_vectors", ()))
        seen_primary: set[str] = set()

        for index, row in enumerate(rows):
            location = f"{domain}[{index}]"
            for field in required_fields:
                if field not in row:
                    add("FIELD_MISSING", f"{location}.{field}", "required field is absent")
            for field in non_empty_fields:
                if field in row and not _non_empty(row[field]):
                    add("FIELD_EMPTY", f"{location}.{field}", "field must not be null or blank")
            if isinstance(enum_fields, Mapping):
                for field, allowed in enum_fields.items():
                    if field in row and row[field] not in allowed:
                        add("ENUM_VALUE_INVALID", f"{location}.{field}", f"value must be one of {list(allowed)}")
            for field in finite_fields:
                if field in row and not _finite_number(row[field]):
                    add("NUMBER_NOT_FINITE", f"{location}.{field}", "value must be a finite number")
            for vector_fields in unit_vectors:
                if not all(field in row and _finite_number(row[field]) for field in vector_fields):
                    continue
                length = math.sqrt(sum(float(row[field]) ** 2 for field in vector_fields))
                tolerance = float(profile.get("unit_vector_tolerance", 1e-6))
                if abs(length - 1.0) > tolerance:
                    add("VECTOR_NOT_UNIT", location, f"{list(vector_fields)} has length {length!r}")

            value = row.get(primary_key)
            if not isinstance(value, str) or not value.strip():
                add("PRIMARY_KEY_INVALID", f"{location}.{primary_key}", "primary key must be a non-empty string")
            elif value in seen_primary:
                add("PRIMARY_KEY_DUPLICATE", f"{location}.{primary_key}", f"duplicate key {value!r}")
            else:
                seen_primary.add(value)
        keys[domain] = seen_primary

        for fields in spec.get("unique_together", ()):
            seen_values: set[tuple[Any, ...]] = set()
            for index, row in enumerate(rows):
                if not all(field in row for field in fields):
                    continue
                value = tuple(row[field] for field in fields)
                if any(item is None for item in value):
                    continue
                try:
                    duplicate = value in seen_values
                    seen_values.add(value)
                except TypeError:
                    add("UNIQUE_VALUE_INVALID", f"{domain}[{index}]", f"{list(fields)} must be scalar values")
                    continue
                if duplicate:
                    add("UNIQUE_CONSTRAINT", f"{domain}[{index}]", f"duplicate values for {list(fields)}")

    for domain, raw_spec in domain_specs.items():
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        for foreign_key in spec.get("foreign_keys", ()):
            field = foreign_key["field"]
            target = foreign_key["target_domain"]
            nullable = bool(foreign_key.get("nullable", False))
            target_keys = keys.get(target, set())
            for index, row in enumerate(normalized[domain]):
                if field not in row:
                    continue
                value = row[field]
                if value is None and nullable:
                    continue
                if value not in target_keys:
                    add("FOREIGN_KEY_UNKNOWN", f"{domain}[{index}].{field}", f"no {target} row has key {value!r}")

    for graph in profile.get("graph_constraints", ()):
        _validate_parent_graph(normalized, graph, add)

    _validate_axis_vertices(normalized, add)
    _validate_source_lineage(normalized, add)
    _validate_geometry_coverage(normalized, add)
    _validate_ports_and_connections(normalized, add)
    _validate_system_membership(normalized, add)
    _validate_materials(normalized, add)
    _validate_properties(normalized, add)
    _validate_spatial_assignments(normalized, add)
    _validate_piperuns(normalized, add)

    issues.sort(key=lambda issue: (issue.location, issue.code, issue.message))
    return ClosureValidationReport(tuple(issues))


def _validate_source_lineage(
    tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any
) -> None:
    for index, product in enumerate(tables.get("products", ())):
        if product.get("origin_kind") != "source":
            continue
        if not _non_empty(product.get("source_disposition_key")):
            add(
                "SOURCE_PRODUCT_DISPOSITION_MISSING",
                f"products[{index}].source_disposition_key",
                "a source-origin product requires one source disposition",
            )


def _validate_parent_graph(
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    graph: Mapping[str, Any],
    add: Any,
) -> None:
    domain = graph["domain"]
    node_field = graph["node_field"]
    parent_field = graph["parent_field"]
    code = graph["code"]
    parents = {
        row.get(node_field): row.get(parent_field)
        for row in tables.get(domain, ())
        if isinstance(row.get(node_field), str)
    }
    reported: set[str] = set()
    for start in parents:
        seen: set[str] = set()
        current: Any = start
        while current is not None and current in parents:
            if current in seen:
                if start not in reported:
                    add(code, f"{domain}.{start}", f"parent graph contains a cycle through {current!r}")
                    reported.add(start)
                break
            seen.add(current)
            current = parents[current]


def _validate_axis_vertices(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    ordinals: defaultdict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(tables.get("axis_vertices", ())):
        ordinal = row.get("sequence_ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            add("AXIS_ORDINAL_INVALID", f"axis_vertices[{index}].sequence_ordinal", "must be a non-negative integer")
            continue
        product_key = row.get("product_key")
        if isinstance(product_key, str):
            ordinals[product_key].append(ordinal)
    for product_key, values in ordinals.items():
        if sorted(values) != list(range(len(values))):
            add("AXIS_ORDINAL_GAP", f"axis_vertices.{product_key}", "ordinals must be contiguous from zero")


def _validate_geometry_coverage(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    counts = Counter(row.get("product_key") for row in tables.get("geometry_inputs", ()))
    for product in tables.get("products", ()):
        product_key = product.get("product_key")
        if isinstance(product_key, str) and counts[product_key] < 1:
            add("PRODUCT_GEOMETRY_MISSING", f"products.{product_key}", "product requires at least one geometry input")


def _validate_ports_and_connections(
    tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any
) -> None:
    ports = {
        row.get("port_key"): row
        for row in tables.get("ports", ())
        if isinstance(row.get("port_key"), str)
    }
    product_by_port = {key: row.get("owner_product_key") for key, row in ports.items()}
    connection_counts: Counter[str] = Counter()
    for index, row in enumerate(tables.get("connections", ())):
        left = row.get("port_a_key")
        right = row.get("port_b_key")
        if left == right and left is not None:
            add("CONNECTION_SELF_PORT", f"connections[{index}]", "an external connection requires two different ports")
        if left in product_by_port and right in product_by_port and product_by_port[left] == product_by_port[right]:
            add("CONNECTION_INTERNAL_PRODUCT", f"connections[{index}]", "connections table cannot encode an internal product channel")
        if isinstance(left, str):
            connection_counts[left] += 1
        if isinstance(right, str):
            connection_counts[right] += 1

    for key, row in ports.items():
        count = connection_counts[key]
        if count > 1:
            add("PORT_MULTIPLY_CONNECTED", f"ports.{key}", f"port participates in {count} external connections")
        state = row.get("connection_state")
        reason = row.get("open_reason_code")
        if state == "connected" and count != 1:
            add("CONNECTED_PORT_CARDINALITY", f"ports.{key}", "connected port must have exactly one external connection")
        if state == "connected" and _non_empty(reason):
            add("CONNECTED_PORT_HAS_OPEN_REASON", f"ports.{key}", "connected port must not have an open reason")
        if state == "open" and count != 0:
            add("OPEN_PORT_CONNECTED", f"ports.{key}", "open port must have no external connection")
        if state == "open" and not _non_empty(reason):
            add("OPEN_PORT_REASON_MISSING", f"ports.{key}", "open port requires an explicit reason")

        diameter_status = row.get("diameter_status")
        diameter = row.get("outer_diameter_m")
        if diameter_status == "known" and (not _finite_number(diameter) or diameter <= 0):
            add("PORT_DIAMETER_INVALID", f"ports.{key}.outer_diameter_m", "known diameter must be positive and finite")
        if diameter_status == "unknown" and diameter is not None:
            add("UNKNOWN_PORT_HAS_DIAMETER", f"ports.{key}.outer_diameter_m", "unknown diameter must remain null")
        if row.get("flow_direction") != "NOTDEFINED" and not _non_empty(row.get("flow_direction_authority")):
            add("FLOW_DIRECTION_AUTHORITY_MISSING", f"ports.{key}", "directed flow requires an explicit authority")


def _validate_system_membership(
    tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any
) -> None:
    products = {
        row.get("product_key"): row
        for row in tables.get("products", ())
        if isinstance(row.get("product_key"), str)
    }
    ports = {
        row.get("port_key")
        for row in tables.get("ports", ())
        if isinstance(row.get("port_key"), str)
    }
    systems = {
        row.get("system_key")
        for row in tables.get("systems", ())
        if isinstance(row.get("system_key"), str)
    }
    non_leaf = {
        row.get("parent_system_key")
        for row in tables.get("systems", ())
        if row.get("parent_system_key") is not None
    }
    leaf_systems = systems - non_leaf
    leaf_counts: Counter[tuple[str, str]] = Counter()
    for index, row in enumerate(tables.get("system_members", ())):
        kind = row.get("member_kind")
        key = row.get("member_key")
        valid_keys = products if kind == "product" else ports if kind == "port" else set()
        if key not in valid_keys:
            add("SYSTEM_MEMBER_UNKNOWN", f"system_members[{index}].member_key", f"no {kind!r} member has key {key!r}")
        if row.get("system_key") in leaf_systems and isinstance(kind, str) and isinstance(key, str):
            leaf_counts[(kind, key)] += 1

    for product_key, product in products.items():
        count = leaf_counts[("product", product_key)]
        requirement = product.get("system_membership_requirement")
        if requirement == "leaf_required" and count != 1:
            add("PRODUCT_LEAF_SYSTEM_CARDINALITY", f"products.{product_key}", f"requires exactly one leaf system membership, found {count}")
        if requirement == "not_applicable" and count != 0:
            add("PRODUCT_SYSTEM_NOT_APPLICABLE", f"products.{product_key}", "not-applicable product must not be assigned to a leaf system")
    for port_key in ports:
        count = leaf_counts[("port", port_key)]
        if count != 1:
            add("PORT_LEAF_SYSTEM_CARDINALITY", f"ports.{port_key}", f"requires exactly one leaf system membership, found {count}")


def _validate_materials(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    products = {
        row.get("product_key")
        for row in tables.get("products", ())
        if isinstance(row.get("product_key"), str)
    }
    rows = tables.get("materials_styles", ())
    definitions = {
        row.get("material_style_key")
        for row in rows
        if row.get("row_kind") == "definition" and isinstance(row.get("material_style_key"), str)
    }
    binding_counts: Counter[str] = Counter()
    for index, row in enumerate(rows):
        kind = row.get("row_kind")
        key = row.get("material_style_key")
        definition = row.get("definition_key")
        if kind == "definition":
            if definition != key or row.get("owner_kind") != "none" or row.get("owner_key") is not None:
                add("MATERIAL_DEFINITION_SHAPE", f"materials_styles[{index}]", "definition must own itself and have no external owner")
            if not _non_empty(row.get("material_code")) or not _non_empty(row.get("appearance_code")):
                add("MATERIAL_DEFINITION_INCOMPLETE", f"materials_styles[{index}]", "definition requires material and appearance codes")
        elif kind == "binding":
            owner = row.get("owner_key")
            if row.get("owner_kind") != "product" or owner not in products:
                add("MATERIAL_BINDING_OWNER", f"materials_styles[{index}]", "binding requires an existing product owner")
            if definition not in definitions:
                add("MATERIAL_DEFINITION_UNKNOWN", f"materials_styles[{index}].definition_key", "binding references no definition row")
            if isinstance(owner, str):
                binding_counts[owner] += 1
        elif kind == "style":
            if row.get("owner_kind") != "material_definition" or row.get("owner_key") not in definitions:
                add("MATERIAL_STYLE_OWNER", f"materials_styles[{index}]", "style must refer to a material definition")
        elif kind == "layer_binding":
            if row.get("owner_kind") != "presentation_layer" or not _non_empty(row.get("owner_key")) or not _non_empty(row.get("layer_name")):
                add("LAYER_BINDING_SHAPE", f"materials_styles[{index}]", "layer binding requires a presentation-layer owner and layer name")
            if definition not in definitions:
                add("MATERIAL_DEFINITION_UNKNOWN", f"materials_styles[{index}].definition_key", "layer binding references no definition row")
    for product_key in products:
        if binding_counts[product_key] < 1:
            add("PRODUCT_MATERIAL_BINDING_MISSING", f"products.{product_key}", "product requires at least one material binding")


def _validate_properties(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    owner_domains = {
        "product": ("products", "product_key"),
        "port": ("ports", "port_key"),
        "system": ("systems", "system_key"),
        "spatial_node": ("spatial_nodes", "spatial_node_key"),
    }
    owner_keys = {
        kind: {row.get(key_field) for row in tables.get(domain, ())}
        for kind, (domain, key_field) in owner_domains.items()
    }
    for index, row in enumerate(tables.get("property_values", ())):
        kind = row.get("owner_kind")
        if row.get("owner_key") not in owner_keys.get(kind, set()):
            add("PROPERTY_OWNER_UNKNOWN", f"property_values[{index}].owner_key", "typed property owner does not exist")
        if row.get("value_kind") == "number":
            raw_number = (
                row.get("value_number")
                if "value_number" in row
                else row.get("value_text")
            )
            try:
                number = float(raw_number)
            except (TypeError, ValueError):
                number = math.nan
            if not math.isfinite(number):
                add("PROPERTY_NUMBER_INVALID", f"property_values[{index}].value_text", "number value must parse as finite")


def _validate_spatial_assignments(
    tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any
) -> None:
    node_kind = {
        row.get("spatial_node_key"): row.get("node_kind")
        for row in tables.get("spatial_nodes", ())
    }
    for index, row in enumerate(tables.get("spatial_nodes", ())):
        if row.get("node_kind") == "GROUP" and row.get("parent_spatial_node_key") is not None:
            add("GROUP_HAS_SPATIAL_PARENT", f"spatial_nodes[{index}]", "business group cannot masquerade as spatial containment")
    containment_counts: Counter[str] = Counter()
    for index, row in enumerate(tables.get("spatial_assignments", ())):
        kind = row.get("assignment_kind")
        target_kind = node_kind.get(row.get("spatial_node_key"))
        if kind == "spatial_containment":
            containment_counts[row.get("product_key")] += 1
            if target_kind == "GROUP":
                add("GROUP_USED_FOR_CONTAINMENT", f"spatial_assignments[{index}]", "group requires group_membership, not spatial_containment")
        if kind == "group_membership" and target_kind != "GROUP":
            add("GROUP_MEMBERSHIP_TARGET", f"spatial_assignments[{index}]", "group_membership must target a GROUP node")
    for product in tables.get("products", ()):
        product_key = product.get("product_key")
        if not isinstance(product_key, str):
            continue
        requirement = product.get("spatial_assignment_requirement")
        count = containment_counts[product_key]
        if requirement in {"assembly_inherited", "not_applicable"}:
            if count != 0:
                add(
                    "PRODUCT_SPATIAL_CONTAINMENT_NOT_APPLICABLE",
                    f"products.{product_key}",
                    f"{requirement} product must not be directly contained",
                )
        elif count != 1:
            add(
                "PRODUCT_SPATIAL_CONTAINMENT_CARDINALITY",
                f"products.{product_key}",
                f"requires exactly one spatial containment, found {count}",
            )


def _validate_piperuns(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    products = {
        row.get("product_key")
        for row in tables.get("products", ())
        if isinstance(row.get("product_key"), str)
    }
    definitions: dict[str, Mapping[str, Any]] = {}
    members: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    product_runs: defaultdict[str, set[str]] = defaultdict(set)
    for index, row in enumerate(tables.get("piperun_members", ())):
        run_key = row.get("piperun_key")
        if row.get("row_kind") == "run_definition":
            if run_key in definitions:
                add("PIPERUN_DEFINITION_DUPLICATE", f"piperun_members[{index}]", "run has more than one definition")
            definitions[run_key] = row
            if row.get("product_key") is not None or row.get("membership_ordinal") is not None:
                add("PIPERUN_DEFINITION_SHAPE", f"piperun_members[{index}]", "definition cannot be a product member")
            if not _non_empty(row.get("boundary_reason")):
                add("PIPERUN_BOUNDARY_REASON_MISSING", f"piperun_members[{index}]", "run definition requires a boundary reason")
        elif row.get("row_kind") == "member":
            product_key = row.get("product_key")
            if product_key not in products:
                add("PIPERUN_PRODUCT_UNKNOWN", f"piperun_members[{index}].product_key", "member references no product")
            ordinal = row.get("membership_ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
                add("PIPERUN_ORDINAL_INVALID", f"piperun_members[{index}].membership_ordinal", "must be a non-negative membership ordinal")
            members[run_key].append(row)
            if isinstance(product_key, str):
                product_runs[product_key].add(run_key)

    for run_key, definition in definitions.items():
        run_members = members.get(run_key, [])
        if not run_members:
            add("PIPERUN_EMPTY", f"piperun_members.{run_key}", "run definition requires at least one member")
            continue
        expected_homogeneity = definition.get("homogeneity_key")
        if any(row.get("homogeneity_key") != expected_homogeneity for row in run_members):
            add("PIPERUN_HOMOGENEITY_MISMATCH", f"piperun_members.{run_key}", "members must match the run definition homogeneity key")
        ordinals = [row.get("membership_ordinal") for row in run_members]
        if all(isinstance(value, int) and not isinstance(value, bool) for value in ordinals):
            if sorted(ordinals) != list(range(len(ordinals))):
                add("PIPERUN_MEMBERSHIP_ORDINAL_GAP", f"piperun_members.{run_key}", "deterministic membership ordinals must be contiguous from zero")
    for run_key in members.keys() - definitions.keys():
        add("PIPERUN_DEFINITION_MISSING", f"piperun_members.{run_key}", "members require one run_definition row")
    for product_key, run_keys in product_runs.items():
        if len(run_keys) > 1:
            add("PRODUCT_IN_MULTIPLE_PIPERUNS", f"products.{product_key}", "product belongs to more than one pipe run")


def validate_exact_modeling_input_submission(
    tables: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    component_catalog: Mapping[str, Any] | None = None,
) -> ClosureValidationReport:
    """Validate the experiment-only exact row contract and its cross-domain semantics."""

    issues: list[ClosureIssue] = []

    def add(code: str, location: str, message: str) -> None:
        issues.append(ClosureIssue(code=code, location=location, message=message))

    if schema.get("scope") != "experiment_only" or schema.get("public_ifc_mcp_contract") is not False:
        add(
            "SCHEMA_SCOPE_INVALID",
            "schema",
            "exact submission schema must remain experiment-only and outside the public IFC MCP contract",
        )
    domains = schema.get("domains")
    if not isinstance(domains, Mapping):
        return ClosureValidationReport(
            tuple(issues)
            + (ClosureIssue("PROFILE_INVALID", "schema.domains", "domains must be an object"),)
        )

    try:
        profile = _closure_profile_from_exact_schema(schema)
    except (KeyError, TypeError, ValueError) as exc:
        add("PROFILE_INVALID", "schema", str(exc))
        return ClosureValidationReport(tuple(sorted(issues, key=_issue_sort_key)))

    normalized: dict[str, list[Mapping[str, Any]]] = {}
    expected_domains = set(domains)
    actual_domains = set(tables)
    for domain in sorted(expected_domains - actual_domains):
        add("DOMAIN_MISSING", domain, "required logical domain is absent")
    if not schema.get("allow_extra_domains", False):
        for domain in sorted(actual_domains - expected_domains):
            add("DOMAIN_UNDECLARED", domain, "logical domain is not declared by the exact schema")

    for domain, raw_spec in domains.items():
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        rows = tables.get(domain, [])
        if not isinstance(rows, list):
            add("DOMAIN_NOT_ROWS", domain, "domain value must be a list of row objects")
            normalized[domain] = []
            continue
        columns = spec.get("columns", [])
        expected_names = {
            column.get("name") for column in columns if isinstance(column, Mapping)
        }
        normalized[domain] = []
        for index, row in enumerate(rows):
            location = f"{domain}[{index}]"
            if not isinstance(row, Mapping):
                add("ROW_NOT_OBJECT", location, "row must be an object")
                continue
            normalized[domain].append(row)
            actual_names = set(row)
            for field in sorted(expected_names - actual_names):
                add("FIELD_MISSING", f"{location}.{field}", "exact schema field is absent")
            if not schema.get("allow_extra_columns", False):
                for field in sorted(actual_names - expected_names):
                    add("FIELD_UNDECLARED", f"{location}.{field}", "field is not declared by the exact schema")
            for column in columns:
                if not isinstance(column, Mapping) or column.get("name") not in row:
                    continue
                _validate_exact_scalar(row[column["name"]], column, f"{location}.{column['name']}", add)

    base = validate_modeling_input_closure(normalized, profile)
    issues.extend(base.issues)
    _validate_exact_source_contract(normalized, add)
    _validate_exact_product_contract(normalized, add)
    _validate_exact_geometry_contract(normalized, schema, add)
    _validate_exact_port_contract(normalized, add)
    _validate_exact_connection_geometry(normalized, schema, add)
    _validate_exact_material_contract(normalized, add)
    _validate_exact_property_contract(normalized, add)
    _validate_exact_spatial_contract(normalized, add)
    _validate_exact_piperun_contract(normalized, add)
    _validate_exact_unresolved_contract(normalized, add)
    _validate_exact_ifc_enum_contract(normalized, schema, add)
    _validate_exact_package_realization_contract(
        normalized, schema, component_catalog, add
    )

    unique = {(issue.code, issue.location, issue.message): issue for issue in issues}
    return ClosureValidationReport(tuple(sorted(unique.values(), key=_issue_sort_key)))


def validate_exact_modeling_input_arrow_tables(
    tables: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    component_catalog: Mapping[str, Any] | None = None,
) -> ClosureValidationReport:
    """Validate Arrow names/order/types, then enforce logical nullability on rows.

    Parquet/Arrow writers may conservatively mark every physical field nullable
    even when no row contains null.  That storage metadata is not a submission
    fact, so acceptance ignores physical nullable flags and field/schema
    metadata.  The exact contract's nullable declarations remain authoritative
    and are enforced below by ``_validate_exact_scalar`` on every materialized
    row value.
    """

    issues: list[ClosureIssue] = []
    row_tables: dict[str, list[dict[str, Any]]] = {}
    domains = schema.get("domains")
    if not isinstance(domains, Mapping):
        return ClosureValidationReport(
            (ClosureIssue("PROFILE_INVALID", "schema.domains", "domains must be an object"),)
        )
    for domain in domains:
        table = tables.get(domain)
        if not isinstance(table, pa.Table):
            issues.append(
                ClosureIssue("ARROW_TABLE_REQUIRED", domain, "domain must be supplied as a PyArrow Table")
            )
            continue
        expected = arrow_schema_for_domain(schema, domain)
        physical_shape_matches = (
            table.schema.names == expected.names
            and len(table.schema) == len(expected)
            and all(
                table.schema.field(index).type.equals(expected.field(index).type)
                for index in range(len(expected))
            )
        )
        if not physical_shape_matches:
            issues.append(
                ClosureIssue(
                    "ARROW_SCHEMA_MISMATCH",
                    domain,
                    "field names, order and Arrow types must match the exact schema; "
                    f"expected {expected}, received {table.schema}",
                )
            )
        row_tables[domain] = table.to_pylist()
    row_report = validate_exact_modeling_input_submission(
        row_tables,
        schema,
        component_catalog=component_catalog,
    )
    issues.extend(row_report.issues)
    unique = {(issue.code, issue.location, issue.message): issue for issue in issues}
    return ClosureValidationReport(tuple(sorted(unique.values(), key=_issue_sort_key)))


def _issue_sort_key(issue: ClosureIssue) -> tuple[str, str, str]:
    return (issue.location, issue.code, issue.message)


def _closure_profile_from_exact_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    domains = schema["domains"]
    profile_domains: dict[str, Any] = {}
    for domain, spec in domains.items():
        columns = spec["columns"]
        names = [column["name"] for column in columns]
        if len(names) != len(set(names)):
            raise ValueError(f"domain {domain!r} declares duplicate columns")
        if spec["primary_key"] not in names:
            raise ValueError(f"domain {domain!r} primary key is not a declared column")
        for column in columns:
            if column.get("arrow_type") not in _ARROW_TYPES:
                raise ValueError(
                    f"domain {domain!r} column {column.get('name')!r} has an unsupported Arrow type"
                )
        profile_domains[domain] = {
            "primary_key": spec["primary_key"],
            "minimum_rows": spec.get("minimum_rows", 0),
            "required_fields": names,
            "non_empty_fields": [column["name"] for column in columns if column.get("non_empty")],
            "enum_fields": {
                column["name"]: column["enum"] for column in columns if "enum" in column
            },
            "finite_fields": [
                column["name"]
                for column in columns
                if column.get("finite") and not column.get("nullable", False)
            ],
            "foreign_keys": spec.get("foreign_keys", []),
            "unique_together": spec.get("unique_together", []),
            "unit_vectors": spec.get("unit_vectors", []),
        }
    return {
        "allow_extra_domains": schema.get("allow_extra_domains", False),
        "unit_vector_tolerance": schema.get("unit_vector_tolerance", 1e-6),
        "domains": profile_domains,
        "graph_constraints": schema.get("graph_constraints", []),
    }


def _validate_exact_scalar(value: Any, column: Mapping[str, Any], location: str, add: Any) -> None:
    if value is None:
        if not column.get("nullable", False):
            add("FIELD_NULL", location, "non-nullable exact schema field is null")
        return
    arrow_type = column.get("arrow_type")
    valid = {
        "string": isinstance(value, str),
        "int32": isinstance(value, int) and not isinstance(value, bool),
        "int64": isinstance(value, int) and not isinstance(value, bool),
        "float64": isinstance(value, (int, float)) and not isinstance(value, bool),
        "bool": isinstance(value, bool),
    }.get(arrow_type, False)
    if not valid:
        add("FIELD_TYPE_INVALID", location, f"value must match Arrow type {arrow_type!r}")
        return
    if arrow_type in {"int32", "int64", "float64"}:
        numeric = float(value)
        if not math.isfinite(numeric):
            add("NUMBER_NOT_FINITE", location, "numeric value must be finite")
            return
        if "minimum" in column and numeric < float(column["minimum"]):
            add("NUMBER_BELOW_MINIMUM", location, f"value must be >= {column['minimum']}")
        if "exclusive_minimum" in column and numeric <= float(column["exclusive_minimum"]):
            add("NUMBER_NOT_ABOVE_MINIMUM", location, f"value must be > {column['exclusive_minimum']}")
        if "maximum" in column and numeric > float(column["maximum"]):
            add("NUMBER_ABOVE_MAXIMUM", location, f"value must be <= {column['maximum']}")


def _validate_exact_source_contract(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    source_keys = {row.get("source_entity_key") for row in tables.get("source_dispositions", ())}
    modeled_products: Counter[str] = Counter(
        row.get("source_disposition_key")
        for row in tables.get("products", ())
        if row.get("origin_kind") == "source"
    )
    for index, row in enumerate(tables.get("source_dispositions", ())):
        canonical = row.get("canonical_source_entity_key")
        if canonical is not None and canonical not in source_keys:
            add("CANONICAL_SOURCE_UNKNOWN", f"source_dispositions[{index}]", "canonical source key is absent")
        count = modeled_products[row.get("source_disposition_key")]
        disposition = row.get("modeling_disposition")
        if disposition == "modeled" and count < 1:
            add("MODELED_SOURCE_WITHOUT_PRODUCT", f"source_dispositions[{index}]", "modeled source requires a source-origin product")
        if disposition != "modeled" and count:
            add("NONMODELED_SOURCE_HAS_PRODUCT", f"source_dispositions[{index}]", "excluded/non-product/unresolved source cannot own a source-origin product")


def _validate_exact_product_contract(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    products = {row.get("product_key"): row for row in tables.get("products", ())}
    suppressed_unresolved = {
        row.get("subject_key")
        for row in tables.get("unresolved_items", ())
        if row.get("subject_kind") == "product"
        and row.get("issue_code") == "DEGENERATE_FITTING_INTERFACE_GEOMETRY"
        and row.get("severity") == "blocking"
        and row.get("resolution_status") == "open"
    }
    for index, product in enumerate(tables.get("products", ())):
        key = product.get("product_key")
        parent_key = product.get("assembly_parent_product_key")
        role = product.get("assembly_role")
        if role == "assembly_root" and product.get("ifc_class") != "IfcElementAssembly":
            add("ASSEMBLY_ROOT_CLASS_INVALID", f"products[{index}]", "assembly root must be IfcElementAssembly")
        if role == "assembly_component":
            parent = products.get(parent_key)
            if parent is None or parent.get("assembly_role") != "assembly_root":
                add("ASSEMBLY_COMPONENT_PARENT_INVALID", f"products[{index}]", "assembly component requires an assembly-root parent")
        elif parent_key is not None:
            add("NONCOMPONENT_HAS_ASSEMBLY_PARENT", f"products[{index}]", "only assembly components may name a parent")
        if product.get("spatial_assignment_requirement") == "assembly_inherited" and role != "assembly_component":
            add("SPATIAL_INHERITANCE_WITHOUT_ASSEMBLY", f"products[{index}]", "spatial inheritance requires an assembly component")
        if product.get("geometry_strategy") == "TEMPLATE_COMPOSITE" and not _non_empty(product.get("component_resource_key")):
            add("COMPONENT_RESOURCE_MISSING", f"products[{index}]", "template composite requires a component resource key")
        if product.get("geometry_strategy") == "ASSEMBLY_ONLY":
            if product.get("representation_strategy") != "NONE" or product.get("representation_quality") != "NONE":
                add("ASSEMBLY_REPRESENTATION_INVALID", f"products[{index}]", "assembly-only product has no direct representation")
        elif product.get("geometry_strategy") == "REPRESENTATION_SUPPRESSED":
            if product.get("representation_strategy") != "NONE" or product.get("representation_quality") != "NONE":
                add(
                    "SUPPRESSED_REPRESENTATION_INVALID",
                    f"products[{index}]",
                    "representation-suppressed product must declare no representation and no representation quality",
                )
            if product.get("component_resource_key") is not None:
                add(
                    "SUPPRESSED_PRODUCT_RESOURCE_FORBIDDEN",
                    f"products[{index}]",
                    "representation-suppressed product cannot bind a component resource",
                )
            if key not in suppressed_unresolved:
                add(
                    "SUPPRESSED_PRODUCT_UNRESOLVED_MISSING",
                    f"products[{index}]",
                    "representation-suppressed product requires an open DEGENERATE_FITTING_INTERFACE_GEOMETRY item",
                )
        elif product.get("geometry_strategy") == "AXIS_ONLY":
            if product.get("representation_strategy") != "AXIS":
                add(
                    "AXIS_ONLY_REPRESENTATION_INVALID",
                    f"products[{index}]",
                    "axis-only product must declare an Axis-only representation",
                )
        elif product.get("representation_strategy") == "NONE":
            add("PRODUCT_REPRESENTATION_MISSING", f"products[{index}]", "realized product requires an explicit representation strategy")
        if product.get("origin_kind") == "source" and not _non_empty(product.get("source_disposition_key")):
            add("SOURCE_PRODUCT_DISPOSITION_MISSING", f"products.{key}", "source-origin product requires a source disposition")


def _validate_exact_geometry_contract(
    tables: Mapping[str, Sequence[Mapping[str, Any]]], schema: Mapping[str, Any], add: Any
) -> None:
    rows_by_product: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    ports = {row.get("port_key"): row for row in tables.get("ports", ())}
    axis_rows: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    geometry_spec = schema.get("domains", {}).get("geometry_inputs", {})
    resource_required_for = set(geometry_spec.get("resource_binding_required_for", ()))
    for row in tables.get("axis_vertices", ()):
        axis_rows[row.get("product_key")].append(row)
    for index, row in enumerate(tables.get("geometry_inputs", ())):
        product_key = row.get("product_key")
        rows_by_product[product_key].append(row)
        dot = sum(
            float(row.get(axis, 0.0)) * float(row.get(ref, 0.0))
            for axis, ref in zip(
                ("axis_x", "axis_y", "axis_z"),
                ("ref_direction_x", "ref_direction_y", "ref_direction_z"),
            )
        )
        if abs(dot) > float(schema.get("orthogonal_vector_tolerance", 1e-6)):
            add("FRAME_AXES_NOT_ORTHOGONAL", f"geometry_inputs[{index}]", f"axis/ref dot product is {dot}")
        referenced_ports = [row.get(f"port_{number}_key") for number in range(1, 5)]
        referenced_ports = [key for key in referenced_ports if key is not None]
        if len(referenced_ports) != len(set(referenced_ports)):
            add("GEOMETRY_PORT_DUPLICATE", f"geometry_inputs[{index}]", "geometry input repeats a port")
        for port_key in referenced_ports:
            if port_key in ports and ports[port_key].get("owner_product_key") != product_key:
                add("GEOMETRY_PORT_WRONG_OWNER", f"geometry_inputs[{index}]", "geometry input may reference only owner-product ports")

        kind = row.get("geometry_kind")
        if kind in resource_required_for and (
            not _non_empty(row.get("resource_key"))
            or not _non_empty(row.get("resource_digest"))
        ):
            add(
                "GEOMETRY_RESOURCE_BINDING_MISSING",
                f"geometry_inputs[{index}]",
                f"{kind} requires an exact approved resource key and digest",
            )
        if kind == "NO_GEOMETRY":
            if row.get("representation_role") != "NONE":
                add(
                    "NO_GEOMETRY_ROLE_INVALID",
                    f"geometry_inputs[{index}]",
                    "no-geometry input must declare representation role NONE",
                )
            forbidden_fields = (
                "resource_key",
                "resource_digest",
                "outer_diameter_m",
                "inner_diameter_m",
                "trim_start_m",
                "trim_end_m",
                "length_m",
                "width_m",
                "height_m",
                "radius_m",
                "scale_x",
                "scale_y",
                "scale_z",
            )
            if any(row.get(field) is not None for field in forbidden_fields):
                add(
                    "NO_GEOMETRY_PARAMETER_FORBIDDEN",
                    f"geometry_inputs[{index}]",
                    "no-geometry input cannot carry resource, OD, trim, dimension, radius or scale values",
                )
            owner_ports = {
                key for key, port in ports.items() if port.get("owner_product_key") == product_key
            }
            if not referenced_ports or set(referenced_ports) != owner_ports:
                add(
                    "NO_GEOMETRY_PORT_BINDING_INVALID",
                    f"geometry_inputs[{index}]",
                    "no-geometry input must explicitly bind every owner port",
                )
        elif kind == "AXIS_ONLY":
            if len(axis_rows[product_key]) < 2:
                add(
                    "AXIS_ONLY_INPUT_INCOMPLETE",
                    f"geometry_inputs[{index}]",
                    "axis-only realization requires at least two vertices",
                )
            if len(referenced_ports) != 2:
                add(
                    "AXIS_ONLY_PORTS_INVALID",
                    f"geometry_inputs[{index}]",
                    "axis-only realization requires exactly two interface ports",
                )
            if row.get("outer_diameter_m") is not None:
                add(
                    "AXIS_ONLY_DIAMETER_FORBIDDEN",
                    f"geometry_inputs[{index}]",
                    "axis-only realization must not invent an outer diameter",
                )
            if row.get("representation_role") != "AXIS":
                add(
                    "AXIS_ONLY_ROLE_INVALID",
                    f"geometry_inputs[{index}]",
                    "axis-only realization must publish only the Axis role",
                )
        elif kind == "AXIS_SWEEP":
            if len(axis_rows[product_key]) < 2 or not _finite_number(row.get("outer_diameter_m")):
                add("AXIS_SWEEP_INPUT_INCOMPLETE", f"geometry_inputs[{index}]", "axis sweep requires at least two vertices and positive OD")
            if len(referenced_ports) != 2:
                add("AXIS_SWEEP_PORTS_INVALID", f"geometry_inputs[{index}]", "axis sweep requires exactly two interface ports")
        elif kind == "PARAMETRIC_FITTING" and not 2 <= len(referenced_ports) <= 4:
            add("FITTING_PORTS_INVALID", f"geometry_inputs[{index}]", "parametric fitting requires two through four ports")
        elif kind == "TEMPLATE_MESH":
            if not all(_finite_number(row.get(field)) and row[field] > 0 for field in ("scale_x", "scale_y", "scale_z")):
                add("TEMPLATE_SCALE_INVALID", f"geometry_inputs[{index}]", "template mesh requires positive XYZ scales")
        elif kind == "SIMPLE_BOX" and not all(
            _finite_number(row.get(field)) and row[field] > 0
            for field in ("length_m", "width_m", "height_m")
        ):
            add("BOX_DIMENSIONS_INVALID", f"geometry_inputs[{index}]", "simple box requires positive length, width and height")
        elif kind == "METER_MESH":
            if len(referenced_ports) != 2 or not all(
                _finite_number(row.get(field)) and row[field] > 0
                for field in ("length_m", "width_m", "height_m")
            ):
                add(
                    "METER_INPUT_INCOMPLETE",
                    f"geometry_inputs[{index}]",
                    "meter mesh requires exactly two ports and positive length, width and height",
                )
        elif kind == "SERVICE_SADDLE" and len(referenced_ports) != 3:
            add("SERVICE_SADDLE_PORTS_INVALID", f"geometry_inputs[{index}]", "service saddle requires two host ports and one branch port")
        elif kind == "SPHERE" and (not _finite_number(row.get("radius_m")) or row["radius_m"] <= 0):
            add("SPHERE_RADIUS_INVALID", f"geometry_inputs[{index}]", "sphere requires positive radius")

    required_kind = {
        "ASSEMBLY_ONLY": "FRAME_ONLY",
        "REPRESENTATION_SUPPRESSED": "NO_GEOMETRY",
        "AXIS_ONLY": "AXIS_ONLY",
        "LINEAR_AXIS_SWEEP": "AXIS_SWEEP",
        "PARAMETRIC_FITTING": "PARAMETRIC_FITTING",
        "TEMPLATE_COMPOSITE": "TEMPLATE_MESH",
        "SIMPLE_BOX": "SIMPLE_BOX",
        "METER_MESH": "METER_MESH",
        "SERVICE_SADDLE": "SERVICE_SADDLE",
        "SPHERE": "SPHERE",
    }
    for product in tables.get("products", ()):
        key = product.get("product_key")
        kinds = {row.get("geometry_kind") for row in rows_by_product[key]}
        expected = required_kind.get(product.get("geometry_strategy"))
        if expected not in kinds:
            add("GEOMETRY_STRATEGY_INPUT_MISSING", f"products.{key}", f"strategy requires geometry kind {expected}")
        if product.get("geometry_strategy") == "REPRESENTATION_SUPPRESSED":
            if len(rows_by_product[key]) != 1 or kinds != {"NO_GEOMETRY"}:
                add(
                    "SUPPRESSED_GEOMETRY_CARDINALITY",
                    f"products.{key}",
                    "representation-suppressed product requires exactly one NO_GEOMETRY row",
                )
            if axis_rows[key]:
                add(
                    "SUPPRESSED_AXIS_FORBIDDEN",
                    f"products.{key}",
                    "representation-suppressed product cannot publish Axis vertices",
                )
        if product.get("geometry_strategy") in {"AXIS_ONLY", "LINEAR_AXIS_SWEEP"}:
            ordered = sorted(axis_rows[key], key=lambda row: row.get("sequence_ordinal", -1))
            expected_geometry_kind = (
                "AXIS_ONLY"
                if product.get("geometry_strategy") == "AXIS_ONLY"
                else "AXIS_SWEEP"
            )
            geometry = next(
                (
                    row
                    for row in rows_by_product[key]
                    if row.get("geometry_kind") == expected_geometry_kind
                ),
                None,
            )
            if len(ordered) >= 2 and geometry is not None:
                tolerance = float(schema.get("connection_position_tolerance_m", 1e-6))
                for vertex, port_field in ((ordered[0], "port_1_key"), (ordered[-1], "port_2_key")):
                    port = ports.get(geometry.get(port_field))
                    if port and math.dist(
                        (vertex["x_m"], vertex["y_m"], vertex["z_m"]),
                        (port["x_m"], port["y_m"], port["z_m"]),
                    ) > tolerance:
                        add("AXIS_ENDPOINT_PORT_MISMATCH", f"products.{key}", "axis endpoint and declared port do not coincide")


def _validate_exact_port_contract(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    counts: Counter[str] = Counter()
    ordinals: defaultdict[str, list[int]] = defaultdict(list)
    leaf_system_by_port: defaultdict[str, set[str]] = defaultdict(set)
    systems = {row.get("system_key"): row.get("parent_system_key") for row in tables.get("systems", ())}
    non_leaf = {parent for parent in systems.values() if parent is not None}
    leaf_systems = set(systems) - non_leaf
    for row in tables.get("system_members", ()):
        if row.get("member_kind") == "port" and row.get("system_key") in leaf_systems:
            leaf_system_by_port[row.get("member_key")].add(row.get("system_key"))
    for row in tables.get("ports", ()):
        owner = row.get("owner_product_key")
        counts[owner] += 1
        ordinal = row.get("sequence_ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            ordinals[owner].append(ordinal)
        if leaf_system_by_port[row.get("port_key")] != {row.get("system_key")}:
            add("PORT_SYSTEM_MISMATCH", f"ports.{row.get('port_key')}", "port system_key must equal its one leaf-system membership")
    for product in tables.get("products", ()):
        key = product.get("product_key")
        requirement = product.get("port_requirement")
        count = counts[key]
        if requirement == "none" and count != 0:
            add("PRODUCT_PORTS_NOT_APPLICABLE", f"products.{key}", "product must have no flow port")
        if requirement == "one_or_more" and count < 1:
            add("PRODUCT_PORTS_MISSING", f"products.{key}", "product requires at least one port")
        if requirement == "two_or_more" and count < 2:
            add("PRODUCT_PORTS_MISSING", f"products.{key}", "product requires at least two ports")
    for owner, values in ordinals.items():
        if sorted(values) != list(range(len(values))):
            add("PORT_ORDINAL_GAP", f"ports.{owner}", "port ordinals must be contiguous from zero")


def _validate_exact_connection_geometry(
    tables: Mapping[str, Sequence[Mapping[str, Any]]], schema: Mapping[str, Any], add: Any
) -> None:
    ports = {row.get("port_key"): row for row in tables.get("ports", ())}
    products = {row.get("product_key"): row for row in tables.get("products", ())}
    position_tolerance = float(schema.get("connection_position_tolerance_m", 1e-6))
    dot_maximum = float(schema.get("connection_direction_dot_maximum", -0.999999))

    def assembly_root(product_key: Any) -> Any:
        current = product_key
        while current in products and products[current].get("assembly_parent_product_key") is not None:
            current = products[current]["assembly_parent_product_key"]
        return current

    for index, row in enumerate(tables.get("connections", ())):
        left = ports.get(row.get("port_a_key"))
        right = ports.get(row.get("port_b_key"))
        if left is None or right is None:
            continue
        left_position = (left["x_m"], left["y_m"], left["z_m"])
        right_position = (right["x_m"], right["y_m"], right["z_m"])
        if math.dist(left_position, right_position) > position_tolerance:
            add("CONNECTED_PORT_POSITION_MISMATCH", f"connections[{index}]", "connected world positions exceed exact tolerance")
        direction_dot = sum(left[field] * right[field] for field in ("direction_x", "direction_y", "direction_z"))
        if direction_dot > dot_maximum:
            add("CONNECTED_PORT_DIRECTION_MISMATCH", f"connections[{index}]", f"outward directions have dot product {direction_dot}")
        if row.get("connection_scope") == "accepted_assembly_internal":
            left_root = assembly_root(left.get("owner_product_key"))
            right_root = assembly_root(right.get("owner_product_key"))
            if left_root != right_root or products.get(left_root, {}).get("assembly_role") != "assembly_root":
                add("ASSEMBLY_INTERNAL_SCOPE_INVALID", f"connections[{index}]", "internal connection owners must belong to the same assembly root")


def _validate_exact_material_contract(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    for index, row in enumerate(tables.get("materials_styles", ())):
        if row.get("row_kind") != "definition":
            continue
        required = (
            "material_code", "material_name", "material_category", "appearance_code",
            "appearance_name", "red", "green", "blue", "transparency", "roughness",
            "metallic", "reflectance_method",
        )
        if any(not _non_empty(row.get(field)) for field in required[:5]) or any(
            not _finite_number(row.get(field)) for field in required[5:-1]
        ) or not _non_empty(row.get("reflectance_method")):
            add("MATERIAL_REALIZATION_INCOMPLETE", f"materials_styles[{index}]", "material definition requires exact material, color and surface values")


def _validate_exact_property_contract(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    value_fields = {
        "string": "value_string",
        "number": "value_number",
        "boolean": "value_boolean",
        "identifier": "value_identifier",
    }
    all_fields = tuple(value_fields.values())
    for index, row in enumerate(tables.get("property_values", ())):
        populated = {field for field in all_fields if row.get(field) is not None}
        expected = value_fields.get(row.get("value_kind"))
        if row.get("status") == "known":
            if populated != {expected}:
                add("PROPERTY_TYPED_VALUE_INVALID", f"property_values[{index}]", "known property requires exactly its declared typed value")
        elif populated:
            add("PROPERTY_UNKNOWN_HAS_VALUE", f"property_values[{index}]", "unknown/conflict property cannot assert a definitive value")
        if row.get("status") in {"unknown", "conflict"} and not _non_empty(row.get("details")):
            add("PROPERTY_UNKNOWN_DETAILS_MISSING", f"property_values[{index}]", "unknown/conflict property requires details")


def _validate_exact_spatial_contract(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    nodes = {row.get("spatial_node_key"): row for row in tables.get("spatial_nodes", ())}
    projects = [row for row in nodes.values() if row.get("node_kind") == "PROJECT"]
    if len(projects) != 1 or projects[0].get("parent_spatial_node_key") is not None:
        add("PROJECT_ROOT_INVALID", "spatial_nodes", "exactly one root PROJECT is required")
    for index, row in enumerate(tables.get("spatial_nodes", ())):
        if row.get("node_kind") == "SITE":
            parent = nodes.get(row.get("parent_spatial_node_key"))
            if parent is None or parent.get("node_kind") != "PROJECT":
                add("SITE_PARENT_INVALID", f"spatial_nodes[{index}]", "SITE must be a direct child of PROJECT")
    for index, row in enumerate(tables.get("spatial_assignments", ())):
        expected = {
            "spatial_containment": {"IfcRelContainedInSpatialStructure", "IfcRelReferencedInSpatialStructure"},
            "group_membership": {"IfcRelAssignsToGroup"},
        }.get(row.get("assignment_kind"), set())
        if row.get("relation_kind") not in expected:
            add("SPATIAL_RELATION_KIND_INVALID", f"spatial_assignments[{index}]", "relation_kind must explicitly implement assignment_kind")


def _validate_exact_piperun_contract(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    memberships: Counter[str] = Counter(
        row.get("product_key")
        for row in tables.get("piperun_members", ())
        if row.get("row_kind") == "member"
    )
    for product in tables.get("products", ()):
        key = product.get("product_key")
        count = memberships[key]
        requirement = product.get("piperun_membership_requirement")
        if requirement == "required" and count != 1:
            add("PRODUCT_PIPERUN_CARDINALITY", f"products.{key}", f"requires exactly one PipeRun membership, found {count}")
        if requirement == "not_applicable" and count != 0:
            add("PRODUCT_PIPERUN_NOT_APPLICABLE", f"products.{key}", "product must not be a PipeRun member")
    for index, row in enumerate(tables.get("piperun_members", ())):
        if row.get("row_kind") == "run_definition":
            if row.get("requires_split") and not _non_empty(row.get("split_reason")):
                add("PIPERUN_SPLIT_REASON_MISSING", f"piperun_members[{index}]", "split-required run needs an explicit reason")
            if not row.get("requires_split") and row.get("split_reason") is not None:
                add("PIPERUN_UNNEEDED_SPLIT_REASON", f"piperun_members[{index}]", "unsplit run must keep split_reason null")


def _validate_exact_unresolved_contract(tables: Mapping[str, Sequence[Mapping[str, Any]]], add: Any) -> None:
    subject_domains = {
        "source": ("source_dispositions", "source_entity_key"),
        "product": ("products", "product_key"),
        "port": ("ports", "port_key"),
        "system": ("systems", "system_key"),
        "spatial_node": ("spatial_nodes", "spatial_node_key"),
        "piperun": ("piperun_members", "piperun_key"),
    }
    keys = {
        kind: {row.get(field) for row in tables.get(domain, ())}
        for kind, (domain, field) in subject_domains.items()
    }
    for index, row in enumerate(tables.get("unresolved_items", ())):
        kind = row.get("subject_kind")
        if kind != "package" and row.get("subject_key") not in keys.get(kind, set()):
            add("UNRESOLVED_SUBJECT_UNKNOWN", f"unresolved_items[{index}]", "unresolved subject does not exist")
        if row.get("resolution_status") == "resolved" and not _non_empty(row.get("authority_key")):
            add("RESOLVED_AUTHORITY_MISSING", f"unresolved_items[{index}]", "resolved item requires an authority key")


def _semantic_rule(schema: Mapping[str, Any], rule_id: str) -> Mapping[str, Any]:
    constraints = schema.get("semantic_constraints")
    if not isinstance(constraints, Mapping):
        return {}
    rules = constraints.get("rules")
    if not isinstance(rules, list):
        return {}
    return next(
        (
            rule
            for rule in rules
            if isinstance(rule, Mapping) and rule.get("id") == rule_id
        ),
        {},
    )


def _validate_exact_ifc_enum_contract(
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    schema: Mapping[str, Any],
    add: Any,
) -> None:
    """Apply the public experiment IFC enum allowlists before IfcOpenShell."""

    requirements = _semantic_rule(schema, "ifc.enumerated_attributes").get(
        "requirements", {}
    )
    if not isinstance(requirements, Mapping):
        return
    product_values = requirements.get("product_predefined_type_by_ifc_class", {})
    if isinstance(product_values, Mapping):
        for index, row in enumerate(tables.get("products", ())):
            allowed = product_values.get(row.get("ifc_class"))
            if isinstance(allowed, list) and row.get("predefined_type") not in allowed:
                add(
                    "IFC_PRODUCT_PREDEFINED_TYPE_INVALID",
                    f"products[{index}].predefined_type",
                    "predefined_type is not valid for the submitted IFC product class",
                )
    port_predefined = requirements.get("port_predefined_type")
    port_system = requirements.get("port_system_type")
    for index, row in enumerate(tables.get("ports", ())):
        if isinstance(port_predefined, list) and row.get("predefined_type") not in port_predefined:
            add(
                "IFC_PORT_PREDEFINED_TYPE_INVALID",
                f"ports[{index}].predefined_type",
                "port predefined_type is outside the public experiment allowlist",
            )
        if isinstance(port_system, list) and row.get("system_type") not in port_system:
            add(
                "IFC_PORT_SYSTEM_TYPE_INVALID",
                f"ports[{index}].system_type",
                "port system_type is outside the public experiment allowlist",
            )
    system_predefined = requirements.get("system_predefined_type")
    for index, row in enumerate(tables.get("systems", ())):
        if isinstance(system_predefined, list) and row.get("predefined_type") not in system_predefined:
            add(
                "IFC_SYSTEM_PREDEFINED_TYPE_INVALID",
                f"systems[{index}].predefined_type",
                "system predefined_type is outside the public experiment allowlist",
            )


def _vector3(row: Mapping[str, Any], prefix: str) -> tuple[float, float, float] | None:
    if prefix in {"axis", "ref_direction", "local_direction", "direction"}:
        names = tuple(f"{prefix}_{axis}" for axis in "xyz")
    elif prefix:
        names = tuple(f"{prefix}_{axis}_m" for axis in "xyz")
    else:
        names = tuple(f"{axis}_m" for axis in "xyz")
    values = tuple(row.get(name) for name in names)
    if not all(_finite_number(value) for value in values):
        return None
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def _cross3(
    left: Sequence[float], right: Sequence[float]
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot3(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _frame_transform(
    geometry: Mapping[str, Any],
    local: Sequence[float],
    *,
    direction: bool,
) -> tuple[float, float, float] | None:
    axis = _vector3(geometry, "axis")
    ref = _vector3(geometry, "ref_direction")
    origin = _vector3(geometry, "origin")
    if axis is None or ref is None or origin is None:
        return None
    side = _cross3(axis, ref)
    result = tuple(
        ref[index] * local[0] + side[index] * local[1] + axis[index] * local[2]
        for index in range(3)
    )
    if direction:
        return result
    return tuple(result[index] + origin[index] for index in range(3))


def _polyline_length(rows: Sequence[Mapping[str, Any]]) -> float | None:
    ordered = sorted(rows, key=lambda row: row.get("sequence_ordinal", -1))
    if len(ordered) < 2:
        return None
    points = [_vector3(row, "") for row in ordered]
    if any(point is None for point in points):
        return None
    return sum(
        math.dist(left, right)
        for left, right in zip(points, points[1:])
    )


def _ray_intersection(
    first_point: tuple[float, float, float],
    first_direction: tuple[float, float, float],
    second_point: tuple[float, float, float],
    second_direction: tuple[float, float, float],
    tolerance: float,
) -> tuple[float, float, float] | None:
    delta = tuple(first_point[index] - second_point[index] for index in range(3))
    a = _dot3(first_direction, first_direction)
    b = _dot3(first_direction, second_direction)
    c = _dot3(second_direction, second_direction)
    d = _dot3(first_direction, delta)
    e = _dot3(second_direction, delta)
    denominator = a * c - b * b
    if abs(denominator) <= 1.0e-12:
        return None
    first_distance = (b * e - c * d) / denominator
    second_distance = (a * e - b * d) / denominator
    if first_distance < -tolerance or second_distance < -tolerance:
        return None
    first = tuple(
        first_point[index] + first_direction[index] * first_distance
        for index in range(3)
    )
    second = tuple(
        second_point[index] + second_direction[index] * second_distance
        for index in range(3)
    )
    if math.dist(first, second) > tolerance:
        return None
    return tuple((first[index] + second[index]) / 2.0 for index in range(3))


def _catalog_resources(
    component_catalog: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if component_catalog is None:
        return {}
    resources = component_catalog.get("resources")
    if not isinstance(resources, list):
        return {}
    return {
        str(row["resource_key"]): row
        for row in resources
        if isinstance(row, Mapping) and _non_empty(row.get("resource_key"))
    }


def _declared_geometry_ports(
    geometry: Mapping[str, Any],
    ports: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [
        ports[key]
        for number in range(1, 5)
        if (key := geometry.get(f"port_{number}_key")) in ports
    ]


def _validate_exact_package_realization_contract(
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    schema: Mapping[str, Any],
    component_catalog: Mapping[str, Any] | None,
    add: Any,
) -> None:
    """Close every Package predicate used by the hidden experiment compiler."""

    tolerance = float(schema.get("connection_position_tolerance_m", 1e-6))
    products = {row.get("product_key"): row for row in tables.get("products", ())}
    ports = {row.get("port_key"): row for row in tables.get("ports", ())}
    geometry_by_product: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    axes_by_product: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in tables.get("geometry_inputs", ()):
        geometry_by_product[row.get("product_key")].append(row)
    for row in tables.get("axis_vertices", ()):
        axes_by_product[row.get("product_key")].append(row)

    for product_key, product in products.items():
        geometries = geometry_by_product[product_key]
        strategy = product.get("geometry_strategy")
        valid = (
            bool(geometries)
            if strategy == "TEMPLATE_COMPOSITE"
            else len(geometries) == 1
        )
        if not valid:
            add(
                "GEOMETRY_INPUT_CARDINALITY_INVALID",
                f"products.{product_key}",
                "strategy requires exactly one geometry row except TEMPLATE_COMPOSITE, which requires one or more",
            )

    for index, geometry in enumerate(tables.get("geometry_inputs", ())):
        product_key = geometry.get("product_key")
        if geometry.get("geometry_kind") == "AXIS_SWEEP":
            length = _polyline_length(axes_by_product[product_key])
            trim_start = geometry.get("trim_start_m") or 0.0
            trim_end = geometry.get("trim_end_m") or 0.0
            if (
                length is not None
                and _finite_number(trim_start)
                and _finite_number(trim_end)
                and float(trim_start) + float(trim_end) >= length - tolerance
            ):
                add(
                    "AXIS_TRIM_EXCEEDS_LENGTH",
                    f"geometry_inputs[{index}]",
                    "trim_start_m plus trim_end_m must be less than the submitted Axis polyline length",
                )

    root_gas = [
        row
        for row in tables.get("systems", ())
        if row.get("parent_system_key") is None and row.get("predefined_type") == "GAS"
    ]
    if len(root_gas) != 1:
        add(
            "SITE_SERVICE_SYSTEM_CARDINALITY",
            "systems",
            "the public site-service selector must match exactly one root GAS system",
        )
    spatial = {
        row.get("spatial_node_key"): row for row in tables.get("spatial_nodes", ())
    }
    engineering_sites = [
        row
        for row in spatial.values()
        if row.get("node_kind") == "SITE"
        and row.get("object_type") is None
        and spatial.get(row.get("parent_spatial_node_key"), {}).get("node_kind")
        == "PROJECT"
    ]
    if len(engineering_sites) != 1:
        add(
            "SITE_SERVICE_SITE_CARDINALITY",
            "spatial_nodes",
            "the public site-service selector must match exactly one engineering SITE",
        )

    for port_key, port in ports.items():
        geometries = sorted(
            geometry_by_product[port.get("owner_product_key")],
            key=lambda row: row.get("sequence_ordinal", -1),
        )
        if not geometries:
            continue
        geometry = geometries[0]
        local_position = _vector3(port, "local")
        world_position = _vector3(port, "")
        local_direction = _vector3(port, "local_direction")
        world_direction = _vector3(port, "direction")
        expected_position = (
            _frame_transform(geometry, local_position, direction=False)
            if local_position is not None
            else None
        )
        expected_direction = (
            _frame_transform(geometry, local_direction, direction=True)
            if local_direction is not None
            else None
        )
        if (
            expected_position is not None
            and world_position is not None
            and math.dist(expected_position, world_position) > tolerance
        ):
            add(
                "PORT_LOCAL_WORLD_POSITION_MISMATCH",
                f"ports.{port_key}",
                "world position must equal the owner geometry frame applied to local position",
            )
        if (
            expected_direction is not None
            and world_direction is not None
            and math.dist(expected_direction, world_direction) > tolerance
        ):
            add(
                "PORT_LOCAL_WORLD_DIRECTION_MISMATCH",
                f"ports.{port_key}",
                "world direction must equal the owner geometry frame applied to local direction",
            )

    resources = _catalog_resources(component_catalog)
    if component_catalog is not None:
        _validate_exact_catalog_resources(
            tables,
            schema,
            component_catalog,
            resources,
            products,
            ports,
            add,
        )

    represented_products = {
        key
        for key, product in products.items()
        if product.get("geometry_strategy")
        not in {"ASSEMBLY_ONLY", "REPRESENTATION_SUPPRESSED"}
    }
    binding_products: defaultdict[str, set[str]] = defaultdict(set)
    for row in tables.get("materials_styles", ()):
        if row.get("row_kind") == "binding" and row.get("owner_key") in represented_products:
            binding_products[row.get("definition_key")].add(row.get("owner_key"))
    for index, row in enumerate(tables.get("materials_styles", ())):
        if row.get("row_kind") == "layer_binding" and not binding_products[row.get("definition_key")]:
            add(
                "LAYER_BINDING_TARGET_EMPTY",
                f"materials_styles[{index}]",
                "layer binding definition must select at least one product with representation items",
            )


def _validate_exact_catalog_resources(
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    schema: Mapping[str, Any],
    component_catalog: Mapping[str, Any],
    resources: Mapping[str, Mapping[str, Any]],
    products: Mapping[str, Mapping[str, Any]],
    ports: Mapping[str, Mapping[str, Any]],
    add: Any,
) -> None:
    tolerance = float(
        component_catalog.get("realization_contract", {}).get(
            "comparison_tolerance_m",
            schema.get("connection_position_tolerance_m", 1e-6),
        )
    )
    compatibility = {
        "PARAMETRIC_FITTING": {
            "PARAMETRIC_BEND",
            "PARAMETRIC_TEE",
            "PARAMETRIC_CROSS",
            "PARAMETRIC_STRAIGHT_CONNECTOR",
        },
        "SERVICE_SADDLE": {"PARAMETRIC_SERVICE_SADDLE"},
        "METER_MESH": {"PARAMETRIC_GAS_METER_BODY"},
        "TEMPLATE_COMPOSITE": {
            "VALVE_BODY_WITHOUT_CONNECTING_BEND",
            "REGULATOR_FUNCTIONAL_BODY",
        },
    }
    bend_contract = resources.get("PARAMETRIC_BEND", {}).get("realization_contract", {})
    for index, geometry in enumerate(tables.get("geometry_inputs", ())):
        resource_key = geometry.get("resource_key")
        if resource_key is None:
            continue
        resource = resources.get(str(resource_key))
        if resource is None or geometry.get("resource_digest") != resource.get("resource_digest"):
            add(
                "RESOURCE_BINDING_UNKNOWN",
                f"geometry_inputs[{index}]",
                "resource key and digest must match one public catalog declaration",
            )
            continue
        product = products.get(geometry.get("product_key"), {})
        strategy = product.get("geometry_strategy")
        if resource_key not in compatibility.get(str(strategy), set()):
            add(
                "RESOURCE_STRATEGY_MISMATCH",
                f"geometry_inputs[{index}]",
                "resource is not allowed for the product geometry strategy",
            )
        if product.get("component_resource_key") != resource_key:
            add(
                "PRODUCT_RESOURCE_BINDING_MISMATCH",
                f"products.{geometry.get('product_key')}",
                "product component_resource_key must equal its geometry resource_key",
            )
        declared = _declared_geometry_ports(geometry, ports)
        contract = resource.get("realization_contract")
        if not isinstance(contract, Mapping):
            continue
        expected_count = contract.get("declared_port_count")
        positive_od = all(
            _finite_number(port.get("outer_diameter_m"))
            and float(port["outer_diameter_m"]) > 0.0
            for port in declared
        )
        if expected_count != len(declared) or not positive_od:
            add(
                "RESOURCE_PORT_CARDINALITY_INVALID",
                f"geometry_inputs[{index}]",
                "resource port count and positive interface diameters must satisfy its public catalog contract",
            )
            continue
        if resource_key == "PARAMETRIC_BEND":
            _validate_exact_bend_resource(geometry, declared, contract, tolerance, index, add)
        elif resource_key == "PARAMETRIC_TEE":
            _validate_exact_tee_resource(
                geometry, declared, contract, bend_contract, tolerance, index, add
            )
        elif resource_key == "PARAMETRIC_STRAIGHT_CONNECTOR":
            points = [_vector3(port, "local") for port in declared]
            if None in points or math.dist(points[0], points[1]) <= tolerance:
                add(
                    "STRAIGHT_CONNECTOR_INTERFACE_COLLAPSED",
                    f"geometry_inputs[{index}]",
                    "straight connector interface points must be distinct",
                )
        elif resource_key == "PARAMETRIC_CROSS":
            invalid = False
            for port in declared:
                point = _vector3(port, "local")
                direction = _vector3(port, "local_direction")
                length = math.dist(point, (0.0, 0.0, 0.0)) if point else 0.0
                if point is None or direction is None or length <= tolerance:
                    invalid = True
                    break
                radial = tuple(value / length for value in point)
                if math.dist(radial, direction) > tolerance:
                    invalid = True
                    break
            if invalid:
                add(
                    "CROSS_INTERFACE_NOT_RADIAL",
                    f"geometry_inputs[{index}]",
                    "cross interfaces must be non-collapsed and point radially outward",
                )
        elif resource_key == "PARAMETRIC_SERVICE_SADDLE":
            _validate_exact_saddle_resource(declared, tolerance, index, add)
        elif resource_key == "PARAMETRIC_GAS_METER_BODY":
            _validate_exact_meter_resource(geometry, declared, tolerance, index, add)


def _validate_exact_bend_resource(
    geometry: Mapping[str, Any],
    declared: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
    tolerance: float,
    index: int,
    add: Any,
) -> None:
    points = tuple(_vector3(port, "local") for port in declared)
    outward = tuple(_vector3(port, "local_direction") for port in declared)
    if any(value is None for value in (*points, *outward)):
        add("BEND_INTERFACE_RAYS_INVALID", f"geometry_inputs[{index}]", "bend interface rays must be finite")
        return
    angle = math.degrees(math.acos(max(-1.0, min(1.0, _dot3(outward[0], outward[1])))))
    if angle + tolerance < float(contract.get("minimum_angle_degrees", 0.0)):
        add("BEND_ANGLE_BELOW_MINIMUM", f"geometry_inputs[{index}]", "bend angle is below the public resource minimum")
        return
    inward = tuple(tuple(-value for value in direction) for direction in outward)
    junction = _ray_intersection(points[0], inward[0], points[1], inward[1], tolerance)
    if junction is None:
        add("BEND_INTERFACE_RAYS_INVALID", f"geometry_inputs[{index}]", "bend inward interface rays must meet ahead at one junction")
        return
    try:
        plan = plan_elbow(
            inward_directions=inward,
            outer_diameters_m=tuple(float(port["outer_diameter_m"]) for port in declared),
            realization_contract=contract,
        )
    except (KeyError, TypeError, ValueError, ResourceGeometryError):
        add("BEND_TAKEOFF_INVALID", f"geometry_inputs[{index}]", "bend inputs cannot satisfy the public takeoff equation")
        return
    for point, direction, expected in zip(points, outward, plan.ports, strict=True):
        offset = tuple(point[axis] - junction[axis] for axis in range(3))
        distance = math.sqrt(_dot3(offset, offset))
        minimum = math.dist(expected.point_m, (0.0, 0.0, 0.0))
        if distance <= tolerance or distance + tolerance < minimum:
            add("BEND_TAKEOFF_INVALID", f"geometry_inputs[{index}]", "bend interface is shorter than its public minimum takeoff")
            return
        radial = tuple(value / distance for value in offset)
        if math.dist(radial, direction) > tolerance:
            add("BEND_TAKEOFF_INVALID", f"geometry_inputs[{index}]", "bend interface leaves its approved junction ray")
            return


def _validate_exact_tee_resource(
    geometry: Mapping[str, Any],
    declared: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
    bend_contract: Any,
    tolerance: float,
    index: int,
    add: Any,
) -> None:
    outward = tuple(_vector3(port, "local_direction") for port in declared)
    points = tuple(_vector3(port, "local") for port in declared)
    if any(value is None for value in (*points, *outward)) or not isinstance(bend_contract, Mapping):
        add("TEE_INTERFACE_GEOMETRY_INVALID", f"geometry_inputs[{index}]", "tee interface frame or public dependency is incomplete")
        return
    try:
        plan = plan_tee(
            inward_directions=tuple(tuple(-value for value in direction) for direction in outward),
            outer_diameters_m=tuple(float(port["outer_diameter_m"]) for port in declared),
            realization_contract=contract,
            bend_realization_contract=bend_contract,
        )
    except (KeyError, TypeError, ValueError, ResourceGeometryError):
        add("TEE_INTERFACE_GEOMETRY_INVALID", f"geometry_inputs[{index}]", "tee inputs cannot satisfy the public canonical plan")
        return
    for point, direction, port, expected in zip(points, outward, declared, plan.ports, strict=True):
        if (
            math.dist(point, expected.point_m) > tolerance
            or math.dist(direction, expected.outward_direction) > tolerance
            or abs(float(port["outer_diameter_m"]) - expected.outer_diameter_m) > tolerance
        ):
            add("TEE_INTERFACE_GEOMETRY_INVALID", f"geometry_inputs[{index}]", "tee interfaces must equal the public canonical plan")
            return


def _validate_exact_saddle_resource(
    declared: Sequence[Mapping[str, Any]], tolerance: float, index: int, add: Any
) -> None:
    by_role = {str(port.get("port_role")): port for port in declared}
    if set(by_role) != {"MAIN_A", "MAIN_B", "BRANCH"}:
        add("SERVICE_SADDLE_ROLE_INVALID", f"geometry_inputs[{index}]", "service saddle requires exactly MAIN_A, MAIN_B and BRANCH")
        return
    main_a, main_b, branch = by_role["MAIN_A"], by_role["MAIN_B"], by_role["BRANCH"]
    directions = (_vector3(main_a, "local_direction"), _vector3(main_b, "local_direction"))
    if None in directions or _dot3(directions[0], directions[1]) > -1.0 + tolerance:
        add("SERVICE_SADDLE_MAIN_DIRECTION_INVALID", f"geometry_inputs[{index}]", "service saddle main directions must be opposite")
    if abs(float(main_a["outer_diameter_m"]) - float(main_b["outer_diameter_m"])) > tolerance:
        add("SERVICE_SADDLE_MAIN_DIAMETER_MISMATCH", f"geometry_inputs[{index}]", "service saddle main diameters must match")
    points = tuple(_vector3(port, "local") for port in (main_a, main_b, branch))
    if None not in points:
        junction = tuple((points[0][axis] + points[1][axis]) / 2.0 for axis in range(3))
        if math.dist(junction, points[2]) <= tolerance:
            add("SERVICE_SADDLE_BRANCH_COLLAPSED", f"geometry_inputs[{index}]", "service saddle branch must not collapse into the main junction")


def _validate_exact_meter_resource(
    geometry: Mapping[str, Any],
    declared: Sequence[Mapping[str, Any]],
    tolerance: float,
    index: int,
    add: Any,
) -> None:
    sides: set[int] = set()
    for port in declared:
        point = _vector3(port, "local")
        direction = _vector3(port, "local_direction")
        if (
            point is None
            or direction is None
            or abs(point[1]) > tolerance
            or abs(point[2]) > tolerance
            or abs(direction[1]) > tolerance
            or abs(direction[2]) > tolerance
            or abs(abs(direction[0]) - 1.0) > tolerance
            or point[0] * direction[0] <= 0.0
        ):
            add("METER_INTERFACE_FRAME_INVALID", f"geometry_inputs[{index}]", "meter interfaces must lie on the local X axis and point outward")
            return
        sides.add(1 if direction[0] > 0 else -1)
        if _finite_number(geometry.get("length_m")) and abs(point[0]) + tolerance < float(geometry["length_m"]) / 2.0:
            add("METER_INTERFACE_INSIDE_BODY", f"geometry_inputs[{index}]", "meter interface cannot lie inside the declared body")
    if sides != {-1, 1}:
        add("METER_SIDE_CARDINALITY_INVALID", f"geometry_inputs[{index}]", "meter requires one negative-X and one positive-X interface")
