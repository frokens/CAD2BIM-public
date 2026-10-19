"""Shared read projections of immutable product-operation report facts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProductReportDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    check_index: int | None = Field(default=None, ge=0)
    input_row_index: int | None = Field(default=None, ge=0)
    product_key: str | None = None
    target_global_id: str | None = None
    related_global_id: str | None = None
    phase: str
    code: str | None = None
    severity: Literal["error", "warning"]
    field_path: str | None = None
    message: str
    recommended_action: str
    metric: str | None = None
    residual: float | None = None
    tolerance: float | None = None
    unit: str | None = None


class ProductReportDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProductReportDiagnostic, ...]
    complete: bool
