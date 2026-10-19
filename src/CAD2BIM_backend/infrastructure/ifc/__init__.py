"""IfcOpenShell infrastructure for the durable IFC kernel."""

from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.geometry_basis import (
    IfcOpenShellGeometryBasisCompiler,
)

__all__ = ["IfcOpenShellGeometryBasisCompiler", "IfcOpenShellModelCodec"]
