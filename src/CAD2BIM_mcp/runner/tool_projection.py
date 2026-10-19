"""Exact entry adaptation of shared public usage and complete request examples."""

from copy import deepcopy
import json

_MEMBER_TOOLS = frozenset({"ifc_geometry_resource_write", "ifc_geometry_resource_source_read", "ifc_product_family_write"})


def runner_public_request(name, request):
    value = deepcopy(request)
    value.pop("direct_session_alias", None)
    if name in _MEMBER_TOOLS:
        if "source_path" in value:
            value["path"] = value.pop("source_path")
        for item in value.get("source", {}).get("source_files", ()):
            item["path"] = item.pop("source_path")
        for item in value.get("source_changes", ()):
            item["path"] = item.pop("source_path")
        if "source_changes_json" in value:
            document = json.loads(value["source_changes_json"])
            for item in document["changes"]:
                item["path"] = item.pop("source_path")
            value["source_changes_json"] = json.dumps(document, ensure_ascii=False)
    return {"request": value}


def runner_tool_description(name, text):
    """Keep shared rules, adapting only explicit Direct entry wording/examples."""
    decoder = json.JSONDecoder()
    position, parts = 0, []
    while position < len(text):
        start = text.find("{", position)
        if start < 0:
            parts.append(text[position:])
            break
        parts.append(text[position:start])
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except ValueError:
            parts.append("{")
            position = start + 1
            continue
        parts.append(json.dumps(runner_public_request(name, value), ensure_ascii=False)
                     if isinstance(value, dict) and "direct_session_alias" in value else text[start:start + consumed])
        position = start + consumed
    projected = "".join(parts)
    projected = projected.replace("current Direct scope", "current Runner scope")
    projected = projected.replace("Add the current Direct session.", "Supply the fields inside request; identity comes from the current Runner credential.")
    projected = projected.replace("direct_session_alias plus only", "only")
    projected = projected.replace("replace direct_001 with the opened session alias", "use the request wrapper")
    if name in _MEMBER_TOOLS:
        projected = projected.replace("source_path", "path")
    return "Supply the public fields inside request. " + projected


def runner_public_document(name, document):
    """Project only complete Direct request examples, retaining business JSON."""
    if isinstance(document, list):
        return [runner_public_document(name, item) for item in document]
    if not isinstance(document, dict):
        return deepcopy(document)
    if "direct_session_alias" in document:
        return runner_public_request(name, document)
    return {key: runner_public_document(name, item) for key, item in document.items()}
