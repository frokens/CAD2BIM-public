"""Thin Direct projection over the owner-backed CodeDiscoveryService."""

from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError

from CAD2BIM_backend.application.code_discovery_service import (
    CodeCard,
    CodeListResult,
    CodeReadResult,
)
from CAD2BIM_mcp.direct.code.contracts import (
    BatchCodeReadInput,
    CodeListCursorInput,
    CodeListFirstPageInput,
    DirectCodeCard,
    DirectCodeListInput,
    DirectCodeListResult,
    DirectCodeReadInput,
    DirectCodeReadResult,
    DirectCodeReadBatchResult,
    DirectCodeReadResponse,
    to_code_list_query,
    to_code_read_selection,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.source_view import project_text_source_view


class CodeProviderApplication(Protocol):
    async def list_code(self, request: DirectCodeListInput) -> DirectCodeListResult: ...

    async def read_code(self, request: DirectCodeReadInput) -> DirectCodeReadResponse: ...


class CodeApplicationMixin:
    async def list_code(self, request: DirectCodeListInput) -> DirectCodeListResult:
        from CAD2BIM_backend.application.code_discovery_service import (
            CodeDiscoveryApplicationError,
        )

        try:
            if isinstance(request, CodeListFirstPageInput):
                query = to_code_list_query(request.query)
                cursor = None
            elif isinstance(request, CodeListCursorInput):
                query = None
                cursor = request.cursor
            else:  # pragma: no cover - closed Provider union
                raise ValueError("unknown code list branch")
            result = await self._run_runtime_read(
                request.direct_session_alias,
                "code_list",
                lambda scope: self._require_code_discovery_service().list_codes(
                    scope, query, cursor
                ),
            )
        except CodeDiscoveryApplicationError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase="read") from None

        try:
            return _project_code_list(result)
        except (TypeError, ValueError):
            raise DirectApplicationError(
                "CODE_INTEGRITY_ERROR",
                "The owner-backed Code directory could not be projected",
                phase="read",
            ) from None

    async def read_code(self, request: DirectCodeReadInput) -> DirectCodeReadResponse:
        from CAD2BIM_backend.application.code_discovery_service import (
            CodeDiscoveryApplicationError,
        )

        try:
            if isinstance(request, BatchCodeReadInput):
                selections = tuple(
                    to_code_read_selection(read) for read in request.reads
                )
                batch = await self._run_runtime_read(
                    request.direct_session_alias,
                    "code_read",
                    lambda scope: self._require_code_discovery_service().read_code_batch(
                        scope, selections
                    ),
                )
            else:
                selection = to_code_read_selection(request)
                result = await self._run_runtime_read(
                    request.direct_session_alias,
                    "code_read",
                    lambda scope: self._require_code_discovery_service().read_code(
                        scope,
                        selection.code_locator,
                        selection.source_path,
                        selection.view,
                    ),
                )
        except CodeDiscoveryApplicationError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase="read") from None

        if isinstance(request, BatchCodeReadInput):
            try:
                projected_reads = tuple(_project_code_read(result) for result in batch.reads)
            except (TypeError, ValueError):
                raise DirectApplicationError(
                    "CODE_INTEGRITY_ERROR",
                    "The owner-backed Code source could not be projected",
                    phase="read",
                ) from None
            try:
                return DirectCodeReadBatchResult(reads=projected_reads)
            except ValidationError:
                raise DirectApplicationError(
                    "CODE_BATCH_BUDGET_EXCEEDED",
                    "Batch exceeds its response budget; use fewer members or bounded views",
                    phase="read",
                ) from None

        try:
            return _project_code_read(result)
        except (TypeError, ValueError):
            raise DirectApplicationError(
                "CODE_INTEGRITY_ERROR",
                "The owner-backed Code source could not be projected",
                phase="read",
            ) from None

    def _require_code_discovery_service(self):
        service = self._code_discovery_service
        if service is None:
            raise DirectApplicationError(
                "CODE_DISCOVERY_UNAVAILABLE",
                "Code discovery is not configured",
                phase="read",
            )
        return service


def _project_code_card(result: CodeCard) -> DirectCodeCard:
    return DirectCodeCard(
        code_locator=result.code_locator,
        code_kind=result.code_kind,
        language=result.language,
        source_form=result.source_form,
        source_sha256=result.source_sha256,
        members=result.members,
        owner_type=result.owner_type,
        owner_alias=result.owner_alias,
        status=result.status,
        diagnostic_code=result.diagnostic_code,
        diagnostic_message=result.diagnostic_message,
        parent_owner_alias=result.parent_owner_alias,
        ownership=result.ownership,
        repairable=result.repairable,
        repair_entry=result.repair_entry,
        repair_unavailable_reason=result.repair_unavailable_reason,
        result_alias=result.result_alias,
        result_code_locator=result.result_code_locator,
        result_available=result.result_available,
    )


def _project_code_list(result: CodeListResult) -> DirectCodeListResult:
    from CAD2BIM_mcp.direct.ifc.product_family.application import _family_rejection

    return DirectCodeListResult(
        code_kind=result.code_kind,
        codes=tuple(_project_code_card(item) for item in result.codes),
        rejections=tuple(_family_rejection(item) for item in result.rejections),
        returned_count=result.returned_count,
        next_cursor=result.next_cursor,
    )


def _project_code_read(result: CodeReadResult) -> DirectCodeReadResult:
    projected = project_text_source_view(result.view)
    selected_source_sha256 = projected.pop("source_sha256")
    expected_selected_sha256 = (
        result.source_sha256 if result.source_form == "single_text" else result.member_sha256
    )
    if selected_source_sha256 != expected_selected_sha256:
        raise ValueError("selected Code source digest does not match its owner/member digest")
    return DirectCodeReadResult(
        code_locator=result.code_locator,
        code_kind=result.code_kind,
        language=result.language,
        source_form=result.source_form,
        source_sha256=result.source_sha256,
        source_path=result.selected_path,
        member_sha256=result.member_sha256,
        **projected,
    )


__all__ = ["CodeApplicationMixin", "CodeProviderApplication"]
