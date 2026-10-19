"""Provider-facing adapter for source-first, read-only IFC query tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from time import monotonic

from pydantic import ValidationError

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.ifc_query import IfcQueryInputBinding
from CAD2BIM_backend.domain.text_source import (
    SourceEdit,
)
from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.application_support import ifc_direct_error

from CAD2BIM_mcp.direct.ifc.query.contracts import (
    DirectIfcQueryPythonInput,
    DirectIfcQueryCursorInput,
    DirectIfcQueryFreshInput,
    DirectIfcQueryRepairInput,
    IfcQueryPythonResult,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.ifc_query_service import IfcQueryService
    from CAD2BIM_backend.domain.invocation import InvocationScope


from CAD2BIM_backend.domain.runtime_execution import IFC_QUERY_EXECUTION, RUNTIME_RESPONSE_MARGIN_SECONDS

_IFC_QUERY_TIMEOUT_SECONDS = IFC_QUERY_EXECUTION.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS


class IfcQueryProviderApplication(Protocol):
    """Entry adapter implemented by the shared Direct application facade."""

    async def query_ifc_python(
        self,
        request: DirectIfcQueryPythonInput,
        *,
        request_identity: str,
    ) -> IfcQueryPythonResult: ...


class IfcQueryApplicationMixin:
    """Thin Direct projection over the entry-neutral IFC Query Application Core."""

    async def query_ifc_python(
        self,
        request: DirectIfcQueryPythonInput,
        *,
        request_identity: str,
    ) -> IfcQueryPythonResult:
        deadline = monotonic() + IFC_QUERY_EXECUTION.total_seconds
        async def action(invocation: InvocationScope) -> IfcQueryPythonResult:
            service = self._require_ifc_query_service()
            if isinstance(request, DirectIfcQueryFreshInput):
                page = await service.fresh(
                    invocation,
                    request.code,
                    request_identity,
                    tuple(
                        IfcQueryInputBinding(
                            input_name=item.input_name,
                            package_alias=item.package_alias,
                            payload_name=item.payload_name,
                        )
                        for item in request.inputs
                    ),
                    request.page_size,
                    deadline=deadline,
                )
            elif isinstance(request, DirectIfcQueryRepairInput):
                page = await service.repair_from_alias(
                    invocation,
                    request.parent_attempt_alias,

                    tuple(
                        SourceEdit(old_text=edit.old_text, new_text=edit.new_text)
                        for edit in request.edits
                    ),
                    request_identity,
                    request.page_size,
                    deadline=deadline,
                )
            elif isinstance(request, DirectIfcQueryCursorInput):
                page = await service.continue_page(invocation, request.cursor)
            else:  # pragma: no cover - closed Provider union
                raise DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The IFC query branch is invalid",
                    phase="preflight",
                )
            result_json = canonical_json(list(page.results))
            if len(result_json.encode("utf-8")) > 65_536:
                raise DirectApplicationError(
                    "IFC_QUERY_PAGE_TOO_LARGE",
                    "The query page exceeds the Provider response budget; restart with a smaller page_size",
                    phase="output_validation",
                )
            return IfcQueryPythonResult(
                attempt_alias=page.attempt_alias,
                parent_attempt_alias=page.parent_attempt_alias,
                source_sha256=page.source_sha256,
                status="succeeded",
                summary=(
                    f"Returned {page.returned_count} of {page.total_count} bounded IFC-query "
                    f"results from {page.result_bytes} persisted canonical JSON bytes."
                ),
                result_json=result_json,
                next_cursor=page.next_cursor,
            )

        return await self._ifc_query_read(
            request.direct_session_alias,
            "ifc_query_python",
            action,
            timeout_seconds=_IFC_QUERY_TIMEOUT_SECONDS,
            timeout_code="IFC_QUERY_TIMEOUT",
            timeout_message="The Direct IFC-query total deadline expired",
        )

    async def _ifc_query_read(
        self,
        direct_session_alias: str,
        action_name: str,
        operation,
        **timeout_options,
    ):
        from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
        from CAD2BIM_backend.application.ifc_query_service import IfcQueryApplicationError

        try:
            return await self._run_runtime_read(
                direct_session_alias,
                action_name,
                operation,
                **timeout_options,
            )
        except DirectApplicationError:
            raise
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except IfcQueryApplicationError as exc:
            diagnostic = dict(exc.diagnostic or {})
            if exc.attempt_alias is not None:
                diagnostic["attempt_alias"] = exc.attempt_alias
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase=exc.phase,
                retryable=exc.retryable,
                diagnostic=diagnostic or None,
            ) from None
        except (KeyError, TypeError, ValueError, ValidationError, UnicodeError):
            raise DirectApplicationError(
                "IFC_QUERY_INTEGRITY_ERROR",
                "The exact IFC query result could not be safely projected",
                phase="output_validation",
            ) from None
        except Exception as exc:
            raise ifc_direct_error(exc) from None

    def _require_ifc_query_service(self) -> IfcQueryService:
        service = getattr(self, "_ifc_query_service", None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC query service is unavailable; wait for server-side recovery",
                phase="preflight",
            )
        return service


__all__ = ["IfcQueryApplicationMixin", "IfcQueryProviderApplication"]
