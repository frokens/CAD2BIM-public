"""Child-safe OCP/CadQuery I/O; saved BREP identity is its actual bytes."""

from __future__ import annotations

import io
from typing import Any

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D

MAX_USER_GEOMETRY_BYTES = 64 * 1024 * 1024


def coerce_user_shape(value: Any, *, entry_name: str) -> Any:
    """Close the accepted return ABI to exactly one non-empty OCP shape."""
    import cadquery as cq
    from OCP.TopoDS import TopoDS_Shape

    if isinstance(value, cq.Workplane):
        values = value.vals()
        if len(values) != 1:
            raise TypeError(f"{entry_name}(ctx) Workplane must contain exactly one shape")
        value = values[0]
    if isinstance(value, TopoDS_Shape):
        value = cq.Shape(value)
    if not isinstance(value, cq.Shape):
        raise TypeError(
            f"{entry_name}(ctx) must return cq.Shape, one-shape cq.Workplane, or TopoDS_Shape"
        )
    _require_nonempty_native_shape(value.wrapped, entry_name=entry_name)
    return value


def validate_user_shape(value: Any) -> None:
    """Validate actual OCCT topology, independently of serializer spelling."""
    value = coerce_user_shape(value, entry_name="geometry")
    validate_native_shape(value.wrapped)


def _require_nonempty_native_shape(shape: Any, *, entry_name: str) -> None:
    from OCP.TopAbs import TopAbs_VERTEX, TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    if shape.IsNull():
        raise ValueError(f"{entry_name}(ctx) returned a null shape")
    if not any(TopExp_Explorer(shape, kind).More()
               for kind in (TopAbs_VERTEX, TopAbs_EDGE, TopAbs_FACE)):
        raise ValueError(f"{entry_name}(ctx) returned an empty shape")


def validate_native_shape(shape: Any) -> None:
    """Same Analyzer used by Shape.isValid, after the owning read/coerce check."""
    from OCP.BRepCheck import BRepCheck_Analyzer

    if not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError("OCCT BREP geometry is invalid")


def serialize_user_shape(value: Any) -> bytes:
    """Save one private shape, excluding derived triangulation caches."""
    from OCP.BRepTools import BRepTools
    from OCP.TopTools import TopTools_FormatVersion

    stream = io.BytesIO()
    shape = value.copy(mesh=False)
    BRepTools.Clean_s(shape.wrapped)
    BRepTools.Write_s(
        shape.wrapped, stream, False, False,
        TopTools_FormatVersion.TopTools_FormatVersion_VERSION_3,
    )
    payload = stream.getvalue()
    if payload and not payload.startswith(b"DBRep_DrawableShape\n"):
        payload = b"DBRep_DrawableShape\n" + payload
    if not payload or len(payload) > MAX_USER_GEOMETRY_BYTES:
        raise ValueError("user geometry is outside the output byte budget")
    return payload


def save_user_shape(value: Any) -> tuple[Any, bytes]:
    """Save once and verify the actual saved geometry can reopen validly."""
    payload = serialize_user_shape(value)
    reopened = reopen_user_shape(payload)
    validate_user_shape(reopened)
    return reopened, payload


def transform_occt_shape(shape: Any, transform: RigidTransform3D) -> Any:
    """Apply the shared rigid frame inside a supervised child."""
    import cadquery as cq
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf

    native = gp_Trsf()
    native.SetValues(*(value for row, offset in zip(transform.rotation, transform.translation_m, strict=True) for value in (*row, offset)))
    operation = BRepBuilderAPI_Transform(shape.wrapped, native, True)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("OCCT rigid geometry transform failed")
    return cq.Shape(operation.Shape())


def reopen_user_shape(payload: bytes) -> Any:
    """Read saved bytes in memory without rewriting or normalizing them."""
    import cadquery as cq

    return cq.Shape(read_native_shape(payload))


def read_native_shape(payload: bytes) -> Any:
    """Read the same exact nonempty TopoDS payload without a CadQuery wrapper."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    if not payload or len(payload) > MAX_USER_GEOMETRY_BYTES:
        raise ValueError("user geometry is outside the input byte budget")
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, io.BytesIO(payload), BRep_Builder())
    if shape.IsNull():
        raise ValueError("OCCT BREP payload could not be reopened")
    _require_nonempty_native_shape(shape, entry_name="geometry")
    return shape


__all__ = [
    "MAX_USER_GEOMETRY_BYTES", "coerce_user_shape", "reopen_user_shape",
    "save_user_shape", "serialize_user_shape", "transform_occt_shape", "validate_user_shape",
]
