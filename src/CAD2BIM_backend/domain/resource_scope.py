"""Shared owner-scope identity for durable reusable catalog resources."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResourceOwnerScopeKind(StrEnum):
    SYSTEM = "system"
    TENANT = "tenant"
    TASK = "task"


class ResourceDeletionError(RuntimeError):
    """A catalog root cannot leave the library under its current dependencies."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ResourceOwnerScope(BaseModel):
    """One durable owner boundary reused by geometry and product-family catalogs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_kind: ResourceOwnerScopeKind
    tenant_id: str | None = Field(default=None, min_length=1, max_length=80)
    task_id: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_owner_shape(self) -> Self:
        if self.scope_kind is ResourceOwnerScopeKind.SYSTEM:
            if self.tenant_id is not None or self.task_id is not None:
                raise ValueError("system resource scope has no tenant or task owner")
        elif self.scope_kind is ResourceOwnerScopeKind.TENANT:
            if self.tenant_id is None or self.task_id is not None:
                raise ValueError("tenant resource scope requires only tenant_id")
        elif self.tenant_id is None or self.task_id is None:
            raise ValueError("task resource scope requires tenant_id and task_id")
        return self

    @property
    def scope_id(self) -> str:
        if self.scope_kind is ResourceOwnerScopeKind.SYSTEM:
            return ResourceOwnerScopeKind.SYSTEM.value
        if self.scope_kind is ResourceOwnerScopeKind.TENANT:
            assert self.tenant_id is not None
            return self.tenant_id
        assert self.task_id is not None
        return self.task_id

    def permits_dependency(self, dependency: "ResourceOwnerScope") -> bool:
        """A reusable resource cannot depend on a more private resource."""
        return (
            dependency.scope_kind is ResourceOwnerScopeKind.SYSTEM
            or dependency == self
            or (
                self.scope_kind is ResourceOwnerScopeKind.TASK
                and dependency.scope_kind is ResourceOwnerScopeKind.TENANT
                and dependency.tenant_id == self.tenant_id
            )
        )


__all__ = ["ResourceOwnerScope", "ResourceOwnerScopeKind", "ResourceDeletionError"]
