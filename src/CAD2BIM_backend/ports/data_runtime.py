"""Persistence boundary for Direct sessions and Data Runtime resources."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Protocol, Sequence

from CAD2BIM_backend.domain.control_plane import RunRecord
from CAD2BIM_backend.domain.data_runtime import (
    CatalogEntryPage,
    CatalogResourceKind,
    AuthoringExecutionClaim,
    AuthoringExecutionState,
    CriticalOperationRecord,
    CriticalOperationKind,
    CriticalOperationStatus,
    IfcProductsRecoveryOperation,
    DataCatalogEntry,
    DataPackageMemberRecord,
    DataPackageRecord,
    DataTransformExecutionClaim,
    DataTransformOperationState,
    OpenDirectSessionRequest,
    OpenedDirectSession,
    PublishedCriticalOperation,
    PublishedOperationCatalogSummary,
    ProductFamilyOperationSource,
    PublishedDataPackage,
    PublishedImportResult,
    PublishedPackageResult,
)
from CAD2BIM_backend.domain.invocation import InvocationScope


class DataRuntimeRepository(Protocol):
    async def list_ifc_products_recovery_operations(
        self, scope: InvocationScope, *, tool_name: str, before_created_at: datetime | None = None,
        before_operation_id: str | None = None, limit: int = 20,
    ) -> tuple[IfcProductsRecoveryOperation, ...]: ...

    async def get_ifc_products_recovery_operation(
        self, scope: InvocationScope, *, origin_run_id: str, operation_id: str,
    ) -> IfcProductsRecoveryOperation | None: ...

    async def get_operation_summary(self, scope: InvocationScope, operation_id: str,
                                    ) -> PublishedOperationCatalogSummary | None: ...

    async def get_operation_for_result_package(
        self, scope: InvocationScope, package_id: str,
    ) -> PublishedCriticalOperation | None: ...

    async def get_operation_by_locator(
        self, scope: InvocationScope, *, origin_run_id: str, operation_id: str,
    ) -> PublishedCriticalOperation | None: ...

    async def get_authoring_publication_for_version(
        self, scope: InvocationScope, *, root_kind: str, version_id: str,
    ) -> PublishedOperationCatalogSummary | None: ...

    async def get_geometry_case_reports_for_snapshot(
        self, scope: InvocationScope, *, resource_id: str, snapshot_version_id: str,
        before: tuple[datetime, str] | None = None, page_size: int = 20,
    ) -> tuple[PublishedCriticalOperation, ...]: ...

    async def get_authoring_save_for_snapshot(
        self, scope: InvocationScope, *, root_kind: str, root_id: str,
        snapshot_artifact_version_id: str,
    ) -> PublishedCriticalOperation | None: ...

    async def prepare_geometry_inspection_operation(
        self, scope: InvocationScope, prepared: CriticalOperationRecord,
    ) -> CriticalOperationRecord: ...

    async def get_authoring_execution_state(
        self, scope: InvocationScope, operation_id: str,
        *, operation_kind: CriticalOperationKind,
    ) -> AuthoringExecutionState | None: ...

    async def claim_authoring_execution(
        self, scope: InvocationScope, operation_id: str,
        *, operation_kind: CriticalOperationKind,
    ) -> AuthoringExecutionClaim | None: ...

    async def reclaim_authoring_execution(
        self, scope: InvocationScope, operation_id: str,
        *, operation_kind: CriticalOperationKind,
        expected_owner: str, expected_generation: int,
    ) -> AuthoringExecutionClaim | None: ...

    async def complete_geometry_inspection_operation(
        self, scope: InvocationScope, terminal: CriticalOperationRecord,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> PublishedCriticalOperation: ...

    async def open_direct_session(
        self,
        request: OpenDirectSessionRequest,
    ) -> OpenedDirectSession: ...

    async def get_direct_session_by_alias(
        self,
        *,
        tenant_id: str,
        configured_subject: str,
        session_alias: str,
    ) -> OpenedDirectSession | None: ...

    async def get_run(self, owner_id: str, run_id: str) -> RunRecord | None: ...

    async def publish_import_result(
        self,
        scope: InvocationScope,
        package: DataPackageRecord,
        members: Sequence[DataPackageMemberRecord],
        operation: CriticalOperationRecord,
    ) -> PublishedImportResult: ...

    async def publish_package_result(
        self,
        scope: InvocationScope,
        package: DataPackageRecord,
        members: Sequence[DataPackageMemberRecord],
        operation: CriticalOperationRecord,
        *,
        execution_claim: DataTransformExecutionClaim | None = None,
    ) -> PublishedPackageResult: ...

    async def record_failed_operation(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *,
        execution_claim: DataTransformExecutionClaim | None = None,
    ) -> PublishedCriticalOperation: ...

    async def get_data_transform_operation_by_identity(
        self, scope: InvocationScope, request_identity: str
    ) -> DataTransformOperationState | None: ...

    async def prepare_data_transform_operation(
        self,
        scope: InvocationScope,
        prepared: CriticalOperationRecord,
        *,
        request_identity: str,
        request_digest: str,
        request_payload: Mapping[str, object],
        resolved_command: Mapping[str, object],
        resolved_parent_operation_alias: str | None = None,
        deadline_at: datetime,
    ) -> DataTransformOperationState:
        """Admit data_derive, retaining exact sources and checking new repair CURRENT atomically."""
        ...

    async def claim_data_transform_execution(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        owner: str,
        lease_seconds: float,
    ) -> DataTransformExecutionClaim | None: ...

    async def reclaim_data_transform_execution(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        expected_owner: str,
        expected_generation: int,
        owner: str,
        lease_seconds: float,
    ) -> DataTransformExecutionClaim | None: ...

    async def expire_data_transform_operation(
        self,
        scope: InvocationScope,
        operation_id: str,
        *,
        expected_owner: str | None,
        expected_generation: int,
        failed_operation: CriticalOperationRecord,
    ) -> DataTransformOperationState | None: ...

    async def stage_data_transform_candidate(
        self,
        scope: InvocationScope,
        operation_id: str,
        package: DataPackageRecord,
        members: Sequence[DataPackageMemberRecord],
        *,
        execution_claim: DataTransformExecutionClaim,
    ) -> DataTransformOperationState: ...

    async def get_result_for_operation(
        self, scope: InvocationScope, operation_id: str
    ) -> PublishedPackageResult | None: ...

    async def get_input_package_for_operation(
        self, scope: InvocationScope, operation_id: str, package_id: str
    ) -> PublishedDataPackage | None: ...

    async def get_package(
        self,
        scope: InvocationScope,
        package_id: str,
    ) -> PublishedDataPackage | None: ...

    async def get_operation(
        self,
        scope: InvocationScope,
        operation_id: str,
    ) -> PublishedCriticalOperation | None: ...

    async def get_operation_replay(
        self,
        scope: InvocationScope,
        tool_names: Sequence[str],
        request_fingerprint: str,
    ) -> PublishedCriticalOperation | None: ...

    async def get_product_family_operation_source(
        self, scope: InvocationScope, operation_id: str,
    ) -> ProductFamilyOperationSource | None: ...

    async def get_catalog_entry_by_alias(
        self,
        scope: InvocationScope,
        resource_kind: CatalogResourceKind,
        alias: str,
    ) -> DataCatalogEntry | None: ...

    async def list_package_catalog(
        self,
        scope: InvocationScope,
        before_sequence: int | None = None,
        page_size: int = 20,
    ) -> CatalogEntryPage: ...

    async def list_operation_catalog(
        self,
        scope: InvocationScope,
        before_sequence: int | None = None,
        page_size: int = 20,
        operation_kind: CriticalOperationKind | None = None,
        *, tool_name: str | None = None, outcome: CriticalOperationStatus | None = None,
    ) -> CatalogEntryPage: ...

    async def list_terminal_operation_catalog(
        self,
        scope: InvocationScope,
        *,
        tool_names: Sequence[str],
        statuses: Sequence[CriticalOperationStatus],
        require_code_artifact: bool = False,
        before_sequence: int | None = None,
        page_size: int = 20,
    ) -> CatalogEntryPage: ...

    async def list_catalog(
        self,
        scope: InvocationScope,
        resource_kind: CatalogResourceKind | None = None,
    ) -> tuple[DataCatalogEntry, ...]: ...


__all__ = ["DataRuntimeRepository"]
