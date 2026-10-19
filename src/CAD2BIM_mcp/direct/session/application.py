"""Direct application behavior owned by this capability family."""

from __future__ import annotations

from typing import TYPE_CHECKING


from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)

from CAD2BIM_mcp.direct.application_support import (
    _direct_error,
)

if TYPE_CHECKING:
    pass


class SessionApplicationMixin:
    async def open_direct_session(self, session_name: str) -> DirectSessionResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        try:
            opened = await self._sessions.open_direct_session(session_name)
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc) from None
        return DirectSessionResult(
            direct_session_alias=opened.binding.direct_session_alias,
            session_name=opened.binding.session_name,
            resumed=opened.resumed,
        )
