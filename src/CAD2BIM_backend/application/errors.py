"""Stable application error names used by API exception handlers."""

from CAD2BIM_backend.domain.control_plane import (
    ConflictError,
    ControlPlaneError,
    ResourceNotFoundError,
)


NotFoundError = ResourceNotFoundError


class ValidationError(ControlPlaneError, ValueError):
    """The request is well formed but refers to invalid domain input."""


__all__ = ["ConflictError", "ControlPlaneError", "NotFoundError", "ValidationError"]
