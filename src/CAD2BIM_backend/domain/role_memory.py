"""Canonical LangChain messages in role memory."""

from __future__ import annotations

from typing import Any, Mapping

from langchain_core.messages import (
    BaseMessage,
    convert_to_messages,
    messages_from_dict,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
)


def parse_role_messages(values: Any) -> list[BaseMessage]:
    """Read current LangChain message objects or their official saved dicts."""

    if not isinstance(values, (list, tuple)):
        raise ValueError("role messages must be a list of LangChain messages")
    messages: list[BaseMessage] = []
    for value in values:
        if isinstance(value, BaseMessage):
            messages.append(value)
            continue
        if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
            raise ValueError("role message is not a LangChain message")
        try:
            if isinstance(value.get("data"), Mapping):
                messages.extend(messages_from_dict([dict(value)]))
            elif "content" in value:
                messages.extend(convert_to_messages([dict(value)]))
            else:
                raise ValueError("role message has no content")
        except (ValueError, TypeError, NotImplementedError) as exc:
            raise ValueError("role message is not a LangChain message") from exc
    return messages


class RoleMemory(BaseModel):
    """One role/stage slot; prior message prefixes are not merged on output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: list[SerializeAsAny[BaseMessage]] = Field(default_factory=list)
    conversation_summary: str = ""
    tool_chain_summary: str = ""

    @field_validator("messages", mode="before")
    @classmethod
    def normalize_messages(cls, value: Any) -> list[BaseMessage]:
        return parse_role_messages(value)


__all__ = ["RoleMemory", "parse_role_messages"]
