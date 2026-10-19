"""Official Deep Agents Skills projected into the existing Role graph.

The deployment owns this read-only package directory. Only the official read
tool is exposed; filesystem mutation, execution and message eviction hooks are
never installed. Skill bodies have exactly one owner: ordinary ToolMessages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain.tools import ToolRuntime
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.runtime import Runtime
from pydantic import ValidationError


class AgentSkills:
    """Thin composition adapter; discovery and file interpretation remain official."""

    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise ValueError("The deployed Skill package directory does not exist")
        backend = FilesystemBackend(root_dir=root, virtual_mode=True)
        self.middleware = SkillsMiddleware(backend=backend, sources=["/"])
        filesystem = FilesystemMiddleware(
            backend=backend,
            tool_token_limit_before_evict=None,
            human_message_token_limit_before_evict=None,
        )
        self.read_tool = next(tool for tool in filesystem.tools if tool.name == "read_file")
        self.read_tool.description += (
            "\nOnly deployed Skill packages are accessible under this virtual root. "
            "Python/JSON files are read as supporting material, never executed. "
            "Chat history, user attachments, business assets and host files are not mounted."
        )
        self._catalog: dict[str, Any] | None = None

    async def discover(self) -> None:
        # Deployment metadata is rebuilt on startup, not a second checkpoint or
        # a body cache. The official hook owns all scanning/frontmatter parsing.
        update = await self.middleware.abefore_agent({}, Runtime(), {})
        if update is None or update.get("skills_load_errors"):
            raise ValueError("The deployed Skill package directory could not be read")
        self._catalog = dict(update)

    def project_system_message(self, message: SystemMessage, *, model: Any = None) -> SystemMessage:
        if self._catalog is None:
            raise RuntimeError("Skills must be discovered before building Role requests")
        request = ModelRequest(model=model, messages=[], system_message=message, state=self._catalog)
        projected = self.middleware.modify_request(request).system_message
        assert isinstance(projected, SystemMessage)
        return projected

    async def aread(self, call, *, state, config, runtime) -> ToolMessage:
        """Inject the current saved call into the official read tool implementation."""
        try:
            arguments = self.read_tool.args_schema.model_validate(call["args"]).model_dump()
        except ValidationError as exc:
            return ToolMessage(content=str(exc), name="read_file", tool_call_id=call["id"], status="error")
        tool_runtime = ToolRuntime(
            state=state, context=getattr(runtime, "context", None), config=config,
            stream_writer=lambda _chunk: None, tool_call_id=call["id"],
            store=None, tools=[self.read_tool],
        )
        try:
            return await self.read_tool.coroutine(**arguments, runtime=tool_runtime)
        except (ValueError, OSError):
            # The official virtual backend rejects escaping symlinks by raising.
            # Return a settled READ error without exposing resolved host paths.
            return ToolMessage(
                content="The requested Skill file is unavailable within the deployed package directory.",
                name="read_file", tool_call_id=call["id"], status="error",
            )


__all__ = ["AgentSkills"]
