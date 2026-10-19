"""Backend SYSTEM copy requests and facts retained by the original Operation."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from CAD2BIM_backend.domain.geometry_resource import GeometryResourceVersionBundle
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyVersionAssetRecord,
    _ProductFamilyModel,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind


def _system_owner() -> ResourceOwnerScope:
    return ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.SYSTEM)


class SystemFamilyPublicationCommand(_ProductFamilyModel):
    """One stable management request; authority is supplied by the composition root."""

    request_id: str = Field(min_length=1, max_length=200)
    source_family_id: str = Field(min_length=1, max_length=80)
    source_family_version_id: str = Field(min_length=1, max_length=80)
    target_owner: ResourceOwnerScope = Field(default_factory=_system_owner)

    @model_validator(mode="after")
    def require_system_target(self) -> Self:
        if self.target_owner != _system_owner():
            raise ValueError("SYSTEM publication requires an explicit SYSTEM target owner")
        return self


class SystemGeometryCopyDraft(_ProductFamilyModel):
    """Source identity and a fully prepared current Geometry bundle, without bytes."""

    source_resource_id: str = Field(min_length=1, max_length=80)
    source_resource_version_id: str = Field(min_length=1, max_length=80)
    source_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_owner_scope: ResourceOwnerScope
    target_version: GeometryResourceVersionBundle
    reuse_existing: bool = False

    @model_validator(mode="after")
    def require_system_copy(self) -> Self:
        target = self.target_version
        if target.resource.owner_scope != _system_owner():
            raise ValueError("published Family Geometry dependencies must be SYSTEM")
        if not self.reuse_existing and (
            target.version.version_no != 1
            or target.resource.resource_id == self.source_resource_id
            or target.version.resource_version_id == self.source_resource_version_id
        ):
            raise ValueError("Geometry copy requires a new root and version 1")
        return self


class SystemFamilyCopyPlan(_ProductFamilyModel):
    """Recovery facts on the same Family Operation, after immutable byte preparation."""

    source_family_id: str = Field(min_length=1, max_length=80)
    source_family_version_id: str = Field(min_length=1, max_length=80)
    source_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_owner_scope: ResourceOwnerScope
    source_kind: Literal["authorized_catalog", "selected_library"]
    source_reference: str = Field(min_length=1, max_length=2048)
    target_owner: ResourceOwnerScope = Field(default_factory=_system_owner)
    geometry_copies: tuple[SystemGeometryCopyDraft, ...] = Field(min_length=1)
    family_assets: tuple[ProductFamilyVersionAssetRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_closed_copy_plan(self) -> Self:
        if self.target_owner != _system_owner():
            raise ValueError("Family copy plan can target only SYSTEM")
        if self.source_owner_scope.scope_kind is not ResourceOwnerScopeKind.TASK:
            raise ValueError("SYSTEM copy publication requires a TASK source Family")
        sources = [item.source_resource_version_id for item in self.geometry_copies]
        targets = [item.target_version.version.resource_version_id for item in self.geometry_copies]
        if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
            raise ValueError("Geometry copy mapping must identify each exact resource once")
        return self


__all__ = [
    "SystemFamilyPublicationCommand", "SystemGeometryCopyDraft", "SystemFamilyCopyPlan",
]
