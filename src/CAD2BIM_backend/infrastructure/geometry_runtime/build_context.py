"""Shared build context for one exact geometry-function invocation."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import TypeAdapter

from CAD2BIM_backend.domain.geometry_frame import rigid_transform_from_axis_reference
from CAD2BIM_backend.domain.geometry_interface import (
    GeometryModelInterface,
    GeometryModelInterfaces,
)


_INTERFACES_ADAPTER = TypeAdapter(GeometryModelInterfaces)


class GeometryInputNotFoundError(KeyError):
    """A missing declaration detected before any native copy executes."""


class GeometryBuildContext:
    """Expose declared parameters and collect interfaces from the same build."""

    __slots__ = ("_interfaces", "_parameters", "_geometries", "allow_interfaces")

    def __init__(self, parameters: dict[str, Any], geometries: Mapping[str, Any] | None = None, *, allow_interfaces: bool = True) -> None:
        self._parameters = deepcopy(parameters)
        self._interfaces: GeometryModelInterfaces = ()
        self._geometries = dict(geometries or {})
        self.allow_interfaces = allow_interfaces

    def geometry(self, key: str) -> Any:
        """Return a fresh editable copy of one declared BREP for this build."""
        try:
            shape = self._geometries[key]
        except KeyError as exc:
            raise GeometryInputNotFoundError(f"geometry input {key!r} was not declared; use a declared input_key") from exc
        return shape.copy(mesh=False)

    def param(self, name: str) -> Any:
        try:
            return deepcopy(self._parameters[name])
        except KeyError as exc:
            raise KeyError(f"parameter {name!r} was not declared") from exc

    def define_interface(
        self,
        *,
        key: str,
        origin_m: tuple[float, float, float],
        axis: tuple[float, float, float],
        reference_direction: tuple[float, float, float],
        profile: Mapping[str, Any] | None = None,
    ) -> None:
        if any(item.interface_key == key for item in self._interfaces):
            raise ValueError(f"geometry interface {key!r} was defined more than once")
        interface = GeometryModelInterface(
            interface_key=key,
            local_frame=rigid_transform_from_axis_reference(
                origin_m,
                axis,
                reference_direction,
            ),
            profile=None if profile is None else dict(profile),
        )
        self._interfaces = _INTERFACES_ADAPTER.validate_python((*self._interfaces, interface))

    def interfaces(self) -> GeometryModelInterfaces:
        return tuple(self._interfaces)


__all__ = ["GeometryBuildContext"]
