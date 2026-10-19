"""Backend orchestration of exact copies through the original resource owners."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService, GeometryResourceIntegrityError
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService, ProductFamilyAssetIntegrityError
from CAD2BIM_backend.application.product_family_authoring_service import (
    ProductFamilyAuthoringApplicationError, ProductFamilyAuthoringService,
)
from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService
from CAD2BIM_backend.domain.control_plane import IdempotencyConflictError
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogIntegrityError
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogIntegrityError, ProductFamilyCatalogNotFoundError,
)
from CAD2BIM_backend.ports.identity import IdentityAccessDenied
from CAD2BIM_backend.domain.data_runtime import (
    AuthoringExecutionClaim, CriticalOperationErrorPhase,
    CriticalOperationRecord, CriticalOperationStatus,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceVersionBundle, geometry_resource_inputs,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext, stable_internal_identity_id
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_family import ProductFamilyVersionBundle
from CAD2BIM_backend.domain.product_family_authoring import ProductFamilyAuthoringCommitResult
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScopeKind
from CAD2BIM_backend.domain.system_family_publication import (
    SystemFamilyPublicationCommand, SystemGeometryCopyDraft,
)
from CAD2BIM_backend.ports.product_family_authoring import ProductFamilyAuthoringRepository
from CAD2BIM_backend.ports.system_family_publication import SystemFamilyPublicationSource

IdentityBinder = Callable[[TenantExecutionContext], AbstractContextManager[None]]


class CatalogSystemFamilySource:
    """Read with the source login and identity, separately from target authority."""

    source_kind = "authorized_catalog"

    def __init__(
        self, context: TenantExecutionContext, family_catalog: ProductFamilyCatalogService,
        family_assets: ProductFamilyAssetService, geometry_resources: GeometryResourceService,
        *, bind_identity: IdentityBinder,
    ) -> None:
        self.context = context
        self.source_reference = f"catalog-task:{context.task_id}"
        self._family_catalog = family_catalog
        self._family_assets = family_assets
        self._geometry_resources = geometry_resources
        self._bind_identity = bind_identity

    async def read_family(self, *, family_id: str, family_version_id: str):
        with self._bind_identity(self.context):
            ref = await self._family_catalog.version_ref_for_visible_id(self.context, family_version_id)
            bundle = await self._family_catalog.resolve_exact_version(self.context, ref)
            if bundle.family.family_id != family_id:
                raise ValueError("source Family exact root does not match")
            contents = tuple([await self._family_assets.read_exact_asset(bundle, asset)
                              for asset in bundle.assets])
            return bundle, contents

    async def read_geometry(self, *, resource_id: str, resource_version_id: str):
        with self._bind_identity(self.context):
            bundle = await self._geometry_resources.get_exact_version(
                self.context, resource_id=resource_id, resource_version_id=resource_version_id,
            )
            if bundle is None:
                raise ValueError("source Geometry exact version is not authorized or available")
            contents = []
            for asset in bundle.assets:
                result = await self._geometry_resources.read_exact_asset(
                    self.context, resource_id=resource_id, resource_version_id=resource_version_id,
                    asset_role=asset.asset_role,
                )
                if result is None or result[0] != asset:
                    raise ValueError("source Geometry exact asset changed")
                contents.append(result[1])
            return bundle, tuple(contents)


class SystemFamilyPublicationService:
    def __init__(
        self, repository: ProductFamilyAuthoringRepository,
        family_authoring: ProductFamilyAuthoringService,
        geometry_resources: GeometryResourceService, source: SystemFamilyPublicationSource | None,
        *, bind_identity: IdentityBinder,
    ) -> None:
        self._repository = repository
        self._family_authoring = family_authoring
        self._geometry_resources = geometry_resources
        self._source = source
        self._bind_identity = bind_identity

    async def publish(
        self, scope: InvocationScope, command: SystemFamilyPublicationCommand,
    ) -> ProductFamilyAuthoringCommitResult:
        command = SystemFamilyPublicationCommand.model_validate(command.model_dump(mode="python"))
        with self._bind_identity(scope.identity):
            original = await self._family_authoring.find_system_copy(scope, command)
        if original is None:
            try:
                if self._source is None:
                    raise ValueError("a new publication requires an authorized exact source")
                source, contents = await self._source.read_family(
                    family_id=command.source_family_id,
                    family_version_id=command.source_family_version_id,
                )
                if (source.family.family_id != command.source_family_id
                    or source.version.family_version_id != command.source_family_version_id
                    or source.family.owner_scope.scope_kind is not ResourceOwnerScopeKind.TASK):
                    raise ValueError("source Family changed its exact identity, content, or owner")
                planned = await self._prepare_geometry_closure(scope, source)
            except IdempotencyConflictError:
                raise
            except (ValueError, IdentityAccessDenied, ProductFamilyCatalogNotFoundError,
                    ProductFamilyCatalogIntegrityError, ProductFamilyAssetIntegrityError,
                    GeometryCatalogIntegrityError, GeometryResourceIntegrityError) as exc:
                raise ProductFamilyAuthoringApplicationError(
                    "SYSTEM_PUBLICATION_SOURCE_INVALID", "Source exact closure is unauthorized, incomplete, or corrupt",
                    phase="preflight", retryable=False,
                ) from exc
            # Immutable bytes are prepared outside DB transactions. Only after all
            # are readable is their complete current bundle retained on the Operation.
            copies = []
            with self._bind_identity(scope.identity):
                for draft, payloads in planned:
                    target = draft.target_version
                    if not draft.reuse_existing:
                        target = await self._geometry_resources.store_candidate_assets(
                            scope.identity, target, payloads,
                        )
                    await self._geometry_resources.verify_candidate_assets(scope.identity, target)
                    copies.append(draft.model_copy(update={"target_version": target}))
                try:
                    original = await self._family_authoring.prepare_system_copy(
                        scope, command, source=source, source_assets=contents,
                        source_kind=self._source.source_kind,
                        source_reference=self._source.source_reference, geometry_copies=tuple(copies),
                    )
                except ProductFamilyAuthoringApplicationError as exc:
                    if exc.code != "PRODUCT_FAMILY_SYSTEM_COPY_INVALID":
                        raise
                    raise ProductFamilyAuthoringApplicationError(
                        "SYSTEM_PUBLICATION_SOURCE_INVALID", str(exc),
                        phase="preflight", retryable=False,
                    ) from exc
        with self._bind_identity(scope.identity):
            if original.status is CriticalOperationStatus.SUCCEEDED:
                await self._verify_saved_geometry(scope, original)
                return await self._family_authoring.commit_system_copy(scope, original)
            claim = await self._family_authoring.claim_system_copy(scope, original)
            try:
                for draft in original.detail.system_copy.geometry_copies:
                    # publish_candidate reconciles both an already committed dependency
                    # and a lost response, and owns its entry asset verification.
                    # A saved candidate is never rebuilt from source.
                    await self._geometry_resources.publish_candidate(scope.identity, draft.target_version)
                return await self._family_authoring.commit_system_copy(scope, original, execution_claim=claim)
            except BaseException:
                await self._pause_copy(scope, original, claim)
                raise

    async def _prepare_geometry_closure(self, scope, family):
        planned: dict[str, tuple[SystemGeometryCopyDraft, tuple[bytes, ...]]] = {}
        visiting: set[str] = set()

        async def visit(resource_id, version_id, digest):
            if version_id in planned:
                draft = planned[version_id][0]
                if draft.source_resource_id != resource_id or draft.source_content_digest != digest:
                    raise ValueError("source Geometry closure contains conflicting exact identities")
                return draft.target_version
            if version_id in visiting:
                raise ValueError("source Geometry closure contains an input cycle")
            visiting.add(version_id)
            source, contents = await self._source.read_geometry(resource_id=resource_id, resource_version_id=version_id)
            if (source.resource.resource_id != resource_id or source.version.resource_version_id != version_id
                or source.version.content_digest != digest):
                raise ValueError("source Geometry exact content changed")
            inputs = {}
            for item in geometry_resource_inputs(source.version.manifest):
                inputs[item.input_key] = await visit(item.resource_id, item.resource_version_id, item.content_digest)
            target_id = stable_internal_identity_id("grs_", "system-library-geometry-root", scope.run_id, version_id)
            target_version_id = stable_internal_identity_id("grsv_", "system-library-geometry-version", scope.run_id, version_id)
            candidate, payloads = self._geometry_resources.prepare_system_copy(
                source=source, source_assets=contents, target_resource_id=target_id,
                target_resource_version_id=target_version_id, geometry_inputs=inputs, created_at=datetime.now(UTC),
            )
            with self._bind_identity(scope.identity):
                existing = None
                if source.resource.owner_scope.scope_kind is ResourceOwnerScopeKind.SYSTEM:
                    existing = await self._geometry_resources.get_exact_version(
                        scope.identity, resource_id=resource_id, resource_version_id=version_id,
                    )
                    if existing is not None and _geometry_semantics(existing) != _geometry_semantics(source):
                        raise IdempotencyConflictError("visible SYSTEM source Geometry content changed")
                if existing is None:
                    existing = await self._geometry_resources.get_exact_version(
                        scope.identity, resource_id=target_id, resource_version_id=target_version_id,
                    )
                    if existing is not None and _geometry_semantics(existing) != _geometry_semantics(candidate):
                        raise IdempotencyConflictError("saved SYSTEM Geometry mapping changed")
                if existing is not None:
                    await self._geometry_resources.verify_candidate_assets(scope.identity, existing)
                    candidate = existing
            draft = SystemGeometryCopyDraft(
                source_resource_id=resource_id, source_resource_version_id=version_id,
                source_content_digest=digest, source_owner_scope=source.resource.owner_scope,
                target_version=candidate, reuse_existing=existing is not None,
            )
            planned[version_id] = draft, payloads
            visiting.remove(version_id)
            return candidate

        for binding in family.geometry_bindings:
            await visit(binding.geometry_resource_id, binding.geometry_resource_version_id, binding.geometry_content_digest)
        return tuple(planned.values())

    async def _verify_saved_geometry(self, scope, operation):
        for draft in operation.detail.system_copy.geometry_copies:
            await self._geometry_resources.verify_candidate_assets(scope.identity, draft.target_version)

    async def _pause_copy(self, scope: InvocationScope, operation: CriticalOperationRecord, claim: AuthoringExecutionClaim):
        current = await self._repository.get_product_family_authoring_operation_state(scope, operation_id=operation.operation_id)
        if current is None or current.status is not CriticalOperationStatus.PREPARED:
            return
        paused = CriticalOperationRecord.model_validate(current.model_dump(mode="python") | {
            "status": CriticalOperationStatus.INDETERMINATE,
            "error_code": "SYSTEM_PUBLICATION_INTERRUPTED",
            "error_message": "The exact SYSTEM copy must resume its saved Operation and candidates",
            "error_phase": CriticalOperationErrorPhase.RECONCILE,
            "finished_at": datetime.now(UTC),
        })
        await self._repository.mark_product_family_authoring_indeterminate(scope, paused, execution_claim=claim)


def _geometry_semantics(bundle: GeometryResourceVersionBundle):
    return bundle.model_dump(mode="python", exclude={
        "resource": {"created_at"}, "version": {"created_at"}, "assets": {"__all__": {"created_at"}},
    })
