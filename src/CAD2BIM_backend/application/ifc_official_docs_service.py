"""Application orchestration for stateless official IFC documentation reads."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from CAD2BIM_backend.domain.ifc_official_docs import (
    OFFICIAL_IFC_DOCS_TIMEOUT_SECONDS,
    IfcOfficialDocsErrorCode,
    IfcOfficialDocsReadQuery,
    IfcOfficialDocsReadResult,
    IfcOfficialDocsSearchCard,
    IfcOfficialDocsSearchQuery,
    IfcOfficialDocsSearchResult,
)
from CAD2BIM_backend.ports.ifc_official_docs import (
    OfficialIfcDocsClientError,
    OfficialIfcDocsClientPort,
    OfficialIfcDocument,
)


_MAX_RESPONSE_BYTES = 32 * 1024
_MAX_SNIPPET_BYTES = 4 * 1024
_MAX_ERROR_BYTES = 2_000
_FOCUS_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
_ResultT = TypeVar("_ResultT")


class IfcOfficialDocsApplicationError(RuntimeError):
    """A bounded official-document failure suitable for MCP ToolError projection."""

    def __init__(
        self,
        code: str | IfcOfficialDocsErrorCode,
        message: str,
        *,
        phase: Literal["search", "read"],
        retryable: bool,
    ) -> None:
        bounded = _utf8_prefix(message.strip() or "Official IFC documentation read failed", _MAX_ERROR_BYTES)
        super().__init__(bounded)
        self.code = code.value if isinstance(code, IfcOfficialDocsErrorCode) else code
        self.phase = phase
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class IfcOfficialDocsPolicy:
    timeout_seconds: float = OFFICIAL_IFC_DOCS_TIMEOUT_SECONDS
    response_bytes: int = _MAX_RESPONSE_BYTES
    snippet_bytes: int = _MAX_SNIPPET_BYTES

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.response_bytes < 1 or self.snippet_bytes < 1:
            raise ValueError("IFC documentation application policy values must be positive")
        if (
            self.timeout_seconds > OFFICIAL_IFC_DOCS_TIMEOUT_SECONDS
            or self.response_bytes > _MAX_RESPONSE_BYTES
            or self.snippet_bytes > _MAX_SNIPPET_BYTES
        ):
            raise ValueError("IFC documentation application policy cannot weaken the hard limits")


class IfcOfficialDocsService:
    """Return search cards, then read only the official page selected by the Agent."""

    def __init__(
        self,
        client: OfficialIfcDocsClientPort,
        policy: IfcOfficialDocsPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._policy = policy or IfcOfficialDocsPolicy()
        self._clock = clock

    async def search_official_docs(
        self,
        query: IfcOfficialDocsSearchQuery,
        *,
        deadline: float | None = None,
    ) -> IfcOfficialDocsSearchResult:
        if not isinstance(query, IfcOfficialDocsSearchQuery):
            raise self._invalid("The official IFC documentation search input is invalid", "search")
        try:
            raw_cards = await self._within_deadline(
                self._client.search(query.query, query.max_results),
                deadline,
            )
            cards: list[IfcOfficialDocsSearchCard] = []
            for raw in raw_cards[: query.max_results]:
                card = IfcOfficialDocsSearchCard(
                    title=raw.title,
                    url=raw.url,
                    snippet=_utf8_prefix(raw.snippet, self._policy.snippet_bytes),
                )
                candidate = IfcOfficialDocsSearchResult(
                    query=query.query,
                    results=tuple((*cards, card)),
                )
                if _model_bytes(candidate) > self._policy.response_bytes:
                    break
                cards.append(card)
            return IfcOfficialDocsSearchResult(query=query.query, results=tuple(cards))
        except IfcOfficialDocsApplicationError:
            raise
        except OfficialIfcDocsClientError as exc:
            raise self._client_error(exc, "search") from exc
        except ValidationError as exc:
            raise self._invalid(
                "The official IFC search returned invalid card data",
                "search",
            ) from exc
        except TimeoutError as exc:
            raise self._unavailable(
                "The official IFC documentation search timed out before any official material was obtained",
                "search",
            ) from exc

    async def read_official_doc(
        self,
        query: IfcOfficialDocsReadQuery,
        *,
        deadline: float | None = None,
    ) -> IfcOfficialDocsReadResult:
        if not isinstance(query, IfcOfficialDocsReadQuery):
            raise self._invalid("The official IFC documentation read input is invalid", "read")
        try:
            document = await self._within_deadline(self._client.read(query.url), deadline)
            indexes = _focused_block_indexes(document, query.focus)
            selection_is_partial = len(indexes) < len(document.blocks)
            return _bounded_read_result(
                document,
                indexes,
                response_bytes=self._policy.response_bytes,
                already_truncated=selection_is_partial,
            )
        except IfcOfficialDocsApplicationError:
            raise
        except OfficialIfcDocsClientError as exc:
            raise self._client_error(exc, "read") from exc
        except (ValidationError, ValueError) as exc:
            raise self._invalid(
                "The official IFC page returned invalid document data",
                "read",
            ) from exc
        except TimeoutError as exc:
            raise self._unavailable(
                "The official IFC page read timed out before any official material was obtained",
                "read",
            ) from exc

    async def search(
        self,
        query: IfcOfficialDocsSearchQuery,
        *,
        deadline: float | None = None,
    ) -> IfcOfficialDocsSearchResult:
        return await self.search_official_docs(query, deadline=deadline)

    async def read(
        self,
        query: IfcOfficialDocsReadQuery,
        *,
        deadline: float | None = None,
    ) -> IfcOfficialDocsReadResult:
        return await self.read_official_doc(query, deadline=deadline)

    async def _within_deadline(
        self,
        awaitable: Awaitable[_ResultT],
        deadline: float | None,
    ) -> _ResultT:
        effective_deadline = min(
            deadline if deadline is not None else float("inf"),
            self._clock() + self._policy.timeout_seconds,
        )
        timeout = max(0.0, effective_deadline - self._clock())
        async with asyncio.timeout(timeout):
            return await awaitable

    @staticmethod
    def _client_error(
        error: OfficialIfcDocsClientError,
        phase: Literal["search", "read"],
    ) -> IfcOfficialDocsApplicationError:
        if error.kind == "unavailable":
            return IfcOfficialDocsApplicationError(
                IfcOfficialDocsErrorCode.UNAVAILABLE,
                f"No official IFC documentation was obtained: {error}",
                phase=phase,
                retryable=True,
            )
        return IfcOfficialDocsApplicationError(
            IfcOfficialDocsErrorCode.RESPONSE_INVALID,
            f"No official IFC documentation was obtained: {error}",
            phase=phase,
            retryable=False,
        )

    @staticmethod
    def _invalid(
        message: str,
        phase: Literal["search", "read"],
    ) -> IfcOfficialDocsApplicationError:
        return IfcOfficialDocsApplicationError(
            IfcOfficialDocsErrorCode.RESPONSE_INVALID,
            message,
            phase=phase,
            retryable=False,
        )

    @staticmethod
    def _unavailable(
        message: str,
        phase: Literal["search", "read"],
    ) -> IfcOfficialDocsApplicationError:
        return IfcOfficialDocsApplicationError(
            IfcOfficialDocsErrorCode.UNAVAILABLE,
            message,
            phase=phase,
            retryable=True,
        )


def _focused_block_indexes(document: OfficialIfcDocument, focus: str | None) -> tuple[int, ...]:
    all_indexes = tuple(range(len(document.blocks)))
    if focus is None:
        return all_indexes
    terms = tuple(dict.fromkeys(term.casefold() for term in _FOCUS_TERM.findall(focus)))
    if not terms:
        return all_indexes
    scored = []
    focus_folded = focus.casefold()
    for index, block in enumerate(document.blocks):
        folded = block.casefold()
        score = sum(1 for term in terms if term in folded)
        if focus_folded in folded:
            score += len(terms) + 1
        if score:
            scored.append((score, index))
    if not scored:
        return all_indexes

    selected: set[int] = set()
    for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))[:24]:
        selected.add(index)
        previous_headings = [heading for heading in document.heading_indexes if heading <= index]
        if previous_headings:
            selected.add(previous_headings[-1])
    return tuple(sorted(selected))


def _bounded_read_result(
    document: OfficialIfcDocument,
    indexes: Sequence[int],
    *,
    response_bytes: int,
    already_truncated: bool,
) -> IfcOfficialDocsReadResult:
    accepted: list[str] = []
    truncated = already_truncated
    for index in indexes:
        block = document.blocks[index]
        content = "\n\n".join((*accepted, block))
        candidate = IfcOfficialDocsReadResult(
            title=document.title,
            url=document.url,
            content=content,
            truncated=truncated,
        )
        if _model_bytes(candidate) <= response_bytes:
            accepted.append(block)
            continue
        truncated = True
        break

    if not accepted and indexes:
        block = document.blocks[indexes[0]]
        low, high = 0, len(block)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = IfcOfficialDocsReadResult(
                title=document.title,
                url=document.url,
                content=block[:middle],
                truncated=True,
            )
            if _model_bytes(candidate) <= response_bytes:
                low = middle
            else:
                high = middle - 1
        accepted.append(block[:low])
        truncated = True
    if len(accepted) < len(indexes):
        truncated = True

    result = IfcOfficialDocsReadResult(
        title=document.title,
        url=document.url,
        content="\n\n".join(accepted),
        truncated=truncated,
    )
    if _model_bytes(result) > response_bytes:
        raise ValueError("official IFC document metadata exceeds the response budget")
    return result


def _model_bytes(model: BaseModel) -> int:
    return len(model.model_dump_json().encode("utf-8"))


def _utf8_prefix(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore").rstrip()


__all__ = [
    "IfcOfficialDocsApplicationError",
    "IfcOfficialDocsPolicy",
    "IfcOfficialDocsService",
]
