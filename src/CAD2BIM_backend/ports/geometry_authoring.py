"""Application-facing boundaries for Operation-backed geometry authoring."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationRecord,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.geometry_catalog import GeometrySourceMember
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.text_source import SourceBundleChange
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftCommitResult, AuthoringDraftRecord
from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceRecord
from CAD2BIM_backend.domain.control_plane import ArtifactRecord


class GeometryAuthoringRepository(Protocol):
    async def get_authoring_draft_snapshot(
        self, scope: InvocationScope, *, draft: AuthoringDraftRecord,
    ) -> ArtifactRecord: ...

    async def get_geometry_draft(
        self, scope: InvocationScope, *, resource_id: str,
    ) -> AuthoringDraftRecord | None: ...

    async def list_geometry_drafts(
        self, scope: InvocationScope, *, after_root_id: str | None = None, limit: int = 50,
    ) -> tuple[AuthoringDraftRecord, ...]: ...

    async def commit_geometry_draft(
        self, scope: InvocationScope, *, resource: GeometryResourceRecord,
        snapshot: ArtifactVersionRef, expected_generation: int | None,
        operation: CriticalOperationRecord,
    ) -> AuthoringDraftCommitResult: ...


    async def get_geometry_authoring_operation_state(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> CriticalOperationRecord | None: ...

    async def get_geometry_authoring_operation(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> PublishedCriticalOperation | None: ...


class GeometryAuthoringSourceFailure(StrEnum):
    """Small adapter-neutral vocabulary for bounded bundle failures."""

    INVALID = "invalid"
    MEMBER_NOT_FOUND = "member_not_found"
    MEMBER_NOT_TEXT = "member_not_text"
    STALE_SOURCE = "stale_source"
    MATCH_NOT_FOUND = "match_not_found"
    MATCH_NOT_UNIQUE = "match_not_unique"
    OVERLAP = "overlap"
    BUDGET_EXCEEDED = "budget_exceeded"
    ENGINE_FAILED = "engine_failed"


class GeometryAuthoringSourceError(RuntimeError):
    """Safe source-bundle failure projected by an infrastructure adapter."""

    def __init__(self, failure: GeometryAuthoringSourceFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class GeometryAuthoringPackedSource:
    content: bytes
    sha256: str


class GeometryAuthoringInspectedSource(Protocol):
    @property
    def sha256(self) -> str: ...

    @property
    def members(self) -> tuple[GeometrySourceMember, ...]: ...

    def read_text(self, path: str) -> str: ...


class GeometryAuthoringSourcePort(Protocol):
    """Canonical bundle codec/patch adapter used by the Application Core."""

    @property
    def maximum_text_member_bytes(self) -> int: ...

    def pack(self, members: Mapping[str, bytes]) -> GeometryAuthoringPackedSource: ...

    def inspect(self, content: bytes) -> GeometryAuthoringInspectedSource: ...

    def validate_function_source(self, content: bytes) -> None: ...

    def mutate(
        self,
        content: bytes,
        expected_sha256: str,
        changes: Sequence[SourceBundleChange],
        *,
        maximum_members: int,
        maximum_member_bytes: int,
        maximum_total_bytes: int,
    ) -> GeometryAuthoringPackedSource: ...


__all__ = [
    "GeometryAuthoringInspectedSource",
    "GeometryAuthoringPackedSource",
    "GeometryAuthoringRepository",
    "GeometryAuthoringSourceError",
    "GeometryAuthoringSourceFailure",
    "GeometryAuthoringSourcePort",
]
