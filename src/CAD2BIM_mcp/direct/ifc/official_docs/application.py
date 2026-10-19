"""Direct application adapter for official buildingSMART documentation."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

from CAD2BIM_backend.domain.ifc_official_docs import OFFICIAL_IFC_DOCS_TIMEOUT_SECONDS
from CAD2BIM_mcp.direct.errors import DirectApplicationError

if TYPE_CHECKING:
    from CAD2BIM_backend.application.ifc_official_docs_service import IfcOfficialDocsService
    from CAD2BIM_backend.domain.ifc_official_docs import (
        IfcOfficialDocsReadQuery,
        IfcOfficialDocsReadResult,
        IfcOfficialDocsSearchQuery,
        IfcOfficialDocsSearchResult,
    )


_IFC_DOCS_TIMEOUT_SECONDS = OFFICIAL_IFC_DOCS_TIMEOUT_SECONDS


class IfcOfficialDocsApplicationMixin:
    async def search_ifc_official_docs(
        self,
        query: IfcOfficialDocsSearchQuery,
    ) -> IfcOfficialDocsSearchResult:
        from CAD2BIM_backend.application.ifc_official_docs_service import (
            IfcOfficialDocsApplicationError,
        )

        service = self._require_ifc_official_docs_service()
        deadline = monotonic() + _IFC_DOCS_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                return await service.search_official_docs(query, deadline=deadline)
        except TimeoutError:
            raise DirectApplicationError(
                "IFC_DOCS_UNAVAILABLE",
                "No official IFC documentation was obtained before the 45-second timeout",
                phase="read",
                retryable=True,
            ) from None
        except IfcOfficialDocsApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase="read",
                retryable=exc.retryable,
            ) from None

    async def read_ifc_official_doc(
        self,
        query: IfcOfficialDocsReadQuery,
    ) -> IfcOfficialDocsReadResult:
        from CAD2BIM_backend.application.ifc_official_docs_service import (
            IfcOfficialDocsApplicationError,
        )

        service = self._require_ifc_official_docs_service()
        deadline = monotonic() + _IFC_DOCS_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                return await service.read_official_doc(query, deadline=deadline)
        except TimeoutError:
            raise DirectApplicationError(
                "IFC_DOCS_UNAVAILABLE",
                "No official IFC documentation was obtained before the 45-second timeout",
                phase="read",
                retryable=True,
            ) from None
        except IfcOfficialDocsApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase="read",
                retryable=exc.retryable,
            ) from None

    def _require_ifc_official_docs_service(self) -> IfcOfficialDocsService:
        if self._ifc_official_docs_service is None:
            raise DirectApplicationError(
                "IFC_DOCS_UNAVAILABLE",
                "The official IFC documentation client is not configured",
                retryable=True,
            )
        return self._ifc_official_docs_service
