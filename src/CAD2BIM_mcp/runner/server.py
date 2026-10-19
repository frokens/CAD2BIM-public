"""Independent HTTP FastMCP profile for Runner State and domain tools."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import os

from fastmcp import FastMCP

from CAD2BIM_backend.infrastructure.state_access_tokens import (
    FastMcpStateTokenVerifier,
    StateAccessTokenCodec,
)
from CAD2BIM_backend.infrastructure.runner_domain_tokens import (
    FastMcpRunnerTokenVerifier,
    RunnerDomainTokenCodec,
)
from CAD2BIM_mcp.runner.backend_client import RunnerStateBackendClient
from CAD2BIM_mcp.runner.authoring_tools import register_runner_authoring_tools
from CAD2BIM_mcp.runner.data_tools import register_runner_data_tools
from CAD2BIM_mcp.runner.ifc_tools import register_runner_ifc_tools
from CAD2BIM_mcp.runner.references import RunnerReferenceMiddleware
from CAD2BIM_mcp.runner.resources import (
    RunnerStateBoundaryMiddleware,
    RunnerStateResourceProvider,
)
from CAD2BIM_mcp.direct.tool_support import DirectToolInputErrorMiddleware, register_shared_provider_documents


def create_runner_state_mcp(
    *,
    codec: StateAccessTokenCodec,
    backend: RunnerStateBackendClient,
    domain_codec: RunnerDomainTokenCodec | None = None,
) -> FastMCP:
    """Build the authenticated provider without any request-global identity state."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield
        finally:
            await backend.aclose()

    server = FastMCP(
        name="CAD2BIM Runner State MCP",
        instructions=(
            "Domain failures carry operation_status. Only operation_status=failed with an "
            "Operation alias confirms the original persisted write failure. operation_status=not_started "
            "is an Application proof that the fixed original request was rejected without admission; "
            "Main may inspect that diagnostic and submit corrected inputs as a new action. Unknown outcomes, "
            "transport loss, and request timeouts require retaining the saved action identity "
            "and arguments for reconciliation; retryable=false alone does not prove FAILED "
            "or absence of a commit. Read failures do not change the underlying write outcome."
            if domain_codec is not None else None
        ),
        auth=(
            FastMcpRunnerTokenVerifier(codec, domain_codec)
            if domain_codec is not None
            else FastMcpStateTokenVerifier(codec)
        ),
        mask_error_details=True,
        strict_input_validation=False,
        lifespan=lifespan,
    )
    # This is one protected provider: authorization failures must not become empty listings.
    server.provider_error_strategy = "raise"
    server.add_middleware(RunnerStateBoundaryMiddleware(codec, backend, domain_codec))
    if domain_codec is not None:
        server.add_middleware(RunnerReferenceMiddleware(backend))
    server.add_middleware(DirectToolInputErrorMiddleware())
    server.add_provider(RunnerStateResourceProvider(codec=codec, backend=backend))
    if domain_codec is not None:
        server._provider_contract_documents = []
        register_runner_data_tools(server, backend)
        register_runner_ifc_tools(server, backend)
        register_runner_authoring_tools(server, backend)
        register_shared_provider_documents(server)
    return server


__all__ = ["create_runner_state_mcp"]


def cli() -> None:
    """Start the independent HTTP profile from explicit deployment configuration."""

    try:
        secret = base64.b64decode(os.environ["CAD2BIM_STATE_ACCESS_SECRET_B64"], validate=True)
        backend_url = os.environ["CAD2BIM_RUNNER_STATE_BACKEND_URL"]
        host = os.environ.get("CAD2BIM_RUNNER_MCP_HOST", "127.0.0.1")
        port = int(os.environ["CAD2BIM_RUNNER_MCP_PORT"])
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "Runner State MCP requires explicit service configuration"
        ) from exc
    codec = StateAccessTokenCodec(secret)
    server = create_runner_state_mcp(
        codec=codec,
        backend=RunnerStateBackendClient(backend_url),
        domain_codec=RunnerDomainTokenCodec(secret),
    )
    server.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    cli()
