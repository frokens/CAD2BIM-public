"""Call-level admission and execution budgets, separate from worker resource caps."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeExecutionPolicy:
    queue_seconds: float
    execution_seconds: float
    terminal_seconds: float = 15.0
    cleanup_seconds: float = 5.0

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) or value <= 0 for value in (
            self.queue_seconds, self.execution_seconds, self.terminal_seconds, self.cleanup_seconds,
        )):
            raise ValueError("runtime execution budgets must be positive and finite")

    @property
    def total_seconds(self) -> float:
        return self.queue_seconds + self.execution_seconds + self.terminal_seconds + self.cleanup_seconds


# H covers the complete existing chain, including bounded staging/verification.
# Per-child CPU/RSS/output/wall caps remain with each existing worker policy.
GEOMETRY_CODEC_EXECUTION = RuntimeExecutionPolicy(600.0, 45.0)
GEOMETRY_AUTHORING_EXECUTION = RuntimeExecutionPolicy(600.0, 120.0)
GEOMETRY_PREVIEW_EXECUTION = RuntimeExecutionPolicy(600.0, 150.0)
GEOMETRY_QUERY_EXECUTION = RuntimeExecutionPolicy(600.0, 270.0)
PRODUCT_REALIZATION_EXECUTION = RuntimeExecutionPolicy(600.0, 900.0)
MAX_PRODUCT_BATCH_OUTPUT_BYTES = 256 * 1024 * 1024
PRODUCT_PREVIEW_EXECUTION = RuntimeExecutionPolicy(600.0, 240.0)
PRODUCT_CURRENT_IFC_PREVIEW_EXECUTION = RuntimeExecutionPolicy(600.0, 410.0)
# One readiness child plus up to 16 validation scenarios at 30 seconds each,
# with 30 seconds for their bounded host-side preparation and verification.
PRODUCT_AUTHORING_EXECUTION = RuntimeExecutionPolicy(600.0, 540.0)
# Exact revision inspection (170s) followed by a query child and bounded IO (60s).
IFC_QUERY_EXECUTION = RuntimeExecutionPolicy(600.0, 230.0)
IFC_REVISION_EXECUTION = RuntimeExecutionPolicy(600.0, 170.0)
IFC_REVISION_INVOCATION_EXECUTION = RuntimeExecutionPolicy(600.0, 510.0)
DATA_PYTHON_EXECUTION = RuntimeExecutionPolicy(600.0, 110.0)
DATA_SQL_EXECUTION = RuntimeExecutionPolicy(120.0, 60.0)
CAD_ANALYSIS_EXECUTION = RuntimeExecutionPolicy(360.0, 110.0)
CAD_MEASUREMENT_EXECUTION = RuntimeExecutionPolicy(360.0, 170.0)
RUNTIME_RESPONSE_MARGIN_SECONDS = 10.0
DEFAULT_MAX_WAITING_REQUESTS = 32


__all__ = [name for name in globals() if name.endswith("_EXECUTION")] + [
    "RuntimeExecutionPolicy", "RUNTIME_RESPONSE_MARGIN_SECONDS", "MAX_PRODUCT_BATCH_OUTPUT_BYTES",
]
