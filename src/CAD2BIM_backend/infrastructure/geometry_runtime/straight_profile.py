"""Pure straight-profile construction shared by derived displays and future writers."""

from __future__ import annotations

import math

import cadquery as cq

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_interface import (
    CircularInterfaceProfile,
    PolygonInterfaceProfile,
    RectangleInterfaceProfile,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryBuildDisplayContextRequest,
    GeometryBuildDisplayContextResult,
    GeometryNormalizeRequest,
    GeometryRuntimeValidationError,
    GeometryStraightProfileRecipe,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec import normalize_geometry
from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import serialize_user_shape


def build_straight_profile_shape(recipe: GeometryStraightProfileRecipe) -> cq.Shape:
    """Extrude the declared local XY profile along the same frame's positive Z."""
    frame = recipe.frame
    axis = tuple(row[2] for row in frame.rotation)
    reference = tuple(row[0] for row in frame.rotation)
    if not all(
        math.isfinite(origin + direction * recipe.length_m)
        for origin, direction in zip(frame.translation_m, axis)
    ):
        raise GeometryRuntimeValidationError("straight profile endpoint exceeds finite arithmetic")
    plane = cq.Plane(origin=frame.translation_m, xDir=reference, normal=axis)
    section = cq.Workplane(plane)
    profile = recipe.profile
    if isinstance(profile, CircularInterfaceProfile):
        section = section.circle(profile.diameter_m / 2)
    elif isinstance(profile, RectangleInterfaceProfile):
        section = section.rect(profile.width_m, profile.height_m)
    elif isinstance(profile, PolygonInterfaceProfile):
        section = section.polyline(profile.points_m).close()
    else:
        raise GeometryRuntimeValidationError("straight profile kind is unsupported")
    shape = section.extrude(recipe.length_m, combine=False).val()
    if not isinstance(shape, cq.Shape) or not shape.isValid() or len(shape.Solids()) != 1:
        raise GeometryRuntimeValidationError("straight profile did not form one valid solid")
    return shape


def build_display_context(
    request: GeometryBuildDisplayContextRequest,
) -> GeometryBuildDisplayContextResult:
    """Build one canonical compound; it has no product or persistent resource identity."""
    try:
        shapes = [build_straight_profile_shape(recipe) for recipe in request.recipes]
        payload = serialize_user_shape(cq.Compound.makeCompound(shapes))
        geometry = normalize_geometry(
            GeometryNormalizeRequest(format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=payload)
        )
    except GeometryRuntimeValidationError:
        raise
    except Exception as exc:
        raise GeometryRuntimeValidationError(
            "straight profile display context could not be constructed or canonically reopened"
        ) from exc
    return GeometryBuildDisplayContextResult(
        geometry=geometry,
        recipe_digest=sha256_digest(request),
        recipe_keys=tuple(recipe.recipe_key for recipe in request.recipes),
    )


__all__ = ["build_display_context", "build_straight_profile_shape"]
