"""Owner-backed discovery and reads for persisted executable source."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from CAD2BIM_backend.application.data_query_attempt_service import (
    DataQueryAttemptApplicationError,
)
from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
from CAD2BIM_backend.application.exact_artifact_reader import ExactArtifactReader
from CAD2BIM_backend.application.geometry_authoring_service import (
    GeometryAuthoringApplicationError,
)
from CAD2BIM_backend.application.geometry_query_service import GeometryQueryApplicationError
from CAD2BIM_backend.application.ifc_query_service import IfcQueryApplicationError
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService
from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService
from CAD2BIM_backend.application.text_source_view import TextSourceViewError, TextSourceViewService
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationStatus,
    DataTransformOperationDetail,
)
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryAttemptStatus,
    DataQueryEngine,
)
from CAD2BIM_backend.domain.geometry_query import GeometryQueryAttemptStatus
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogError
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
from CAD2BIM_backend.domain.ifc_query import IfcQueryAttemptStatus
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError, ProductFamilyCatalogRejection
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScopeKind
from CAD2BIM_backend.domain.text_source import TextSourceView, TextSourceViewResult
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError, OpaqueCursorCodecPort
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringSourcePort


_CURSOR_DOMAIN = "code_discovery_page"
_LOCATOR_DOMAIN = "code_discovery_locator"
_MAX_OWNER_ERROR_BYTES = 2_000
MAX_CODE_READ_BATCH_ITEMS = 8
MAX_CODE_READ_BATCH_BYTES = 256 * 1024
_CODE_LOCATOR_INVALID_MESSAGE = (
    "The code navigation token is invalid; run code_list again in the current scope "
    "and copy code_locator and source_form unchanged from the same card, plus a "
    "listed source_path for bundle code"
)


class CodeDiscoveryApplicationError(RuntimeError):
    """Bounded owner-backed discovery failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CodeKind(StrEnum):
    GEOMETRY_DRAFT = "geometry_draft"
    PRODUCT_FAMILY_DRAFT = "product_family_draft"
    DATA_TRANSFORM_ATTEMPT = "data_transform_attempt"
    CAD_MEASUREMENT_IMPLEMENTATION = "cad_measurement_implementation"
    DATA_QUERY_SQL_ATTEMPT = "data_query_sql_attempt"
    DATA_QUERY_PYTHON_ATTEMPT = "data_query_python_attempt"
    GEOMETRY_AUTHORING_ATTEMPT = "geometry_authoring_attempt"
    GEOMETRY_RESOURCE_PROGRAM = "geometry_resource_program"
    GEOMETRY_QUERY_ATTEMPT = "geometry_query_attempt"
    IFC_QUERY_ATTEMPT = "ifc_query_attempt"
    PRODUCT_FAMILY_AUTHORING_ATTEMPT = "product_family_authoring_attempt"
    PRODUCT_FAMILY_REALIZATION_PROGRAM = "product_family_realization_program"


_LOCATOR_OWNER_KINDS: dict[CodeKind, frozenset[str]] = {
    CodeKind.GEOMETRY_DRAFT: frozenset({"authoring_draft"}),
    CodeKind.PRODUCT_FAMILY_DRAFT: frozenset({"authoring_draft"}),
    CodeKind.DATA_TRANSFORM_ATTEMPT: frozenset(
        {"data_transform_operation", "data_transform_package"}
    ),
    CodeKind.DATA_QUERY_SQL_ATTEMPT: frozenset({"data_query_attempt"}),
    CodeKind.DATA_QUERY_PYTHON_ATTEMPT: frozenset({"data_query_attempt"}),
    CodeKind.CAD_MEASUREMENT_IMPLEMENTATION: frozenset(
        {"data_transform_operation", "data_transform_package"}
    ),
    CodeKind.GEOMETRY_AUTHORING_ATTEMPT: frozenset({"geometry_authoring_operation"}),
    CodeKind.GEOMETRY_RESOURCE_PROGRAM: frozenset({"geometry_resource_version"}),
    CodeKind.GEOMETRY_QUERY_ATTEMPT: frozenset({"geometry_query_attempt"}),
    CodeKind.IFC_QUERY_ATTEMPT: frozenset({"ifc_query_attempt"}),
    CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT: frozenset({"product_family_authoring_operation"}),
    CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM: frozenset({"product_family_version"}),
}

_RUN_BOUND_CODE_KINDS = frozenset(
    {
        CodeKind.DATA_TRANSFORM_ATTEMPT,
        CodeKind.CAD_MEASUREMENT_IMPLEMENTATION,
        CodeKind.DATA_QUERY_SQL_ATTEMPT,
        CodeKind.DATA_QUERY_PYTHON_ATTEMPT,
        CodeKind.GEOMETRY_AUTHORING_ATTEMPT,
        CodeKind.IFC_QUERY_ATTEMPT,
        CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT,
    }
)
_TASK_BOUND_CODE_KINDS = frozenset(
    {
        CodeKind.GEOMETRY_DRAFT,
        CodeKind.PRODUCT_FAMILY_DRAFT,
        CodeKind.GEOMETRY_RESOURCE_PROGRAM,
        CodeKind.GEOMETRY_QUERY_ATTEMPT,
        CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
    }
)

_KNOWN_OWNER_ERRORS = (
    DataQueryAttemptApplicationError,
    DataRuntimeApplicationError,
    GeometryAuthoringApplicationError,
    GeometryCatalogError,
    GeometryQueryApplicationError,
    IfcQueryApplicationError,
    ProductFamilyCatalogError,
)


class CodeTerminalStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CodeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeListQuery(CodeContract):
    code_kind: CodeKind
    draft_ref: str | None = Field(default=None, min_length=1, max_length=16_384)
    statuses: tuple[CodeTerminalStatus, ...] = ()
    page_size: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def validate_filters(self) -> "CodeListQuery":
        is_draft = self.code_kind in {CodeKind.GEOMETRY_DRAFT, CodeKind.PRODUCT_FAMILY_DRAFT}
        if is_draft != (self.draft_ref is not None) or (is_draft and self.statuses):
            raise ValueError("draft code requires only its draft_ref and no status filter")
        if len(self.statuses) != len(set(self.statuses)):
            raise ValueError("code status filters must be unique")
        if (
            self.code_kind
            in {
                CodeKind.GEOMETRY_RESOURCE_PROGRAM,
                CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
            }
            and self.statuses
        ):
            raise ValueError("published programs do not accept a status filter")
        return self


class CodeMember(CodeContract):
    source_path: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_available: bool


class CodeCard(CodeContract):
    code_locator: str = Field(min_length=1, max_length=16_384)
    code_kind: CodeKind
    language: Literal["sql", "python"]
    source_form: Literal["single_text", "bundle"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    members: tuple[CodeMember, ...] = Field(default=(), max_length=64)
    owner_type: Literal[
        "authoring_draft",
        "critical_operation",
        "geometry_resource_version",
        "data_query_attempt",
        "geometry_query_attempt",
        "ifc_query_attempt",
        "product_family_version",
    ]
    owner_alias: str = Field(min_length=1, max_length=16_384)
    status: Literal["succeeded", "failed", "published", "draft"]
    diagnostic_code: str | None = Field(default=None, min_length=1, max_length=120)
    diagnostic_message: str | None = Field(default=None, min_length=1, max_length=2_000)
    parent_owner_alias: str | None = Field(default=None, min_length=1, max_length=16_384)
    ownership: Literal["agent", "server"]
    repairable: bool
    repair_entry: str | None = Field(default=None, min_length=1, max_length=120)
    repair_unavailable_reason: (
        Literal[
            "operation_succeeded",
            "owner_scope_read_only",
            "server_recovery_required",
        ]
        | None
    ) = None
    result_alias: str | None = Field(default=None, min_length=1, max_length=16_384)
    result_code_locator: str | None = Field(default=None, min_length=1, max_length=16_384)
    result_available: bool | None = Field(
        default=None,
        description=(
            "Whether a Data QueryAttempt's temporary in-process result snapshot is currently "
            "available. Other owner types return null and expose durable results through "
            "result_alias."
        ),
    )
    created_at: datetime | None = None
    finished_at: datetime | None = None
    sequence: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_shape(self) -> "CodeCard":
        if self.source_form == "single_text" and self.members:
            raise ValueError("single text code cannot contain bundle members")
        if self.source_form == "bundle" and not self.members:
            raise ValueError("bundle code requires a member directory")
        if self.repairable != (self.repair_entry is not None):
            raise ValueError("repairable and repair_entry disagree")
        if self.repairable and self.repair_unavailable_reason is not None:
            raise ValueError("repairable code cannot have an unavailable reason")
        if self.code_kind is CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT:
            if not self.repairable and self.repair_unavailable_reason is None:
                raise ValueError("non-repairable ProductFamily code requires a safe reason")
        if self.code_kind is CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM:
            if not self.repairable and self.repair_unavailable_reason != "owner_scope_read_only":
                raise ValueError(
                    "non-repairable ProductFamily program requires an owner-scope reason"
                )
        if (self.owner_type == "data_query_attempt") != (self.result_available is not None):
            raise ValueError("result_available is reserved for data query Attempts")
        return self


class CodeListResult(CodeContract):
    code_kind: CodeKind
    codes: tuple[CodeCard, ...] = Field(max_length=50)
    returned_count: int = Field(ge=0, le=50)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=16_384)
    rejections: tuple[ProductFamilyCatalogRejection, ...] = ()

    @model_validator(mode="after")
    def validate_page(self) -> "CodeListResult":
        if self.returned_count != len(self.codes):
            raise ValueError("returned_count must equal codes length")
        if any(item.code_kind is not self.code_kind for item in self.codes):
            raise ValueError("one code page cannot mix code kinds")
        return self


class CodeReadResult(CodeContract):
    code_locator: str = Field(min_length=1, max_length=16_384)
    code_kind: CodeKind
    language: Literal["sql", "python"]
    source_form: Literal["single_text", "bundle"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_path: str | None = Field(default=None, min_length=1, max_length=255)
    member_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    view: TextSourceViewResult

    @model_validator(mode="after")
    def validate_selection(self) -> "CodeReadResult":
        selected = self.selected_path is not None and self.member_sha256 is not None
        if self.source_form == "bundle" and not selected:
            raise ValueError("bundle code read requires an exact selected member")
        if self.source_form == "single_text" and selected:
            raise ValueError("single text code read cannot select a bundle member")
        return self


class CodeReadSelection(CodeContract):
    """One existing owner-backed read inside a bounded batch."""

    code_locator: str = Field(min_length=1, max_length=16_384)
    source_path: str | None = Field(default=None, min_length=1, max_length=255)
    view: TextSourceView


class CodeReadBatchResult(CodeContract):
    reads: tuple[CodeReadResult, ...] = Field(min_length=1, max_length=MAX_CODE_READ_BATCH_ITEMS)


class CodeDiscoveryService:
    """Project code facts without owning their persistence or authorization."""

    def __init__(
        self,
        data_runtime,
        geometry_resources,
        geometry_catalog,
        geometry_sources,
        geometry_queries,
        ifc_queries,
        geometry_authoring,
        cursor_codec: OpaqueCursorCodecPort,
        *,
        data_query_attempts=None,
        product_family_catalog: ProductFamilyCatalogService | None = None,
        product_family_assets: ProductFamilyAssetService | None = None,
        exact_artifacts: ExactArtifactReader | None = None,
        source_bundles: GeometryAuthoringSourcePort | None = None,
        product_family_authoring=None,
        text_views: TextSourceViewService | None = None,
    ) -> None:
        self._data_runtime = data_runtime
        self._geometry_resources = geometry_resources
        self._geometry_catalog = geometry_catalog
        self._geometry_sources = geometry_sources
        self._geometry_queries = geometry_queries
        self._ifc_queries = ifc_queries
        self._geometry_authoring = geometry_authoring
        self._data_query_attempts = data_query_attempts
        self._product_family_catalog = product_family_catalog
        self._product_family_assets = product_family_assets
        self._exact_artifacts = exact_artifacts
        self._source_bundles = source_bundles
        self._product_family_authoring = product_family_authoring
        self._cursor_codec = cursor_codec
        self._text_views = text_views or TextSourceViewService()

    async def list_codes(
        self,
        scope: InvocationScope,
        query: CodeListQuery | None,
        cursor: str | None,
    ) -> CodeListResult:
        if (query is None) == (cursor is None):
            raise CodeDiscoveryApplicationError(
                "CODE_LIST_INPUT_INVALID",
                "Provide exactly one first-page query or continuation cursor",
            )
        boundary: dict[str, object] = {}
        if cursor is not None:
            query, boundary = self._decode_cursor(scope, cursor)
        assert query is not None
        try:
            if query.code_kind in {CodeKind.GEOMETRY_DRAFT, CodeKind.PRODUCT_FAMILY_DRAFT}:
                return await self._list_draft(scope, query)
            if query.code_kind in {
                CodeKind.DATA_TRANSFORM_ATTEMPT,
                CodeKind.CAD_MEASUREMENT_IMPLEMENTATION,
                CodeKind.GEOMETRY_AUTHORING_ATTEMPT,
                CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT,
            }:
                return await self._list_operations(scope, query, boundary)
            if query.code_kind is CodeKind.GEOMETRY_RESOURCE_PROGRAM:
                return await self._list_geometry_programs(scope, query, boundary)
            if query.code_kind in {
                CodeKind.DATA_QUERY_SQL_ATTEMPT,
                CodeKind.DATA_QUERY_PYTHON_ATTEMPT,
            }:
                return await self._list_data_query_attempts(scope, query, boundary)
            if query.code_kind is CodeKind.GEOMETRY_QUERY_ATTEMPT:
                return await self._list_geometry_attempts(scope, query, boundary)
            if query.code_kind is CodeKind.IFC_QUERY_ATTEMPT:
                return await self._list_ifc_attempts(scope, query, boundary)
            if query.code_kind is CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM:
                return await self._list_product_family_programs(scope, query, boundary)
        except CodeDiscoveryApplicationError:
            raise
        except Exception as exc:
            raise self._owner_error(exc) from exc
        raise CodeDiscoveryApplicationError("CODE_KIND_UNSUPPORTED", "Code kind is unsupported")

    async def read_code(
        self,
        scope: InvocationScope,
        code_locator: str,
        source_path: str | None,
        view: TextSourceView,
        *,
        allowed_code_kinds: frozenset[CodeKind] | None = None,
    ) -> CodeReadResult:
        claims = self._decode_locator(scope, code_locator)
        if allowed_code_kinds is not None and CodeKind(claims["code_kind"]) not in allowed_code_kinds:
            raise CodeDiscoveryApplicationError("CODE_LOCATOR_INVALID", _CODE_LOCATOR_INVALID_MESSAGE)
        owner_kind = claims["owner_kind"]
        try:
            if owner_kind == "authoring_draft":
                result = await self._read_draft_code(scope, claims, source_path, view)
            elif owner_kind in {"data_transform_operation", "data_transform_package"}:
                result = await self._read_transform(scope, claims, view)
            elif owner_kind == "geometry_authoring_operation":
                result = await self._read_authoring(scope, claims, source_path, view)
            elif owner_kind == "geometry_resource_version":
                result = await self._read_geometry_program(scope, claims, source_path, view)
            elif owner_kind == "data_query_attempt":
                result = await self._read_data_query_attempt(scope, claims, source_path, view)
            elif owner_kind == "geometry_query_attempt":
                result = await self._read_geometry_attempt(scope, claims, source_path, view)
            elif owner_kind == "ifc_query_attempt":
                result = await self._read_ifc_attempt(scope, claims, source_path, view)
            elif owner_kind == "product_family_authoring_operation":
                result = await self._read_product_family_authoring(scope, claims, source_path, view)
            elif owner_kind == "product_family_version":
                result = await self._read_product_family_program(scope, claims, source_path, view)
            else:
                raise CodeDiscoveryApplicationError(
                    "CODE_LOCATOR_INVALID", "The code locator is invalid"
                )
            if result["code_kind"].value != claims["code_kind"]:
                raise CodeDiscoveryApplicationError(
                    "CODE_OWNER_INTEGRITY_ERROR",
                    "The code locator no longer matches its exact owner",
                )
            return CodeReadResult(code_locator=code_locator, **result)
        except CodeDiscoveryApplicationError:
            raise
        except Exception as exc:
            raise self._owner_error(exc) from exc

    async def read_code_batch(
        self,
        scope: InvocationScope,
        selections: tuple[CodeReadSelection, ...],
    ) -> CodeReadBatchResult:
        if not 1 <= len(selections) <= MAX_CODE_READ_BATCH_ITEMS:
            raise CodeDiscoveryApplicationError(
                "CODE_BATCH_INVALID", "A code read batch requires 1 through 8 selections"
            )
        identities = [item.model_dump_json() for item in selections]
        if len(set(identities)) != len(identities):
            raise CodeDiscoveryApplicationError(
                "CODE_BATCH_INVALID", "A code read batch cannot repeat an identical selection"
            )
        reads: list[CodeReadResult] = []
        for index, selection in enumerate(selections):
            try:
                result = await self.read_code(
                    scope, selection.code_locator, selection.source_path, selection.view
                )
            except CodeDiscoveryApplicationError as exc:
                raise CodeDiscoveryApplicationError(exc.code, f"reads[{index}]: {exc}") from exc
            reads.append(result)
            batch = CodeReadBatchResult(reads=tuple(reads))
            if len(batch.model_dump_json().encode("utf-8")) > MAX_CODE_READ_BATCH_BYTES:
                raise CodeDiscoveryApplicationError(
                    "CODE_BATCH_BUDGET_EXCEEDED",
                    "The code read batch exceeds 262144 UTF-8 response bytes; select fewer "
                    "members or bounded lines/search views. No partial batch is returned.",
                )
        return batch

    async def _list_operations(self, scope, query, boundary) -> CodeListResult:
        if query.code_kind is CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT:
            page = await self._data_runtime.list_product_family_operation_sources(
                scope, statuses=_operation_statuses(query.statuses),
                before_sequence=_optional_int(boundary, "before_sequence"), page_size=query.page_size,
            )
            cards = tuple([await self._product_family_authoring_operation_card(scope, source)
                           for source in page.sources])
            cursor = None if page.complete else self._encode_cursor(
                scope, query, {"before_sequence": page.next_before_sequence},
            )
            return CodeListResult(code_kind=query.code_kind, codes=cards,
                                  returned_count=len(cards), next_cursor=cursor)
        if query.code_kind is CodeKind.DATA_TRANSFORM_ATTEMPT:
            tool_names = ("data_derive",)
        elif query.code_kind is CodeKind.CAD_MEASUREMENT_IMPLEMENTATION:
            tool_names = ("cad_compute_measurements",)
        elif query.code_kind is CodeKind.GEOMETRY_AUTHORING_ATTEMPT:
            tool_names = ("ifc_geometry_resource_write",)
        else:
            raise CodeDiscoveryApplicationError("CODE_KIND_UNSUPPORTED", "Code kind is unsupported")
        statuses = _operation_statuses(query.statuses)
        if query.code_kind is CodeKind.GEOMETRY_AUTHORING_ATTEMPT:
            page = await self._data_runtime.list_terminal_operation_catalog(
                scope,
                tool_names=tool_names,
                statuses=statuses,
                require_code_artifact=True,
                before_sequence=_optional_int(boundary, "before_sequence"),
                page_size=query.page_size,
            )
        else:
            page = await self._data_runtime.list_terminal_operation_catalog(
                scope,
                tool_names=tool_names,
                statuses=statuses,
                before_sequence=_optional_int(boundary, "before_sequence"),
                page_size=query.page_size,
            )
        cards: list[CodeCard] = []
        for published in page.operations:
            if query.code_kind is CodeKind.GEOMETRY_AUTHORING_ATTEMPT:
                cards.append(await self._authoring_operation_card(scope, published))
            else:
                cards.append(
                    await self._data_operation_card(scope, published, page.result_packages)
                )
        next_cursor = None
        if not page.complete:
            assert page.next_before_sequence is not None
            next_cursor = self._encode_cursor(
                scope, query, {"before_sequence": page.next_before_sequence}
            )
        return CodeListResult(
            code_kind=query.code_kind,
            codes=tuple(cards),
            returned_count=len(cards),
            next_cursor=next_cursor,
        )

    async def _product_family_authoring_operation_card(self, scope, operation) -> CodeCard:
        if (
            operation.code_artifact is None
            or operation.finished_at is None
        ):
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "The ProductFamily authoring Operation lost its exact source binding",
            )
        source = await self._read_product_family_operation_bundle(scope, operation)
        result_ref = None
        result_code_locator = None
        parent_alias = operation.parent_owner_alias
        repairable = False
        repair_unavailable_reason = "owner_scope_read_only"
        return CodeCard(
            code_locator=self._locator(
                scope,
                CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT,
                "product_family_authoring_operation",
                operation_alias=operation.catalog_entry.alias,
            ),
            code_kind=CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT,
            language="python",
            source_form="bundle",
            source_sha256=source.sha256,
            members=_members(source.members),
            owner_type="critical_operation",
            owner_alias=operation.catalog_entry.alias,
            status=operation.status.value,
            diagnostic_code=operation.error_code,
            diagnostic_message=operation.error_message,
            parent_owner_alias=parent_alias,
            ownership="agent",
            repairable=repairable,
            repair_entry="ifc_product_family_write" if repairable else None,
            repair_unavailable_reason=repair_unavailable_reason,
            result_alias=result_ref,
            result_code_locator=result_code_locator,
            created_at=operation.started_at,
            finished_at=operation.finished_at,
            sequence=operation.catalog_entry.ordinal,
        )

    def data_operation_locator(self, scope, published) -> str | None:
        """Sign the already-resolved current-Run transform source for code_read."""
        operation = published.operation
        if (operation.tool_name != "data_derive" or operation.run_id != scope.run_id
                or operation.code_artifact is None or operation.finished_at is None
                or operation.status not in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}):
            return None
        return self._locator(scope, CodeKind.DATA_TRANSFORM_ATTEMPT,
            "data_transform_operation", operation_alias=published.catalog_entry.alias)

    async def _data_operation_card(self, scope, published, result_packages) -> CodeCard:
        operation = published.operation
        artifact = operation.code_artifact
        if artifact is None or operation.finished_at is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR", "The Operation lost its exact code binding"
            )
        measurement = operation.tool_name == "cad_compute_measurements"
        language = "python" if measurement else _transform_language(operation)
        kind = (
            CodeKind.CAD_MEASUREMENT_IMPLEMENTATION
            if measurement
            else CodeKind.DATA_TRANSFORM_ATTEMPT
        )
        result_package = result_packages.get(operation.operation_id)
        result_locator = None
        if result_package is not None:
            result_locator = self._locator(
                scope,
                kind,
                "data_transform_package",
                package_alias=result_package.catalog_entry.alias,
            )
        parent_alias = await self._operation_parent_alias(scope, published)
        return CodeCard(
            code_locator=self._locator(
                scope,
                kind,
                "data_transform_operation",
                operation_alias=published.catalog_entry.alias,
            ),
            code_kind=kind,
            language=language,
            source_form="single_text",
            source_sha256=artifact.sha256,
            owner_type="critical_operation",
            owner_alias=published.catalog_entry.alias,
            status=operation.status.value,
            diagnostic_code=operation.error_code,
            diagnostic_message=operation.error_message,
            parent_owner_alias=parent_alias,
            ownership="server" if measurement else "agent",
            repairable=not measurement,
            repair_entry=None if measurement else "data_derive",
            result_alias=(result_package.catalog_entry.alias if result_package else None),
            result_code_locator=result_locator,
            created_at=operation.started_at,
            finished_at=operation.finished_at,
            sequence=published.catalog_entry.ordinal,
        )

    async def _authoring_operation_card(self, scope, published) -> CodeCard:
        operation = published.operation
        if operation.code_artifact is None or operation.finished_at is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR", "The authoring Operation lost its source binding"
            )
        source = await self._geometry_authoring.read_source(
            scope, operation_id=operation.operation_id, source_path=None, view=None
        )
        result_ref = None
        if (operation.result_geometry_resource_version_id
                and operation.target_geometry_resource_id):
            bundle = await self._geometry_resources.get_exact_version(
                scope.identity,
                resource_id=operation.target_geometry_resource_id,
                resource_version_id=operation.result_geometry_resource_version_id,
            )
            if bundle is not None:
                result_ref = self._geometry_catalog.version_ref(scope.identity, bundle)
        parent_alias = await self._operation_parent_alias(scope, published)
        return CodeCard(
            code_locator=self._locator(
                scope,
                CodeKind.GEOMETRY_AUTHORING_ATTEMPT,
                "geometry_authoring_operation",
                operation_alias=published.catalog_entry.alias,
            ),
            code_kind=CodeKind.GEOMETRY_AUTHORING_ATTEMPT,
            language="python",
            source_form="bundle",
            source_sha256=source.bundle_sha256,
            members=_members(source.members),
            owner_type="critical_operation",
            owner_alias=published.catalog_entry.alias,
            status=operation.status.value,
            diagnostic_code=operation.error_code,
            diagnostic_message=operation.error_message,
            parent_owner_alias=parent_alias,
            ownership="agent",
            repairable=False,
            repair_entry=None,
            repair_unavailable_reason="owner_scope_read_only",
            result_alias=result_ref,
            created_at=operation.started_at,
            finished_at=operation.finished_at,
            sequence=published.catalog_entry.ordinal,
        )

    async def _operation_parent_alias(self, scope, published) -> str | None:
        if published.operation.repair_parent_operation_id is None:
            return None
        inspection = await self._data_runtime.get_operation(scope, published.catalog_entry.alias)
        parent = inspection.repair_parent_operation
        if parent is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "A repair Operation lost its exact parent binding",
            )
        return parent.catalog_entry.alias

    async def published_program_card(self, scope: InvocationScope, code_kind: CodeKind,
                                     version_ref: str) -> CodeCard:
        """Navigate one exact published version through its existing source owner."""
        try:
            if code_kind is CodeKind.GEOMETRY_RESOURCE_PROGRAM:
                bundle = await self._geometry_catalog.resolve_exact_version(scope.identity, version_ref)
                return await self._geometry_program_card(scope, bundle)
            if code_kind is CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM:
                bundle = await self._require_product_family_catalog().resolve_program_source(scope.identity, version_ref)
                return await self._family_program_card(scope, bundle)
            raise CodeDiscoveryApplicationError("CODE_KIND_UNSUPPORTED", "An exact published program is required")
        except CodeDiscoveryApplicationError:
            raise
        except Exception as exc:
            raise self._owner_error(exc) from exc

    async def _geometry_program_card(self, scope, bundle) -> CodeCard:
        version_ref = self._geometry_catalog.version_ref(scope.identity, bundle)
        source = await self._geometry_sources.read_source(scope.identity, version_ref, None, None)
        if not any(member.text_available for member in source.members):
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR", "An executable geometry program has no readable text member")
        source_asset = next(asset for asset in bundle.assets
            if asset.asset_role is GeometryResourceAssetRole.SOURCE_BUNDLE)
        system_owned = bundle.resource.owner_scope.scope_kind.value == "system"
        return CodeCard(
            code_locator=self._locator(scope, CodeKind.GEOMETRY_RESOURCE_PROGRAM,
                "geometry_resource_version", resource_version_ref=version_ref),
            code_kind=CodeKind.GEOMETRY_RESOURCE_PROGRAM, language="python", source_form="bundle",
            source_sha256=source_asset.sha256, members=_members(source.members),
            owner_type="geometry_resource_version", owner_alias=version_ref, status="published",
            ownership="server" if system_owned else "agent", repairable=False, repair_entry=None,
            created_at=bundle.version.created_at, sequence=bundle.version.version_no)

    async def _family_program_card(self, scope, bundle) -> CodeCard:
        catalog = self._require_product_family_catalog()
        content = await self._require_product_family_assets().read_program_source(bundle)
        source = self._inspect_product_family_source(content, bundle.source_asset.sha256)
        version_ref = catalog.program_source_ref(scope.identity, bundle)
        owner_scope = bundle.family.owner_scope
        current_task_owned = (owner_scope.scope_kind is ResourceOwnerScopeKind.TASK
            and owner_scope.tenant_id == scope.identity.tenant_id
            and owner_scope.task_id == scope.identity.task_id)
        return CodeCard(
            code_locator=self._locator(scope, CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
                "product_family_version", family_version_ref=version_ref),
            code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM, language="python", source_form="bundle",
            source_sha256=source.sha256, members=_members(source.members),
            owner_type="product_family_version", owner_alias=version_ref, status="published",
            ownership="agent" if current_task_owned else "server", repairable=current_task_owned,
            repair_entry="ifc_product_family_write" if current_task_owned else None,
            repair_unavailable_reason=None if current_task_owned else "owner_scope_read_only",
            created_at=bundle.created_at, sequence=bundle.version_no)

    async def _list_geometry_programs(self, scope, query, boundary) -> CodeListResult:
        items = await self._geometry_resources.list_executable_program_versions(
            scope.identity,
            before_created_at=_optional_datetime(boundary, "before_created_at"),
            before_resource_version_id=_optional_str(boundary, "before_id"),
            limit=query.page_size + 1,
        )
        visible = items[: query.page_size]
        cards = [await self._geometry_program_card(scope, bundle) for bundle in visible]
        next_cursor = None
        if len(items) > query.page_size:
            last = visible[-1].version
            next_cursor = self._encode_cursor(
                scope,
                query,
                {
                    "before_created_at": last.created_at.isoformat(),
                    "before_id": last.resource_version_id,
                },
            )
        return CodeListResult(
            code_kind=query.code_kind,
            codes=tuple(cards),
            returned_count=len(cards),
            next_cursor=next_cursor,
        )

    async def _list_product_family_programs(self, scope, query, boundary) -> CodeListResult:
        catalog = self._require_product_family_catalog()
        assets = self._require_product_family_assets()
        page = await catalog.list_program_sources(
            scope.identity,
            before_created_at=_optional_datetime(boundary, "before_created_at"),
            before_family_version_id=_optional_str(boundary, "before_id"),
            limit=query.page_size,
        )
        cards = [await self._family_program_card(scope, bundle) for bundle in page.sources]
        next_cursor = None
        if not page.complete:
            assert page.next_before_created_at is not None
            assert page.next_before_family_version_id is not None
            next_cursor = self._encode_cursor(
                scope,
                query,
                {
                    "before_created_at": page.next_before_created_at.isoformat(),
                    "before_id": page.next_before_family_version_id,
                },
            )
        return CodeListResult(
            code_kind=query.code_kind,
            codes=tuple(cards),
            returned_count=len(cards),
            next_cursor=next_cursor,
            rejections=page.rejections,
        )

    async def _list_geometry_attempts(self, scope, query, boundary) -> CodeListResult:
        statuses = tuple(
            GeometryQueryAttemptStatus(status.value.upper())
            for status in (query.statuses or tuple(CodeTerminalStatus))
        )
        items = await self._geometry_queries.list_terminal_geometry_query_attempts(
            scope,
            statuses=statuses,
            before_created_at=_optional_datetime(boundary, "before_created_at"),
            before_attempt_id=_optional_str(boundary, "before_id"),
            limit=query.page_size + 1,
        )
        visible = items[: query.page_size]
        cards = tuple([await self._geometry_attempt_card(scope, item) for item in visible])
        next_cursor = self._attempt_cursor(scope, query, items, visible)
        return CodeListResult(
            code_kind=query.code_kind,
            codes=cards,
            returned_count=len(cards),
            next_cursor=next_cursor,
        )

    async def _list_data_query_attempts(self, scope, query, boundary) -> CodeListResult:
        owner = self._require_data_query_attempts()
        engine = _data_query_engine(query.code_kind)
        statuses = tuple(
            DataQueryAttemptStatus(status.value.upper())
            for status in (query.statuses or tuple(CodeTerminalStatus))
        )
        items = await owner.list_terminal_attempts(
            scope,
            engine=engine,
            statuses=statuses,
            before_created_at=_optional_datetime(boundary, "before_created_at"),
            before_attempt_id=_optional_str(boundary, "before_id"),
            limit=query.page_size + 1,
        )
        visible = items[: query.page_size]
        cards = tuple(
            [await self._data_query_attempt_card(scope, item, query.code_kind) for item in visible]
        )
        next_cursor = self._attempt_cursor(scope, query, items, visible)
        return CodeListResult(
            code_kind=query.code_kind,
            codes=cards,
            returned_count=len(cards),
            next_cursor=next_cursor,
        )

    async def _list_ifc_attempts(self, scope, query, boundary) -> CodeListResult:
        statuses = tuple(
            IfcQueryAttemptStatus(status.value.upper())
            for status in (query.statuses or tuple(CodeTerminalStatus))
        )
        items = await self._ifc_queries.list_terminal_ifc_query_attempts(
            scope,
            statuses=statuses,
            before_created_at=_optional_datetime(boundary, "before_created_at"),
            before_attempt_id=_optional_str(boundary, "before_id"),
            limit=query.page_size + 1,
        )
        visible = items[: query.page_size]
        cards = tuple([await self._ifc_attempt_card(scope, item) for item in visible])
        next_cursor = self._attempt_cursor(scope, query, items, visible)
        return CodeListResult(
            code_kind=query.code_kind,
            codes=cards,
            returned_count=len(cards),
            next_cursor=next_cursor,
        )

    async def _geometry_attempt_card(self, scope, attempt) -> CodeCard:
        alias = self._geometry_queries.attempt_alias(scope, attempt)
        diagnostic = attempt.diagnostics[0] if attempt.diagnostics else None
        parent_alias = await self._attempt_parent_alias(
            scope,
            self._geometry_queries,
            attempt.repair_parent_attempt_id,
            owner_name="geometry query",
        )
        return CodeCard(
            code_locator=self._locator(
                scope,
                CodeKind.GEOMETRY_QUERY_ATTEMPT,
                "geometry_query_attempt",
                attempt_alias=alias,
            ),
            code_kind=CodeKind.GEOMETRY_QUERY_ATTEMPT,
            language="python",
            source_form="single_text",
            source_sha256=attempt.source_artifact.sha256,
            owner_type="geometry_query_attempt",
            owner_alias=alias,
            status=attempt.status.value.lower(),
            diagnostic_code=diagnostic.code if diagnostic else None,
            diagnostic_message=diagnostic.message if diagnostic else None,
            parent_owner_alias=parent_alias,
            ownership="agent",
            repairable=True,
            repair_entry="ifc_geometry_query_python",
            created_at=attempt.created_at,
            finished_at=attempt.finished_at,
        )

    async def _data_query_attempt_card(self, scope, attempt, expected_kind) -> CodeCard:
        owner = self._require_data_query_attempts()
        actual_kind = _data_query_kind(attempt.engine)
        if actual_kind is not expected_kind:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "A data query Attempt no longer matches the requested engine",
            )
        if attempt.status is DataQueryAttemptStatus.PREPARED or attempt.finished_at is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "A listed data query Attempt is not terminal",
            )
        diagnostic = attempt.diagnostic
        parent_alias = await self._data_query_parent_alias(scope, attempt)
        alias = owner.attempt_alias(scope, attempt)
        return CodeCard(
            code_locator=self._locator(
                scope,
                actual_kind,
                "data_query_attempt",
                attempt_alias=alias,
            ),
            code_kind=actual_kind,
            language="sql" if attempt.engine is DataQueryEngine.DUCKDB_SQL else "python",
            source_form="single_text",
            source_sha256=attempt.source_artifact.sha256,
            owner_type="data_query_attempt",
            owner_alias=alias,
            status=attempt.status.value.lower(),
            diagnostic_code=diagnostic.code if diagnostic else None,
            diagnostic_message=diagnostic.message if diagnostic else None,
            parent_owner_alias=parent_alias,
            ownership="agent",
            repairable=True,
            repair_entry=(
                "data_query_sql"
                if attempt.engine is DataQueryEngine.DUCKDB_SQL
                else "data_query_python"
            ),
            result_available=await owner.result_available(scope, attempt),
            created_at=attempt.created_at,
            finished_at=attempt.finished_at,
            sequence=attempt.sequence,
        )

    async def _data_query_parent_alias(self, scope, attempt) -> str | None:
        parent_attempt_id = attempt.repair_parent_attempt_id
        if parent_attempt_id is None:
            return None
        owner = self._require_data_query_attempts()
        parent = await owner.get_attempt(scope, parent_attempt_id)
        if parent is None or parent.engine is not attempt.engine:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "A repair data query Attempt lost its exact parent binding",
            )
        return owner.attempt_alias(scope, parent)

    async def _ifc_attempt_card(self, scope, attempt) -> CodeCard:
        alias = self._ifc_queries.attempt_alias(scope, attempt)
        diagnostic = attempt.diagnostics[0] if attempt.diagnostics else None
        parent_alias = await self._attempt_parent_alias(
            scope,
            self._ifc_queries,
            attempt.repair_parent_attempt_id,
            owner_name="IFC query",
        )
        return CodeCard(
            code_locator=self._locator(
                scope,
                CodeKind.IFC_QUERY_ATTEMPT,
                "ifc_query_attempt",
                attempt_alias=alias,
            ),
            code_kind=CodeKind.IFC_QUERY_ATTEMPT,
            language="python",
            source_form="single_text",
            source_sha256=attempt.source_artifact.sha256,
            owner_type="ifc_query_attempt",
            owner_alias=alias,
            status=attempt.status.value.lower(),
            diagnostic_code=diagnostic.code if diagnostic else None,
            diagnostic_message=diagnostic.message if diagnostic else None,
            parent_owner_alias=parent_alias,
            ownership="agent",
            repairable=True,
            repair_entry="ifc_query_python",
            created_at=attempt.created_at,
            finished_at=attempt.finished_at,
        )

    @staticmethod
    async def _attempt_parent_alias(
        scope,
        owner,
        parent_attempt_id: str | None,
        *,
        owner_name: str,
    ) -> str | None:
        if parent_attempt_id is None:
            return None
        try:
            return await owner.attempt_alias_for_visible_id(scope, parent_attempt_id)
        except Exception as exc:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                f"A repair {owner_name} Attempt lost its exact parent binding",
            ) from exc

    async def _read_transform(self, scope, claims, view):
        from CAD2BIM_backend.domain.data_transform import (
            OperationCodeReference,
            PackageCodeReference,
        )

        if claims["owner_kind"] == "data_transform_package":
            reference = PackageCodeReference(
                reference_kind="package", package_alias=claims["package_alias"]
            )
        else:
            reference = OperationCodeReference(
                reference_kind="operation", operation_alias=claims["operation_alias"]
            )
        source = await self._data_runtime.get_transformation_code(scope, reference)
        projected = self._text_views.view(source.code, view)
        return {
            "code_kind": CodeKind.CAD_MEASUREMENT_IMPLEMENTATION
            if source.engine == "cad_measurement"
            else CodeKind.DATA_TRANSFORM_ATTEMPT,
            "language": source.language,
            "source_form": "single_text",
            "source_sha256": projected.sha256,
            "view": projected,
        }

    async def _read_authoring(self, scope, claims, source_path, view):
        if source_path is None:
            raise CodeDiscoveryApplicationError(
                "CODE_SOURCE_PATH_REQUIRED", "Bundle code reads require source_path"
            )
        inspection = await self._data_runtime.get_operation(scope, claims["operation_alias"])
        if (
            inspection.operation.operation.operation_kind
            is not CriticalOperationKind.GEOMETRY_RESOURCE_WRITE
        ):
            raise CodeDiscoveryApplicationError("CODE_LOCATOR_INVALID", "Code owner kind changed")
        source = await self._geometry_authoring.read_source(
            scope,
            operation_id=inspection.operation.operation.operation_id,
            source_path=source_path,
            view=view,
        )
        if source.view is None or source.selected_path is None:
            raise CodeDiscoveryApplicationError("CODE_OWNER_INTEGRITY_ERROR", "Source read failed")
        member = next(item for item in source.members if item.source_path == source.selected_path)
        return {
            "code_kind": CodeKind.GEOMETRY_AUTHORING_ATTEMPT,
            "language": "python",
            "source_form": "bundle",
            "source_sha256": source.bundle_sha256,
            "selected_path": source.selected_path,
            "member_sha256": member.sha256,
            "view": source.view,
        }

    async def _read_geometry_program(self, scope, claims, source_path, view):
        if source_path is None:
            raise CodeDiscoveryApplicationError(
                "CODE_SOURCE_PATH_REQUIRED", "Bundle code reads require source_path"
            )
        source = await self._geometry_sources.read_source(
            scope.identity, claims["resource_version_ref"], source_path, view
        )
        if source.view is None or source.selected_path is None:
            raise CodeDiscoveryApplicationError("CODE_OWNER_INTEGRITY_ERROR", "Source read failed")
        member = next(item for item in source.members if item.source_path == source.selected_path)
        return {
            "code_kind": CodeKind.GEOMETRY_RESOURCE_PROGRAM,
            "language": "python",
            "source_form": "bundle",
            "source_sha256": source.bundle_sha256,
            "selected_path": source.selected_path,
            "member_sha256": member.sha256,
            "view": source.view,
        }

    async def _read_geometry_attempt(self, scope, claims, source_path, view):
        if source_path is not None:
            raise CodeDiscoveryApplicationError(
                "CODE_SOURCE_PATH_INVALID", "Single text code does not accept source_path"
            )
        attempt_id = await self._geometry_queries.resolve_attempt_alias(
            scope, claims["attempt_alias"]
        )
        source = await self._geometry_queries.read_source(scope, attempt_id, view)
        return {
            "code_kind": CodeKind.GEOMETRY_QUERY_ATTEMPT,
            "language": "python",
            "source_form": "single_text",
            "source_sha256": source.sha256,
            "view": source,
        }

    async def _read_data_query_attempt(self, scope, claims, source_path, view):
        if source_path is not None:
            raise CodeDiscoveryApplicationError(
                "CODE_SOURCE_PATH_INVALID", "Single text code does not accept source_path"
            )
        owner = self._require_data_query_attempts()
        attempt = await owner.resolve_attempt_alias(scope, claims["attempt_alias"])
        actual_kind = _data_query_kind(attempt.engine)
        if actual_kind.value != claims["code_kind"]:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "The data query code locator no longer matches its exact engine",
            )
        source = await owner.read_source(scope, attempt.attempt_id, view)
        if source.sha256 != attempt.source_artifact.sha256:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "The data query source no longer matches its exact owner binding",
            )
        return {
            "code_kind": actual_kind,
            "language": "sql" if attempt.engine is DataQueryEngine.DUCKDB_SQL else "python",
            "source_form": "single_text",
            "source_sha256": source.sha256,
            "view": source,
        }

    def _require_data_query_attempts(self):
        if self._data_query_attempts is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_UNAVAILABLE",
                "Data query Attempt code discovery is unavailable",
            )
        return self._data_query_attempts

    async def _read_ifc_attempt(self, scope, claims, source_path, view):
        if source_path is not None:
            raise CodeDiscoveryApplicationError(
                "CODE_SOURCE_PATH_INVALID", "Single text code does not accept source_path"
            )
        attempt_id = await self._ifc_queries.resolve_attempt_alias(scope, claims["attempt_alias"])
        source = await self._ifc_queries.read_source(scope, attempt_id, view)
        return {
            "code_kind": CodeKind.IFC_QUERY_ATTEMPT,
            "language": "python",
            "source_form": "single_text",
            "source_sha256": source.sha256,
            "view": source,
        }

    async def _read_product_family_authoring(self, scope, claims, source_path, view):
        if source_path is None:
            raise CodeDiscoveryApplicationError(
                "CODE_SOURCE_PATH_REQUIRED", "Bundle code reads require source_path"
            )
        operation = await self._data_runtime.get_product_family_operation_source(
            scope, claims["operation_alias"],
        )
        source = await self._read_product_family_operation_bundle(scope, operation)
        return self._project_product_family_member(
            source,
            CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT,
            source_path,
            view,
        )

    async def _read_product_family_program(self, scope, claims, source_path, view):
        if source_path is None:
            raise CodeDiscoveryApplicationError(
                "CODE_SOURCE_PATH_REQUIRED", "Bundle code reads require source_path"
            )
        catalog = self._require_product_family_catalog()
        bundle = await catalog.resolve_program_source(
            scope.identity,
            claims["family_version_ref"],
        )
        source_asset = bundle.source_asset
        content = await self._require_product_family_assets().read_program_source(bundle)
        source = self._inspect_product_family_source(content, source_asset.sha256)
        return self._project_product_family_member(
            source,
            CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
            source_path,
            view,
        )

    async def _draft_source(self, scope, kind, draft_ref):
        owner = self._geometry_authoring if kind is CodeKind.GEOMETRY_DRAFT else self._product_family_authoring
        if owner is None:
            raise CodeDiscoveryApplicationError("CODE_OWNER_UNAVAILABLE", "Draft source owner is unavailable")
        draft, content = await owner.read_draft(scope, draft_ref=draft_ref)
        if not content.source_files:
            return draft, None
        packed = self._require_source_bundles().pack({item.path: item.source.encode("utf-8") for item in content.source_files})
        return draft, self._require_source_bundles().inspect(packed.content)

    async def _list_draft(self, scope, query):
        draft, source = await self._draft_source(scope, query.code_kind, query.draft_ref)
        cards = () if source is None else (CodeCard(
            code_locator=self._locator(scope, query.code_kind, "authoring_draft", draft_ref=query.draft_ref,
                snapshot_id=draft.snapshot.version_id), code_kind=query.code_kind,
            language="python", source_form="bundle", source_sha256=source.sha256, members=_members(source.members),
            owner_type="authoring_draft", owner_alias=query.draft_ref, status="draft", ownership="agent",
            repairable=True, repair_entry="ifc_geometry_resource_write" if query.code_kind is CodeKind.GEOMETRY_DRAFT else "ifc_product_family_write"),)
        return CodeListResult(code_kind=query.code_kind, codes=cards, returned_count=len(cards))

    async def _read_draft_code(self, scope, claims, source_path, view):
        if source_path is None:
            raise CodeDiscoveryApplicationError("CODE_SOURCE_PATH_REQUIRED", "Draft bundle reads require source_path")
        kind = CodeKind(claims["code_kind"])
        draft, source = await self._draft_source(scope, kind, claims["draft_ref"])
        if draft.snapshot.version_id != claims["snapshot_id"]:
            raise CodeDiscoveryApplicationError("CODE_DRAFT_CHANGED", "Draft changed; list its current source before reading again")
        if source is None:
            raise CodeDiscoveryApplicationError("CODE_SOURCE_UNAVAILABLE", "This fixed-asset draft has no source program")
        return self._project_product_family_member(source, kind, source_path, view)

    async def _read_product_family_operation_bundle(self, scope, operation):
        artifact = operation.code_artifact
        if artifact is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "The ProductFamily Operation lost its exact code Artifact",
            )
        if operation.detail_kind == "authoring_draft_write":
            if self._product_family_authoring is None:
                raise CodeDiscoveryApplicationError(
                    "CODE_OWNER_UNAVAILABLE", "Family draft source owner is unavailable"
                )
            content = await self._product_family_authoring.read_draft_snapshot(scope, artifact)
            packed = self._require_source_bundles().pack({item.path: item.source.encode("utf-8") for item in content.source_files})
            return self._require_source_bundles().inspect(packed.content)
        if operation.detail_kind == "product_family_write":
            exact = await self._require_exact_artifacts().read(
                scope, artifact, expected_content_type="application/zip",
            )
            return self._inspect_product_family_source(exact.content, artifact.sha256)
        raise CodeDiscoveryApplicationError(
            "CODE_OWNER_INTEGRITY_ERROR", "Family source Operation has an invalid kind"
        )

    def _inspect_product_family_source(self, content: bytes, expected_sha256: str):
        source = self._require_source_bundles().inspect(content)
        if source.sha256 != expected_sha256:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "The ProductFamily source bundle no longer matches its exact owner binding",
            )
        return source

    def _project_product_family_member(self, source, kind, source_path, view):
        text = source.read_text(source_path)
        member = next(
            (item for item in source.members if item.source_path == source_path),
            None,
        )
        if member is None or not member.text_available:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_INTEGRITY_ERROR",
                "The ProductFamily source member no longer matches its exact bundle",
            )
        return {
            "code_kind": kind,
            "language": "python",
            "source_form": "bundle",
            "source_sha256": source.sha256,
            "selected_path": member.source_path,
            "member_sha256": member.sha256,
            "view": self._text_views.view(text, view),
        }

    def _require_product_family_catalog(self) -> ProductFamilyCatalogService:
        if self._product_family_catalog is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_UNAVAILABLE",
                "ProductFamily code discovery is unavailable",
            )
        return self._product_family_catalog

    def _require_product_family_assets(self) -> ProductFamilyAssetService:
        if self._product_family_assets is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_UNAVAILABLE",
                "ProductFamily code discovery is unavailable",
            )
        return self._product_family_assets

    def _require_exact_artifacts(self) -> ExactArtifactReader:
        if self._exact_artifacts is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_UNAVAILABLE",
                "ProductFamily authoring source reads are unavailable",
            )
        return self._exact_artifacts

    def _require_source_bundles(self) -> GeometryAuthoringSourcePort:
        if self._source_bundles is None:
            raise CodeDiscoveryApplicationError(
                "CODE_OWNER_UNAVAILABLE",
                "ProductFamily source bundle reads are unavailable",
            )
        return self._source_bundles

    def _attempt_cursor(self, scope, query, items, visible):
        if len(items) <= query.page_size:
            return None
        last = visible[-1]
        return self._encode_cursor(
            scope,
            query,
            {"before_created_at": last.created_at.isoformat(), "before_id": last.attempt_id},
        )

    def _encode_cursor(self, scope, query, boundary) -> str:
        return self._encode(
            {
                "domain": _CURSOR_DOMAIN,
                "scope": _scope_digest(scope, query.code_kind),
                "query": query.model_dump(mode="json"),
                "boundary": boundary,
            }
        )

    def _decode_cursor(self, scope, token):
        claims = self._decode(token, "CODE_CURSOR_INVALID")
        try:
            if set(claims) != {"domain", "scope", "query", "boundary"}:
                raise ValueError
            query = CodeListQuery.model_validate(claims["query"])
            if (
                claims["domain"] != _CURSOR_DOMAIN
                or claims["scope"] != _scope_digest(scope, query.code_kind)
                or not isinstance(claims["boundary"], dict)
            ):
                raise ValueError
            return query, claims["boundary"]
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeDiscoveryApplicationError(
                "CODE_CURSOR_INVALID", "The code list cursor is invalid"
            ) from exc

    def _locator(self, scope, code_kind, owner_kind, **owner):
        return self._encode(
            {
                "domain": _LOCATOR_DOMAIN,
                "scope": _scope_digest(scope, code_kind),
                "code_kind": code_kind.value,
                "owner_kind": owner_kind,
                **owner,
            }
        )

    def _decode_locator(self, scope, token):
        claims = self._decode(token, "CODE_LOCATOR_INVALID")
        try:
            kind = CodeKind(claims["code_kind"])
            owner_kind = claims["owner_kind"]
            required = {"domain", "scope", "code_kind", "owner_kind"}
            owner_keys = {
                "authoring_draft": {"draft_ref", "snapshot_id"},
                "data_transform_operation": {"operation_alias"},
                "data_transform_package": {"package_alias"},
                "geometry_authoring_operation": {"operation_alias"},
                "geometry_resource_version": {"resource_version_ref"},
                "data_query_attempt": {"attempt_alias"},
                "geometry_query_attempt": {"attempt_alias"},
                "ifc_query_attempt": {"attempt_alias"},
                "product_family_authoring_operation": {"operation_alias"},
                "product_family_version": {"family_version_ref"},
            }[owner_kind]
            if (
                set(claims) != required | owner_keys
                or claims["domain"] != _LOCATOR_DOMAIN
                or claims["scope"] != _scope_digest(scope, kind)
                or owner_kind not in _LOCATOR_OWNER_KINDS[kind]
                or any(not isinstance(claims[key], str) or not claims[key] for key in owner_keys)
            ):
                raise ValueError
            return claims
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeDiscoveryApplicationError(
                "CODE_LOCATOR_INVALID", _CODE_LOCATOR_INVALID_MESSAGE
            ) from exc

    def _encode(self, claims):
        try:
            return self._cursor_codec.encode(claims)
        except OpaqueCursorCodecError as exc:
            raise CodeDiscoveryApplicationError(
                "CODE_TOKEN_ISSUE_FAILED", "The code navigation token could not be issued"
            ) from exc

    def _decode(self, token, code):
        try:
            return self._cursor_codec.decode(token)
        except OpaqueCursorCodecError as exc:
            message = (
                _CODE_LOCATOR_INVALID_MESSAGE
                if code == "CODE_LOCATOR_INVALID"
                else "The code navigation token is invalid"
            )
            raise CodeDiscoveryApplicationError(code, message) from exc

    @staticmethod
    def _owner_error(exc: Exception) -> CodeDiscoveryApplicationError:
        if isinstance(exc, TextSourceViewError):
            return CodeDiscoveryApplicationError(
                "CODE_SOURCE_READ_INVALID", _bounded_owner_message(str(exc))
            )
        if isinstance(exc, _KNOWN_OWNER_ERRORS):
            return CodeDiscoveryApplicationError(str(exc.code), _bounded_owner_message(str(exc)))
        return CodeDiscoveryApplicationError(
            "CODE_OWNER_READ_FAILED",
            "The code owner could not be read",
        )


def _operation_statuses(values):
    selected = values or tuple(CodeTerminalStatus)
    return tuple(CriticalOperationStatus(value.value) for value in selected)


def _bounded_owner_message(message: str) -> str:
    normalized = message.strip() or "The code owner request failed"
    encoded = normalized.encode("utf-8")
    if len(encoded) <= _MAX_OWNER_ERROR_BYTES:
        return normalized
    return encoded[:_MAX_OWNER_ERROR_BYTES].decode("utf-8", errors="ignore")


def _transform_language(operation) -> Literal["sql", "python"]:
    detail = operation.detail
    if not isinstance(detail, DataTransformOperationDetail):
        raise CodeDiscoveryApplicationError(
            "CODE_OWNER_INTEGRITY_ERROR", "Transform Operation detail is invalid"
        )
    return "sql" if detail.engine == "duckdb_sql" else "python"


def _data_query_engine(kind: CodeKind) -> DataQueryEngine:
    if kind is CodeKind.DATA_QUERY_SQL_ATTEMPT:
        return DataQueryEngine.DUCKDB_SQL
    if kind is CodeKind.DATA_QUERY_PYTHON_ATTEMPT:
        return DataQueryEngine.PYTHON_ARROW
    raise CodeDiscoveryApplicationError("CODE_KIND_UNSUPPORTED", "Code kind is unsupported")


def _data_query_kind(engine: DataQueryEngine) -> CodeKind:
    if engine is DataQueryEngine.DUCKDB_SQL:
        return CodeKind.DATA_QUERY_SQL_ATTEMPT
    if engine is DataQueryEngine.PYTHON_ARROW:
        return CodeKind.DATA_QUERY_PYTHON_ATTEMPT
    raise CodeDiscoveryApplicationError(
        "CODE_OWNER_INTEGRITY_ERROR", "A data query Attempt has an unsupported engine"
    )


def _members(items) -> tuple[CodeMember, ...]:
    return tuple(
        CodeMember(
            source_path=item.source_path,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            text_available=item.text_available,
        )
        for item in items
    )


def _scope_digest(scope: InvocationScope, kind: CodeKind) -> str:
    values: dict[str, object] = {
        "domain": "code_owner_scope",
        "tenant_id": scope.identity.tenant_id,
        "owner_id": scope.owner_id,
        "task_id": scope.identity.task_id,
    }
    if kind in _RUN_BOUND_CODE_KINDS:
        values["run_id"] = scope.run_id
    elif kind not in _TASK_BOUND_CODE_KINDS:
        raise CodeDiscoveryApplicationError("CODE_KIND_UNSUPPORTED", "Code kind is unsupported")
    return sha256_digest(values)


def _optional_int(boundary, key):
    value = boundary.get(key)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
        raise CodeDiscoveryApplicationError(
            "CODE_CURSOR_INVALID", "Code cursor boundary is invalid"
        )
    return value


def _optional_str(boundary, key):
    value = boundary.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise CodeDiscoveryApplicationError(
            "CODE_CURSOR_INVALID", "Code cursor boundary is invalid"
        )
    return value


def _optional_datetime(boundary, key):
    value = boundary.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CodeDiscoveryApplicationError(
            "CODE_CURSOR_INVALID", "Code cursor boundary is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CodeDiscoveryApplicationError(
            "CODE_CURSOR_INVALID", "Code cursor boundary is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CodeDiscoveryApplicationError(
            "CODE_CURSOR_INVALID", "Code cursor boundary is invalid"
        )
    return parsed


__all__ = [
    "CodeCard",
    "CodeDiscoveryApplicationError",
    "CodeDiscoveryService",
    "CodeKind",
    "CodeListQuery",
    "CodeListResult",
    "CodeMember",
    "CodeReadResult",
    "CodeTerminalStatus",
]
