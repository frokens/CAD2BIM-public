"""Direct application behavior owned by this capability family."""

from __future__ import annotations

from typing import TYPE_CHECKING


from CAD2BIM_mcp.direct.errors import DirectApplicationError

if TYPE_CHECKING:
    from CAD2BIM_backend.application.data_runtime_service import (
        DataRuntimeApplicationError,
    )



def _direct_error(
    error: DataRuntimeApplicationError,
    *,
    phase: str = "read",
) -> DirectApplicationError:
    return DirectApplicationError(
        error.code,
        str(error),
        phase=getattr(error, "phase", None) or phase,
        retryable=error.retryable,
        operation_alias=error.operation_alias,
        operation_ref=getattr(error, "operation_ref", None),
        operation_status=getattr(error, "operation_status", None),
        diagnostic=getattr(error, "diagnostic", None),
    )
