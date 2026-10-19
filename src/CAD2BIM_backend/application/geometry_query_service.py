"""Source-first application orchestration for read-only geometry queries."""

from __future__ import annotations

from CAD2BIM_backend.domain.runtime_execution import GEOMETRY_QUERY_EXECUTION

import asyncio
import hashlib
import json
import re
from time import monotonic
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.artifact_service import ArtifactDownload, ArtifactService
from CAD2BIM_backend.application.async_settlement import drain_shielded_task
from CAD2BIM_backend.application.exact_artifact_reader import (
    ExactArtifactReadError,
    ExactArtifactReader,
)
from CAD2BIM_backend.application.geometry_resource_service import (
    GeometryResourceIntegrityError,
    GeometryResourceService,
)
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetIntegrityError
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError
from CAD2BIM_backend.application.text_source_view import TextSourceViewError, TextSourceViewService
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionCoordinates,
    GeometryInspectionSelection,
    require_inspection_coordinate_selection,
)
from CAD2BIM_backend.domain.geometry_query import (
    GeometryQueryArtifactRef,
    GeometryQueryAttemptPreparation,
    GeometryQueryAttemptRecord,
    GeometryQueryAttemptStatus,
    GeometryQueryAttemptTerminal,
    GeometryQueryDiagnostic,
    GeometryQueryPage,
    GeometryQueryTargetKind,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryNormalizeRequest,
    GeometryQueryRequest,
    GeometryRuntimeError,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeUnavailableError,
    GeometryRuntimeValidationError,
)
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.text_source import SourceEdit, TextSourceView, TextSourceViewResult
from CAD2BIM_backend.infrastructure.source_text.source_patch import (
    SourcePatchError,
    SourcePatchFailure,
    apply_text_edits,
)
from CAD2BIM_backend.ports.geometry_queries import GeometryQueryRepository
from CAD2BIM_backend.ports.geometry_runtime import GeometryRuntimePort
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError, OpaqueCursorCodecPort

if TYPE_CHECKING:
    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
    from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService
    from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService


_ATTEMPT_ALIAS_DOMAIN = "geometry_query_attempt_alias"
_PAGE_CURSOR_DOMAIN = "geometry_query_result_page"
_PYTHON_MEDIA_TYPE = "text/x-python"
_JSON_MEDIA_TYPE = "application/json"
_MAX_SOURCE_BYTES = 64 * 1024
_MAX_DIAGNOSTIC_BYTES = 2_000
_REQUEST_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CURRENT_TARGET_KINDS = frozenset({
    GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION,
    GeometryQueryTargetKind.GEOMETRY_INSPECTION,
    GeometryQueryTargetKind.FAMILY_STANDARD_MODEL,
})
_INSPECTION_SELECTION = TypeAdapter(GeometryInspectionSelection)
_INSPECTION_COORDINATES = TypeAdapter(GeometryInspectionCoordinates)


class GeometryQueryApplicationError(RuntimeError):
    """Bounded query failure suitable for deterministic entry projection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal["read", "target", "preflight", "execution", "output_validation"] = "read",
        retryable: bool = False,
        attempt_alias: str | None = None,
    ) -> None:
        super().__init__(_bounded_message(message))
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.attempt_alias = attempt_alias


class GeometryQueryService:
    """Persist exact source and attempts, execute once, then page exact result bytes."""

    def __init__(
        self,
        repository: GeometryQueryRepository,
        artifacts: ArtifactService,
        resources: GeometryResourceService,
        runtime: GeometryRuntimePort,
        cursor_codec: OpaqueCursorCodecPort,
        *,
        clock: Callable[[], datetime] | None = None,
        text_views: TextSourceViewService | None = None,
        inspections: GeometryInspectionService | None = None,
        family_catalog: ProductFamilyCatalogService | None = None,
        family_assets: ProductFamilyAssetService | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._exact_artifacts = ExactArtifactReader(artifacts)
        self._resources = resources
        self._runtime = runtime
        self._cursor_codec = cursor_codec
        self._clock = clock or (lambda: datetime.now(UTC))
        self._text_views = text_views or TextSourceViewService()
        self._inspections = inspections
        self._family_catalog = family_catalog
        self._family_assets = family_assets

    async def fresh_family_standard_model(
        self, scope: InvocationScope, *, family_version_alias: str,
        source_code: str, request_identity: str, page_size: int = 100,
        deadline: float | None = None,
    ) -> GeometryQueryPage:
        """Query the published standard BREP without executing the Family program."""
        _validate_source(source_code)
        _validate_page_size(page_size)
        _validate_request_identity(request_identity)
        bundle = await self._family_bundle(scope, version_ref=family_version_alias)
        return await self._prepare_and_execute(
            scope, source_code=source_code, target_kind=GeometryQueryTargetKind.FAMILY_STANDARD_MODEL,
            family_version_id=bundle.version.family_version_id, resource_version_id=None,
            target_artifact=None, target_geometry_format=None, repair_parent_attempt_id=None,
            request_identity=request_identity, page_size=page_size,
            deadline=monotonic() + GEOMETRY_QUERY_EXECUTION.total_seconds if deadline is None else deadline,
        )

    def _require_family_catalog(self):
        if self._family_catalog is None or self._family_assets is None:
            raise GeometryQueryApplicationError("GEOMETRY_QUERY_UNAVAILABLE", "Family standard model reads are not configured", phase="preflight")
        return self._family_catalog

    @staticmethod
    def _require_standard_model(bundle):
        if bundle.version.family_contract.standard_model is None:
            raise GeometryQueryApplicationError("GEOMETRY_QUERY_UNAVAILABLE", "This Family version does not define a standard model", phase="preflight")

    async def family_version_alias(self, scope: InvocationScope, family_version_id: str) -> str:
        bundle = await self._family_bundle(scope, version_id=family_version_id)
        return self._require_family_catalog().version_ref(scope.identity, bundle)

    async def _family_bundle(self, scope, *, version_ref=None, version_id=None):
        catalog = self._require_family_catalog()
        try:
            if version_ref is not None:
                bundle = await catalog.resolve_exact_version(scope.identity, version_ref, verify_assets=False)
            else:
                bundle = await catalog.resolve_visible_version(scope.identity, version_id, verify_assets=False)
        except ProductFamilyCatalogError as exc:
            raise GeometryQueryApplicationError("FAMILY_STANDARD_MODEL_UNAVAILABLE", str(exc), phase="target") from exc
        self._require_standard_model(bundle)
        return bundle

    async def fresh(
        self,
        scope: InvocationScope,
        resource_id: str,
        resource_version_id: str,
        source_code: str,
        request_identity: str,
        page_size: int = 100,
        *, deadline: float | None = None,
    ) -> GeometryQueryPage:
        """Create or replay one source-first query over an exact visible version."""

        deadline = monotonic() + GEOMETRY_QUERY_EXECUTION.total_seconds if deadline is None else deadline
        _validate_source(source_code)
        _validate_page_size(page_size)
        _validate_request_identity(request_identity)
        bundle = await self._resources.get_exact_version(
            scope.identity,
            resource_id=resource_id,
            resource_version_id=resource_version_id,
        )
        if bundle is None:
            raise GeometryQueryApplicationError(
                "GEOMETRY_RESOURCE_NOT_FOUND",
                "The exact geometry resource version was not found",
                phase="preflight",
            )
        _require_queryable_resource_kind(bundle.resource.resource_kind)
        return await self._prepare_and_execute(
            scope,
            source_code=source_code,
            target_kind=GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION,
            deadline=deadline,
            resource_version_id=bundle.version.resource_version_id,
            target_artifact=None,
            target_geometry_format=None,
            repair_parent_attempt_id=None,
            request_identity=request_identity,
            page_size=page_size,
        )

    async def fresh_inspection(
        self,
        scope: InvocationScope,
        *,
        inspection_ref: str,
        selection: GeometryInspectionSelection,
        coordinate_space: GeometryInspectionCoordinates,
        source_code: str,
        request_identity: str,
        page_size: int = 100,
        deadline: float | None = None,
    ) -> GeometryQueryPage:
        """Investigate an exact saved bundle without rerunning its author or builder."""

        deadline = monotonic() + GEOMETRY_QUERY_EXECUTION.total_seconds if deadline is None else deadline
        _validate_source(source_code)
        _validate_page_size(page_size)
        _validate_request_identity(request_identity)
        try:
            selection = _INSPECTION_SELECTION.validate_python(selection)
            coordinate_space = _INSPECTION_COORDINATES.validate_python(coordinate_space)
            require_inspection_coordinate_selection(selection, coordinate_space)
        except (ValueError, ValidationError) as exc:
            raise GeometryQueryApplicationError(
                "INSPECTION_SELECTION_INVALID", str(exc), phase="preflight",
            ) from exc
        loaded = await self._read_inspection(scope, inspection_ref=inspection_ref)
        self._validate_inspection_selection(loaded, selection, coordinate_space)
        return await self._prepare_and_execute(
            scope,
            source_code=source_code,
            target_kind=GeometryQueryTargetKind.GEOMETRY_INSPECTION,
            resource_version_id=None,
            target_artifact=loaded.artifact,
            target_geometry_format=None,
            selection=selection,
            coordinate_space=coordinate_space,
            repair_parent_attempt_id=None,
            request_identity=request_identity,
            page_size=page_size,
            deadline=deadline,
        )

    async def repair(
        self,
        scope: InvocationScope,
        parent_attempt_id: str,

        edits: Sequence[SourceEdit],
        request_identity: str,
        page_size: int = 100,
        *, deadline: float | None = None,
    ) -> GeometryQueryPage:
        """Patch a visible parent's exact source and preserve all exact target choices."""

        deadline = monotonic() + GEOMETRY_QUERY_EXECUTION.total_seconds if deadline is None else deadline
        _validate_page_size(page_size)
        _validate_request_identity(request_identity)
        parent = await self._repository.get_geometry_query_attempt(
            scope.identity,
            attempt_id=parent_attempt_id,
        )
        if parent is None:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ATTEMPT_NOT_FOUND",
                "The parent geometry query attempt was not found",
                phase="preflight",
            )
        _require_current_target(parent)
        if parent.status is GeometryQueryAttemptStatus.PREPARED:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_PARENT_NOT_TERMINAL",
                "The parent geometry query attempt is not terminal",
                phase="preflight",
            )
        source = await self._read_exact_artifact(
            scope,
            parent.source_artifact,
            expected_content_type=_PYTHON_MEDIA_TYPE,
        )
        try:
            decoded = source.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_SOURCE_INVALID",
                "The parent geometry query source is not valid UTF-8",
                phase="preflight",
            ) from exc
        try:
            patched = apply_text_edits(decoded, edits, maximum_source_bytes=_MAX_SOURCE_BYTES)
        except SourcePatchError as exc:
            code, retryable = _patch_error(exc.failure)
            raise GeometryQueryApplicationError(
                code,
                str(exc),
                phase="preflight",
                retryable=retryable,
                attempt_alias=self._attempt_alias(scope, parent),
            ) from None
        _validate_source(patched)

        if parent.target_kind is GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION:
            assert parent.resource_version_id is not None
            bundle = await self._resources.get_version_by_id(
                scope.identity,
                resource_version_id=parent.resource_version_id,
            )
            if bundle is None:
                raise GeometryQueryApplicationError(
                    "GEOMETRY_RESOURCE_NOT_FOUND",
                    "The parent's exact geometry resource version is no longer visible",
                    phase="preflight",
                )
            _require_queryable_resource_kind(bundle.resource.resource_kind)
        elif parent.target_kind is GeometryQueryTargetKind.FAMILY_STANDARD_MODEL:
            await self._family_bundle(scope, version_id=parent.family_version_id)
        return await self._prepare_and_execute(
            scope,
            source_code=patched,
            target_kind=parent.target_kind,
            deadline=deadline,
            resource_version_id=parent.resource_version_id,
            family_version_id=parent.family_version_id,
            target_artifact=parent.target_artifact,
            target_geometry_format=parent.target_geometry_format,
            selection=parent.selection,
            coordinate_space=parent.coordinate_space,
            repair_parent_attempt_id=parent.attempt_id,
            request_identity=request_identity,
            page_size=page_size,
        )

    async def read_source(
        self,
        scope: InvocationScope,
        attempt_id: str,
        view: TextSourceView,
    ) -> TextSourceViewResult:
        """Project one RLS-visible attempt's exact persisted Python source."""

        attempt = await self._repository.get_geometry_query_attempt(
            scope.identity,
            attempt_id=attempt_id,
        )
        if attempt is None:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ATTEMPT_NOT_FOUND",
                "The geometry query attempt was not found",
            )
        _require_current_target(attempt)
        downloaded = await self._read_exact_artifact(
            scope,
            attempt.source_artifact,
            expected_content_type=_PYTHON_MEDIA_TYPE,
        )
        try:
            source = downloaded.content.decode("utf-8")
            return self._text_views.view(source, view)
        except UnicodeDecodeError as exc:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_SOURCE_INVALID",
                "The exact geometry query source is not valid UTF-8",
            ) from exc
        except TextSourceViewError as exc:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_SOURCE_READ_INVALID",
                str(exc),
            ) from exc

    async def list_terminal_geometry_query_attempts(
        self,
        scope: InvocationScope,
        *,
        statuses: tuple[GeometryQueryAttemptStatus, ...],
        before_created_at: datetime | None,
        before_attempt_id: str | None,
        limit: int,
    ) -> tuple[GeometryQueryAttemptRecord, ...]:
        """List terminal attempts from the current Task using a stable raw keyset."""

        _validate_terminal_attempt_list(
            statuses=statuses,
            before_created_at=before_created_at,
            before_attempt_id=before_attempt_id,
            limit=limit,
        )
        attempts = await self._repository.list_terminal_geometry_query_attempts(
            scope.identity,
            statuses=statuses,
            before_created_at=before_created_at,
            before_attempt_id=before_attempt_id,
            limit=limit,
        )
        return tuple(item for item in attempts if item.target_kind in _CURRENT_TARGET_KINDS)

    def attempt_alias(
        self,
        scope: InvocationScope,
        attempt: GeometryQueryAttemptRecord,
    ) -> str:
        """Project the existing Task-bound opaque alias for one visible attempt."""

        _require_current_target(attempt)
        return self._attempt_alias(scope, attempt)

    async def attempt_alias_for_visible_id(
        self,
        scope: InvocationScope,
        attempt_id: str,
    ) -> str:
        """Reauthorize one Task-bound attempt before projecting its opaque alias."""

        attempt = await self._repository.get_geometry_query_attempt(
            scope.identity,
            attempt_id=attempt_id,
        )
        if attempt is None:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ATTEMPT_NOT_FOUND",
                "The geometry query attempt was not found",
            )
        _require_current_target(attempt)
        return self._attempt_alias(scope, attempt)

    async def continue_page(
        self,
        scope: InvocationScope,
        cursor: str,
    ) -> GeometryQueryPage:
        """Read another page from one exact result Artifact without executing code."""

        try:
            claims = self._cursor_codec.decode(cursor)
        except OpaqueCursorCodecError as exc:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_CURSOR_INVALID",
                "The geometry query cursor is invalid",
            ) from exc
        if claims.get("domain") != _PAGE_CURSOR_DOMAIN:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_CURSOR_INVALID",
                "The geometry query cursor is invalid",
            )
        if claims.get("scope") != _task_scope_digest(scope):
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_CURSOR_SCOPE_MISMATCH",
                "The geometry query cursor belongs to a different Task",
            )
        attempt_id = claims.get("attempt_id")
        offset = claims.get("offset")
        page_size = claims.get("page_size")
        if (
            not isinstance(attempt_id, str)
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(page_size, int)
            or isinstance(page_size, bool)
        ):
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_CURSOR_INVALID",
                "The geometry query cursor is invalid",
            )
        _validate_page_size(page_size, cursor=True)
        attempt = await self._repository.get_geometry_query_attempt(
            scope.identity,
            attempt_id=attempt_id,
        )
        if attempt is None or attempt.status is not GeometryQueryAttemptStatus.SUCCEEDED:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_CURSOR_INVALID",
                "The cursor's terminal geometry query attempt was not found",
            )
        _require_current_target(attempt)
        _validate_result_claim(claims.get("result"), attempt)
        return await self._page_from_attempt(scope, attempt, offset, page_size)

    async def resolve_attempt_alias(
        self,
        scope: InvocationScope,
        attempt_alias: str,
    ) -> str:
        """Decode one Task-bound alias and reauthorize its exact attempt through RLS."""

        try:
            claims = self._cursor_codec.decode(attempt_alias)
        except OpaqueCursorCodecError as exc:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ATTEMPT_ALIAS_INVALID",
                "The geometry query attempt alias is invalid",
            ) from exc
        if claims.get("domain") != _ATTEMPT_ALIAS_DOMAIN:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ATTEMPT_ALIAS_INVALID",
                "The geometry query attempt alias is invalid",
            )
        if claims.get("scope") != _task_scope_digest(scope):
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ATTEMPT_SCOPE_MISMATCH",
                "The geometry query attempt alias belongs to a different Task",
            )
        attempt_id = claims.get("attempt_id")
        if not isinstance(attempt_id, str) or _REQUEST_IDENTITY.fullmatch(attempt_id) is None:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ATTEMPT_ALIAS_INVALID",
                "The geometry query attempt alias is invalid",
            )
        attempt = await self._repository.get_geometry_query_attempt(
            scope.identity,
            attempt_id=attempt_id,
        )
        if attempt is None:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ATTEMPT_NOT_FOUND",
                "The geometry query attempt was not found",
            )
        _require_current_target(attempt)
        return attempt.attempt_id

    async def _prepare_and_execute(
        self, scope: InvocationScope, *, source_code: str,
        target_kind: GeometryQueryTargetKind, resource_version_id: str | None,
        target_artifact: GeometryQueryArtifactRef | None,
        target_geometry_format: CanonicalGeometryFormat | None,
        repair_parent_attempt_id: str | None, request_identity: str,
        page_size: int, deadline: float,
        selection: GeometryInspectionSelection | None = None,
        coordinate_space: GeometryInspectionCoordinates | None = None,
        family_version_id: str | None = None,
    ) -> GeometryQueryPage:
        async with self._runtime.invocation_scope(GEOMETRY_QUERY_EXECUTION, deadline=deadline):
            return await self._prepare_and_execute_scoped(
                scope, source_code=source_code, target_kind=target_kind,
                resource_version_id=resource_version_id, target_artifact=target_artifact,
                family_version_id=family_version_id,
                target_geometry_format=target_geometry_format,
                selection=selection, coordinate_space=coordinate_space,
                repair_parent_attempt_id=repair_parent_attempt_id,
                request_identity=request_identity, page_size=page_size,
                deadline=deadline,
            )

    async def _prepare_and_execute_scoped(
        self,
        scope: InvocationScope,
        *,
        source_code: str,
        target_kind: GeometryQueryTargetKind,
        resource_version_id: str | None,
        target_artifact: GeometryQueryArtifactRef | None,
        target_geometry_format: CanonicalGeometryFormat | None,
        repair_parent_attempt_id: str | None,
        request_identity: str,
        page_size: int,
        deadline: float,
        selection: GeometryInspectionSelection | None = None,
        coordinate_space: GeometryInspectionCoordinates | None = None,
        family_version_id: str | None = None,
    ) -> GeometryQueryPage:
        if target_kind not in _CURRENT_TARGET_KINDS:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_TARGET_UNSUPPORTED",
                "Preview query targets are no longer available; use an exact mapped resource version or saved inspection",
                phase="preflight",
            )
        source_bytes = source_code.encode("utf-8")
        inspection_choices = {}
        family_target = {"family_version_id": family_version_id} if family_version_id is not None else {}
        if target_kind is GeometryQueryTargetKind.GEOMETRY_INSPECTION:
            inspection_choices = {
                "selection": None if selection is None else selection.model_dump(mode="json"),
                "coordinate_space": None if coordinate_space is None else coordinate_space.model_dump(mode="json"),
            }
        request_digest = sha256_digest({
                "domain": "geometry_query_request",
                "target_kind": target_kind.value,
                "resource_version_id": resource_version_id,
                "target_artifact": (
                    None if target_artifact is None else _artifact_claim(target_artifact)
                ),
                "target_geometry_format": (
                    None if target_geometry_format is None else target_geometry_format.value
                ),
                **inspection_choices,
                **family_target,
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "repair_parent_attempt_id": repair_parent_attempt_id,
                "page_size": page_size,
            })
        attempt_id = stable_internal_identity_id(
            "gqa_",
            "geometry_query_attempt",
            scope.identity.tenant_id,
            scope.identity.task_id or "",
            request_identity,
        )
        source_artifact = await self._artifacts.upload_bytes(
            owner_id=scope.owner_id,
            task_id=scope.identity.task_id or "",
            filename="geometry_query.py",
            content_type=_PYTHON_MEDIA_TYPE,
            content=source_bytes,
            idempotency_key=f"geometry-query-source:{attempt_id}",
            kind="geometry_query_source",
            run_id=scope.run_id,
            version_metadata={
                "target_kind": target_kind.value,
                "resource_version_id": resource_version_id,
                **family_target,
                "target_geometry_sha256": (
                    None if target_artifact is None else target_artifact.sha256
                ),
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                **({"target_artifact": _artifact_claim(target_artifact), **inspection_choices}
                    if target_artifact is not None else {}),
            },
        )
        source_ref = _artifact_ref(source_artifact)
        attempt = await self._repository.create_geometry_query_attempt(
            scope.identity,
            GeometryQueryAttemptPreparation(
                attempt_id=attempt_id,
                origin_run_id=scope.run_id,
                target_kind=target_kind,
                resource_version_id=resource_version_id,
                family_version_id=family_version_id,
                target_artifact=target_artifact,
                target_geometry_format=target_geometry_format,
                selection=selection,
                coordinate_space=coordinate_space,
                source_artifact=source_ref,
                repair_parent_attempt_id=repair_parent_attempt_id,
                request_identity=request_identity,
                request_digest=request_digest,
                created_at=self._now(),
            ),
        )
        if attempt.status is GeometryQueryAttemptStatus.SUCCEEDED:
            return await self._page_from_attempt(scope, attempt, 0, page_size)
        if attempt.status is GeometryQueryAttemptStatus.FAILED:
            self._raise_persisted_failure(scope, attempt)

        try:
            if attempt.target_kind is GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION:
                assert attempt.resource_version_id is not None
                bundle = await self._resources.get_version_by_id(
                    scope.identity,
                    resource_version_id=attempt.resource_version_id,
                )
                if bundle is None:
                    raise GeometryQueryApplicationError(
                        "GEOMETRY_RESOURCE_NOT_FOUND",
                        "The exact geometry resource version was not found",
                        phase="preflight",
                    )
                resolved = await self._resources.read_exact_asset(
                    scope.identity,
                    resource_id=bundle.resource.resource_id,
                    resource_version_id=attempt.resource_version_id,
                    asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
                )
                if resolved is None:
                    raise GeometryQueryApplicationError(
                        "GEOMETRY_QUERY_UNAVAILABLE",
                        "The exact resource version has no canonical geometry asset",
                        phase="preflight",
                    )
                asset, geometry_bytes = resolved
                try:
                    geometry_format = CanonicalGeometryFormat(asset.asset_format)
                except ValueError as exc:
                    raise GeometryQueryApplicationError(
                        "GEOMETRY_QUERY_UNAVAILABLE",
                        "The exact geometry asset format is not queryable",
                        phase="preflight",
                    ) from exc
                expected_geometry_sha256 = asset.sha256
                execution = await self._execute_geometry(geometry_format, geometry_bytes, expected_geometry_sha256, source_code)
            elif attempt.target_kind is GeometryQueryTargetKind.GEOMETRY_INSPECTION:
                execution = await self._execute_inspection(scope, attempt, source_code)
            elif attempt.target_kind is GeometryQueryTargetKind.FAMILY_STANDARD_MODEL:
                bundle = await self._family_bundle(scope, version_id=attempt.family_version_id)
                manifest, geometry_bytes = await self._family_assets.standard_model_assets(bundle)
                execution = await self._execute_geometry(
                    CanonicalGeometryFormat.OCCT_BREP_V1, geometry_bytes, manifest.geometry_sha256, source_code,
                )
            else:
                _require_current_target(attempt)
                raise AssertionError("unreachable geometry query target")
            encoded_result = canonical_json(execution.result).encode("utf-8")
            if len(encoded_result) != execution.result_bytes:
                raise GeometryQueryApplicationError(
                    "GEOMETRY_QUERY_RESULT_INVALID",
                    "The runtime result byte count is inconsistent",
                    phase="output_validation",
                )
            document = canonical_json(
                {
                    "geometry_sha256": execution.geometry_sha256,
                    "result": execution.result,
                    "result_bytes": execution.result_bytes,
                }
            ).encode("utf-8")
            result_artifact = await self._artifacts.upload_bytes(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename="geometry_query_result.json",
                content_type=_JSON_MEDIA_TYPE,
                content=document,
                idempotency_key=f"geometry-query-result:{attempt.attempt_id}",
                kind="geometry_query_result",
                run_id=attempt.origin_run_id,
                version_metadata={
                    "target_kind": attempt.target_kind.value,
                    "resource_version_id": attempt.resource_version_id,
                    **family_target,
                    "geometry_sha256": execution.geometry_sha256,
                    **({"target_artifact": _artifact_claim(attempt.target_artifact), **inspection_choices}
                        if attempt.target_artifact is not None else {}),
                },
            )
            terminal = GeometryQueryAttemptTerminal(
                status=GeometryQueryAttemptStatus.SUCCEEDED,
                result_artifact=_artifact_ref(result_artifact),
                finished_at=self._now(),
            )
            attempt = await self._complete_or_reconcile(scope, attempt, terminal)
            return await self._page_from_attempt(scope, attempt, 0, page_size)
        except asyncio.CancelledError as exc:
            async def settle_cancelled() -> None:
                async with asyncio.timeout(max(0.0, min(GEOMETRY_QUERY_EXECUTION.terminal_seconds, deadline - monotonic()))):
                    await self._persist_failure(
                        scope, attempt,
                        GeometryQueryApplicationError(
                            "GEOMETRY_QUERY_CANCELLED",
                            "The query was cancelled; read the saved attempt before submitting a new query",
                            phase="execution",
                        ),
                    )

            settlement = asyncio.create_task(settle_cancelled())
            failure = await drain_shielded_task(settlement)
            if failure is not None:
                setattr(exc, "query_settlement_failure", failure)
            raise
        except GeometryQueryApplicationError as exc:
            await self._persist_failure(scope, attempt, exc)
        except GeometryRuntimeResourceLimitError as exc:
            message = "The geometry query exceeded its supervised execution limit"
            if exc.diagnostic is not None:
                message += (
                    "; resource_diagnostic=" + exc.diagnostic.model_dump_json()
                    + "; sampled peaks are lower bounds, null means unavailable; "
                    "elapsed excludes cleanup and includes worker startup; "
                    "inspect the trigger and change its cause before retrying"
                )
            await self._persist_failure(
                scope,
                attempt,
                GeometryQueryApplicationError(
                    "GEOMETRY_QUERY_RESOURCE_LIMIT",
                    message,
                    phase="execution",
                    retryable=False,
                ),
            )
        except GeometryRuntimeUnavailableError as exc:
            message = str(exc) or "The geometry query runtime is unavailable"
            if exc.diagnostic is not None:
                message += "; diagnostic=" + json.dumps(exc.diagnostic, separators=(",", ":"), allow_nan=False)
            await self._persist_failure(
                scope,
                attempt,
                GeometryQueryApplicationError(
                    "GEOMETRY_QUERY_RUNTIME_UNAVAILABLE",
                    message,
                    phase="execution",
                    retryable=exc.retryable,
                ),
            )
        except GeometryRuntimeValidationError as exc:
            await self._persist_failure(
                scope,
                attempt,
                GeometryQueryApplicationError(
                    "GEOMETRY_QUERY_INVALID",
                    str(exc) or "The geometry query is invalid",
                    phase="execution",
                ),
            )
        except GeometryRuntimeError as exc:
            await self._persist_failure(
                scope,
                attempt,
                GeometryQueryApplicationError(
                    "GEOMETRY_QUERY_EXECUTION_FAILED",
                    str(exc) or "The geometry query failed",
                    phase="execution",
                ),
            )
        except (GeometryResourceIntegrityError, ProductFamilyAssetIntegrityError) as exc:
            await self._persist_failure(
                scope,
                attempt,
                GeometryQueryApplicationError(
                    "GEOMETRY_QUERY_RESOURCE_INTEGRITY_FAILED",
                    str(exc),
                    phase="preflight",
                ),
            )
        except ProductFamilyCatalogError as exc:
            await self._persist_failure(scope, attempt, GeometryQueryApplicationError(
                "FAMILY_STANDARD_MODEL_UNAVAILABLE", str(exc), phase="target",
            ))
        except ValidationError as exc:
            await self._persist_failure(scope, attempt, GeometryQueryApplicationError(
                "GEOMETRY_QUERY_TARGET_INVALID", str(exc), phase="preflight",
            ))
        except Exception:
            await self._persist_failure(
                scope,
                attempt,
                GeometryQueryApplicationError(
                    "GEOMETRY_QUERY_SERVICE_UNAVAILABLE",
                    "The geometry query could not publish a terminal result",
                    phase="output_validation",
                    retryable=True,
                ),
            )
        raise AssertionError("geometry query terminal path did not return or raise")

    async def _execute_geometry(self, geometry_format, geometry_bytes, expected_geometry_sha256, source_code):
        async with self._runtime.execution_scope(GEOMETRY_QUERY_EXECUTION):
            geometry = await self._runtime.normalize(
                GeometryNormalizeRequest(format=geometry_format, payload=geometry_bytes)
            )
            if (
                geometry.format is not geometry_format
                or geometry.payload != geometry_bytes
                or geometry.sha256 != expected_geometry_sha256
                or hashlib.sha256(geometry.payload).hexdigest() != geometry.sha256
            ):
                raise GeometryQueryApplicationError(
                    "GEOMETRY_QUERY_RESOURCE_INTEGRITY_FAILED",
                    "The runtime did not reopen the exact canonical geometry bytes",
                    phase="preflight",
                )
            return await self._runtime.query(
                GeometryQueryRequest(geometry=geometry, source_code=source_code)
            )

    async def _execute_inspection(self, scope, attempt, source_code):
        assert attempt.target_artifact is not None
        loaded = await self._read_inspection(scope, artifact=attempt.target_artifact)
        self._validate_inspection_selection(loaded, attempt.selection, attempt.coordinate_space)
        async with self._runtime.execution_scope(GEOMETRY_QUERY_EXECUTION):
            return await self._runtime.query(
                GeometryQueryRequest(
                    geometry=loaded.geometry,
                    source_code=source_code,
                    inspection_context=loaded.context,
                    fragment_inputs=loaded.fragment_inputs,
                    selection=attempt.selection,
                    coordinates=attempt.coordinate_space,
                )
            )

    def _require_inspections(self) -> GeometryInspectionService:
        if self._inspections is None:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_INSPECTION_UNAVAILABLE",
                "The geometry inspection store is unavailable",
                phase="preflight",
            )
        return self._inspections

    async def _read_inspection(self, scope, *, inspection_ref=None, artifact=None):
        store = self._require_inspections()
        try:
            if artifact is not None:
                return await store.read_artifact(scope, artifact)
            return await store.read(scope, inspection_ref)
        except GeometryInspectionApplicationError as exc:
            raise GeometryQueryApplicationError(
                exc.code, str(exc), phase=exc.phase, retryable=exc.retryable,
            ) from exc

    def _validate_inspection_selection(self, loaded, selection, coordinate_space):
        try:
            self._require_inspections().validate_selection(loaded, selection, coordinate_space)
        except GeometryInspectionApplicationError as exc:
            raise GeometryQueryApplicationError(
                exc.code, str(exc), phase=exc.phase, retryable=exc.retryable,
            ) from exc

    async def _persist_failure(
        self,
        scope: InvocationScope,
        attempt: GeometryQueryAttemptRecord,
        error: GeometryQueryApplicationError,
    ) -> NoReturn:
        terminal = GeometryQueryAttemptTerminal(
            status=GeometryQueryAttemptStatus.FAILED,
            diagnostics=(
                GeometryQueryDiagnostic(
                    code=error.code, message=_bounded_message(str(error)),
                    phase=error.phase, retryable=error.retryable,
                ),
            ),
            finished_at=self._now(),
        )
        completed = await self._complete_or_reconcile(scope, attempt, terminal)
        if completed.status is GeometryQueryAttemptStatus.SUCCEEDED:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_RESULT_RECONCILIATION_REQUIRED",
                "The query completed while failure publication lost its response",
                phase="output_validation",
                retryable=True,
                attempt_alias=self._attempt_alias(scope, completed),
            )
        raise GeometryQueryApplicationError(
            error.code,
            str(error),
            phase=error.phase,
            retryable=error.retryable,
            attempt_alias=self._attempt_alias(scope, completed),
        )

    async def _complete_or_reconcile(
        self,
        scope: InvocationScope,
        attempt: GeometryQueryAttemptRecord,
        terminal: GeometryQueryAttemptTerminal,
    ) -> GeometryQueryAttemptRecord:
        try:
            return await self._repository.complete_geometry_query_attempt(
                scope.identity,
                attempt_id=attempt.attempt_id,
                terminal=terminal,
            )
        except Exception:
            recovered = await self._repository.get_geometry_query_attempt(
                scope.identity,
                attempt_id=attempt.attempt_id,
            )
            if (
                recovered is not None
                and recovered.status is not GeometryQueryAttemptStatus.PREPARED
            ):
                return recovered
            raise

    async def _page_from_attempt(
        self,
        scope: InvocationScope,
        attempt: GeometryQueryAttemptRecord,
        offset: int,
        page_size: int,
    ) -> GeometryQueryPage:
        _require_current_target(attempt)
        if attempt.status is not GeometryQueryAttemptStatus.SUCCEEDED:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_RESULT_MISSING",
                "The geometry query attempt has no successful result",
            )
        if attempt.result_artifact is None:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_RESULT_INTEGRITY_FAILED",
                "The successful attempt has no exact result Artifact",
            )
        inspection_ref = None
        if attempt.target_kind is GeometryQueryTargetKind.FAMILY_STANDARD_MODEL:
            await self._family_bundle(scope, version_id=attempt.family_version_id)
        if attempt.target_kind is GeometryQueryTargetKind.GEOMETRY_INSPECTION:
            assert attempt.target_artifact is not None
            inspections = self._require_inspections()
            await self._read_inspection(scope, artifact=attempt.target_artifact)
            inspection_ref = inspections.reference(scope, attempt.target_artifact)
        downloaded = await self._read_exact_artifact(
            scope,
            attempt.result_artifact,
            expected_content_type=_JSON_MEDIA_TYPE,
        )
        document = _decode_result_document(downloaded.content)
        raw_result = document["result"]
        results = raw_result if isinstance(raw_result, list) else [raw_result]
        if offset > len(results):
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_CURSOR_INVALID",
                "The geometry query cursor offset exceeds the exact result",
            )
        end = min(len(results), offset + page_size)
        next_cursor = None
        if end < len(results):
            next_cursor = self._cursor_codec.encode(
                {
                    "domain": _PAGE_CURSOR_DOMAIN,
                    "scope": _task_scope_digest(scope),
                    "attempt_id": attempt.attempt_id,
                    "result": _artifact_claim(attempt.result_artifact),
                    "offset": end,
                    "page_size": page_size,
                }
            )
        return GeometryQueryPage(
            attempt_alias=self._attempt_alias(scope, attempt),
            target_kind=attempt.target_kind,
            resource_version_id=attempt.resource_version_id,
            family_version_id=attempt.family_version_id,
            inspection_ref=inspection_ref,
            selection=attempt.selection,
            coordinate_space=attempt.coordinate_space,
            source_sha256=attempt.source_artifact.sha256,
            geometry_sha256=document["geometry_sha256"],
            result_bytes=document["result_bytes"],
            total_count=len(results),
            returned_count=end - offset,
            results=tuple(results[offset:end]),
            next_cursor=next_cursor,
        )

    async def _read_exact_artifact(
        self,
        scope: InvocationScope,
        reference: GeometryQueryArtifactRef,
        *,
        expected_content_type: str,
    ) -> ArtifactDownload:
        try:
            return await self._exact_artifacts.read(
                scope,
                reference,
                expected_content_type=expected_content_type,
            )
        except ExactArtifactReadError as exc:
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_ARTIFACT_INTEGRITY_FAILED",
                "The exact geometry query Artifact failed integrity validation",
                phase="read",
            ) from exc

    def _attempt_alias(
        self,
        scope: InvocationScope,
        attempt: GeometryQueryAttemptRecord,
    ) -> str:
        return self._cursor_codec.encode(
            {
                "domain": _ATTEMPT_ALIAS_DOMAIN,
                "scope": _task_scope_digest(scope),
                "attempt_id": attempt.attempt_id,
            }
        )

    def _raise_persisted_failure(
        self,
        scope: InvocationScope,
        attempt: GeometryQueryAttemptRecord,
    ) -> NoReturn:
        diagnostic = attempt.diagnostics[0]
        raise GeometryQueryApplicationError(
            diagnostic.code,
            diagnostic.message,
            phase=diagnostic.phase,
            retryable=diagnostic.retryable,
            attempt_alias=self._attempt_alias(scope, attempt),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("geometry query clock must return an aware datetime")
        return value


def _artifact_ref(record) -> GeometryQueryArtifactRef:
    return GeometryQueryArtifactRef(
        artifact_id=record.artifact_id,
        version_id=record.version_id,
        sha256=record.sha256,
    )


def _artifact_claim(reference: GeometryQueryArtifactRef) -> dict[str, str]:
    return {
        "artifact_id": reference.artifact_id,
        "version_id": reference.version_id,
        "sha256": reference.sha256,
    }


def _validate_result_claim(value: object, attempt: GeometryQueryAttemptRecord) -> None:
    if attempt.result_artifact is None or value != _artifact_claim(attempt.result_artifact):
        raise GeometryQueryApplicationError(
            "GEOMETRY_QUERY_CURSOR_INVALID",
            "The geometry query cursor does not bind the exact result Artifact",
        )


def _decode_result_document(content: bytes) -> dict:
    try:
        value = json.loads(content.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "geometry_sha256",
            "result",
            "result_bytes",
        }:
            raise ValueError("unexpected result document shape")
        if canonical_json(value).encode("utf-8") != content:
            raise ValueError("result document is not canonical")
        geometry_sha256 = value["geometry_sha256"]
        result_bytes = value["result_bytes"]
        if (
            not isinstance(geometry_sha256, str)
            or len(geometry_sha256) != 64
            or any(character not in "0123456789abcdef" for character in geometry_sha256)
            or not isinstance(result_bytes, int)
            or isinstance(result_bytes, bool)
            or result_bytes < 0
            or result_bytes > 1024 * 1024
            or len(canonical_json(value["result"]).encode("utf-8")) != result_bytes
        ):
            raise ValueError("result document metadata is invalid")
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise GeometryQueryApplicationError(
            "GEOMETRY_QUERY_RESULT_INTEGRITY_FAILED",
            "The exact geometry query result Artifact is invalid",
        ) from exc


def _task_scope_digest(scope: InvocationScope) -> str:
    return sha256_digest(
        {
            "domain": "geometry_query_task_scope",
            "tenant_id": scope.identity.tenant_id,
            "task_id": scope.identity.task_id,
            "owner_id": scope.owner_id,
        }
    )


def _validate_source(source: str) -> None:
    if not isinstance(source, str) or not source.strip() or "\x00" in source:
        raise GeometryQueryApplicationError(
            "GEOMETRY_QUERY_SOURCE_INVALID",
            "Geometry query source must be non-blank UTF-8 text without NUL",
            phase="preflight",
        )
    try:
        size = len(source.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise GeometryQueryApplicationError(
            "GEOMETRY_QUERY_SOURCE_INVALID",
            "Geometry query source must be valid UTF-8",
            phase="preflight",
        ) from exc
    if size > _MAX_SOURCE_BYTES:
        raise GeometryQueryApplicationError(
            "GEOMETRY_QUERY_SOURCE_INVALID",
            "Geometry query source exceeds 65,536 UTF-8 bytes",
            phase="preflight",
        )


def _require_queryable_resource_kind(resource_kind: GeometryResourceKind) -> None:
    if resource_kind is not GeometryResourceKind.MAPPED_MODEL:
        raise GeometryQueryApplicationError(
            "GEOMETRY_QUERY_RESOURCE_KIND_UNSUPPORTED",
            "The geometry_resource_version target accepts only exact mapped_model versions. "
            "A modeling_function definition, sampled instance, or Product preview is not a "
            "queryable mapped resource version.",
            phase="preflight",
        )


def _require_current_target(attempt: GeometryQueryAttemptRecord) -> None:
    if attempt.target_kind not in _CURRENT_TARGET_KINDS:
        raise GeometryQueryApplicationError(
            "GEOMETRY_QUERY_TARGET_UNSUPPORTED",
            "Preview query targets are no longer available; use an exact mapped resource version or saved inspection",
            phase="preflight",
        )


def _validate_page_size(page_size: int, *, cursor: bool = False) -> None:
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 200:
        raise GeometryQueryApplicationError(
            "GEOMETRY_QUERY_CURSOR_INVALID" if cursor else "INVALID_ARGUMENT",
            "Geometry query page_size must be an integer from 1 through 200",
            phase="read" if cursor else "preflight",
        )


def _validate_terminal_attempt_list(
    *,
    statuses: tuple[GeometryQueryAttemptStatus, ...],
    before_created_at: datetime | None,
    before_attempt_id: str | None,
    limit: int,
) -> None:
    terminal_statuses = {
        GeometryQueryAttemptStatus.SUCCEEDED,
        GeometryQueryAttemptStatus.FAILED,
    }
    if (
        not isinstance(statuses, tuple)
        or not statuses
        or any(
            not isinstance(status, GeometryQueryAttemptStatus) or status not in terminal_statuses
            for status in statuses
        )
        or len({status.value for status in statuses}) != len(statuses)
    ):
        raise GeometryQueryApplicationError(
            "INVALID_ARGUMENT",
            "Geometry query attempt statuses must be a non-empty unique tuple of terminal statuses",
        )
    if (before_created_at is None) != (before_attempt_id is None):
        raise GeometryQueryApplicationError(
            "INVALID_ARGUMENT",
            "Geometry query attempt keyset fields must be provided together",
        )
    if before_created_at is not None and (
        not isinstance(before_created_at, datetime)
        or before_created_at.tzinfo is None
        or before_created_at.utcoffset() is None
    ):
        raise GeometryQueryApplicationError(
            "INVALID_ARGUMENT",
            "Geometry query attempt keyset timestamp must be timezone-aware",
        )
    if before_attempt_id is not None and (
        not isinstance(before_attempt_id, str)
        or _REQUEST_IDENTITY.fullmatch(before_attempt_id) is None
        or len(before_attempt_id) > 80
    ):
        raise GeometryQueryApplicationError(
            "INVALID_ARGUMENT",
            "Geometry query attempt keyset id is invalid",
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 201:
        raise GeometryQueryApplicationError(
            "INVALID_ARGUMENT",
            "Geometry query attempt list limit must be an integer from 1 through 201",
        )


def _validate_request_identity(value: str) -> None:
    if not isinstance(value, str) or _REQUEST_IDENTITY.fullmatch(value) is None:
        raise GeometryQueryApplicationError(
            "INVALID_ARGUMENT",
            "Geometry query request_identity is invalid",
            phase="preflight",
        )


def _patch_error(failure: SourcePatchFailure) -> tuple[str, bool]:
    if failure is SourcePatchFailure.ENGINE_FAILED:
        return "GEOMETRY_QUERY_SERVICE_UNAVAILABLE", True
    return "GEOMETRY_QUERY_REPAIR_INVALID", False


def _bounded_message(message: str) -> str:
    normalized = message.strip() or "Geometry query failed"
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_DIAGNOSTIC_BYTES:
        return normalized
    suffix = "... [truncated]"
    budget = _MAX_DIAGNOSTIC_BYTES - len(suffix.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


__all__ = ["GeometryQueryApplicationError", "GeometryQueryService"]
