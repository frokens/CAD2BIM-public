"""Per-request FastMCP State Resource provider; no global user registration."""

from __future__ import annotations

from collections.abc import Sequence

from fastmcp.resources import Resource
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.base import Provider

from CAD2BIM_backend.infrastructure.state_access_tokens import (
    StateAccessDenied,
    StateAccessTokenCodec,
)
from CAD2BIM_backend.infrastructure.runner_domain_tokens import (
    RunnerDomainAccessDenied,
    RunnerDomainTokenCodec,
)
from CAD2BIM_mcp.runner.backend_client import BackendStateReadError, RunnerStateBackendClient


_BINDING_HEADER = "x-cad2bim-state-binding"
_ALLOWED_URIS = frozenset(
    {"state://requirements", "state://clarifications", "state://requirement-md"}
)


def request_state_credentials(codec: StateAccessTokenCodec) -> tuple[str, str]:
    """Read and validate the current HTTP request, including its exact binding."""

    access = get_access_token()
    if access is None:
        raise StateAccessDenied("State MCP request has no service credential")
    read_binding = get_http_request().headers.get(_BINDING_HEADER, "")
    if not read_binding:
        raise StateAccessDenied("State MCP request has no exact read binding")
    codec.verify_pair(access.token, read_binding)
    return access.token, read_binding


def request_domain_credential() -> str:
    """Use the HTTP credential authenticated by FastMCP for this request."""

    access = get_access_token()
    if access is None or "domain:execute" not in access.scopes:
        raise RunnerDomainAccessDenied("Runner domain request has no execution credential")
    return access.token


class RunnerStateBoundaryMiddleware(Middleware):
    """Authorize local tools; Resource requests authorize at their backend endpoint."""

    def __init__(
        self,
        codec: StateAccessTokenCodec,
        backend: RunnerStateBackendClient,
        domain_codec: RunnerDomainTokenCodec | None = None,
    ) -> None:
        self._codec = codec
        self._backend = backend
        self._domain_codec = domain_codec

    async def _guard(self) -> None:
        access_token, read_binding = request_state_credentials(self._codec)
        await self._backend.authorize(access_token=access_token, read_binding=read_binding)

    async def on_list_tools(self, context, call_next):
        access = get_access_token()
        if access is not None and "domain:execute" in access.scopes:
            if self._domain_codec is None:
                raise RunnerDomainAccessDenied("Runner domain tools are unavailable")
            token = request_domain_credential()
            await self._backend.authorize_domain(access_token=token)
        else:
            await self._guard()
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        if self._domain_codec is None:
            await self._guard()
        else:
            request_domain_credential()
        return await call_next(context)

    async def on_read_resource(self, context, call_next):
        if str(context.message.uri).startswith("cad2bim://contracts/"):
            # Static contracts do not call a Backend endpoint. Validate the
            # existing exact State grant at this boundary before their read.
            await self._guard()
        return await call_next(context)


class RunnerStateResourceProvider(Provider):
    def __init__(self, *, codec: StateAccessTokenCodec, backend: RunnerStateBackendClient) -> None:
        super().__init__()
        self._codec = codec
        self._backend = backend

    async def _list_resources(self) -> Sequence[Resource]:
        access_token, read_binding = request_state_credentials(self._codec)
        entries = await self._backend.list_resources(
            access_token=access_token, read_binding=read_binding
        )
        resources: list[Resource] = []
        for entry in entries:
            uri = entry.get("uri")
            if uri not in _ALLOWED_URIS:
                raise RuntimeError("backend returned an undeclared State Resource")

            async def _read_current(*, _uri: str = uri) -> str:
                # A listed Resource must not retain the listing request's credentials.
                current_token, current_binding = request_state_credentials(self._codec)
                _, text = await self._backend.read_resource(
                    access_token=current_token, read_binding=current_binding, uri=_uri
                )
                return text

            resources.append(
                Resource.from_function(
                    _read_current,
                    uri=uri,
                    name=str(entry["name"]),
                    description=str(entry["description"]),
                    mime_type=str(entry["mime_type"]),
                )
            )
        return resources

    async def _get_resource(self, uri: str, version=None) -> Resource | None:
        if uri.startswith("cad2bim://contracts/"):
            # The existing static owner resolves this URI; its read middleware
            # already authorizes the exact grant once for the current request.
            return None
        access_token, read_binding = request_state_credentials(self._codec)
        if uri not in _ALLOWED_URIS:
            # No Resource request follows; retain dynamic authorization for this local miss.
            await self._backend.authorize(access_token=access_token, read_binding=read_binding)
            return None
        try:
            entry, text = await self._backend.read_resource(
                access_token=access_token, read_binding=read_binding, uri=uri
            )
        except BackendStateReadError as exc:
            if exc.status_code == 404:
                return None
            raise

        async def _read_exact() -> str:
            return text

        return Resource.from_function(
            _read_exact,
            uri=uri,
            name=str(entry["name"]),
            description=str(entry["description"]),
            mime_type=str(entry["mime_type"]),
        )


__all__ = [
    "RunnerStateBoundaryMiddleware",
    "RunnerStateResourceProvider",
    "request_domain_credential",
    "request_state_credentials",
]
