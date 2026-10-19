"""buildingSMART maintained-documentation HTTP adapter."""

from CAD2BIM_backend.infrastructure.ifc_official_docs.buildingsmart_client import (
    BuildingSmartIfcDocsClient,
    BuildingSmartIfcDocsPolicy,
)

__all__ = ["BuildingSmartIfcDocsClient", "BuildingSmartIfcDocsPolicy"]
