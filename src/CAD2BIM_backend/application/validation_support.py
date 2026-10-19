"""Compatibility imports for the shared, entry-neutral validation diagnostics."""

from CAD2BIM_backend.domain.validation_diagnostics import (
    json_schema_validation_message,
    select_json_schema_error,
    validation_error_message,
)

__all__ = ["json_schema_validation_message", "select_json_schema_error", "validation_error_message"]
