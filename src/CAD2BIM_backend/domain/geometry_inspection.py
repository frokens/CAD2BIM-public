"""Closed object, coordinate and observation choices for exact geometry evidence.

These types contain no catalog authority or runtime objects.  Both authenticated
entries and the existing geometry workers consume the same choices.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.geometry_frame import Vector3


class GeometryInspectionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GeometryInspectionDiagnostic(GeometryInspectionContract):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    phase: Literal["target", "selection", "measurement", "render", "publication"]
    message: str = Field(min_length=1, max_length=2000)
    action: str = Field(min_length=1, max_length=512)


class InspectionWholeSelection(GeometryInspectionContract):
    selection_kind: Literal["whole"] = "whole"


class InspectionComponentSelection(GeometryInspectionContract):
    selection_kind: Literal["component"] = "component"
    component_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class InspectionFragmentSelection(GeometryInspectionContract):
    selection_kind: Literal["fragment"] = "fragment"
    fragment_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


GeometryInspectionSelection = Annotated[
    InspectionWholeSelection | InspectionComponentSelection | InspectionFragmentSelection,
    Field(discriminator="selection_kind"),
]


class InspectionResultCoordinates(GeometryInspectionContract):
    coordinate_space: Literal["result"] = "result"


class InspectionFragmentLocalCoordinates(GeometryInspectionContract):
    coordinate_space: Literal["fragment_local"] = "fragment_local"
    fragment_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


GeometryInspectionCoordinates = Annotated[
    InspectionResultCoordinates | InspectionFragmentLocalCoordinates,
    Field(discriminator="coordinate_space"),
]


class GeometrySectionPlane(GeometryInspectionContract):
    origin_m: Vector3 = Field(description="Plane origin in metres in the explicitly selected coordinates.")
    normal: Vector3 = Field(description="Finite nonzero plane normal; the backend resolves a unit basis.")

    @field_validator("origin_m", "normal")
    @classmethod
    def finite_vectors(cls, value: Vector3) -> Vector3:
        if not all(math.isfinite(item) for item in value):
            raise ValueError("section plane coordinates must be finite")
        return value

    @field_validator("normal")
    @classmethod
    def nonzero_normal(cls, value: Vector3) -> Vector3:
        if math.hypot(*value) <= 0:
            raise ValueError("section plane normal must be nonzero")
        return value


class GeometrySectionDisplay(GeometryInspectionContract):
    u_min_m: float = Field(allow_inf_nan=False)
    u_max_m: float = Field(allow_inf_nan=False)
    v_min_m: float = Field(allow_inf_nan=False)
    v_max_m: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered_extent(self):
        if self.u_min_m >= self.u_max_m or self.v_min_m >= self.v_max_m:
            raise ValueError("section display extents must have positive width and height")
        return self


class GeometrySectionObservation(GeometryInspectionContract):
    observation_kind: Literal["section"] = "section"
    plane: GeometrySectionPlane
    offsets_m: tuple[float, ...] = Field(default=(0.0,), min_length=1, max_length=4)
    display: GeometrySectionDisplay | None = None

    @field_validator("offsets_m")
    @classmethod
    def finite_offsets(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not all(math.isfinite(item) for item in value):
            raise ValueError("section offsets must be finite")
        if len(set(value)) != len(value):
            raise ValueError("section offsets must be distinct")
        return value


def require_inspection_coordinate_selection(
    selection: GeometryInspectionSelection,
    coordinates: GeometryInspectionCoordinates,
) -> None:
    if isinstance(coordinates, InspectionFragmentLocalCoordinates) and (
        not isinstance(selection, InspectionFragmentSelection)
        or selection.fragment_key != coordinates.fragment_key
    ):
        raise ValueError("fragment_local coordinates require selection of that exact fragment")
