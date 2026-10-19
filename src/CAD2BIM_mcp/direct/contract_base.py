"""Shared primitives for Direct provider response contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


_INT64_MAX = 9_223_372_036_854_775_807
_MAX_MARKDOWN_BYTES = 65_536
_ERROR_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,119}$"


class DirectContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class AvailableOperationNavigation(DirectContract):
    availability: Literal["available"] = "available"
    operation_ref: str = Field(min_length=1, max_length=16_384)
    operation_alias: str | None = Field(default=None, pattern=r"^op_[0-9]{3,}$", max_length=32)


class UnavailableOperationNavigation(DirectContract):
    availability: Literal["unavailable"] = "unavailable"


OperationNavigation = AvailableOperationNavigation | UnavailableOperationNavigation


def _non_blank_no_nul(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be empty or whitespace")
    if "\x00" in value:
        raise ValueError("value must not contain NUL")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _validate_terminal_times(started_at: datetime, finished_at: datetime) -> None:
    if finished_at < started_at:
        raise ValueError("finished_at must not precede started_at")


PositiveInt64 = Annotated[StrictInt, Field(ge=1, le=_INT64_MAX)]
NonNegativeInt64 = Annotated[StrictInt, Field(ge=0, le=_INT64_MAX)]
SectionTitle = Annotated[str, Field(min_length=1, max_length=160)]
LogicalKeyField = Annotated[str, Field(min_length=1, max_length=255)]
