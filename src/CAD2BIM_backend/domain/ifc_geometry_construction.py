"""Private same-execution construction facts for IFC representation encoding."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

class IfcGeometryRepresentationRoute(str, Enum):
    TFS = "tfs"
    PARAMETRIC = "parametric"
    ADVANCED_BREP = "advanced_brep"


class GeometryConstructionNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    id: int = Field(ge=0)
    op: str = Field(min_length=1)
    receiver: int | None = Field(default=None, ge=0)
    args: tuple[JsonValue, ...] = ()
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    children: tuple[int, ...] = ()
    # Exact same-execution native snapshot, base64 encoded; never a live handle.
    shape_brep: str | None = None

class GeometryConstruction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    root: int = Field(ge=0)
    nodes: tuple[GeometryConstructionNode, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_closed_acyclic_construction(self):
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes) or self.root not in by_id:
            raise ValueError("construction requires unique nodes and an existing root")

        def argument_refs(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    ref = value["$ref"]
                    if isinstance(ref, bool) or not isinstance(ref, int):
                        raise ValueError("construction handle reference must be an integer")
                    yield ref
                else:
                    for item in value.values():
                        yield from argument_refs(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    yield from argument_refs(item)

        dependencies = {}
        readers = {node.id: [] for node in self.nodes}
        for node in self.nodes:
            refs = set(node.children) | set(argument_refs(node.args)) | set(argument_refs(node.kwargs))
            if node.receiver is not None:
                refs.add(node.receiver)
            if not refs <= by_id.keys():
                raise ValueError("construction references a missing node")
            dependencies[node.id] = len(refs)
            for ref in refs:
                readers[ref].append(node.id)
        ready = [key for key, count in dependencies.items() if count == 0]
        consumed = 0
        while ready:
            key = ready.pop()
            consumed += 1
            for reader in readers[key]:
                dependencies[reader] -= 1
                if dependencies[reader] == 0:
                    ready.append(reader)
        if consumed != len(self.nodes):
            raise ValueError("construction must be acyclic")
        return self
