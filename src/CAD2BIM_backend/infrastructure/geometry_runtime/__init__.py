"""OCP/CadQuery implementation of the canonical geometry runtime."""

from __future__ import annotations

from typing import Any


__all__ = ["OcpCadQueryGeometryRuntime"]


def __getattr__(name: str) -> Any:
    if name != "OcpCadQueryGeometryRuntime":
        raise AttributeError(name)
    from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import OcpCadQueryGeometryRuntime

    return OcpCadQueryGeometryRuntime
