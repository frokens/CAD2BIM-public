"""Entry-neutral contract for strict, single-Family product installation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductApplyContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApplyIfcProductsCommand(ProductApplyContract):
    family_version_id: str = Field(min_length=1, max_length=80)
    package_id: str = Field(min_length=1, max_length=80)
    payload_name: str = Field(min_length=1, max_length=120)


class ProductApplyInputBinding(ProductApplyContract):
    package_id: str = Field(min_length=1, max_length=80)
    member_id: str = Field(min_length=1, max_length=80)
    payload_name: str = Field(min_length=1, max_length=120)
    logical_key_fields: tuple[str, ...]


class ProductApplySummary(ProductApplyContract):
    outcome: Literal["committed", "rejected", "technical_failure"]
    input_count: int = Field(ge=0)
    solve_executed_count: int = Field(ge=0)
    solve_unconfirmed_count: int = Field(default=0, ge=0)
    solved_count: int = Field(ge=0)
    interface_constraints_evaluated_product_count: int = Field(ge=0, description=(
        "Product rows whose solved named-interface constraints completed evaluation, including FAIL; "
        "not a passed-check count, interface/port count, or completed external installation count."))
    build_executed_count: int = Field(ge=0)
    build_unconfirmed_count: int = Field(default=0, ge=0)
    built_count: int = Field(ge=0)
    committed_count: int = Field(ge=0)
    not_executed_count: int = Field(ge=0)
    timings_seconds: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def strict_counts(self):
        counts = (self.solve_executed_count, self.solve_unconfirmed_count, self.solved_count,
                  self.interface_constraints_evaluated_product_count, self.build_executed_count,
                  self.build_unconfirmed_count,
                  self.built_count, self.committed_count, self.not_executed_count)
        if any(value > self.input_count for value in counts):
            raise ValueError("execution counts exceed the complete input denominator")
        if self.solved_count > self.solve_executed_count or self.built_count > self.build_executed_count:
            raise ValueError("successful counts exceed executed counts")
        if (self.solve_executed_count + self.solve_unconfirmed_count + self.not_executed_count != self.input_count
                or self.build_executed_count + self.build_unconfirmed_count > self.input_count):
            raise ValueError("execution coverage must preserve confirmed, unknown and unexecuted inputs")
        if self.outcome == "committed":
            if self.committed_count != self.input_count or self.built_count != self.input_count:
                raise ValueError("strict commit requires every input product")
        elif self.committed_count:
            raise ValueError("a failed strict call commits zero products")
        if any(value < 0 for value in self.timings_seconds.values()):
            raise ValueError("elapsed times cannot be negative")
        return self


class ProductApplyResult(ProductApplyContract):
    summary: ProductApplySummary | None
    operation_id: str = Field(min_length=1, max_length=80)
    operation_alias: str
    report_package_alias: str | None = None
    ifc_revision_id: str | None = None
    error_code: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def revision_is_committed(self):
        if (self.summary is not None and self.summary.outcome == "committed") != (self.ifc_revision_id is not None):
            raise ValueError("only a committed result carries its exact IFC revision")
        return self
