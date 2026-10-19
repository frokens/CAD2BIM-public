"""Entry-neutral Arrow input envelope shared by bounded read/transform workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pyarrow as pa


@dataclass(slots=True)
class ArrowInput:
    input_name: str
    reader: pa.RecordBatchReader
    input_format: Literal["parquet", "inline", "json"] = "parquet"
    json_kind: Literal["document", "records"] | None = None
    row_count: int | None = None

    def __post_init__(self) -> None:
        if self.input_format == "json" and self.json_kind not in {"document", "records"}:
            raise ValueError("JSON Arrow input requires json_kind")
        if self.input_format != "json" and self.json_kind is not None:
            raise ValueError("Only JSON Arrow input accepts json_kind")
        if self.row_count is not None and (
            not isinstance(self.row_count, int)
            or isinstance(self.row_count, bool)
            or self.row_count < 0
        ):
            raise ValueError("Arrow input row_count must be a non-negative integer")


__all__ = ["ArrowInput"]
