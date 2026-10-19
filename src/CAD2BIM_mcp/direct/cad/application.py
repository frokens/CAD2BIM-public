"""Direct application behavior owned by this capability family."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

from pydantic import ValidationError

from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_COMPUTE_BUDGET,
    CAD_TOLERANCE_ANALYZER_BUDGET,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    DataTransformToolResult,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError

from CAD2BIM_mcp.direct.transform.application import (
    _data_transform_result,
)
from CAD2BIM_mcp.direct.application_support import (
    _direct_error,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.cad_measurement_service import CadMeasurementService
    from CAD2BIM_backend.domain.cad_measurement import (
        CadAnalyzeToleranceCandidatesCommand,
        CadComputeMeasurementsCommand,
        CadToleranceAnalysisResult,
    )


class CadApplicationMixin:
    async def analyze_tolerance_candidates(
        self,
        direct_session_alias: str,
        command: CadAnalyzeToleranceCandidatesCommand,
    ) -> CadToleranceAnalysisResult:
        from CAD2BIM_backend.application.cad_measurement_service import (
            CadMeasurementApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        service = self._require_cad_measurement_service()
        deadline = monotonic() + CAD_TOLERANCE_ANALYZER_BUDGET.total_timeout_seconds
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                scope = await self._resolve_scope(
                    direct_session_alias,
                    "cad_analyze_tolerance_candidates",
                )
                with self._scope_binder(scope.identity):
                    return await service.analyze(scope, command, deadline=deadline)
        except TimeoutError:
            raise DirectApplicationError(
                "TIMEOUT",
                "The tolerance analyzer total deadline expired",
                phase="execution",
                retryable=False,
            ) from None
        except CadMeasurementApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase=exc.phase,
                retryable=exc.retryable,
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None

    async def compute_measurements(
        self,
        direct_session_alias: str,
        command: CadComputeMeasurementsCommand,
    ) -> DataTransformToolResult:
        from CAD2BIM_backend.application.cad_measurement_service import (
            CadMeasurementApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        service = self._require_cad_measurement_service()
        deadline = monotonic() + CAD_MEASUREMENT_COMPUTE_BUDGET.total_timeout_seconds
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                scope = await self._resolve_scope(
                    direct_session_alias,
                    "cad_compute_measurements",
                )
                with self._scope_binder(scope.identity):
                    result = await service.compute(scope, command, deadline=deadline)
            return _data_transform_result(result)
        except TimeoutError:
            raise DirectApplicationError(
                "TIMEOUT",
                "The 180-second CAD measurement deadline expired before publication",
                phase="execution",
                retryable=False,
            ) from None
        except CadMeasurementApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase=exc.phase,
                retryable=exc.retryable,
                operation_alias=exc.operation_alias,
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except (KeyError, TypeError, ValueError, ValidationError):
            raise DirectApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The CAD measurement result could not be projected",
                phase="publication",
            ) from None

    def _require_cad_measurement_service(self) -> CadMeasurementService:
        if self._cad_measurement_service is None:
            raise DirectApplicationError(
                "SERVICE_UNAVAILABLE",
                "The M3 CAD measurement runtime is not configured",
                phase="preflight",
                retryable=True,
            )
        return self._cad_measurement_service
