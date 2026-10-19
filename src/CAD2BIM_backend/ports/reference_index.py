"""Durable navigation index; referenced domain objects remain their own authority."""

from typing import Protocol

from CAD2BIM_backend.domain.identity import TenantExecutionContext


class ReferenceIndexError(ValueError):
    """A short reference is unavailable or its stored binding is inconsistent."""


class ReferenceIndexPort(Protocol):
    async def get_or_create_reference(
        self,
        context: TenantExecutionContext,
        *,
        scope_digest: str,
        reference_kind: str,
        sealed_reference: str,
    ) -> str: ...

    async def resolve_reference(
        self,
        context: TenantExecutionContext,
        *,
        scope_digest: str,
        handle: str,
    ) -> tuple[str, str] | None: ...
