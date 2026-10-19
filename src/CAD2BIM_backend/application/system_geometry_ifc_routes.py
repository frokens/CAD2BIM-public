"""Backend policy for the exact, owned SYSTEM Geometry closure.

This selects one representation per immutable Geometry, before execution.
It neither detects arbitrary author shapes nor tries a second encoder on failure.
"""
from typing import Literal

from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceScopeKind,
    GeometryResourceVersionBundle,
)
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogIntegrityError
from CAD2BIM_backend.domain.ifc_geometry_construction import IfcGeometryRepresentationRoute


_SYSTEM_GEOMETRY_ROUTES = {
    "grsv_ebe07875929e1cd808c5ad7f23df4a35a291e03ce98dfc6703f6aa1980394c6a": IfcGeometryRepresentationRoute.TFS,
    "grsv_e6f60154872a2db22e516b5f08276a203d021c4a319e4fb95f963d6b1001e017": IfcGeometryRepresentationRoute.ADVANCED_BREP,
    "grsv_fe8e297fb70d01910439f4ee938fb9471e58872d1900b0ceb37245aee330661f": IfcGeometryRepresentationRoute.ADVANCED_BREP,
    "grsv_d0445cda0fac41ef9875a0acc27b51b37ec0fa5a559c3c53c65900208f9bfb89": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_70d9d6ad9ff5504d98194a4237a4f9accf7cce278aa18f143cb492ed487ae555": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_89b70e97b766a0aaf5d3063fb3d6cc66f8aa4dfc14bb8ef8308acb3ff87c9ce6": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_cdce56bdfd16e2d203d01784544adafb1fba9a9a5a0b1b63caecff0489ad51be": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_c93edae125b4f07bf54c4742e99b3dd482e0b9ba0a6cd69a8038ded9e21d4c9d": IfcGeometryRepresentationRoute.TFS,
    "grsv_f25b514aa72ff044fbb1d743f0bd92e9316a65b228e142016b7ba77bcfdd7c3a": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_5944e9a0739ba91e6aec341ee24032ef48e395737326081c3aa88b6fed942f27": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_225603a650fbc008feea1f6bf9e265dfb5863d4bf582666e47a580766710a69a": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_33de27ba960f9dd0ba2ed25caa2f30341a1c630b454f81e90ad7ceea206e8b63": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_cc34bb058471e6d991b1e9ec437e8db3022d99f1c5121ee78d6cb0631d73b0ac": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_bcdccb696c7527b8655ad3186efd9017de58a4749d49df3ba51a13415117d668": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_4fbab681467cb282b7a69baef0a12b74bad33e7a15bc003395d380bc7b83cd49": IfcGeometryRepresentationRoute.TFS,
    "grsv_f200c0744eaf899f306bd582d980889507424eeff9352813169eefc04aacc51a": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_5f83660abba68a02ab56504b4bf3d1a4c74e83c10dbd23a4e01ead0e687d84ef": IfcGeometryRepresentationRoute.PARAMETRIC,
    "grsv_82db65bb0739fff37daf12bb270ec02beab3033db61d9a3f5056324e01e7eb7f": IfcGeometryRepresentationRoute.PARAMETRIC,
}


def system_geometry_ifc_route(bundle: GeometryResourceVersionBundle) -> IfcGeometryRepresentationRoute:
    selected = _SYSTEM_GEOMETRY_ROUTES.get(bundle.version.resource_version_id)
    if selected is None:
        return IfcGeometryRepresentationRoute.TFS
    if bundle.resource.owner_scope.scope_kind is not GeometryResourceScopeKind.SYSTEM:
        raise GeometryCatalogIntegrityError("Known SYSTEM Geometry version has inconsistent ownership")
    return selected


def system_geometry_source_reuse_mode(
    bundle: GeometryResourceVersionBundle,
) -> Literal["exact", "rigid_two_port"]:
    """Only this owned source has a proven rigid-pose-independent curved body."""
    if bundle.version.resource_version_id != "grsv_225603a650fbc008feea1f6bf9e265dfb5863d4bf582666e47a580766710a69a":
        return "exact"
    # Reuse the exact-version ownership boundary; names and author copy labels
    # never confer SYSTEM-specific source projection.
    system_geometry_ifc_route(bundle)
    return "rigid_two_port"
