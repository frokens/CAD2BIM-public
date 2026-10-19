"""Runner node adapter for one immutable MCP State READ_BATCH binding."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import CallToolResult, Resource, TextResourceContents

from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.state_read_binding import ExactStateReadBinding
from CAD2BIM_backend.infrastructure.state_access_tokens import StateAccessTokenCodec


class RunnerStateReadBatch:
    """The fixed exact binding for one node decision; the next node opens a new batch."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def list_resources(self) -> list[Resource]:
        return await self._client.list_resources()

    async def read_text(self, uri: str) -> str:
        contents = await self._client.read_resource(uri)
        if len(contents) != 1 or not isinstance(contents[0], TextResourceContents):
            raise RuntimeError("State Resource did not return exactly one text body")
        return contents[0].text

class RunnerStateMcpClient:
    def __init__(self, *, mcp_url: str, codec: StateAccessTokenCodec) -> None:
        if not mcp_url:
            raise ValueError("Runner State MCP URL is required")
        self._mcp_url = mcp_url
        self._codec = codec

    @property
    def mcp_url(self) -> str:
        return self._mcp_url

    @asynccontextmanager
    async def open_read_batch(
        self, *, grant: RunnerCheckpointGrant, binding: ExactStateReadBinding
    ) -> AsyncIterator[RunnerStateReadBatch]:
        pair = self._codec.issue_running(grant, binding)
        # Never mutate headers on a shared transport or persist either credential.
        transport = StreamableHttpTransport(
            self._mcp_url,
            headers={
                "Authorization": f"Bearer {pair.access_token}",
                "X-CAD2BIM-State-Binding": pair.read_binding,
            },
        )
        async with Client(transport) as client:
            yield RunnerStateReadBatch(client)


__all__ = ["RunnerStateMcpClient", "RunnerStateReadBatch"]
