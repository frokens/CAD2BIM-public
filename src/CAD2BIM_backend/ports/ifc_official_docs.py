"""Ports for stateless reads of the maintained buildingSMART IFC documentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class OfficialIfcDocsSearchCard:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True, slots=True)
class OfficialIfcDocument:
    title: str
    url: str
    blocks: tuple[str, ...]
    heading_indexes: tuple[int, ...]


class OfficialIfcDocsClientError(RuntimeError):
    def __init__(self, kind: Literal["unavailable", "invalid"], message: str) -> None:
        super().__init__(message)
        self.kind = kind


class OfficialIfcDocsClientPort(Protocol):
    async def search(
        self,
        query: str,
        max_results: int,
    ) -> tuple[OfficialIfcDocsSearchCard, ...]: ...

    async def read(self, url: str) -> OfficialIfcDocument: ...


__all__ = [
    "OfficialIfcDocsClientError",
    "OfficialIfcDocsClientPort",
    "OfficialIfcDocsSearchCard",
    "OfficialIfcDocument",
]
