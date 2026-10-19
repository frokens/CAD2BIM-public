"""Input budget measurements with an explicit local-estimate fallback."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

import tiktoken
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableBinding


@dataclass(frozen=True, slots=True)
class InputTokenCount:
    tokens: int
    source: Literal["official", "local_estimate"]
    fallback_reason: str | None = None


def estimate_input_tokens(
    messages: Sequence[BaseMessage], provider_tools: Sequence[dict[str, Any]],
) -> int:
    """Estimate complete messages and compact tools; this is not provider usage."""

    total = count_tokens_approximately(list(messages))
    if provider_tools:
        text = json.dumps(list(provider_tools), ensure_ascii=False, separators=(",", ":"))
        total += len(_budget_encoding().encode(text, disallowed_special=()))
    return total


@lru_cache(maxsize=1)
def _budget_encoding():
    return tiktoken.get_encoding("o200k_base")


def local_input_token_count(
    messages: Sequence[BaseMessage], provider_tools: Sequence[dict[str, Any]], *, reason: str,
) -> InputTokenCount:
    result = InputTokenCount(estimate_input_tokens(messages, provider_tools), "local_estimate", reason)
    logging.getLogger(__name__).warning(
        "Input token count source=%s tokens=%s fallback_reason=%s",
        result.source, result.tokens, result.fallback_reason,
    )
    return result


async def count_request_input_tokens(
    model: Any, messages: Sequence[BaseMessage], provider_tools: Sequence[dict[str, Any]] = (),
) -> InputTokenCount:
    """Use the actual bound model's request constructor, retaining invocation kwargs."""

    kwargs: dict[str, Any] = {}
    while isinstance(model, RunnableBinding):
        kwargs = {**model.kwargs, **kwargs}
        model = model.bound
    counter = getattr(model, "acount_input_tokens", None)
    if counter is None:
        return local_input_token_count(messages, provider_tools, reason="model_has_no_official_counter")
    return await counter(list(messages), **kwargs)


__all__ = ["InputTokenCount", "count_request_input_tokens", "estimate_input_tokens", "local_input_token_count"]
