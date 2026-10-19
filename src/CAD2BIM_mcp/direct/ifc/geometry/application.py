"""Provider-facing application protocol for Direct geometry-resource tools.

The shared Direct facade can implement this protocol by resolving Direct scope and
delegating to the entry-neutral Geometry Application Core.  No storage, database,
runtime, or transaction behavior belongs in this module.
"""

from __future__ import annotations

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringApplicationError
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetIntegrityError
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError

from CAD2BIM_mcp.direct.geometry_preview_contracts import (
    IfcPreviewDisplayQualityWarningResult, IfcPreviewPanelResult,
)

from CAD2BIM_backend.domain.runtime_execution import (
    GEOMETRY_PREVIEW_EXECUTION, GEOMETRY_QUERY_EXECUTION, RUNTIME_RESPONSE_MARGIN_SECONDS,
)

import json
from time import monotonic
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import TypeAdapter, ValidationError
from CAD2BIM_mcp.direct.ifc.preview_cases import parse_cases, parse_render, project_case_batch

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogError,
    GeometryCatalogListQuery,
    GeometryCatalogRootInfo,
    GeometryCatalogVersionInfo,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    geometry_resource_inputs,
)
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_query import GeometryQueryTargetKind
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionPreviewOutcome
from CAD2BIM_backend.domain.geometry_runtime import GeometrySummary, GeometryPreview
from CAD2BIM_backend.domain.text_source import (
    SourceEdit,
)
from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError

from CAD2BIM_mcp.direct.ifc.geometry.contracts import (
    DirectIfcGeometryPreviewInput,
    DirectIfcGeometryCasesPreviewInput,
    DirectIfcGeometryInspectionPreviewInput,
    DirectIfcFamilyStandardModelPreviewInput,
    IfcFamilyStandardModelPreviewResult, IfcFamilyStandardSectionPreviewResult,
    DirectIfcGeometryQueryPythonInput,
    DirectIfcGeometryResourceInfoInput,
    DirectIfcGeometryResourceListInput,
    DirectIfcGeometryResourceSourceReadInput,
    DirectIfcGeometryQueryCursorInput,
    DirectIfcGeometryQueryFreshInput,
    DirectIfcGeometryQueryRepairInput,
    DirectIfcGeometryResourceListCursorInput,
    IfcGeometryResourceVersionQueryResultTarget,
    IfcGeometryResourceVersionQueryTarget,
    IfcGeometryInspectionQueryTarget,
    IfcFamilyStandardModelQueryTarget,
    IfcGeometryBoundingBoxResult,
    IfcGeometryPreviewOutcomeResult, IfcGeometryInspectionImageResult,
    IfcGeometryInspectionModelPreviewResult, IfcGeometryInspectionSectionPreviewResult,
    IfcGeometrySectionMeasurementResult, IfcGeometrySectionObjectResult,
    IfcGeometrySectionRegionResult, IfcGeometrySectionBoundaryResult,
    IfcGeometrySectionCurveResult, IfcGeometrySectionImageResult,
    IfcGeometryQueryPythonResult,
    IfcGeometryResourceCard,
    IfcGeometryResourceInfoResult,
    IfcGeometryResourceListResult,
    IfcGeometryResourceRootInfoResult,
    IfcGeometryResourceVersionCard,
    IfcGeometryResourceVersionInfoResult,
    IfcGeometrySourceFileResult,
    IfcGeometryResourceSourceReadResult,
    IfcGeometryTopologyResult,
    decode_geometry_preview_profile_json,
)
from CAD2BIM_mcp.direct.source_view import (
    project_text_source_view,
    to_text_source_view,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
    from CAD2BIM_backend.application.geometry_query_service import GeometryQueryService
    from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
    from CAD2BIM_backend.application.geometry_source_service import GeometrySourceService
    from CAD2BIM_backend.domain.geometry_catalog import (
        GeometryCatalogResourceSummary,
        GeometryCatalogVersionSummary,
        GeometrySourceReadResult,
    )
    from CAD2BIM_backend.domain.geometry_query import GeometryQueryPage
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceVersionBundle
    from CAD2BIM_backend.domain.invocation import InvocationScope


_GEOMETRY_MODEL_INTERFACES = TypeAdapter(GeometryModelInterfaces)


@dataclass(frozen=True, slots=True)
class IfcGeometryPreviewAuthorization:
    """One invocation-scoped preview image, not Provider structured data."""

    preview: IfcGeometryPreviewOutcomeResult
    content: bytes | None
    images: tuple[bytes, ...] = ()


class GeometryProviderApplication(Protocol):
    async def list_ifc_geometry_resources(
        self, request: DirectIfcGeometryResourceListInput
    ) -> IfcGeometryResourceListResult: ...

    async def get_ifc_geometry_resource_info(
        self, request: DirectIfcGeometryResourceInfoInput
    ) -> IfcGeometryResourceInfoResult: ...

    async def read_ifc_geometry_resource_source(
        self, request: DirectIfcGeometryResourceSourceReadInput
    ) -> IfcGeometryResourceSourceReadResult: ...

    async def preview_ifc_geometry(
        self, request: DirectIfcGeometryPreviewInput, *, request_identity: str | None = None,
    ) -> IfcGeometryPreviewAuthorization: ...

    async def query_ifc_geometry_python(
        self,
        request: DirectIfcGeometryQueryPythonInput,
        *,
        request_identity: str,
    ) -> IfcGeometryQueryPythonResult: ...


_GEOMETRY_PREVIEW_TIMEOUT_SECONDS = GEOMETRY_PREVIEW_EXECUTION.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS
_GEOMETRY_QUERY_TIMEOUT_SECONDS = GEOMETRY_QUERY_EXECUTION.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS


class IfcGeometryApplicationMixin:
    """Thin Direct projection over the entry-neutral Geometry Application Core."""

    async def list_ifc_geometry_resources(
        self,
        request: DirectIfcGeometryResourceListInput,
    ) -> IfcGeometryResourceListResult:
        async def action(scope: InvocationScope) -> IfcGeometryResourceListResult:
            if isinstance(request, DirectIfcGeometryResourceListCursorInput):
                result = await self._require_geometry_catalog_service().list(
                    scope.identity,
                    None,
                    request.cursor,
                )
            else:
                result = await self._require_geometry_catalog_service().list(
                    scope.identity,
                    GeometryCatalogListQuery(
                        resource_kinds=request.resource_kinds,
                        page_size=request.page_size,
                    ),
                    None,
                )
            return IfcGeometryResourceListResult(
                resources=tuple(_resource_card(item) for item in result.resources),
                complete=result.next_cursor is None,
                next_cursor=result.next_cursor,
            )

        return await self._geometry_read(request.direct_session_alias, "resource_list", action)

    async def get_ifc_geometry_resource_info(
        self,
        request: DirectIfcGeometryResourceInfoInput,
    ) -> IfcGeometryResourceInfoResult:
        target = request.target
        if target.target_kind == "draft":
            async def read_draft(scope):
                info = await self._require_geometry_authoring_service().draft_info(scope, draft_ref=target.draft_ref)
                return IfcGeometryResourceInfoResult(info=info)
            return await self._geometry_read(request.direct_session_alias, "resource_info", read_draft)
        target_ref = (
            target.resource_alias
            if target.target_kind == "resource_root"
            else target.resource_version_alias
        )

        async def action(scope: InvocationScope) -> IfcGeometryResourceInfoResult:
            catalog = self._require_geometry_catalog_service()
            result = await catalog.info(scope.identity, target_ref)
            if isinstance(result, GeometryCatalogRootInfo):
                versions = []
                for version in result.recent_versions:
                    bundle = await catalog.resolve_exact_version(
                        scope.identity,
                        version.resource_version_ref,
                    )
                    versions.append(_version_card(version, bundle))
                info = IfcGeometryResourceRootInfoResult(
                    target_kind="resource_root",
                    resource_alias=result.resource.resource_root_ref,
                    resource_kind=result.resource.resource_kind.value,
                    owner_scope=result.resource.owner_scope.value,
                    display_name=result.resource.display_name,
                    summary=_resource_summary(result.resource.resource_kind),
                    versions=tuple(versions),
                )
            else:
                info = await self._version_info(scope, result)
                bundle = await catalog.resolve_exact_version(scope.identity, target_ref)
                publication = await self._publication_navigation(scope, root_kind="geometry",
                    version_id=bundle.version.resource_version_id)
                info = info.model_copy(update={"publication": publication})
            return IfcGeometryResourceInfoResult(info=info)

        return await self._geometry_read(request.direct_session_alias, "resource_info", action)

    async def read_ifc_geometry_resource_source(
        self,
        request: DirectIfcGeometryResourceSourceReadInput,
    ) -> IfcGeometryResourceSourceReadResult:
        async def action(scope: InvocationScope) -> IfcGeometryResourceSourceReadResult:
            result = await self._require_geometry_source_service().read_source(
                scope.identity,
                request.resource_version_alias,
                request.source_path,
                to_text_source_view(request),
            )
            return _geometry_resource_source_result(result)

        return await self._geometry_read(request.direct_session_alias, "source_read", action)

    async def preview_ifc_geometry(
        self,
        request: DirectIfcGeometryPreviewInput,
        *, request_identity: str | None = None,
    ) -> IfcGeometryPreviewAuthorization:
        async def action(scope: InvocationScope) -> IfcGeometryPreviewAuthorization:
            service = self._require_geometry_preview_service()
            if isinstance(request, DirectIfcFamilyStandardModelPreviewInput):
                outcome = await service.preview_family_standard_model(scope, request.family_version_alias, request.observation)
                return _project_family_standard_preview(outcome)
            if isinstance(request, DirectIfcGeometryInspectionPreviewInput):
                outcome = await service.preview_inspection(
                    scope, request.inspection_ref, request.selection,
                    request.coordinate_space, request.observation,
                )
                return _project_inspection_preview(outcome)
            if isinstance(request, DirectIfcGeometryCasesPreviewInput):
                cases = await self._require_geometry_inspection_service().resolve_cases(
                    scope, parse_cases(request.cases_json, case_kind="geometry"), case_kind="geometry",
                )
                parameters = tuple(item.parameters for item in cases)
            else:
                cases = None
                parameters = (json.loads(request.sample_parameters_json),)
            if request.source.source_kind == "draft":
                prepared = await self._require_geometry_authoring_service().prepare_draft_preview(
                    scope, draft_ref=request.source.draft_ref, parameters=parameters,
                )
            else:
                prepared = await service.prepare_published_cases(scope, request.source.resource_version_alias)
            common = dict(
                **prepared, retain_for_inspection=request.retain, request_identity=request_identity,
                datum_transform=request.datum_transform,
                reference_geometries=tuple(item.to_domain() for item in request.reference_geometries),
            )
            if cases is None:
                outcome = await service.preview_loaded(
                    scope, parameters=parameters[0],
                    render_profile=decode_geometry_preview_profile_json(request.preview_profile_json), **common,
                )
            else:
                outcome = await service.preview_cases(scope, cases=cases, render=parse_render(request.render_json), **common)
            return IfcGeometryPreviewAuthorization(
                preview=project_case_batch(outcome), content=None,
                images=tuple(item.payload for _, item in outcome.images),
            )

        return await self._geometry_read(
            request.direct_session_alias,
            "preview",
            action,
            timeout_seconds=_GEOMETRY_PREVIEW_TIMEOUT_SECONDS,
            timeout_code="GEOMETRY_PREVIEW_TIMEOUT",
            timeout_message="The Direct geometry-preview total deadline expired",
        )

    async def query_ifc_geometry_python(
        self,
        request: DirectIfcGeometryQueryPythonInput,
        *,
        request_identity: str,
    ) -> IfcGeometryQueryPythonResult:
        deadline = monotonic() + GEOMETRY_QUERY_EXECUTION.total_seconds
        async def action(scope: InvocationScope) -> IfcGeometryQueryPythonResult:
            service = self._require_geometry_query_service()
            parent_alias = None
            if isinstance(request, DirectIfcGeometryQueryFreshInput):
                target = request.target
                if isinstance(target, IfcGeometryResourceVersionQueryTarget):
                    bundle = await self._require_geometry_catalog_service().resolve_exact_version(
                        scope.identity,
                        target.resource_version_alias,
                    )
                    page = await service.fresh(
                        scope,
                        bundle.resource.resource_id,
                        bundle.version.resource_version_id,
                        request.code,
                        request_identity,
                        request.page_size,
                        deadline=deadline,
                    )
                elif isinstance(target, IfcGeometryInspectionQueryTarget):
                    page = await service.fresh_inspection(
                        scope, inspection_ref=target.inspection_ref,
                        selection=target.selection, coordinate_space=target.coordinate_space,
                        source_code=request.code, request_identity=request_identity,
                        page_size=request.page_size, deadline=deadline,
                    )
                elif isinstance(target, IfcFamilyStandardModelQueryTarget):
                    page = await service.fresh_family_standard_model(
                        scope, family_version_alias=target.family_version_alias,
                        source_code=request.code, request_identity=request_identity,
                        page_size=request.page_size, deadline=deadline,
                    )
                else:  # pragma: no cover - closed Provider union
                    raise DirectApplicationError(
                        "INVALID_ARGUMENT",
                        "The fresh geometry query target is invalid",
                        phase="preflight",
                    )
            elif isinstance(request, DirectIfcGeometryQueryRepairInput):
                parent_alias = request.parent_attempt_alias
                parent_id = await service.resolve_attempt_alias(scope, parent_alias)
                page = await service.repair(
                    scope,
                    parent_id,

                    tuple(
                        SourceEdit(old_text=edit.old_text, new_text=edit.new_text)
                        for edit in request.edits
                    ),
                    request_identity,
                    request.page_size,
                    deadline=deadline,
                )
            elif isinstance(request, DirectIfcGeometryQueryCursorInput):
                page = await service.continue_page(scope, request.cursor)
            else:  # pragma: no cover - closed Provider union
                raise DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The geometry query branch is invalid",
                    phase="preflight",
                )
            return await self._query_page_result(
                scope,
                page,
                parent_alias=parent_alias,
            )

        return await self._geometry_read(
            request.direct_session_alias,
            "query_python",
            action,
            timeout_seconds=_GEOMETRY_QUERY_TIMEOUT_SECONDS,
            timeout_code="GEOMETRY_QUERY_TIMEOUT",
            timeout_message="The Direct geometry-query total deadline expired",
        )

    async def _version_info(
        self,
        scope: InvocationScope,
        result: GeometryCatalogVersionInfo,
    ) -> IfcGeometryResourceVersionInfoResult:
        catalog = self._require_geometry_catalog_service()
        resources = self._require_geometry_resource_service()
        bundle = await catalog.resolve_exact_version(
            scope.identity,
            result.version.resource_version_ref,
        )
        geometry_summary = _geometry_summary(bundle)
        source_files: tuple[IfcGeometrySourceFileResult, ...] = ()
        if result.version.source_available:
            source = await self._require_geometry_source_service().read_source(
                scope.identity,
                result.version.resource_version_ref,
                None,
                None,
            )
            source_files = tuple(
                IfcGeometrySourceFileResult(
                    source_path=member.source_path,
                    size_bytes=member.size_bytes,
                    sha256=member.sha256,
                    text_available=member.text_available,
                )
                for member in source.members
            )
        parameter_schema_json = None
        parameter_asset = next(
            (
                asset
                for asset in bundle.assets
                if asset.asset_role is GeometryResourceAssetRole.PARAMETER_SCHEMA
            ),
            None,
        )
        if parameter_asset is not None:
            exact = await resources.read_exact_asset(
                scope.identity,
                resource_id=bundle.resource.resource_id,
                resource_version_id=bundle.version.resource_version_id,
                asset_role=GeometryResourceAssetRole.PARAMETER_SCHEMA,
            )
            if exact is None:
                raise ValueError("parameter schema asset disappeared from exact version")
            asset, content = exact
            if asset.sha256 != parameter_asset.sha256 or len(content) != asset.size_bytes:
                raise ValueError("parameter schema asset failed exact verification")
            parameter_schema_json = canonical_json(json.loads(content.decode("utf-8")))
            if len(parameter_schema_json.encode("utf-8")) > 65_536:
                raise ValueError("parameter schema exceeds Provider response budget")

        manifest = bundle.version.manifest
        local_frame = manifest.get("local_frame")
        if not isinstance(local_frame, dict) or local_frame.get("handedness") != "right":
            raise ValueError("geometry version local basis is not right-handed")
        _representative_geometry_sha256(bundle)
        input_infos = []
        for item in geometry_resource_inputs(manifest):
            target = await resources.get_exact_version(scope.identity,
                resource_id=item.resource_id, resource_version_id=item.resource_version_id)
            if target is None or target.version.content_digest != item.content_digest:
                raise ValueError("geometry input exact version is no longer available")
            input_infos.append({
                "input_key": item.input_key,
                "resource_version_alias": catalog.version_ref(scope.identity, target),
            })
        return IfcGeometryResourceVersionInfoResult(
            target_kind="resource_version",
            resource_alias=result.resource_root_ref,
            resource_version_alias=result.version.resource_version_ref,
            resource_kind=result.resource_kind.value,
            owner_scope=result.owner_scope.value,
            display_name=result.display_name,
            usage_instructions=(
                manifest.get("usage_instructions")
                if isinstance(manifest.get("usage_instructions"), str)
                else None
            ),
            version_no=result.version.version_no,
            origin_kind=result.version.origin_kind,
            geometry_format=geometry_summary.format.value if geometry_summary else None,
            unit="METRE",
            local_basis="right_handed",
            geometry_inputs=tuple(input_infos),
            interfaces=_GEOMETRY_MODEL_INTERFACES.validate_python(manifest.get("interfaces", ())),
            bbox=IfcGeometryBoundingBoxResult(
                minimum=geometry_summary.bounds.minimum_m,
                maximum=geometry_summary.bounds.maximum_m,
            ) if geometry_summary else None,
            topology=IfcGeometryTopologyResult(
                solid_count=geometry_summary.topology.solid_count,
                shell_count=geometry_summary.topology.shell_count,
                face_count=geometry_summary.topology.face_count,
                edge_count=geometry_summary.topology.edge_count,
                vertex_count=geometry_summary.topology.vertex_count,
                triangle_count=geometry_summary.topology.triangle_count,
            ) if geometry_summary else None,
            source_available=result.version.source_available,
            source_files=source_files,
            preview_available=(
                bundle.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL
                or result.version.preview_available
            ),
            parameter_schema_json=parameter_schema_json,
            created_at=result.version.created_at,
        )

    async def _query_page_result(
        self,
        scope: InvocationScope,
        page: GeometryQueryPage,
        *,
        parent_alias: str | None,
    ) -> IfcGeometryQueryPythonResult:
        if page.target_kind is GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION:
            if page.resource_version_id is None:
                raise ValueError("geometry resource query result has no exact version")
            bundle = await self._require_geometry_resource_service().get_version_by_id(
                scope.identity,
                resource_version_id=page.resource_version_id,
            )
            if bundle is None:
                raise ValueError("geometry query result references an invisible exact version")
            target = IfcGeometryResourceVersionQueryResultTarget(
                target_kind="geometry_resource_version",
                resource_version_alias=self._require_geometry_catalog_service().version_ref(
                    scope.identity,
                    bundle,
                ),
            )
        elif page.target_kind is GeometryQueryTargetKind.GEOMETRY_INSPECTION:
            if page.inspection_ref is None or page.selection is None or page.coordinate_space is None:
                raise ValueError("inspection query result lost its exact target context")
            target = IfcGeometryInspectionQueryTarget(
                target_kind="geometry_inspection", inspection_ref=page.inspection_ref,
                selection=page.selection, coordinate_space=page.coordinate_space,
            )
        elif page.target_kind is GeometryQueryTargetKind.FAMILY_STANDARD_MODEL:
            target = IfcFamilyStandardModelQueryTarget(target_kind="family_standard_model",
                family_version_alias=await self._require_geometry_query_service().family_version_alias(scope, page.family_version_id))
        else:  # pragma: no cover - closed Domain enum
            raise ValueError("geometry query result has an unsupported target kind")
        result_json = canonical_json(list(page.results))
        if len(result_json.encode("utf-8")) > 65_536:
            raise DirectApplicationError(
                "GEOMETRY_QUERY_PAGE_TOO_LARGE",
                "The query page exceeds the Provider response budget; start a fresh query with a smaller page_size",
                phase="output_validation",
            )
        return IfcGeometryQueryPythonResult(
            attempt_alias=page.attempt_alias,
            parent_attempt_alias=parent_alias,
            target=target,
            source_sha256=page.source_sha256,
            status="succeeded",
            summary=(
                f"Returned {page.returned_count} of {page.total_count} bounded geometry-query results."
            ),
            result_json=result_json,
            next_cursor=page.next_cursor,
        )

    async def _geometry_read(
        self,
        direct_session_alias: str,
        action_name: str,
        operation,
        **timeout_options,
    ):
        from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
        from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
        from CAD2BIM_backend.application.geometry_query_service import (
            GeometryQueryApplicationError,
        )
        from CAD2BIM_backend.domain.geometry_runtime import (
            GeometryPreviewRequestError,
            GeometryRuntimeError,
            GeometryRuntimeResourceLimitError,
            GeometryRuntimeUnavailableError,
        )

        try:
            return await self._run_runtime_read(
                direct_session_alias,
                f"ifc_geometry_{action_name}",
                operation,
                **timeout_options,
            )
        except DirectApplicationError:
            raise
        except AuthoringDraftError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase="preflight", retryable=False) from None
        except GeometryAuthoringApplicationError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase=exc.phase, retryable=exc.retryable,
                                         operation_alias=exc.operation_alias, diagnostic=exc.diagnostic) from None
        except GeometryInspectionApplicationError as exc:
            raise DirectApplicationError(
                exc.code, str(exc), phase=exc.phase, retryable=exc.retryable,
                operation_alias=exc.operation_alias,
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except GeometryQueryApplicationError as exc:
            diagnostic = (
                {"attempt_alias": exc.attempt_alias} if exc.attempt_alias is not None else None
            )
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase=exc.phase,
                retryable=exc.retryable,
                diagnostic=diagnostic,
            ) from None
        except (GeometryCatalogError, ProductFamilyCatalogError) as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase="read",
                retryable=False,
            ) from None
        except ProductFamilyAssetIntegrityError as exc:
            raise DirectApplicationError("PRODUCT_FAMILY_INTEGRITY_ERROR", str(exc),
                phase="read", retryable=False) from None
        except GeometryPreviewRequestError as exc:
            raise DirectApplicationError(
                "GEOMETRY_PREVIEW_REQUEST_INVALID",
                str(exc),
                phase="input_validation",
                retryable=False,
            ) from None
        except GeometryRuntimeResourceLimitError as exc:
            raise DirectApplicationError(
                "GEOMETRY_RUNTIME_UNAVAILABLE",
                "Geometry execution exceeded its fixed resource limit",
                phase="execution",
                retryable=False,
                diagnostic=(exc.diagnostic.model_dump(mode="json", exclude_none=True)
                            if exc.diagnostic is not None else None),
            ) from None
        except GeometryRuntimeUnavailableError as exc:
            raise DirectApplicationError(
                "GEOMETRY_RUNTIME_UNAVAILABLE",
                str(exc),
                phase="execution",
                retryable=exc.retryable,
                diagnostic=exc.diagnostic,
            ) from None
        except GeometryRuntimeError as exc:
            raise DirectApplicationError(
                "GEOMETRY_RESOURCE_INTEGRITY_ERROR",
                str(exc),
                phase="output_validation",
                retryable=False,
            ) from None
        except (
            KeyError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            raise DirectApplicationError(
                "GEOMETRY_RESOURCE_INTEGRITY_ERROR",
                "The exact geometry resource could not be safely projected",
                phase="output_validation",
            ) from None

    def _require_geometry_resource_service(self) -> GeometryResourceService:
        return self._require_geometry_service("_geometry_resource_service", "resource")

    def _require_geometry_catalog_service(self) -> GeometryCatalogService:
        return self._require_geometry_service("_geometry_catalog_service", "Catalog")

    def _require_geometry_source_service(self) -> GeometrySourceService:
        return self._require_geometry_service("_geometry_source_service", "source")

    def _require_geometry_preview_service(self) -> GeometryPreviewService:
        return self._require_geometry_service("_geometry_preview_service", "preview")

    def _require_geometry_query_service(self) -> GeometryQueryService:
        return self._require_geometry_service("_geometry_query_service", "query")

    def _require_geometry_inspection_service(self) -> GeometryInspectionService:
        return self._require_geometry_service("_geometry_inspection_service", "inspection")

    def _require_geometry_service(self, attribute: str, label: str):
        service = getattr(self, attribute, None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                f"The IFC geometry {label} service is unavailable; wait for server-side recovery",
                phase="preflight",
            )
        return service


def _inspection_image_result(preview: GeometryPreview) -> IfcGeometryInspectionImageResult:
    return IfcGeometryInspectionImageResult(
        size_bytes=len(preview.payload), width_px=preview.width_px, height_px=preview.height_px,
        display_quality_warnings=tuple(
            IfcPreviewDisplayQualityWarningResult.from_domain(item)
            for item in preview.display_quality_warnings
        ),
        panels=tuple(IfcPreviewPanelResult.from_domain(item) for item in preview.resolved_panels),
        connection_context=preview.connection_context,
        connection_context_diagnostics=preview.connection_context_diagnostics,
    )


def _project_section_measurement(value) -> IfcGeometrySectionMeasurementResult:
    # Curves retain exact kernel facts; raster sampling points remain rendering data.
    return IfcGeometrySectionMeasurementResult(
        **value.model_dump(exclude={"objects"}),
        objects=tuple(IfcGeometrySectionObjectResult(
            **obj.model_dump(exclude={"material_regions", "intersection_curves"}),
            intersection_curves=tuple(IfcGeometrySectionCurveResult(
                **curve.model_dump(exclude={"drawing_points_uv_m"}),
            ) for curve in obj.intersection_curves),
            material_regions=tuple(IfcGeometrySectionRegionResult(
                **region.model_dump(exclude={"boundaries"}),
                boundaries=tuple(IfcGeometrySectionBoundaryResult(
                    **boundary.model_dump(exclude={"curves"}),
                    curves=tuple(IfcGeometrySectionCurveResult(
                        **curve.model_dump(exclude={"drawing_points_uv_m"}),
                    ) for curve in boundary.curves),
                ) for boundary in region.boundaries),
            ) for region in obj.material_regions),
        ) for obj in value.objects),
    )


def _project_inspection_preview(outcome: GeometryInspectionPreviewOutcome) -> IfcGeometryPreviewAuthorization:
    common = {
        "inspection_ref": outcome.inspection_ref, "operation_alias": outcome.operation_alias,
        "operation_ref": outcome.operation_ref, "case_key": outcome.case_key,
        "source_kind": outcome.source_kind, "source_version_ref": outcome.source_version_ref,
        "result_coordinate_space": outcome.result_coordinate_space,
        "selection": outcome.selection, "coordinate_space": outcome.coordinates,
        "diagnostics": outcome.diagnostics,
    }
    return _project_saved_geometry_observation(outcome, common,
        IfcGeometryInspectionModelPreviewResult, IfcGeometryInspectionSectionPreviewResult)


def _project_family_standard_preview(outcome) -> IfcGeometryPreviewAuthorization:
    common = {"family_version_alias": outcome.family_version_ref,
        "public_parameters_json": outcome.public_parameters_json, "diagnostics": outcome.diagnostics}
    return _project_saved_geometry_observation(outcome, common,
        IfcFamilyStandardModelPreviewResult, IfcFamilyStandardSectionPreviewResult)


def _project_saved_geometry_observation(outcome, common, model_type, section_type):
    if outcome.section is not None:
        section = outcome.section
        result = section_type(
            **common,
            measurements=tuple(_project_section_measurement(item) for item in section.measurements),
            images=tuple(IfcGeometrySectionImageResult(
                **_inspection_image_result(item.preview).model_dump(),
                section_index=item.section_index, resolved_display=item.resolved_display,
                display_clipped=item.display_clipped, diagnostics=item.diagnostics,
            ) for item in section.images),
            render_errors=section.render_errors,
        )
        return IfcGeometryPreviewAuthorization(
            preview=result, content=None, images=tuple(item.preview.payload for item in section.images),
        )
    result = model_type(
        **common, image=None if outcome.preview is None else _inspection_image_result(outcome.preview),
    )
    return IfcGeometryPreviewAuthorization(
        preview=result, content=None if outcome.preview is None else outcome.preview.payload,
    )


def _resource_summary(kind: GeometryResourceKind) -> str:
    if kind is GeometryResourceKind.MAPPED_MODEL:
        return "Ready reusable mapped geometry with exact immutable versions."
    return "Ready reusable modeling function with exact immutable versions."


def _resource_card(item: GeometryCatalogResourceSummary) -> IfcGeometryResourceCard:
    return IfcGeometryResourceCard(
        resource_alias=item.resource_root_ref,
        resource_kind=item.resource_kind.value,
        owner_scope=item.owner_scope.value,
        display_name=item.display_name,
        summary=_resource_summary(item.resource_kind),
        latest_version_alias=item.latest_version.resource_version_ref,
        latest_version_no=item.latest_version.version_no,
        created_at=item.created_at,
    )


def _version_card(
    item: GeometryCatalogVersionSummary,
    bundle: GeometryResourceVersionBundle,
) -> IfcGeometryResourceVersionCard:
    summary = _geometry_summary(bundle)
    return IfcGeometryResourceVersionCard(
        resource_version_alias=item.resource_version_ref,
        version_no=item.version_no,
        origin_kind=item.origin_kind,
        geometry_format=summary.format.value if summary else None,
        has_source=item.source_available,
        has_preview=(
            bundle.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL
            or item.preview_available
        ),
        created_at=item.created_at,
    )


def _geometry_summary(bundle: GeometryResourceVersionBundle) -> GeometrySummary | None:
    manifest = bundle.version.manifest
    raw = manifest.get("geometry_summary")
    if raw is None and bundle.resource.resource_kind is GeometryResourceKind.MODELING_FUNCTION:
        return None
    summary = GeometrySummary.model_validate(raw)
    if manifest.get("unit") != "METRE" or summary.unit != "METRE":
        raise ValueError("geometry resource unit is not METRE")
    if manifest.get("geometry_format") != summary.format.value:
        raise ValueError("geometry resource format summary is inconsistent")
    return summary


def _representative_geometry_sha256(
    bundle: GeometryResourceVersionBundle,
) -> str | None:
    role = (
        GeometryResourceAssetRole.CANONICAL_GEOMETRY
        if bundle.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL
        else GeometryResourceAssetRole.SAMPLE_OUTPUT
    )
    manifest_key = (
        "canonical_geometry_sha256"
        if role is GeometryResourceAssetRole.CANONICAL_GEOMETRY
        else "sample_output_sha256"
    )
    asset = next((item for item in bundle.assets if item.asset_role is role), None)
    if asset is None:
        if role is GeometryResourceAssetRole.SAMPLE_OUTPUT and manifest_key not in bundle.version.manifest:
            return None
        raise ValueError("geometry resource has no representative geometry asset")
    if bundle.version.manifest.get(manifest_key) != asset.sha256:
        raise ValueError("geometry resource manifest changed its representative geometry")
    return asset.sha256


def _geometry_resource_source_result(
    result: GeometrySourceReadResult,
) -> IfcGeometryResourceSourceReadResult:
    if result.selected_path is None or result.view is None:
        raise ValueError("Provider source read requires one selected source member")
    return IfcGeometryResourceSourceReadResult(
        resource_version_alias=result.resource_version_ref,
        source_path=result.selected_path,
        **project_text_source_view(result.view),
    )


__all__ = [
    "GeometryProviderApplication",
    "IfcGeometryApplicationMixin",
    "IfcGeometryPreviewAuthorization",
]
