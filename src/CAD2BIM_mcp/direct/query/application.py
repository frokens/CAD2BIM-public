"""Direct application behavior owned by this capability family."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

from pydantic import ValidationError

from CAD2BIM_backend.domain.data_query import (
    CadEntitiesQuery,
    CadEntityQuery,
    CadGetEntityRequest,
    CadGetEntityResult,
    CadQueryEntitiesRequest,
    CadQueryEntitiesResult,
    CadQueryXdataRequest,
    CadQueryXdataResult,
    CadXdataQuery,
    DataQueryRowsRequest,
    DataQueryRowsResult,
    DataRowsQuery,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError

from CAD2BIM_mcp.direct.application_support import (
    _direct_error,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.data_query_service import DataQueryService
    from CAD2BIM_backend.application.data_query_python_service import DataQueryPythonService
    from CAD2BIM_backend.application.data_query_sql_service import DataQuerySqlService
    from CAD2BIM_backend.application.data_json_service import DataJsonService
    from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlQuery, DataQuerySqlResult
    from CAD2BIM_backend.domain.data_query_python import (
        DataQueryPythonQuery,
        DataQueryPythonResult,
    )
    from CAD2BIM_backend.domain.data_json import JsonReadQuery, JsonReadResult


from CAD2BIM_backend.domain.runtime_execution import DATA_SQL_EXECUTION, DATA_PYTHON_EXECUTION

_TRANSFORM_TIMEOUT_SECONDS = DATA_SQL_EXECUTION.total_seconds
_PYTHON_QUERY_TIMEOUT_SECONDS = DATA_PYTHON_EXECUTION.total_seconds
_QUERY_CONTINUATION_TIMEOUT_SECONDS = 15.0


class QueryApplicationMixin:
    async def query_sql(
        self,
        direct_session_alias: str,
        query: DataQuerySqlQuery | None,
        cursor: str | None,
        *,
        request_identity: str | None = None,
    ) -> DataQuerySqlResult:
        from CAD2BIM_backend.application.data_query_sql_service import (
            DataQuerySqlApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlRequest

        service = self._require_query_sql_service()
        try:
            request = DataQuerySqlRequest(query=query, cursor=cursor)
        except ValidationError as exc:
            raise DirectApplicationError(
                "INVALID_ARGUMENT",
                "Select exactly one complete data_query_sql mode: fresh, repair, or cursor",
                phase="preflight",
            ) from exc

        deadline = monotonic() + _TRANSFORM_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                scope = await self._resolve_scope(direct_session_alias, "data_query_sql")
        except TimeoutError:
            raise DirectApplicationError(
                "TRANSFORM_TIMEOUT",
                "The SQL query total deadline expired before execution could start",
                phase="preflight",
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None

        try:
            with self._scope_binder(scope.identity):
                return await service.query(
                    scope, request, deadline=deadline, request_identity=request_identity
                )
        except DataQuerySqlApplicationError as exc:
            diagnostic = (
                {"attempt_alias": exc.attempt_alias} if exc.attempt_alias is not None else None
            )
            raise DirectApplicationError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                phase=exc.phase,
                diagnostic=diagnostic,
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None

    async def query_python(
        self,
        direct_session_alias: str,
        query: DataQueryPythonQuery | None,
        cursor: str | None,
        *,
        request_identity: str | None = None,
    ) -> DataQueryPythonResult:
        from CAD2BIM_backend.application.data_query_python_service import (
            DataQueryPythonApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        from CAD2BIM_backend.domain.data_query_python import DataQueryPythonRequest

        service = self._require_query_python_service()
        try:
            request = DataQueryPythonRequest(query=query, cursor=cursor)
        except ValidationError as exc:
            raise DirectApplicationError(
                "INVALID_ARGUMENT",
                "Select exactly one complete data_query_python mode: fresh, repair, or cursor",
                phase="preflight",
            ) from exc

        timeout_seconds = (
            _PYTHON_QUERY_TIMEOUT_SECONDS
            if query is not None
            else _QUERY_CONTINUATION_TIMEOUT_SECONDS
        )
        deadline = monotonic() + timeout_seconds
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                scope = await self._resolve_scope(direct_session_alias, "data_query_python")
        except TimeoutError:
            raise DirectApplicationError(
                "QUERY_TIMEOUT",
                f"The {int(timeout_seconds)}-second Python query deadline expired before execution",
                phase="preflight",
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None

        try:
            with self._scope_binder(scope.identity):
                return await service.query(
                    scope, request, deadline=deadline, request_identity=request_identity
                )
        except DataQueryPythonApplicationError as exc:
            diagnostic = (
                {"attempt_alias": exc.attempt_alias} if exc.attempt_alias is not None else None
            )
            raise DirectApplicationError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                phase=exc.phase,
                diagnostic=diagnostic,
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None

    async def repair_query_sql(
        self,
        direct_session_alias: str,
        parent_attempt_alias: str,

        edits,
        page_size: int,
        *,
        request_identity: str,
    ) -> DataQuerySqlResult:
        service = self._require_query_sql_service()
        from CAD2BIM_backend.application.data_query_sql_service import DataQuerySqlApplicationError
        from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError

        deadline = monotonic() + _TRANSFORM_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                scope = await self._resolve_scope(direct_session_alias, "data_query_sql")
        except TimeoutError:
            raise DirectApplicationError(
                "TRANSFORM_TIMEOUT",
                "The SQL repair total deadline expired before execution could start",
                phase="preflight",
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        try:
            with self._scope_binder(scope.identity):
                return await service.repair(
                    scope,
                    parent_attempt_alias=parent_attempt_alias,

                    edits=edits,
                    page_size=page_size,
                    request_identity=request_identity,
                    deadline=deadline,
                )
        except DataQuerySqlApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                phase=exc.phase,
                diagnostic=({"attempt_alias": exc.attempt_alias} if exc.attempt_alias else None),
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None

    async def repair_query_python(
        self,
        direct_session_alias: str,
        parent_attempt_alias: str,

        edits,
        page_size: int,
        *,
        request_identity: str,
    ) -> DataQueryPythonResult:
        service = self._require_query_python_service()
        from CAD2BIM_backend.application.data_query_python_service import (
            DataQueryPythonApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError

        deadline = monotonic() + _PYTHON_QUERY_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                scope = await self._resolve_scope(direct_session_alias, "data_query_python")
        except TimeoutError:
            raise DirectApplicationError(
                "QUERY_TIMEOUT",
                "The Python repair total deadline expired before execution could start",
                phase="preflight",
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        try:
            with self._scope_binder(scope.identity):
                return await service.repair(
                    scope,
                    parent_attempt_alias=parent_attempt_alias,

                    edits=edits,
                    page_size=page_size,
                    request_identity=request_identity,
                    deadline=deadline,
                )
        except DataQueryPythonApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                phase=exc.phase,
                diagnostic=({"attempt_alias": exc.attempt_alias} if exc.attempt_alias else None),
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None

    async def read_json(
        self,
        direct_session_alias: str,
        query: JsonReadQuery | None,
        cursor: str | None,
    ) -> JsonReadResult:
        from CAD2BIM_backend.application.data_json_service import (
            DataJsonApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        from CAD2BIM_backend.domain.data_json import JsonReadRequest

        service = self._require_json_service()
        try:
            request = JsonReadRequest(query=query, cursor=cursor)
            scope = await self._resolve_scope(direct_session_alias, "data_read_json")
            with self._scope_binder(scope.identity):
                return await service.read_json(scope, request)
        except ValidationError as exc:
            raise DirectApplicationError(
                "INVALID_ARGUMENT",
                "Exactly one complete data_read_json query or cursor is required",
            ) from exc
        except DataJsonApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                phase="read",
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc) from None

    async def query_rows(
        self,
        direct_session_alias: str,
        query: DataRowsQuery | None,
        cursor: str | None,
    ) -> DataQueryRowsResult:
        request = self._paged_request(DataQueryRowsRequest, query, cursor)
        return await self._run_query(
            direct_session_alias,
            "query_rows",
            request,
        )

    async def query_entities(
        self,
        direct_session_alias: str,
        query: CadEntitiesQuery | None,
        cursor: str | None,
    ) -> CadQueryEntitiesResult:
        request = self._paged_request(CadQueryEntitiesRequest, query, cursor)
        return await self._run_query(
            direct_session_alias,
            "query_entities",
            request,
        )

    async def get_entity(
        self,
        direct_session_alias: str,
        query: CadEntityQuery | None,
        cursor: str | None,
    ) -> CadGetEntityResult:
        request = self._paged_request(CadGetEntityRequest, query, cursor)
        return await self._run_query(
            direct_session_alias,
            "get_entity",
            request,
        )

    async def query_xdata(
        self,
        direct_session_alias: str,
        query: CadXdataQuery | None,
        cursor: str | None,
    ) -> CadQueryXdataResult:
        request = self._paged_request(CadQueryXdataRequest, query, cursor)
        return await self._run_query(
            direct_session_alias,
            "query_xdata",
            request,
        )

    async def _run_query(self, direct_session_alias: str, method_name: str, request):
        service = self._require_query_service()
        deadline = monotonic() + 15.0
        try:
            async with asyncio.timeout(15.0):
                scope = await self._resolve_scope(direct_session_alias, method_name)
                with self._scope_binder(scope.identity):
                    method = getattr(service, method_name)
                    result = await method(scope, request)
            if monotonic() > deadline:
                raise DirectApplicationError("QUERY_TIMEOUT", "The query timed out")
            return result
        except TimeoutError:
            raise DirectApplicationError("QUERY_TIMEOUT", "The query timed out") from None
        except Exception as exc:
            self._raise_query_error(exc)

    @staticmethod
    def _paged_request(request_type, query, cursor):
        try:
            return request_type(query=query, cursor=cursor)
        except ValidationError as exc:
            raise DirectApplicationError(
                "INVALID_ARGUMENT",
                "Exactly one of query or cursor is required",
            ) from exc

    def _require_query_service(self) -> DataQueryService:
        if self._query_service is None:
            raise DirectApplicationError(
                "QUERY_RUNTIME_UNAVAILABLE",
                "The Phase-2 query runtime is not configured",
            )
        return self._query_service

    def _require_query_python_service(self) -> DataQueryPythonService:
        if self._query_python_service is None:
            raise DirectApplicationError(
                "QUERY_SERVICE_UNAVAILABLE",
                "The Q0 Python query runtime is not configured",
                phase="preflight",
                retryable=True,
            )
        return self._query_python_service

    def _require_query_sql_service(self) -> DataQuerySqlService:
        if self._query_sql_service is None:
            raise DirectApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE",
                "The M2b SQL query runtime is not configured",
                phase="preflight",
                retryable=True,
            )
        return self._query_sql_service

    def _require_json_service(self) -> DataJsonService:
        if self._json_service is None:
            raise DirectApplicationError(
                "JSON_RUNTIME_UNAVAILABLE",
                "The Data JSON read runtime is not configured",
            )
        return self._json_service

    @staticmethod
    def _raise_query_error(error: Exception):
        from CAD2BIM_backend.application.data_query_service import (
            DataQueryApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        if isinstance(error, DataQueryApplicationError):
            raise DirectApplicationError(error.code, str(error)) from None
        if isinstance(error, DataRuntimeApplicationError):
            raise _direct_error(error) from None
        if isinstance(error, DirectApplicationError):
            raise error
        raise error
