"""Application-facing boundary for Operation-backed ProductFamily authoring."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.domain.data_runtime import (
    AuthoringExecutionClaim,
    AuthoringExecutionState,
    CriticalOperationRecord,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.product_family_authoring import (
    ProductFamilyAuthoringCommitResult,
    ProductFamilyAuthoringPublication,
)
from CAD2BIM_backend.domain.authoring_draft import (
    AuthoringDraftCommitResult, AuthoringDraftRecord, AuthoringPublicationCommitResult, AuthoringPublicationResources,
)
from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef
from CAD2BIM_backend.domain.geometry_authoring import GeometryAuthoringSuccessPublication
from CAD2BIM_backend.domain.product_family import ProductFamilyRecord
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceVersionBundle
from CAD2BIM_backend.domain.control_plane import ArtifactRecord


class ProductFamilyAuthoringCommitFenceError(RuntimeError):
    """The authoring write was deterministically rejected before publication."""


class ProductFamilyAuthoringCommitOutcomeUnknownError(RuntimeError):
    """The authoring write may have committed and requires state reconciliation."""


class ProductFamilyAuthoringRepository(Protocol):
    async def get_latest_product_family_version_id(
        self, context: TenantExecutionContext, *, family_id: str,
    ) -> str | None: ...

    async def get_authoring_draft_snapshot(
        self, scope: InvocationScope, *, draft: AuthoringDraftRecord,
    ) -> ArtifactRecord: ...

    async def begin_authoring_publication(
        self, scope: InvocationScope, *, operation: CriticalOperationRecord,
        expected_drafts: tuple[AuthoringDraftRecord, ...],
    ) -> CriticalOperationRecord: ...

    async def finalize_authoring_publication_operation(
        self, scope: InvocationScope, *, operation: CriticalOperationRecord,
        execution_claim: AuthoringExecutionClaim,
    ) -> PublishedCriticalOperation: ...

    async def find_published_geometry_draft(
        self, scope: InvocationScope, *, draft: AuthoringDraftRecord,
    ) -> GeometryResourceVersionBundle | None: ...

    async def get_product_family_authoring_root(
        self, scope: InvocationScope, *, family_id: str,
    ) -> ProductFamilyRecord | None: ...

    async def find_current_authoring_publication(
        self, scope: InvocationScope, *, root_kind: str, root_id: str,
        expected_drafts: tuple[AuthoringDraftRecord, ...],
    ) -> AuthoringPublicationResources | None: ...

    async def get_product_family_draft(
        self, scope: InvocationScope, *, family_id: str,
    ) -> AuthoringDraftRecord | None: ...

    async def list_product_family_drafts(
        self, scope: InvocationScope, *, after_root_id: str | None = None, limit: int = 50,
    ) -> tuple[AuthoringDraftRecord, ...]: ...

    async def commit_product_family_draft(
        self, scope: InvocationScope, *, family: ProductFamilyRecord,
        snapshot: ArtifactVersionRef, expected_generation: int | None,
        operation: CriticalOperationRecord,
    ) -> AuthoringDraftCommitResult: ...

    async def commit_authoring_publication(
        self, scope: InvocationScope, *, operation: CriticalOperationRecord,
        geometry_publications: tuple[GeometryAuthoringSuccessPublication, ...] = (),
        family_publication: ProductFamilyAuthoringPublication | None = None,
        expected_drafts: tuple[AuthoringDraftRecord, ...],
        execution_claim: AuthoringExecutionClaim,
    ) -> AuthoringPublicationCommitResult: ...

    async def authorize_system_family_publication(self, scope: InvocationScope) -> None: ...

    async def get_product_family_authoring_execution_state(
        self, scope: InvocationScope, operation_id: str,
    ) -> AuthoringExecutionState | None: ...

    async def claim_product_family_authoring_execution(
        self, scope: InvocationScope, operation_id: str,
    ) -> AuthoringExecutionClaim | None: ...

    async def reclaim_product_family_authoring_execution(
        self, scope: InvocationScope, operation_id: str, *,
        expected_owner: str, expected_generation: int,
    ) -> AuthoringExecutionClaim | None: ...

    async def prepare_product_family_authoring_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
    ) -> CriticalOperationRecord: ...

    async def complete_product_family_authoring_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> PublishedCriticalOperation: ...

    async def mark_product_family_authoring_indeterminate(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> CriticalOperationRecord: ...

    async def reopen_product_family_authoring_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> CriticalOperationRecord: ...

    async def commit_product_family_authoring_success(
        self,
        scope: InvocationScope,
        publication: ProductFamilyAuthoringPublication,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> ProductFamilyAuthoringCommitResult: ...

    async def get_product_family_authoring_operation_state(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> CriticalOperationRecord | None: ...

    async def get_product_family_authoring_operation(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> PublishedCriticalOperation | None: ...


__all__ = [
    "ProductFamilyAuthoringCommitFenceError",
    "ProductFamilyAuthoringCommitOutcomeUnknownError",
    "ProductFamilyAuthoringRepository",
]
