"""Declared modeling operations and invocation-owned native objects.

Only this module owns the author/native boundary. Native classes and bound
methods never leave the arena. Restricted compilation and process supervision
are separate, required layers; this module is not a Python sandbox by itself.
"""
from __future__ import annotations

import builtins
import base64
import math
import operator
import types
import weakref
from dataclasses import dataclass
from typing import Any, Callable

from RestrictedPython.Guards import guarded_iter_unpack_sequence, guarded_unpack_sequence


class ModelingCapabilityError(BaseException):
    """Fatal boundary violation; the arena also records it against bare except."""


class ModelingInputError(ValueError):
    """Classified pure input rejection before any native modeling operation."""


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    receiver: tuple[type, ...]
    function: Callable
    returns: tuple[type, ...]
    effect: str = "local_object"
    arguments: str = "exact basic values and same-invocation handles; no callbacks"
    callbacks: str = "none"
    failure: str = "fatal_native_state_unknown"


_SHAPES = ()
_NATIVE = ()
_PROPERTIES = {}
OPERATIONS = ()
_BASIC = (type(None), bool, int, float, str)
_CONTAINERS = (list, tuple, dict, set, frozenset)


class _ConstructionTrace:
    """Invocation-local observations of the operations that built one shape.

    This observes the controlled boundary, including the actual shape and its
    Location after a Workplane operation. It never patches CadQuery methods or
    offers a second way for author code to construct geometry.
    """

    def __init__(self, arena):
        self._arena = arena
        self._ids: dict[object, int] = {}
        self._nodes: dict[int, dict[str, Any]] = {}
        self._snapshots: dict[int, Any] = {}
        self._observed_vals: dict[int, list[Any]] = {}
        self._receiver_states: dict[int, dict[str, Any]] = {}
        self._needed_snapshots: set[int] = set()

    @staticmethod
    def _snapshot(shape):
        # Detached TopoDS value: a later in-place Location change on the
        # author's wrapper cannot alter this operation's observation.
        return cq.Shape.cast(shape.wrapped.Moved(cq.Location().wrapped))

    @staticmethod
    def _brep(shape):
        from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import serialize_user_shape

        return base64.b64encode(serialize_user_shape(shape)).decode("ascii")

    @staticmethod
    def _plane(plane):
        return {"$plane": {
            "origin": list(map(float, plane.origin.toTuple())),
            "x_dir": list(map(float, plane.xDir.toTuple())),
            "z_dir": list(map(float, plane.zDir.toTuple())),
        }}

    def receiver_state(self, op, receiver, raw_args, raw_kwargs):
        """Detach the exact pending sketch before a consuming CQ operation clears it."""
        if receiver is None or op.name not in {"extrude", "revolve", "sweep", "loft"}:
            return None
        native = self._arena._object(receiver)
        if type(native) is not cq.Workplane:
            return None
        state = {
            "plane": self._plane(native.plane),
            "pending_wires": [(self._snapshot(item), type(item).__name__) for item in native.ctx.pendingWires],
            "pending_edges": [(self._snapshot(item), type(item).__name__) for item in native.ctx.pendingEdges],
        }
        if op.name == "revolve":
            # CadQuery's Workplane.revolve interprets explicit axis points in
            # the current local plane and uses this particular default end.
            # Record the actual world axis before the pending sketch is used.
            start = raw_kwargs.get("axisStart", raw_args[1] if len(raw_args) > 1 else None)
            end = raw_kwargs.get("axisEnd", raw_args[2] if len(raw_args) > 2 else None)
            if start is None:
                start = native.plane.toWorldCoords((0, 0)).toTuple()
            else:
                start = native.plane.toWorldCoords(start).toTuple()
            if end is None:
                end = native.plane.toWorldCoords((0, start[1] if start[1] != 0 else 1)).toTuple()
            else:
                end = native.plane.toWorldCoords(end).toTuple()
            state["revolve_axis_world"] = [list(map(float, start)), list(map(float, end))]
        return state

    def _typed_shape(self, shape, kind):
        return {"$shape_brep": self._brep(shape), "$shape_type": kind}

    def _encode(self, value):
        kind = type(value)
        if kind in _BASIC:
            return value
        if kind is _Handle:
            native = self._arena._object(value)
            if type(native) is cq.Vector:
                return {"$vector": [float(native.x), float(native.y), float(native.z)]}
            if type(native) is cq.Plane:
                return self._plane(native)
            node_id = self._ids.get(value._token)
            if node_id is None:
                if type(native) in _SHAPES:
                    return {"$shape_brep": self._brep(native), "$shape_type": type(native).__name__}
                raise ValueError("construction references an unobserved modeling handle")
            return {"$ref": node_id}
        if kind in (tuple, list):
            return [self._encode(item) for item in value]
        if kind is dict and all(type(key) is str for key in value):
            return {key: self._encode(item) for key, item in value.items()}
        raise ValueError("construction argument is outside the controlled modeling values")

    @staticmethod
    def _refs(value):
        if type(value) is dict:
            if set(value) == {"$ref"}:
                yield value["$ref"]
            else:
                for item in value.values():
                    yield from _ConstructionTrace._refs(item)
        elif type(value) is list:
            for item in value:
                yield from _ConstructionTrace._refs(item)

    def record(self, op, receiver, args, kwargs, wrapped, receiver_state=None):
        encoded_args = self._encode(args)
        encoded_kwargs = self._encode(kwargs)
        if {"$observed_vals", "$observed_plane", "$receiver_state"} & encoded_kwargs.keys():
            raise ValueError("construction observation key collides with operation arguments")
        receiver_id = self._ids.get(receiver._token) if receiver is not None else None
        if receiver is not None and receiver_id is None:
            raise ValueError("construction receiver has no recorded operation")
        children = tuple(dict.fromkeys(
            ((receiver_id,) if receiver_id is not None else ())
            + tuple(self._refs(encoded_args)) + tuple(self._refs(encoded_kwargs))
        ))
        # The encoder reads native shape arguments (e.g. a sweep path or the
        # wires of Solid.revolve), but Workplane profiles already have their
        # exact pending-wire snapshot in receiver_state. Boolean and compound
        # operands are encoded recursively, not reopened as intermediate BREPs.
        if op.name in {"extrude", "revolve", "sweep", "loft",
                       "Solid.revolve", "Solid.makeLoft"}:
            self._needed_snapshots.update(self._refs(encoded_args))
            self._needed_snapshots.update(self._refs(encoded_kwargs))
        # These consumers inspect the actual resulting boundary. Other solid
        # primitives use their observed arguments/profile instead of this BREP.
        needs_output = op.name in {"box", "Solid.makeBox", "fillet", "chamfer"}

        def one(handle):
            native = self._arena._object(handle)
            node_id = len(self._nodes)
            shape = native if type(native) in _SHAPES else None
            node_kwargs = dict(encoded_kwargs)
            if type(native) is cq.Workplane:
                node_kwargs["$observed_plane"] = self._plane(native.plane)
                values = native.vals()
                if len(values) == 1 and type(values[0]) in _SHAPES:
                    shape = values[0]
                else:
                    # A selection can contain several distinct subshapes.
                    # Preserve their order and identity; never turn them into
                    # one guessed compound or silently keep the first item.
                    self._observed_vals[node_id] = [
                        (self._snapshot(item), type(item).__name__) if type(item) in _SHAPES else
                        (list(map(float, item.toTuple())), "Vector") if type(item) is cq.Vector else
                        (None, type(item).__name__)
                        for item in values
                    ]
            self._nodes[node_id] = {
                "id": node_id, "op": op.name, "receiver": receiver_id,
                "args": encoded_args, "kwargs": node_kwargs,
                "children": children,
                "shape_brep": None,
            }
            if shape is not None:
                self._snapshots[node_id] = self._snapshot(shape)
            if needs_output:
                self._needed_snapshots.add(node_id)
            if receiver_state is not None:
                self._receiver_states[node_id] = receiver_state
            self._ids[handle._token] = node_id

        def visit(value):
            if type(value) is _Handle:
                one(value)
            elif type(value) in (list, tuple):
                for item in value:
                    visit(item)
            elif type(value) is dict:
                for item in value.values():
                    visit(item)

        visit(wrapped)

    def finish(self, authored, shape):
        from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction

        root = self._ids.get(authored._token)
        if root is None:
            raise ValueError("final geometry has no controlled construction node")
        # Refresh a root snapshot only when the encoder consumes that boundary.
        # The complete final BREP is independently serialized and validated by
        # the canonical Geometry/Codec path; duplicating it here has no reader.
        if root in self._needed_snapshots:
            self._snapshots[root] = self._snapshot(shape)
        reachable = set()

        def retain(node_id):
            if node_id in reachable:
                return
            node = self._nodes[node_id]
            reachable.add(node_id)
            for child in node["children"]:
                retain(child)

        retain(root)
        for node_id in reachable:
            if node_id in self._needed_snapshots and node_id in self._snapshots:
                self._nodes[node_id]["shape_brep"] = self._brep(self._snapshots[node_id])
            if node_id in self._needed_snapshots and node_id in self._observed_vals:
                if any(item is None for item, _ in self._observed_vals[node_id]):
                    raise ValueError("required Workplane selection contains an unsupported native value")
                self._nodes[node_id]["kwargs"]["$observed_vals"] = [
                    (self._typed_shape(item, kind)
                     if kind in {"Shape", "Vertex", "Edge", "Wire", "Face", "Shell", "Solid", "Compound"} else
                     {"$vector": item})
                    for item, kind in self._observed_vals[node_id]
                ]
            if node_id in self._receiver_states:
                state = self._receiver_states[node_id]
                self._nodes[node_id]["kwargs"]["$receiver_state"] = {
                    "plane": state["plane"],
                    "pending_wires": [self._typed_shape(item, kind) for item, kind in state["pending_wires"]],
                    "pending_edges": [self._typed_shape(item, kind) for item, kind in state["pending_edges"]],
                    **({"revolve_axis_world": state["revolve_axis_world"]} if "revolve_axis_world" in state else {}),
                }
        # The owning DTO validates raw finite-JSON nodes and their closed graph
        # together; do not construct and validate intermediate node DTOs first.
        return GeometryConstruction(root=root, nodes=tuple(
            self._nodes[node_id] for node_id in sorted(reachable)
        ))


def _solid_revolve(outerWire, innerWires, angleDegrees, axisStart, axisEnd):
    """Expose only the Wire constructor; reject malformed axes before OCCT."""
    if type(outerWire) is not cq.Wire or type(innerWires) not in (list, tuple) or any(
        type(wire) is not cq.Wire for wire in innerWires
    ):
        raise ValueError("Solid.revolve requires an outer Wire and inner Wire sequence")
    if type(angleDegrees) not in (int, float) or not math.isfinite(angleDegrees):
        raise ValueError("Solid.revolve angle must be finite")

    def point(value):
        if type(value) is cq.Vector:
            value = (value.x, value.y, value.z)
        if type(value) not in (list, tuple) or len(value) != 3 or any(
            type(coordinate) not in (int, float) or not math.isfinite(coordinate)
            for coordinate in value
        ):
            raise ValueError("Solid.revolve axis points must have three finite coordinates")
        return tuple(value)

    start, end = point(axisStart), point(axisEnd)
    length = math.dist(start, end)
    if not math.isfinite(length) or length == 0:
        raise ValueError("Solid.revolve axis must be finite and nondegenerate")
    return cq.Solid.revolve(outerWire, list(innerWires), angleDegrees, start, end)


def _indexed_shape_entities(shape, kind, wrapper):
    """Preserve CQ's exact map order without the native iterator's exit cost."""
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    entities = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape.wrapped, kind, entities)
    result = []
    for index in range(1, entities.Extent() + 1):
        item = entities.FindKey(index)
        # Shape.Edges intentionally omits degenerate native edges.
        if wrapper is cq.Edge and BRep_Tool.Degenerated_s(TopoDS.Edge_s(item)):
            continue
        result.append(wrapper(item))
    return result


def _operations() -> tuple[Operation, ...]:
    """Bind audited descriptors once; never forward author-supplied names."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_VERTEX, TopAbs_WIRE

    result = []

    def add(name, receiver, function, returns, effect="local_object"):
        result.append(Operation(name, receiver, function, returns, effect))

    def topology(kind, wrapper):
        def collect(shape):
            return _indexed_shape_entities(shape, kind, wrapper)
        return collect

    for name, function, returns in (
        ("Vector", cq.Vector, (cq.Vector,)), ("Plane", cq.Plane, (cq.Plane,)),
        ("Workplane", cq.Workplane, (cq.Workplane,)),
        ("Vertex.makeVertex", cq.Vertex.makeVertex, (cq.Vertex,)),
        ("Edge.makeLine", cq.Edge.makeLine, (cq.Edge,)),
        ("Edge.makeThreePointArc", cq.Edge.makeThreePointArc, (cq.Edge,)),
        ("Edge.makeBezier", cq.Edge.makeBezier, (cq.Edge,)),
        ("Edge.makeSpline", cq.Edge.makeSpline, (cq.Edge,)),
        ("Edge.makeCircle", cq.Edge.makeCircle, (cq.Edge,)),
        ("Wire.assembleEdges", cq.Wire.assembleEdges, (cq.Wire,)),
        ("Wire.makePolygon", cq.Wire.makePolygon, (cq.Wire,)),
        ("Wire.makeCircle", cq.Wire.makeCircle, (cq.Wire,)),
        ("Face.makeFromWires", cq.Face.makeFromWires, (cq.Face,)),
        ("Shell.makeShell", cq.Shell.makeShell, (cq.Shell,)),
        ("Solid.makeBox", cq.Solid.makeBox, (cq.Solid,)),
        ("Solid.makeCylinder", cq.Solid.makeCylinder, (cq.Solid,)),
        ("Solid.makeCone", cq.Solid.makeCone, (cq.Solid,)),
        ("Solid.makeSphere", cq.Solid.makeSphere, (cq.Solid,)),
        ("Solid.makeTorus", cq.Solid.makeTorus, (cq.Solid,)),
        ("Solid.makeLoft", cq.Solid.makeLoft, (cq.Solid,)),
        ("Solid.revolve", _solid_revolve, (cq.Solid,)),
        ("Compound.makeCompound", cq.Compound.makeCompound, (cq.Compound,)),
    ):
        add(name, (), function, returns)
    # The table records exact descriptors, receiver and recursive leaf returns.
    for name, function, returns in (
        ("moveTo", cq.Workplane.moveTo, (cq.Workplane,)),
        ("lineTo", cq.Workplane.lineTo, (cq.Workplane,)),
        ("polyline", cq.Workplane.polyline, (cq.Workplane,)),
        ("threePointArc", cq.Workplane.threePointArc, (cq.Workplane,)),
        ("spline", cq.Workplane.spline, (cq.Workplane,)),
        ("close", cq.Workplane.close, (cq.Workplane,)),
        ("circle", cq.Workplane.circle, (cq.Workplane,)),
        ("rect", cq.Workplane.rect, (cq.Workplane,)),
        ("polygon", cq.Workplane.polygon, (cq.Workplane,)),
        ("box", cq.Workplane.box, (cq.Workplane,)),
        ("extrude", cq.Workplane.extrude, (cq.Workplane,)),
        ("revolve", cq.Workplane.revolve, (cq.Workplane,)),
        ("sweep", cq.Workplane.sweep, (cq.Workplane,)),
        ("loft", cq.Workplane.loft, (cq.Workplane,)),
        ("union", cq.Workplane.union, (cq.Workplane,)),
        ("cut", cq.Workplane.cut, (cq.Workplane,)),
        ("intersect", cq.Workplane.intersect, (cq.Workplane,)),
        ("clean", cq.Workplane.clean, (cq.Workplane,)),
        ("translate", cq.Workplane.translate, (cq.Workplane,)),
        ("rotate", cq.Workplane.rotate, (cq.Workplane,)),
        ("transformed", cq.Workplane.transformed, (cq.Workplane,)),
        ("workplane", cq.Workplane.workplane, (cq.Workplane,)),
        ("newObject", cq.Workplane.newObject, (cq.Workplane,)),
        ("edges", cq.Workplane.edges, (cq.Workplane,)),
        ("faces", cq.Workplane.faces, (cq.Workplane,)),
        ("vertices", cq.Workplane.vertices, (cq.Workplane,)),
        ("wires", cq.Workplane.wires, (cq.Workplane,)),
        ("solids", cq.Workplane.solids, (cq.Workplane,)),
        ("fillet", cq.Workplane.fillet, (cq.Workplane,)),
        ("chamfer", cq.Workplane.chamfer, (cq.Workplane,)),
        ("val", cq.Workplane.val, (*_SHAPES, cq.Vector)),
        ("vals", cq.Workplane.vals, (*_SHAPES, cq.Vector)),
    ):
        add(name, (cq.Workplane,), function, returns)
    for name, function, returns in (
        ("copy", cq.Shape.copy, _SHAPES), ("clean", cq.Shape.clean, _SHAPES),
        ("cut", cq.Shape.cut, _SHAPES), ("fuse", cq.Shape.fuse, _SHAPES),
        ("intersect", cq.Shape.intersect, _SHAPES),
        ("translate", cq.Shape.translate, _SHAPES), ("rotate", cq.Shape.rotate, _SHAPES),
        ("scale", cq.Shape.scale, _SHAPES), ("Center", cq.Shape.Center, (cq.Vector,)),
        ("BoundingBox", cq.Shape.BoundingBox, (cq.BoundBox,)),
        ("isValid", cq.Shape.isValid, (bool,)), ("ShapeType", cq.Shape.ShapeType, (str,)),
        ("Area", cq.Shape.Area, (float,)), ("Volume", cq.Shape.Volume, (float,)),
        ("distance", cq.Shape.distance, (float,)),
        ("Edges", topology(TopAbs_EDGE, cq.Edge), (cq.Edge,)),
        ("Faces", topology(TopAbs_FACE, cq.Face), (cq.Face,)),
        ("Solids", topology(TopAbs_SOLID, cq.Solid), (cq.Solid,)),
        ("Shells", topology(TopAbs_SHELL, cq.Shell), (cq.Shell,)),
        ("Wires", topology(TopAbs_WIRE, cq.Wire), (cq.Wire,)),
        ("Vertices", topology(TopAbs_VERTEX, cq.Vertex), (cq.Vertex,)),
    ):
        add(name, _SHAPES, function, returns)
    for name, receiver, function, returns in (
        ("geomType", (cq.Edge,), cq.Edge.geomType, (str,)),
        ("geomType", (cq.Face,), cq.Face.geomType, (str,)),
        ("Length", (cq.Edge, cq.Wire), cq.Edge.Length, (float,)),
        ("radius", (cq.Edge,), cq.Edge.radius, (float,)),
        ("startPoint", (cq.Edge,), cq.Edge.startPoint, (cq.Vector,)),
        ("endPoint", (cq.Edge,), cq.Edge.endPoint, (cq.Vector,)),
        ("fillet", (cq.Solid, cq.Compound), cq.Solid.fillet, _SHAPES),
        ("chamfer", (cq.Solid, cq.Compound), cq.Solid.chamfer, _SHAPES),
        ("dot", (cq.Vector,), cq.Vector.dot, (float,)),
        ("cross", (cq.Vector,), cq.Vector.cross, (cq.Vector,)),
        ("normalized", (cq.Vector,), cq.Vector.normalized, (cq.Vector,)),
        ("toTuple", (cq.Vector,), cq.Vector.toTuple, (int, float)),
        ("rotated", (cq.Plane,), cq.Plane.rotated, (cq.Plane,)),
    ):
        add(name, receiver, function, returns)
    return tuple(result)


def _initialize_native():
    """Numeric capability creation never imports the native geometry stack."""
    global cq, _SHAPES, _NATIVE, OPERATIONS, _PROPERTIES
    if OPERATIONS:
        return
    import cadquery as cq
    _SHAPES = (cq.Shape, cq.Vertex, cq.Edge, cq.Wire, cq.Face, cq.Shell, cq.Solid, cq.Compound)
    _NATIVE = (*_SHAPES, cq.Workplane, cq.Vector, cq.Plane, cq.BoundBox)
    OPERATIONS = _operations()
    _PROPERTIES = {
        cq.Vector: {"x": cq.Vector.x.fget, "y": cq.Vector.y.fget,
                    "z": cq.Vector.z.fget, "Length": cq.Vector.Length.fget},
        cq.BoundBox: {name: (lambda obj, key=name: vars(obj)[key]) for name in
                     ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax", "xlen", "ylen", "zlen", "center", "DiagonalLength")},
        cq.Plane: {"origin": cq.Plane.origin.fget,
                   **{name: (lambda obj, key=name: vars(obj)[key]) for name in ("xDir", "yDir", "zDir")}},
    }


class _Callable:
    __slots__ = ("_arena", "_invoke", "__weakref__")

    def __init__(self, arena, invoke):
        self._arena, self._invoke = arena, invoke
        arena._capabilities.add(self)

    def _revoke(self):
        self._invoke = None

    def __call__(self, *args, **kwargs):
        self._arena.ensure_active()
        return self._invoke(*args, **kwargs)


class _Namespace:
    __slots__ = ("_arena", "_values", "_properties", "__weakref__")

    def __init__(self, arena, values, *, properties=None):
        self._arena, self._values = arena, values
        self._properties = properties or {}
        arena._capabilities.add(self)

    def _revoke(self):
        self._values.clear()
        self._properties.clear()

    def __getattr__(self, name):
        self._arena.ensure_active()
        if name in self._properties:
            try:
                return self._arena.snapshot(self._properties[name]())
            except BaseException as exc:
                self._arena.reject(f"modeling property failed; discard invocation: {type(exc).__name__}: {str(exc)[:240]}")
        if name not in self._values:
            self._arena.reject(f"undeclared modeling import or operation: {name}")
        return self._values[name]


class _Handle:
    __slots__ = ("_arena", "_token")

    def __init__(self, arena, token):
        self._arena, self._token = arena, token

    def __add__(self, other):
        return self._arena.vector_operator(self, other, operator.add)

    def __sub__(self, other):
        return self._arena.vector_operator(self, other, operator.sub)

    def __mul__(self, other):
        return self._arena.vector_operator(self, other, operator.mul)

    def __rmul__(self, other):
        return self.__mul__(other)

    def __truediv__(self, other):
        return self._arena.vector_operator(self, other, operator.truediv)

    def __neg__(self):
        return self.__mul__(-1)

    def __format__(self, spec):
        self._arena.reject("formatting a modeling handle is unavailable; read declared measurements")

    def __str__(self):
        self._arena.reject("converting a modeling handle to text is unavailable; read declared measurements")

    def __repr__(self):
        self._arena.reject("representing a modeling handle as text is unavailable; read declared measurements")


class ModelingArena:
    """One author call: exact native identities, parent topology and sticky failure."""

    def __init__(self, *, profile="geometry", capture_construction=False):
        if profile not in {"geometry", "family_numeric", "geometry_interfaces"}:
            raise ValueError("unknown controlled modeling capability profile")
        if capture_construction and profile != "geometry":
            raise ValueError("only a geometry build can record construction")
        if profile == "geometry":
            _initialize_native()
        self._profile = profile
        self._objects: dict[object, tuple[Any, tuple[_Handle, ...]]] = {}
        self._active = True
        self._failure: str | None = None
        self._modules: set[types.ModuleType] = set()
        self._callback_depth = 0
        self._callback_count = 0
        self._capabilities = weakref.WeakSet()
        self._construction = _ConstructionTrace(self) if capture_construction else None
        self._namespaces = self._make_namespaces()

    def reject(self, message):
        self._failure = self._failure or message
        raise ModelingCapabilityError(self._failure)

    def ensure_active(self):
        if not self._active:
            self.reject("expired modeling invocation")
        if self._failure is not None:
            raise ModelingCapabilityError(self._failure)

    def close(self):
        self._active = False
        # Author modules and local containers can legally form cycles. Revoke
        # every still-live capability's closure/value references now, without
        # waiting for cyclic GC to release Host contexts or native instances.
        for capability in tuple(self._capabilities):
            capability._revoke()
        self._capabilities.clear()
        self._namespaces.clear()
        self._objects.clear()
        self._modules.clear()
        self._construction = None

    def _object(self, handle):
        self.ensure_active()
        if type(handle) is not _Handle or handle._arena is not self or handle._token not in self._objects:
            self.reject("forged, expired or cross-invocation modeling handle")
        return self._objects[handle._token][0]

    def wrap(self, value, *, returns=None, parents=()):
        self.ensure_active()
        returns = _NATIVE + _BASIC if returns is None else returns
        kind = type(value)
        if kind in _BASIC:
            if kind not in returns:
                self.reject("operation returned an undeclared scalar type")
            return value
        if kind in (list, tuple):
            return kind(self.wrap(v, returns=returns, parents=parents) for v in value)
        if kind is dict:
            if any(type(k) is not str for k in value):
                self.reject("snapshot keys must be text")
            return {k: self.wrap(v, returns=returns, parents=parents) for k, v in value.items()}
        if kind not in returns or kind not in _NATIVE:
            self.reject("operation returned an unregistered native type")
        token = object()
        self._objects[token] = (value, tuple(parents))
        return _Handle(self, token)

    def unwrap(self, value):
        self.ensure_active()
        kind = type(value)
        if kind in _BASIC:
            return value
        if kind is _Handle:
            return self._object(value)
        if kind in (list, tuple):
            return kind(self.unwrap(v) for v in value)
        if kind is dict and all(type(k) is str for k in value):
            return {k: self.unwrap(v) for k, v in value.items()}
        self.reject("native arguments require exact basic containers or this invocation's handles")

    def snapshot(self, value):
        """Detached Host data only; no Domain objects, callables or native handles."""
        if type(value) in _BASIC:
            return value
        if type(value) in (tuple, list):
            return type(value)(self.snapshot(v) for v in value)
        if type(value) is dict and all(type(k) is str for k in value):
            return {k: self.snapshot(v) for k, v in value.items()}
        self.reject("author snapshot contains a non-basic value")

    def _native_call(self, op, receiver, args, kwargs):
        raw_args, raw_kwargs = self.unwrap(args), self.unwrap(kwargs)
        parents = (receiver,) if receiver is not None else ()
        receiver_state = None
        try:
            if self._construction is not None:
                receiver_state = self._construction.receiver_state(op, receiver, raw_args, raw_kwargs)
            if receiver is not None and op.name in {"fillet", "chamfer"}:
                target = self._object(receiver)
                if type(target) is cq.Workplane:
                    # newObject may change selected topology while retaining a
                    # different solid in the parent chain. Match the native
                    # Workplane implementation's actual target and selection.
                    selected = target.edges().vals()
                    target = target.findSolid()
                else:
                    edge_index = 1 if op.name == "fillet" else 2
                    selected = raw_kwargs.get("edgeList", raw_args[edge_index] if len(raw_args) > edge_index else None)
                if type(target) not in (cq.Solid, cq.Compound):
                    self.reject("topology operation requires an actual Solid or Compound target")
                owned = target.Edges()
                if type(selected) not in (list, tuple) or any(
                    type(edge) is not cq.Edge or not any(edge.isSame(candidate) for candidate in owned)
                    for edge in selected
                ):
                    self.reject("topology selection does not belong to the operation's parent shape")
            value = op.function(self._object(receiver), *raw_args, **raw_kwargs) if receiver is not None else op.function(*raw_args, **raw_kwargs)
        except BaseException as exc:
            self.reject(f"native operation {op.name} failed; discard invocation: {type(exc).__name__}: {str(exc)[:240]}")
        wrapped = self.wrap(value, returns=op.returns, parents=parents)
        if self._construction is not None:
            try:
                self._construction.record(op, receiver, args, kwargs, wrapped, receiver_state)
            except BaseException as exc:
                self.reject(f"construction observation failed; discard invocation: {type(exc).__name__}: {str(exc)[:240]}")
        return wrapped

    def construction(self, authored, shape):
        self.ensure_active()
        if self._construction is None:
            raise ValueError("construction observation was not requested")
        return self._construction.finish(authored, shape)

    def _native_value(self, function, *args, returns=_NATIVE, parents=(), recoverable=()):
        """Protect trusted copy/getter operations as well as declared methods."""
        try:
            value = function(*args)
        except recoverable:
            raise
        except BaseException as exc:
            self.reject(f"native value access failed; discard invocation: {type(exc).__name__}: {str(exc)[:240]}")
        return self.wrap(value, returns=returns, parents=parents)

    def vector_operator(self, handle, other, function):
        obj = self._object(handle)
        raw = self.unwrap(other)
        if type(obj) is not cq.Vector or type(raw) not in (cq.Vector, int, float):
            self.reject("only declared Vector arithmetic is available on handles")
        try:
            value = function(obj, raw)
        except BaseException as exc:
            self.reject(f"unsupported Vector arithmetic: {type(exc).__name__}")
        wrapped = self.wrap(value, returns=(cq.Vector,), parents=(handle,))
        if self._construction is not None:
            try:
                # Vector magic methods use this same controlled Arena boundary,
                # but not _native_call. A following normalized()/toTuple()
                # must therefore see an actual producer for its receiver.
                self._construction.record(
                    Operation(
                        name={operator.add: "Vector.add", operator.sub: "Vector.sub",
                              operator.mul: "Vector.mul", operator.truediv: "Vector.truediv"}[function],
                        receiver=(cq.Vector,), function=function, returns=(cq.Vector,),
                    ), handle, (other,), {}, wrapped,
                )
            except BaseException as exc:
                self.reject(f"construction observation failed; discard invocation: {type(exc).__name__}: {str(exc)[:240]}")
        return wrapped

    def _make_namespaces(self):
        def constructor(operation):
            def invoke(*args, **kwargs):
                return self._native_call(operation, None, args, kwargs)
            return _Callable(self, invoke)

        def numeric(function):
            def invoke(*args, **kwargs):
                return self.snapshot(function(*self.unwrap(args), **self.unwrap(kwargs)))
            return _Callable(self, invoke)

        groups = {}
        for op in OPERATIONS if self._profile == "geometry" else ():
            if op.receiver:
                continue
            function = constructor(op)
            if "." in op.name:
                group, name = op.name.split(".")
                groups.setdefault(group, {})[name] = function
            else:
                groups[op.name] = function
        cadquery = {k: _Namespace(self, v) if type(v) is dict else v for k, v in groups.items()}
        math_values = {k: getattr(math, k) for k in ("pi", "e", "tau", "inf", "nan")}
        for name in ("acos", "asin", "atan", "atan2", "cos", "sin", "tan", "sqrt", "hypot",
                     "radians", "degrees", "dist", "isclose", "isfinite", "isinf", "isnan",
                     "floor", "ceil", "fabs", "exp", "log", "log10", "copysign", "fmod", "ulp"):
            function = getattr(math, name)  # fixed server-owned names, not author forwarding
            math_values[name] = numeric(function)
        values = {"math": _Namespace(self, math_values)}
        if self._profile == "geometry":
            values["cadquery"] = _Namespace(self, cadquery)
        return values

    def import_module(self, name, globals_=None, locals_=None, fromlist=(), level=0):
        self.ensure_active()
        if level or name not in self._namespaces:
            self.reject("only controlled cadquery/math or exact relative bundle imports are available")
        return self._namespaces[name]

    def author_context(self, host, *, family=False):
        """Expose declared operations, never the Host or its postprocessing methods."""
        if family != (self._profile == "family_numeric"):
            self.reject("execution capability profile does not match the author context")
        def basic_method(function):
            def invoke(*args, **kwargs):
                result = function(*self.snapshot(args), **self.snapshot(kwargs))
                return self.snapshot(result)
            return _Callable(self, invoke)

        values = {"param": basic_method(host.param)}
        if self._profile == "geometry_interfaces":
            values["define_interface"] = basic_method(host.define_interface)
        elif not family:
            from CAD2BIM_backend.infrastructure.geometry_runtime.build_context import GeometryInputNotFoundError
            def geometry(key):
                return self._native_value(host.geometry, self.snapshot(key), returns=_SHAPES,
                                          recoverable=(GeometryInputNotFoundError,))
            values["geometry"] = _Callable(self, geometry)
            if host.allow_interfaces:
                values["define_interface"] = basic_method(host.define_interface)
        else:
            for name in ("contract", "parameters", "product_origin", "external_constraints", "policy"):
                values[name] = basic_method(getattr(host, name))
        return _Namespace(self, values)

    def getattr(self, value, name):
        self.ensure_active()
        if type(name) is not str or name.startswith("_"):
            self.reject("private or dynamic runtime attributes are unavailable")
        if type(value) is _Namespace:
            if value._arena is not self:
                self.reject("cross-invocation capability")
            return value.__getattr__(name)
        if type(value) is _Handle:
            obj = self._object(value)
            properties = _PROPERTIES.get(type(obj), {})
            if name in properties:
                # Native properties can produce a handle used by a later
                # operation (e.g. Plane.xDir * radius). Observe the getter at
                # the same boundary as methods so that receiver has a producer.
                return self._native_call(Operation(
                    name=f"{type(obj).__name__}.{name}", receiver=(type(obj),),
                    function=properties[name], returns=(*_NATIVE, *_BASIC),
                ), value, (), {})
            for op in OPERATIONS:
                if name == op.name and type(obj) in op.receiver:
                    def invoke(*args, **kwargs):
                        return self._native_call(op, value, args, kwargs)
                    return _Callable(self, invoke)
            self.reject(f"undeclared {type(obj).__name__} operation: {name}")
        if type(value) is types.ModuleType and value in self._modules:
            if name not in vars(value):
                self.reject("relative helper has no declared symbol")
            return vars(value)[name]
        basic_methods = {
            list: {"append", "extend", "pop", "insert", "remove", "reverse", "copy", "count", "index", "clear"},
            dict: {"get", "items", "keys", "values", "pop", "update", "setdefault", "copy", "clear"},
            str: {"join", "split", "strip", "replace", "startswith", "endswith", "lower", "upper"},
            tuple: {"count", "index"},
            set: {"add", "discard", "remove", "update", "union", "intersection", "difference", "copy"},
        }
        if name in basic_methods.get(type(value), ()):
            method = getattr(type(value), name)
            def call(*args, **kwargs):
                result = method(value, *args, **kwargs)
                # views retain only this call's local basic dictionary.
                return list(result) if type(result) in (type({}.items()), type({}.keys()), type({}.values())) else result
            return _Callable(self, call)
        self.reject(f"attribute {name} is not a declared author capability")

    def getitem(self, value, key):
        self.ensure_active()
        if type(value) not in (list, tuple, dict, str, range):
            self.reject("indexing requires a local basic container")
        return value[key]

    def write(self, value):
        self.ensure_active()
        if type(value) not in (list, dict):
            self.reject("only local list and dictionary item writes are allowed")
        return value

    def getiter(self, value):
        self.ensure_active()
        if type(value) in (*_CONTAINERS, str, range, types.GeneratorType, enumerate, zip,
                           type(iter([])), type(iter(())), type(iter({})), type(iter(range(0)))):
            return iter(value)
        if type(value) is _Handle and type(self._object(value)) is cq.Vector:
            return iter(self._native_value(self._object(value).toTuple, returns=(int, float)))
        self.reject("iteration requires a local basic iterable or Vector")

    def _key(self, function):
        if function is None:
            return None
        if type(function) is not types.FunctionType or function.__globals__.get("_getattr_") != self.getattr:
            self.reject("sorting callback must be an author helper from this invocation")
        def callback(value):
            self._callback_count += 1
            self._callback_depth += 1
            try:
                if self._callback_depth > 8 or self._callback_count > 100_000:
                    self.reject("synchronous callback budget exceeded")
                result = function(value)
                self.ensure_active()
                return self.snapshot(result)
            finally:
                self._callback_depth -= 1
        return callback

    def globals(self):
        def ordered(function, *args, key=None, **kwargs):
            return function(*args, key=self._key(key), **kwargs)
        values = {name: getattr(builtins, name) for name in (
            "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "len", "list",
            "next", "range", "round", "set", "str", "sum", "tuple", "zip",
            "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "RuntimeError", "ZeroDivisionError",
        )}
        values.update({"min": lambda *a, **kw: ordered(min, *a, **kw),
                       "max": lambda *a, **kw: ordered(max, *a, **kw),
                       "sorted": lambda *a, **kw: ordered(sorted, *a, **kw)})
        def inplace(op, left, right):
            functions = {"+=": operator.iadd, "-=": operator.isub, "*=": operator.imul,
                         "/=": operator.itruediv, "//=": operator.ifloordiv, "%=": operator.imod, "**=": operator.ipow}
            if op not in functions or type(left) not in (int, float, str, list, tuple, _Handle):
                self.reject("unsupported local augmented operation")
            return functions[op](left, right)
        return {"__builtins__": values, "_getattr_": self.getattr, "_getitem_": self.getitem,
                "_getiter_": self.getiter, "_write_": self.write,
                "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
                "_unpack_sequence_": guarded_unpack_sequence,
                "_apply_": lambda function, *a, **kw: function(*a, **kw),
                "_inplacevar_": inplace, "_register_module_": self._modules.add}


__all__ = ["ModelingArena", "ModelingCapabilityError", "ModelingInputError", "OPERATIONS"]
