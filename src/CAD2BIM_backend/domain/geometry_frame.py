"""Shared right-handed rigid frame values for geometry and product realization."""

from __future__ import annotations

import math
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

Vector3 = tuple[float, float, float]
Matrix3 = tuple[Vector3, Vector3, Vector3]


def _finite_vector(value: Vector3, *, label: str) -> Vector3:
    if len(value) != 3:
        raise ValueError(f"{label} must contain exactly three values")
    if not all(math.isfinite(item) for item in value):
        raise ValueError(f"{label} must contain finite values")
    return value


def rigid_transform_from_axis_reference(
    origin_m: Vector3,
    axis: Vector3,
    reference_direction: Vector3,
) -> "RigidTransform3D":
    """Build the project-world transform for one complete right-handed frame.

    Rotation columns are the local X, Y, and Z basis vectors expressed in
    project-world coordinates.  ``reference_direction`` is local X and
    ``axis`` is local Z.
    """

    origin = _finite_vector(tuple(origin_m), label="frame origin")
    z_axis = _finite_vector(tuple(axis), label="frame axis")
    x_axis = _finite_vector(tuple(reference_direction), label="frame reference direction")
    tolerance = 1e-9
    if abs(math.sqrt(sum(item * item for item in z_axis)) - 1.0) > tolerance:
        raise ValueError("frame axis must be a unit vector")
    if abs(math.sqrt(sum(item * item for item in x_axis)) - 1.0) > tolerance:
        raise ValueError("frame reference direction must be a unit vector")
    if abs(sum(a * b for a, b in zip(z_axis, x_axis, strict=True))) > tolerance:
        raise ValueError("frame axis and reference direction must be perpendicular")
    y_axis = (
        z_axis[1] * x_axis[2] - z_axis[2] * x_axis[1],
        z_axis[2] * x_axis[0] - z_axis[0] * x_axis[2],
        z_axis[0] * x_axis[1] - z_axis[1] * x_axis[0],
    )
    rotation = tuple((x_axis[index], y_axis[index], z_axis[index]) for index in range(3))
    return RigidTransform3D(translation_m=origin, rotation=rotation)


class RigidTransform3D(BaseModel):
    """One right-handed rigid transform; rows form an orthonormal matrix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    translation_m: Vector3 = (0.0, 0.0, 0.0)
    rotation: Matrix3 = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )

    @model_validator(mode="after")
    def require_right_handed_rigid_transform(self) -> Self:
        _finite_vector(self.translation_m, label="transform translation")
        for row in self.rotation:
            _finite_vector(row, label="transform rotation")
        tolerance = 1e-9
        for index, left in enumerate(self.rotation):
            for other, right in enumerate(self.rotation):
                dot = sum(a * b for a, b in zip(left, right, strict=True))
                expected = 1.0 if index == other else 0.0
                if abs(dot - expected) > tolerance:
                    raise ValueError("transform rotation must be orthonormal")
        a, b, c = self.rotation
        determinant = (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        )
        if abs(determinant - 1.0) > tolerance:
            raise ValueError("transform rotation must be right-handed")
        return self


def compose_rigid_transform(
    parent: RigidTransform3D,
    local: RigidTransform3D,
) -> RigidTransform3D:
    rotation = tuple(
        tuple(
            sum(parent.rotation[row][inner] * local.rotation[inner][column] for inner in range(3))
            for column in range(3)
        )
        for row in range(3)
    )
    translated = tuple(
        parent.translation_m[row]
        + sum(parent.rotation[row][inner] * local.translation_m[inner] for inner in range(3))
        for row in range(3)
    )
    return RigidTransform3D(translation_m=translated, rotation=rotation)


def inverse_rigid_transform(transform: RigidTransform3D) -> RigidTransform3D:
    """Invert a validated rigid frame without introducing another placement representation."""
    rotation = tuple(tuple(transform.rotation[j][i] for j in range(3)) for i in range(3))
    translation = tuple(-sum(row[j] * transform.translation_m[j] for j in range(3)) for row in rotation)
    return RigidTransform3D(translation_m=translation, rotation=rotation)
