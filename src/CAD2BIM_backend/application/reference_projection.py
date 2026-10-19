"""Project declared navigation fields without traversing arbitrary source or data."""

from copy import deepcopy


async def project_reference_fields(value, paths, translate):
    """Transform declared field paths through the existing reference owner."""
    result = deepcopy(value)

    async def at_path(node, path, kinds):
        key, *remaining = path
        entries = enumerate(node) if key == "*" and isinstance(node, list) else (
            ((key, node[key]),) if isinstance(node, dict) and key in node else ()
        )
        for index, item in entries:
            if remaining:
                await at_path(item, remaining, kinds)
            elif isinstance(item, str):
                node[index] = await translate(item, kinds)

    for path, kinds in paths.items():
        await at_path(result, path, kinds)
    return result


_ALL_KINDS = ("gr", "gv", "fr", "fv", "gd", "fd", "sc", "gq", "gi", "or")
_FIELDS = {
    "operation_ref": ("or",),
    "resource_alias": ("gr", "gv"), "resource_root_ref": ("gr",),
    "resource_version_alias": ("gv",), "resource_version_ref": ("gv",),
    "geometry_resource_version_alias": ("gv",), "base_resource_version_alias": ("gv",),
    "latest_version_alias": ("gv",),
    "family_alias": ("fr", "fv"), "family_root_ref": ("fr",),
    "family_version_alias": ("fv",), "family_version_ref": ("fv",),
    "base_family_version_alias": ("fv",), "parent_family_version_alias": ("fv",),
    "latest_family_version_alias": ("fv",),
    "code_locator": ("sc",), "result_code_locator": ("sc",),
    "owner_alias": _ALL_KINDS, "parent_owner_alias": _ALL_KINDS, "result_alias": _ALL_KINDS,
    "attempt_alias": ("gq",), "parent_attempt_alias": ("gq",),
    "inspection_ref": ("gi",),
    "source_version_ref": ("gv", "fv", "gd", "fd"),
    "draft_ref": ("gd", "fd"), "source_ref": ("gv", "fv", "gd", "fd"),
}


def reference_schema_projection(schema):
    """Project reference constraints and derive paths from the same machine schema."""
    projected = deepcopy(schema)
    definitions = projected.get("$defs", {}) if isinstance(projected, dict) else {}
    paths = {}

    def visit(node, path=(), references=()):
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and ref not in references:
            visit(definitions[ref.removeprefix("#/$defs/")], path, (*references, ref))
        if node.get("type") == "string" and path and path[-1] in _FIELDS:
            kinds = _FIELDS[path[-1]]
            paths[path] = kinds
            pattern = node.get("pattern")
            if isinstance(pattern, str) and "cur" in pattern:
                node["pattern"] = rf"^(cur\..+|({'|'.join(kinds)})_[A-Za-z0-9_-]{{22}})$"
        for key, child in node.get("properties", {}).items():
            visit(child, (*path, key), references)
        if "items" in node:
            visit(node["items"], (*path, "*"), references)
        for branch in ("oneOf", "anyOf"):
            for child in node.get(branch, []):
                visit(child, path, references)

    visit(projected)
    return projected, paths
