"""Application core for Direct sessions and phase-one CAD L0 ingestion.

Provider adapters are responsible for projecting these narrow results into their
own transport contracts.  This module deliberately has no MCP dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import MappingProxyType
from typing import Any, AsyncIterable, AsyncIterator, Callable, ContextManager, Literal, Mapping
from uuid import uuid4

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.exact_artifact_reader import (
    ExactArtifactReadError,
    ExactArtifactReader,
)
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest, stable_id
from CAD2BIM_backend.domain.control_plane import ArtifactRecord
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef,
    CadMeasurementOperationDetail,
    CatalogEntryPage,
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    IfcProductsRecoveryOperation,
    IfcProductsEditOperationDetail,
    DataPackageKind,
    DataCatalogEntry,
    DataPackageMemberRecord,
    DataPackageRecord,
    DataTransformOperationDetail,
    OpenDirectSessionRequest,
    OpenedDirectSession,
    PackageMemberKind,
    PublishedCriticalOperation,
    PublishedOperationCatalogSummary,
    ProductFamilyOperationSource,
    PublishedDataPackage,
    PublishedImportResult,
    SOURCE_L0_SOURCE_MEMBER_NAME,
    SourceIngestionOperationDetail,
    ValidatedSource,
    normalize_direct_session_name,
)
from CAD2BIM_backend.domain.product_report import ProductReportDiagnostic, ProductReportDiagnostics
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject, TenantExecutionContext
from CAD2BIM_backend.domain.data_transform import (
    MAX_TRANSFORM_CODE_BYTES,
    OperationCodeReference,
    PackageCodeReference,
    TransformationCodeReference,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.ifc_model import ExactIfcRevision, IfcRunIdentity
from CAD2BIM_backend.infrastructure.cad_l0 import (
    CadL0ExtractionError,
    CadL0StagingResult,
    extract_ascii_dxf_l0,
)
from CAD2BIM_backend.infrastructure.cad_l0.extractor import DOCUMENT_KEY
from CAD2BIM_backend.infrastructure.cad_l0.schemas import PAYLOAD_SCHEMAS
from CAD2BIM_backend.application.package_synopsis import (
    PackageSynopsisCompiler,
    PackageSynopsisError,
)
from CAD2BIM_backend.ports.data_runtime import DataRuntimeRepository
from CAD2BIM_backend.ports.identity import IdentityAccessDenied, IdentityDirectory
from CAD2BIM_backend.ports.ifc_model import IfcModelRepository


_IMPORT_TOOL = "cad_import_source"
_DESCRIPTION_MEMBER = "description.md"
_MANIFEST_MEMBER = "manifest.json"
_MARKDOWN_MEDIA_TYPE = "text/markdown"
_PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
_MAX_DESCRIPTION_BYTES = 65_536
_MAX_ERROR_MESSAGE_BYTES = 2_000


def _bounded_error_message(
    message: str,
    *,
    fallback: str = "Data Runtime request failed",
) -> str:
    """Return one non-empty error without splitting a UTF-8 code point."""

    normalized = message.strip() or fallback
    encoded = normalized.encode("utf-8")
    if len(encoded) <= _MAX_ERROR_MESSAGE_BYTES:
        return normalized
    return encoded[:_MAX_ERROR_MESSAGE_BYTES].decode("utf-8", errors="ignore")


class DataRuntimeApplicationError(RuntimeError):
    """Bounded application failure suitable for projection by an entry adapter."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        operation_alias: str | None = None,
        phase: Literal["read"] | None = None,
        diagnostic: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(_bounded_error_message(message))
        self.code = code
        self.retryable = retryable
        self.operation_alias = operation_alias
        if phase is not None:
            self.phase = phase
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


@dataclass(frozen=True, slots=True)
class DataImportResult:
    published: PublishedImportResult
    source_artifact: ArtifactVersionRef
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DataCatalog:
    packages: tuple[PublishedDataPackage, ...]
    operations: tuple[PublishedCriticalOperation, ...]


@dataclass(frozen=True, slots=True)
class DataPackageCatalog:
    packages: tuple[PublishedDataPackage, ...]
    producing_operations: Mapping[str, PublishedCriticalOperation]
    complete: bool
    next_before_sequence: int | None


@dataclass(frozen=True, slots=True)
class OperationCatalog:
    operations: tuple[PublishedCriticalOperation, ...]
    result_packages: Mapping[str, PublishedDataPackage]
    complete: bool
    next_before_sequence: int | None


@dataclass(frozen=True, slots=True)
class OperationSummaryCatalog:
    operations: tuple[PublishedOperationCatalogSummary, ...]
    complete: bool
    next_before_sequence: int | None


@dataclass(frozen=True, slots=True)
class ProductFamilySourceCatalog:
    sources: tuple[ProductFamilyOperationSource, ...]
    complete: bool
    next_before_sequence: int | None


@dataclass(frozen=True, slots=True)
class OperationInspection:
    """Exact Operation plus aliases resolved in the current invocation scope."""

    operation: PublishedCriticalOperation | IfcProductsRecoveryOperation
    result_package: PublishedDataPackage | None
    input_packages: Mapping[str, PublishedDataPackage]
    repair_parent_operation: PublishedCriticalOperation | None = None
    committed_ifc_revision: ExactIfcRevision | None = None
    base_revision_no: int | None = None
    diagnostics: ProductReportDiagnostics | None = None


@dataclass(frozen=True, slots=True)
class TransformationCode:
    operation: PublishedCriticalOperation
    result_package: PublishedDataPackage | None
    engine: Literal["duckdb_sql", "python_arrow", "cad_measurement"]
    language: Literal["sql", "python"]
    media_type: Literal["application/sql", "text/x-python"]
    code: str


@dataclass(frozen=True, slots=True)
class PackageDescription:
    package: PublishedDataPackage
    markdown: str


ScopeBinder = Callable[[TenantExecutionContext], ContextManager[None]]


def _no_op_scope_binder(_context: TenantExecutionContext) -> ContextManager[None]:
    return nullcontext()


class DirectSessionService:
    """Create or resume Direct-only Task/Run bindings."""

    def __init__(
        self,
        repository: DataRuntimeRepository,
        identity_directory: IdentityDirectory,
        configured_subject: str,
        scope_binder: ScopeBinder = _no_op_scope_binder,
    ) -> None:
        self._repository = repository
        self._identity_directory = identity_directory
        self._configured_subject = ConfiguredLocalSubject(subject=configured_subject)
        self._scope_binder = scope_binder

    async def open_direct_session(self, session_name: str) -> OpenedDirectSession:
        try:
            normalized = normalize_direct_session_name(session_name)
            context = await self._resolve_identity("open-direct-session")
            request = OpenDirectSessionRequest(
                configured_subject=self._configured_subject.subject,
                session_name=session_name,
                normalized_session_name=normalized,
                owner_id=context.tenant_id,
                tenant_id=context.tenant_id,
                principal_id=context.principal_id,
            )
        except (ValueError, IdentityAccessDenied) as exc:
            raise DataRuntimeApplicationError("DIRECT_SESSION_REJECTED", str(exc)) from None
        with self._scope_binder(context):
            return await self._repository.open_direct_session(request)

    async def _resolve_identity(self, action: str) -> TenantExecutionContext:
        return await self._identity_directory.resolve_configured_local_subject(
            self._configured_subject,
            command_id=f"command_{action}_{uuid4().hex}",
        )


class DirectScopeResolver:
    """Resolve and verify a Direct alias against its authoritative Run."""

    def __init__(
        self,
        repository: DataRuntimeRepository,
        identity_directory: IdentityDirectory,
        configured_subject: str,
        scope_binder: ScopeBinder = _no_op_scope_binder,
    ) -> None:
        self._repository = repository
        self._identity_directory = identity_directory
        self._configured_subject = ConfiguredLocalSubject(subject=configured_subject)
        self._scope_binder = scope_binder

    async def resolve(self, direct_session_alias: str, *, command_id: str) -> InvocationScope:
        try:
            context = await self._identity_directory.resolve_configured_local_subject(
                self._configured_subject,
                command_id=command_id,
            )
        except IdentityAccessDenied as exc:
            raise DataRuntimeApplicationError("DIRECT_IDENTITY_DENIED", str(exc)) from None
        with self._scope_binder(context):
            opened = await self._repository.get_direct_session_by_alias(
                tenant_id=context.tenant_id,
                configured_subject=self._configured_subject.subject,
                session_alias=direct_session_alias,
            )
            if opened is None:
                raise DataRuntimeApplicationError(
                    "DIRECT_SESSION_NOT_FOUND",
                    "Direct session was not found for the configured subject",
                )
            binding = opened.binding
            run = await self._repository.get_run(binding.owner_id, binding.run_id)
        if (
            binding.tenant_id != context.tenant_id
            or binding.principal_id != context.principal_id
            or run is None
            or run.tenant_id != binding.tenant_id
            or run.task_id != binding.task_id
            or run.run_id != binding.run_id
            or run.owner_id != binding.owner_id
            or run.execution_mode is not ExecutionMode.DIRECT_MCP
        ):
            raise DataRuntimeApplicationError(
                "DIRECT_SCOPE_INVALID",
                "Direct session binding does not match its authoritative Run",
            )
        return InvocationScope(
            identity=context.model_copy(update={"task_id": binding.task_id}),
            owner_id=binding.owner_id,
            run_id=binding.run_id,
            execution_mode=ExecutionMode.DIRECT_MCP,
            catalog_frontier=None,
        )


def transform_metadata_page(detail, section, input_packages, *, start_index=0, item_count=100, byte_budget=12_000):
    """Page metadata from the original request; never copy its row or parameter values."""
    from CAD2BIM_backend.domain.data_transform import (
        TransformMetadataPage, TransformInputMetadata, TransformOutputMetadata,
        TransformParameterMetadata, TransformInlineTableMetadata,
    )
    values = getattr(detail, section)
    if start_index > len(values):
        raise DataRuntimeApplicationError("INVALID_ARGUMENT", "Metadata start_index exceeds total_count")
    items = []
    for value in values[start_index:start_index + item_count]:
        if section == "inputs":
            package = input_packages.get(value.input_name)
            if package is None or package.package.package_id != value.package_id:
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "The exact input Package is unavailable")
            member = next((m for m in package.members if m.member_id == value.member_id), None)
            if member is None:
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "The exact input member is unavailable")
            item = TransformInputMetadata(input_name=value.input_name, package_alias=package.catalog_entry.alias,
                payload_name=value.payload_name, logical_key_fields=member.logical_key_fields)
        elif section == "outputs":
            item = TransformOutputMetadata(payload_name=value.payload_name, format=value.format,
                description=value.description, logical_key_fields=value.logical_key_fields,
                json_kind=value.json_kind.value if value.json_kind is not None else None)
        elif section == "parameters":
            item = TransformParameterMetadata(name=value.name, parameter_kind=value.kind,
                value_count=len(value.value) if isinstance(value.value, (tuple, list)) else 1)
        else:
            item = TransformInlineTableMetadata(input_name=value.input_name,
                row_count=len(value.rows), column_count=len(value.columns))
        page = TransformMetadataPage(section=section, total_count=len(values), start_index=start_index,
            items=tuple([*items, item]), returned_count=len(items) + 1,
            complete=start_index + len(items) + 1 == len(values),
            next_index=start_index + len(items) + 1 if start_index + len(items) + 1 < len(values) else None)
        if len(page.model_dump_json().encode("utf-8")) > byte_budget:
            if not items:
                raise DataRuntimeApplicationError("OPERATION_GET_RESPONSE_TOO_LARGE", "One metadata item exceeds the read budget")
            break
        items.append(item)
    end = start_index + len(items)
    return TransformMetadataPage(section=section, total_count=len(values), start_index=start_index,
        items=tuple(items), returned_count=len(items), complete=end == len(values),
        next_index=end if end < len(values) else None)


class DataRuntimeService:
    """Entry-neutral shared core for exact ingestion and Catalog reads."""

    def __init__(
        self,
        repository: DataRuntimeRepository,
        artifacts: ArtifactService,
        *,
        ifc_repository: IfcModelRepository | None = None,
        queries=None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._exact_artifacts = ExactArtifactReader(artifacts)
        self._synopsis_compiler = PackageSynopsisCompiler()
        self._ifc_repository = ifc_repository
        self._queries = queries

    async def get_transform_input(
        self, scope: InvocationScope, operation_alias: str, *,
        input_kind: str, view=None, payload_name: str | None = None, **selection,
    ):
        """Read one exact terminal transform input, including a failed attempt."""
        from CAD2BIM_backend.domain.data_transform import DataTransformTextInputRead
        from CAD2BIM_backend.application.text_source_view import TextSourceViewError, TextSourceViewPolicy, TextSourceViewService

        published = await self._operation_by_alias(scope, operation_alias)
        record = published.operation
        if (record.operation_kind is not CriticalOperationKind.DATA_TRANSFORM
                or record.status not in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}
                or record.tool_name != "data_derive"
                or not isinstance(record.detail, DataTransformOperationDetail)):
            raise DataRuntimeApplicationError("TRANSFORM_INPUT_NOT_AVAILABLE", "The selected Operation has no saved Data Transform input")
        if input_kind not in {"package_description", "output_schema"}:
            if view is not None or payload_name is not None:
                raise DataRuntimeApplicationError("INVALID_ARGUMENT", "Select one complete transform_input branch")
            return await self._read_saved_transform_values(scope, published, input_kind, selection)
        if selection:
            raise DataRuntimeApplicationError("INVALID_ARGUMENT", "Text input reads accept only their own view fields")
        if input_kind == "package_description" and payload_name is None:
            if record.description_artifact is None:
                raise DataRuntimeApplicationError("TRANSFORM_INPUT_NOT_AVAILABLE", "The Operation has no exact Package description Artifact")
            try:
                downloaded = await self._exact_artifacts.read(scope, record.description_artifact, expected_content_type=_MARKDOWN_MEDIA_TYPE)
                source = downloaded.content.decode("utf-8")
            except (ExactArtifactReadError, UnicodeDecodeError) as exc:
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "The exact Package description Artifact is unavailable or invalid") from exc
        elif input_kind == "output_schema" and payload_name is not None:
            output = next((item for item in record.detail.outputs if item.payload_name == payload_name), None)
            if output is None or output.format != "json" or output.schema_json is None:
                raise DataRuntimeApplicationError("TRANSFORM_INPUT_NOT_AVAILABLE", "The selected payload_name has no saved JSON Schema")
            source = output.schema_json
        else:
            raise DataRuntimeApplicationError("INVALID_ARGUMENT", "Select package_description, or output_schema with payload_name")
        try:
            from pydantic import TypeAdapter
            from CAD2BIM_backend.domain.text_source import TextSourceView
            view = TypeAdapter(TextSourceView).validate_python(view)
            projected = TextSourceViewService(TextSourceViewPolicy(
                maximum_source_bytes=65_536, maximum_full_bytes=65_536, maximum_projected_bytes=65_536,
            )).view(source, view)
        except TextSourceViewError as exc:
            raise DataRuntimeApplicationError("TRANSFORM_INPUT_VIEW_INVALID", str(exc)) from exc
        result = DataTransformTextInputRead(operation_alias=operation_alias, input_kind=input_kind, payload_name=payload_name, source=projected)
        if len(result.model_dump_json().encode("utf-8")) > 64 * 1024:
            raise DataRuntimeApplicationError("OPERATION_GET_RESPONSE_TOO_LARGE", "Selected transform input exceeds the 64 KiB response budget; use a smaller lines or search view")
        return result

    async def _read_saved_transform_values(self, scope, published, input_kind, selection):
        from pydantic import TypeAdapter, ValidationError
        from CAD2BIM_backend.domain import data_transform as dto

        alias = published.catalog_entry.alias
        try:
            request = TypeAdapter(dto.TransformSavedInputSelection).validate_python(
                {"operation_alias": alias, "input_kind": input_kind, **selection})
        except ValidationError as exc:
            from CAD2BIM_backend.domain.validation_diagnostics import validation_error_message
            raise DataRuntimeApplicationError("INVALID_ARGUMENT", validation_error_message("Invalid saved-input selection", exc)) from None
        detail = published.operation.detail
        base = {"operation_alias": alias}
        if request.input_kind == "metadata":
            packages = {}
            if request.section == "inputs":
                for binding in detail.inputs[request.start_index:request.start_index + request.item_count]:
                    packages[binding.input_name] = await self._repository.get_package(scope, binding.package_id)
            page = transform_metadata_page(detail, request.section, packages,
                start_index=request.start_index, item_count=request.item_count, byte_budget=60_000)
            return dto.DataTransformMetadataRead(**base, metadata=page)
        if request.input_kind == "parameter":
            parameter = next((v for v in detail.parameters if v.name == request.parameter_name), None)
            if parameter is None:
                raise DataRuntimeApplicationError("TRANSFORM_INPUT_NOT_AVAILABLE", "The original parameter name was not found")
            return dto.DataTransformParameterRead(**base, parameter=parameter.model_dump(mode="json"))
        table = next((v for v in detail.inline_tables if v.input_name == request.input_name), None)
        if table is None:
            raise DataRuntimeApplicationError("TRANSFORM_INPUT_NOT_AVAILABLE", "The original inline table name was not found")
        base["input_name"] = table.input_name
        if request.input_kind == "inline_schema":
            if request.start_column > len(table.columns):
                raise DataRuntimeApplicationError("INVALID_ARGUMENT", "start_column exceeds total_columns")
            end = min(len(table.columns), request.start_column + request.column_count)
            return dto.DataTransformInlineSchemaRead(**base, total_columns=len(table.columns),
                start_column=request.start_column, columns=table.columns[request.start_column:end],
                complete=end == len(table.columns), next_column=end if end < len(table.columns) else None)
        if request.input_kind == "inline_text":
            column = next((n for n, v in enumerate(table.columns) if v.name == request.column_name), None)
            if column is None or request.row_index >= len(table.rows):
                raise DataRuntimeApplicationError("TRANSFORM_INPUT_NOT_AVAILABLE", "The exact inline cell was not found")
            cell = table.rows[request.row_index].values[column]
            if cell.kind != "string":
                raise DataRuntimeApplicationError("INVALID_ARGUMENT", "inline_text requires an original string cell")
            if request.start_character > len(cell.value):
                raise DataRuntimeApplicationError("INVALID_ARGUMENT", "start_character exceeds total_characters")
            end = min(len(cell.value), request.start_character + request.character_count)
            while True:
                result = dto.DataTransformInlineTextRead(**base, row_index=request.row_index,
                    column_name=request.column_name, total_characters=len(cell.value),
                    start_character=request.start_character, returned_characters=end-request.start_character,
                    text=cell.value[request.start_character:end], complete=end == len(cell.value),
                    next_character=end if end < len(cell.value) else None)
                if len(result.model_dump_json().encode("utf-8")) <= 64 * 1024:
                    return result
                end = request.start_character + (end-request.start_character)//2
        if request.start_row > len(table.rows):
            raise DataRuntimeApplicationError("INVALID_ARGUMENT", "start_row exceeds total_rows")
        names = request.select or tuple(c.name for c in table.columns)
        if len(names) != len(set(names)) or any(n not in {c.name for c in table.columns} for n in names):
            raise DataRuntimeApplicationError("INVALID_ARGUMENT", "select must name unique original columns")
        indexes = [next(n for n,c in enumerate(table.columns) if c.name == name) for name in names]
        columns = tuple(table.columns[n] for n in indexes)
        rows = []
        for row in table.rows[request.start_row:request.start_row + request.row_count]:
            candidate = dto.InlineRow(values=tuple(row.values[n] for n in indexes))
            end = request.start_row + len(rows) + 1
            result = dto.DataTransformInlineRowsRead(**base, total_rows=len(table.rows),
                start_row=request.start_row, returned_count=len(rows)+1, columns=columns,
                rows=tuple([*rows, candidate]), complete=end == len(table.rows),
                next_row=end if end < len(table.rows) else None)
            if len(result.model_dump_json().encode("utf-8")) > 64 * 1024:
                if not rows:
                    raise DataRuntimeApplicationError("TRANSFORM_INPUT_CELL_TOO_LARGE",
                        "One selected row exceeds 64 KiB; select fewer columns or use inline_text for a string cell at this physical row")
                break
            rows.append(candidate)
        end = request.start_row + len(rows)
        return dto.DataTransformInlineRowsRead(**base, total_rows=len(table.rows), start_row=request.start_row,
            returned_count=len(rows), columns=columns, rows=tuple(rows), complete=end == len(table.rows),
            next_row=end if end < len(table.rows) else None)


    async def import_cad_source(
        self,
        scope: InvocationScope,
        source: ValidatedSource,
    ) -> DataImportResult:
        source = _validated_source(source)
        source_content, existing_artifact = await self._source_content(scope, source)
        source_sha256 = hashlib.sha256(source_content).hexdigest()
        request_fingerprint = sha256_digest(
            {
                "source_identity": source.source_identity,
                "run_id": scope.run_id,
                "source_sha256": source_sha256,
            }
        )
        replay = await self._repository.get_operation_replay(
            scope,
            (_IMPORT_TOOL,),
            request_fingerprint,
        )
        if replay is not None:
            return await self._resolve_replay(scope, replay)

        started_at = _now()
        operation_id = stable_id(
            "operation", {"run_id": scope.run_id, "fingerprint": request_fingerprint}
        )
        idempotency_key = f"cad-import:{request_fingerprint}"
        try:
            async with self._stage_l0(source_content, source.filename) as staging:
                try:
                    description_markdown = staging.description_path.read_text(encoding="utf-8")
                    synopsis = self._synopsis_compiler.compile(description_markdown)
                except (OSError, UnicodeDecodeError, PackageSynopsisError) as exc:
                    raise DataRuntimeApplicationError(
                        "PACKAGE_DESCRIPTION_INVALID",
                        f"Generated source package description is invalid: {exc}",
                    ) from None
                raw_artifact = existing_artifact or await self._upload(
                    scope=scope,
                    filename=source.filename,
                    content_type=source.media_type,
                    content=source_content,
                    logical_name="source",
                    fingerprint=request_fingerprint,
                    kind="cad_source",
                    version_metadata={
                        **source.adapter_metadata,
                        "source_identity": source.source_identity,
                        "source_sha256": source_sha256,
                    },
                )
                artifacts = await self._upload_staged_members(
                    scope=scope,
                    staging=staging,
                    raw_artifact=raw_artifact,
                    fingerprint=request_fingerprint,
                    operation_id=operation_id,
                )
        except CadL0ExtractionError as exc:
            failed = CriticalOperationRecord(
                operation_id=operation_id,
                owner_id=scope.owner_id,
                tenant_id=scope.identity.tenant_id,
                task_id=scope.identity.task_id or "",
                run_id=scope.run_id,
                tool_name=_IMPORT_TOOL,
                source_filename=source.filename,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                status=CriticalOperationStatus.FAILED,
                error_code=exc.code,
                error_message=_bounded_error_message(str(exc)),
                error_phase=CriticalOperationErrorPhase.SOURCE_EXTRACTION,
                intent_summary=_source_ingestion_intent(source.filename),
                detail=SourceIngestionOperationDetail(
                    filename=source.filename,
                    media_type="application/dxf",
                    size_bytes=len(source_content),
                ),
                started_at=started_at,
                finished_at=_now(),
            )
            published_failure = await self._repository.record_failed_operation(scope, failed)
            raise _failed_operation_error(published_failure) from None

        package_id = stable_id(
            "data_package",
            {"run_id": scope.run_id, "fingerprint": request_fingerprint},
        )
        created_at = _now()
        package = DataPackageRecord(
            package_id=package_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            created_by_run_id=scope.run_id,
            name=_package_name(source.filename),
            package_kind=DataPackageKind.SOURCE_L0,
            source_filename=source.filename,
            description_title=synopsis.description_title,
            summary=synopsis.summary,
            section_titles=synopsis.section_titles,
            idempotency_key=f"cad-l0-package:{request_fingerprint}",
            request_fingerprint=request_fingerprint,
            created_at=created_at,
        )
        members = _package_members(
            scope=scope,
            package_id=package_id,
            artifacts=artifacts,
            row_counts=staging.payload_row_counts,
            created_at=created_at,
        )
        operation = CriticalOperationRecord(
            operation_id=operation_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            run_id=scope.run_id,
            tool_name=_IMPORT_TOOL,
            source_filename=source.filename,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            status=CriticalOperationStatus.SUCCEEDED,
            result_package_id=package_id,
            intent_summary=_source_ingestion_intent(source.filename),
            detail=SourceIngestionOperationDetail(
                filename=source.filename,
                media_type="application/dxf",
                size_bytes=len(source_content),
            ),
            started_at=started_at,
            finished_at=_now(),
        )
        published = await self._repository.publish_import_result(
            scope,
            package,
            members,
            operation,
        )
        return await self._import_result(published)

    async def get_catalog(self, scope: InvocationScope) -> DataCatalog:
        """Compatibility projection for callers not yet switched to split directories."""

        entries = await self._repository.list_catalog(scope, None)
        packages: list[PublishedDataPackage] = []
        operations: list[PublishedCriticalOperation] = []
        for entry in entries:
            if entry.resource_kind is CatalogResourceKind.PACKAGE:
                package = await self._repository.get_package(
                    scope,
                    entry.package_id or "",
                )
                if package is None:
                    raise DataRuntimeApplicationError(
                        "CATALOG_INTEGRITY_ERROR",
                        "Catalog package target is missing",
                    )
                packages.append(package)
            else:
                operation = await self._repository.get_operation(
                    scope,
                    entry.operation_id or "",
                )
                if operation is None:
                    raise DataRuntimeApplicationError(
                        "CATALOG_INTEGRITY_ERROR",
                        "Catalog operation target is missing",
                    )
                operations.append(operation)
        return DataCatalog(packages=tuple(packages), operations=tuple(operations))

    async def list_package_catalog(
        self,
        scope: InvocationScope,
        *,
        before_sequence: int | None = None,
        page_size: int = 20,
    ) -> DataPackageCatalog:
        page = await self._repository.list_package_catalog(
            scope,
            before_sequence=before_sequence,
            page_size=page_size,
        )
        packages = tuple(
            [await self._package_for_catalog_entry(scope, entry) for entry in page.entries]
        )
        producing_operations: dict[str, PublishedCriticalOperation] = {}
        for package in packages:
            producer = await self._repository.get_operation_for_result_package(scope, package.package.package_id)
            if producer is not None:
                producing_operations[package.package.package_id] = producer
            elif package.package.package_kind is DataPackageKind.DERIVED:
                raise DataRuntimeApplicationError("PACKAGE_INTEGRITY_ERROR", "Derived package has no visible producing operation")
        return DataPackageCatalog(
            packages=packages,
            producing_operations=MappingProxyType(producing_operations),
            complete=page.complete,
            next_before_sequence=page.next_before_sequence,
        )

    async def list_operation_catalog(
        self,
        scope: InvocationScope,
        *,
        before_sequence: int | None = None,
        page_size: int = 20,
        operation_kind: CriticalOperationKind | None = None,
        tool_name: str | None = None,
        outcome: CriticalOperationStatus | None = None,
    ) -> OperationSummaryCatalog:
        page = await self._repository.list_operation_catalog(
            scope,
            before_sequence=before_sequence,
            page_size=page_size,
            operation_kind=operation_kind,
            tool_name=tool_name, outcome=outcome,
        )
        operations = []
        for entry in page.entries:
            operation = await self._repository.get_operation_summary(scope, entry.operation_id)
            if operation is None or operation.catalog_entry != entry:
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "Catalog operation target is missing")
            operations.append(operation)
        return OperationSummaryCatalog(operations=tuple(operations), complete=page.complete,
            next_before_sequence=page.next_before_sequence)

    async def list_terminal_operation_catalog(
        self,
        scope: InvocationScope,
        *,
        tool_names: tuple[str, ...],
        statuses: tuple[CriticalOperationStatus, ...],
        require_code_artifact: bool = False,
        before_sequence: int | None = None,
        page_size: int = 20,
    ) -> OperationCatalog:
        if require_code_artifact:
            page = await self._repository.list_terminal_operation_catalog(
                scope,
                tool_names=tool_names,
                statuses=statuses,
                require_code_artifact=True,
                before_sequence=before_sequence,
                page_size=page_size,
            )
        else:
            page = await self._repository.list_terminal_operation_catalog(
                scope,
                tool_names=tool_names,
                statuses=statuses,
                before_sequence=before_sequence,
                page_size=page_size,
            )
        return await self._operation_catalog(scope, page)

    @staticmethod
    @asynccontextmanager
    async def _stage_l0(content: bytes, filename: str):
        temporary = tempfile.TemporaryDirectory(prefix="cad2bim-l0-")
        cancelled = threading.Event()
        transferred = False

        def check_cancelled() -> None:
            if cancelled.is_set():
                raise InterruptedError("DXF extraction was cancelled")

        task = asyncio.create_task(asyncio.to_thread(
            extract_ascii_dxf_l0, content, Path(temporary.name) / "staged",
            source_name=filename, check_cancelled=check_cancelled,
        ))

        def cleanup(completed: asyncio.Task) -> None:
            try:
                completed.result()
            except BaseException:
                pass
            finally:
                temporary.cleanup()

        try:
            try:
                staging = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled.set()
                transferred = True
                task.add_done_callback(cleanup)
                raise
            yield staging
        finally:
            if not transferred:
                temporary.cleanup()

    async def _operation_catalog(
        self,
        scope: InvocationScope,
        page: CatalogEntryPage,
    ) -> OperationCatalog:
        operations = tuple(
            [await self._operation_for_catalog_entry(scope, entry) for entry in page.entries]
        )
        result_packages: dict[str, PublishedDataPackage] = {}
        for operation in operations:
            package_id = operation.operation.result_package_id
            if package_id is None:
                continue
            package = await self._repository.get_package(scope, package_id)
            if package is None:
                raise DataRuntimeApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "Successful operation result package is not visible in this scope",
                )
            result_packages[operation.operation.operation_id] = package
        return OperationCatalog(
            operations=operations,
            result_packages=MappingProxyType(result_packages),
            complete=page.complete,
            next_before_sequence=page.next_before_sequence,
        )

    async def get_product_family_operation_source(
        self, scope: InvocationScope, operation_alias: str,
    ) -> ProductFamilyOperationSource:
        entry = await self._repository.get_catalog_entry_by_alias(scope, CatalogResourceKind.OPERATION, operation_alias)
        if entry is None or entry.operation_id is None:
            raise DataRuntimeApplicationError("OPERATION_NOT_FOUND", "Family source Operation was not found in this invocation scope")
        source = await self._repository.get_product_family_operation_source(scope, entry.operation_id)
        if source is None or source.catalog_entry != entry:
            raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "Family source ownership is missing or changed")
        return source

    async def list_product_family_operation_sources(
        self, scope: InvocationScope, *, statuses: tuple[CriticalOperationStatus, ...],
        before_sequence: int | None = None, page_size: int = 20,
    ) -> ProductFamilySourceCatalog:
        page = await self._repository.list_terminal_operation_catalog(
            scope, tool_names=("ifc_product_family_write",), statuses=statuses,
            require_code_artifact=True, before_sequence=before_sequence, page_size=page_size,
        )
        sources = []
        for entry in page.entries:
            source = await self._repository.get_product_family_operation_source(scope, entry.operation_id)
            if source is None or source.catalog_entry != entry:
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "Family source ownership is missing or changed")
            sources.append(source)
        return ProductFamilySourceCatalog(tuple(sources), page.complete, page.next_before_sequence)

    async def get_operation(
        self,
        scope: InvocationScope,
        operation_alias: str,
    ) -> OperationInspection:
        published = await self._operation_by_alias(scope, operation_alias)
        return await self._inspect_operation(scope, published)

    async def get_operation_by_locator(self, scope, *, origin_run_id, operation_id):
        published = await self._repository.get_operation_by_locator(
            scope, origin_run_id=origin_run_id, operation_id=operation_id)
        if published is None:
            return await self.get_ifc_products_recovery_operation(scope,
                origin_run_id=origin_run_id, operation_id=operation_id)
        original_scope = await self._operation_origin_scope(scope, origin_run_id)
        return await self._inspect_operation(original_scope, published)

    async def _operation_origin_scope(self, scope: InvocationScope, origin_run_id: str) -> InvocationScope:
        """Read an authorized historical Operation using its actual Run mode."""
        if origin_run_id == scope.run_id:
            return scope
        original = await self._repository.get_run(scope.owner_id, origin_run_id)
        if (original is None or original.run_id != origin_run_id
                or original.owner_id != scope.owner_id
                or original.tenant_id != scope.identity.tenant_id
                or original.task_id != scope.identity.task_id):
            raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR",
                "The original Operation Run is unavailable or differs from its authorized scope")
        return scope.model_copy(update={"run_id": original.run_id, "execution_mode": original.execution_mode})

    async def resolve_package(self, scope, package_alias):
        return await self._package_by_alias(scope, package_alias)

    async def list_ifc_products_recovery_operations(
        self, scope, *, tool_name, before_created_at=None, before_operation_id=None, limit=20,
    ):
        return await self._repository.list_ifc_products_recovery_operations(scope,
            tool_name=tool_name, before_created_at=before_created_at, before_operation_id=before_operation_id, limit=limit)

    async def get_ifc_products_recovery_operation(self, scope, *, origin_run_id, operation_id):
        recovered = await self._repository.get_ifc_products_recovery_operation(scope,
            origin_run_id=origin_run_id, operation_id=operation_id)
        if recovered is None:
            raise DataRuntimeApplicationError("OPERATION_NOT_FOUND", "The source operation is unavailable in this scope")
        if recovered.operation.status in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}:
            if recovered.catalog_entry is None:
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "Terminal Apply lost its Catalog entry")
            recovered = PublishedCriticalOperation(operation=recovered.operation,
                catalog_entry=recovered.catalog_entry)
        original_scope = await self._operation_origin_scope(scope, origin_run_id)
        return await self._inspect_operation(original_scope, recovered)

    async def get_authoring_publication_for_version(self, scope, *, root_kind, version_id):
        return await self._repository.get_authoring_publication_for_version(
            scope, root_kind=root_kind, version_id=version_id)

    async def get_authoring_save_for_snapshot(self, scope, *, root_kind, root_id, snapshot_artifact_version_id):
        return await self._repository.get_authoring_save_for_snapshot(scope, root_kind=root_kind,
            root_id=root_id, snapshot_artifact_version_id=snapshot_artifact_version_id)

    async def _inspect_operation(self, scope, published):
        try:
            return await self._inspect_operation_details(scope, published)
        except Exception as exc:
            from CAD2BIM_backend.application.data_query_service import DataQueryApplicationError

            # Report reads belong to this inspection boundary. Entry adapters
            # consume DataRuntimeApplicationError, not the nested query type.
            failure = (DataRuntimeApplicationError(exc.code, str(exc), phase="read",
                retryable=bool(getattr(exc, "retryable", False)), diagnostic=getattr(exc, "diagnostic", None))
                if isinstance(exc, DataQueryApplicationError) else exc)
            failure.operation_id = published.operation.operation_id
            failure.operation_status = published.operation.status.value
            failure.operation_alias = published.catalog_entry.alias if published.catalog_entry is not None else None
            if failure is exc:
                raise
            raise failure from exc

    async def _inspect_operation_details(self, scope, published):
        from CAD2BIM_backend.domain.data_runtime import (
            IfcProductsApplyOperationDetail,
            IfcTrustedBaselineLoadOperationDetail,
        )
        operation = published.operation
        repair_parent_operation: PublishedCriticalOperation | None = None
        if operation.repair_parent_operation_id is not None:
            repair_parent_operation = await self._repository.get_operation(
                scope,
                operation.repair_parent_operation_id,
            )
            if repair_parent_operation is None:
                raise DataRuntimeApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "Repair parent Operation is not visible in this scope",
                )
        result_package: PublishedDataPackage | None = None
        if operation.result_package_id is not None:
            result_package = await self._repository.get_package(
                scope,
                operation.result_package_id,
            )
            if result_package is None:
                raise DataRuntimeApplicationError(
                    "OPERATION_INTEGRITY_ERROR",
                    "Operation result package is not visible in this scope",
                )

        input_packages: dict[str, PublishedDataPackage] = {}
        committed_ifc_revision = None
        if isinstance(
            operation.detail,
            (DataTransformOperationDetail, CadMeasurementOperationDetail),
        ):
            for binding in operation.detail.inputs:
                package = await self._repository.get_package(scope, binding.package_id)
                if package is None or not any(
                    member.member_id == binding.member_id for member in package.members
                ):
                    raise DataRuntimeApplicationError(
                        "OPERATION_INTEGRITY_ERROR",
                        "Transformation input package is not visible or no longer matches its member",
                    )
                input_packages[binding.input_name] = package
        elif isinstance(operation.detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail)):
            binding = operation.detail.input_binding
            package = await self._repository.get_package(scope, binding.package_id)
            member = next((item for item in package.members if item.member_id == binding.member_id), None) if package else None
            if (member is None or member.logical_payload_name != binding.payload_name
                    or member.logical_key_fields != binding.logical_key_fields):
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR",
                    "Apply input package is not visible or no longer matches its exact member")
            input_packages[binding.payload_name] = package
        if (isinstance(operation.detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail, IfcTrustedBaselineLoadOperationDetail))
                and operation.status is CriticalOperationStatus.SUCCEEDED):
            if self._ifc_repository is None:
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR",
                    "The original committed IFC metadata reader is unavailable")
            original_scope = IfcRunIdentity(owner_id=scope.owner_id,
                tenant_id=scope.identity.tenant_id, task_id=scope.identity.task_id,
                run_id=operation.run_id)
            committed_ifc_revision = await self._ifc_repository.get_exact(
                original_scope, revision_id=operation.result_ifc_revision_id)
            if (committed_ifc_revision is None
                    or committed_ifc_revision.scope != original_scope
                    or committed_ifc_revision.revision_id != operation.result_ifc_revision_id):
                raise DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR",
                    "The original committed IFC revision is unavailable or mismatched")
        base_revision_no, diagnostics = None, None
        if isinstance(operation.detail, IfcProductsEditOperationDetail):
            original_scope = IfcRunIdentity(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
                task_id=scope.identity.task_id, run_id=operation.run_id)
            if self._ifc_repository is not None:
                base = await self._ifc_repository.get_exact(original_scope, revision_id=operation.base_ifc_revision_id)
                base_revision_no = base.revision_no if base is not None else None
        if isinstance(operation.detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail)):
            if result_package is not None and self._queries is not None:
                from CAD2BIM_backend.application.product_report import read_product_report_diagnostics
                diagnostics = await read_product_report_diagnostics(self._queries, scope, result_package,
                    edit=isinstance(operation.detail, IfcProductsEditOperationDetail))
            elif operation.error_code is not None:
                diagnostics = ProductReportDiagnostics(items=(ProductReportDiagnostic(phase=operation.error_phase.value,
                    code=operation.error_code, severity="error", message=operation.error_message,
                    recommended_action="Inspect the original Operation and correct the reported condition"),),
                    complete=result_package is None)
        return OperationInspection(
            operation=published,
            repair_parent_operation=repair_parent_operation,
            result_package=result_package,
            input_packages=MappingProxyType(input_packages),
            committed_ifc_revision=committed_ifc_revision,
            base_revision_no=base_revision_no, diagnostics=diagnostics,
        )

    async def get_transformation_code(
        self,
        scope: InvocationScope,
        reference: TransformationCodeReference,
    ) -> TransformationCode:
        if isinstance(reference, PackageCodeReference):
            package = await self._package_by_alias(scope, reference.package_alias)
            if package.package.package_kind is not DataPackageKind.DERIVED:
                raise DataRuntimeApplicationError(
                    "TRANSFORMATION_CODE_NOT_AVAILABLE",
                    "Only a derived package can have transformation code",
                )
            operation = await self._operation_for_result_package(
                scope,
                package.package.package_id,
            )
            result_package: PublishedDataPackage | None = package
        elif isinstance(reference, OperationCodeReference):
            operation = await self._operation_by_alias(scope, reference.operation_alias)
            result_package = None
            if operation.operation.result_package_id is not None:
                result_package = await self._repository.get_package(
                    scope,
                    operation.operation.result_package_id,
                )
                if result_package is None:
                    raise DataRuntimeApplicationError(
                        "OPERATION_INTEGRITY_ERROR",
                        "Transformation result package is not visible in this scope",
                    )
        else:
            raise DataRuntimeApplicationError(
                "TRANSFORMATION_CODE_REFERENCE_INVALID",
                "Transformation code reference is invalid",
            )

        record = operation.operation
        if (
            record.operation_kind is not CriticalOperationKind.DATA_TRANSFORM
            or record.tool_name not in {"data_derive", "cad_compute_measurements"}
        ):
            raise DataRuntimeApplicationError(
                "TRANSFORMATION_CODE_NOT_AVAILABLE",
                "The selected operation has no transformation code",
            )
        code_artifact = record.code_artifact
        if code_artifact is None:
            raise DataRuntimeApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "Transformation operation is missing its exact code Artifact",
            )
        if result_package is not None:
            code_members = [
                member
                for member in result_package.members
                if member.member_kind is PackageMemberKind.CODE
            ]
            if len(code_members) != 1 or code_members[0].artifact != code_artifact:
                raise DataRuntimeApplicationError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "Derived package and operation do not reference the same code Artifact",
                )
        detail = record.detail
        if not isinstance(
            detail,
            (DataTransformOperationDetail, CadMeasurementOperationDetail),
        ):
            raise DataRuntimeApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "Transformation operation is missing its exact engine detail",
            )
        if detail.engine == "duckdb_sql":
            language = "sql"
            media_type = "application/sql"
        elif detail.engine in {"python_arrow", "cad_measurement"}:
            language = "python"
            media_type = "text/x-python"
        else:  # pragma: no cover - closed Domain enum guards persisted values
            raise DataRuntimeApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "Transformation operation has an unsupported engine",
            )
        content = await self._download_exact_artifact(scope, code_artifact, media_type)
        maximum_source_bytes = (
            MAX_TRANSFORM_CODE_BYTES if isinstance(detail, DataTransformOperationDetail) else 65_536
        )
        if not content or len(content) > maximum_source_bytes:
            raise DataRuntimeApplicationError(
                "TRANSFORMATION_CODE_INVALID",
                f"Transformation code is empty or exceeds {maximum_source_bytes:,} UTF-8 bytes",
            )
        try:
            code = content.decode("utf-8")
        except UnicodeDecodeError:
            raise DataRuntimeApplicationError(
                "TRANSFORMATION_CODE_INVALID",
                "Transformation code is not valid UTF-8",
            ) from None
        return TransformationCode(
            operation=operation,
            result_package=result_package,
            engine=detail.engine,
            language=language,
            media_type=media_type,
            code=code,
        )

    async def get_package_description(
        self,
        scope: InvocationScope,
        package_alias: str,
    ) -> PackageDescription:
        package = await self._package_by_alias(scope, package_alias)
        member = next(
            (item for item in package.members if item.member_name == _DESCRIPTION_MEMBER),
            None,
        )
        if member is None or member.member_kind is not PackageMemberKind.DESCRIPTION:
            raise DataRuntimeApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "Package description member is missing",
            )
        content = await self._download_exact_member(member)
        if not content or len(content) > _MAX_DESCRIPTION_BYTES:
            raise DataRuntimeApplicationError(
                "PACKAGE_DESCRIPTION_INVALID",
                "Package description is empty or exceeds 65,536 UTF-8 bytes",
            )
        try:
            markdown = content.decode("utf-8")
        except UnicodeDecodeError:
            raise DataRuntimeApplicationError(
                "PACKAGE_DESCRIPTION_INVALID",
                "Package description is not valid UTF-8",
            ) from None
        return PackageDescription(package=package, markdown=markdown)

    async def _resolve_replay(
        self,
        scope: InvocationScope,
        replay: PublishedCriticalOperation,
    ) -> DataImportResult:
        if replay.operation.status is CriticalOperationStatus.FAILED:
            raise _failed_operation_error(replay)
        package_id = replay.operation.result_package_id
        package = await self._repository.get_package(
            scope,
            package_id or "",
        )
        if package is None:
            raise DataRuntimeApplicationError(
                "OPERATION_REPLAY_INVALID",
                "Successful import operation has no exact package",
                operation_alias=replay.catalog_entry.alias,
            )
        return await self._import_result(PublishedImportResult(package=package, operation=replay))

    async def _source_content(
        self,
        scope: InvocationScope,
        source: ValidatedSource,
    ) -> tuple[bytes, ArtifactRecord | None]:
        if source.content is not None:
            return source.content, None
        reference = source.artifact_version
        if reference is None:  # guarded by ValidatedSource
            raise DataRuntimeApplicationError("SOURCE_INPUT_INVALID", "Source is missing")
        artifact = await self._artifacts.get_version_metadata(
            scope.owner_id,
            reference.artifact_id,
            reference.version_id,
        )
        if (
            artifact.task_id != scope.identity.task_id
            or artifact.version_id != reference.version_id
            or artifact.sha256 != reference.sha256
            or artifact.content_type != source.media_type
        ):
            raise DataRuntimeApplicationError(
                "SOURCE_ARTIFACT_INVALID",
                "Source ArtifactVersion is outside the invocation scope or is not exact",
            )
        try:
            stream = await self._artifacts.stream_version(
                scope.owner_id,
                reference.artifact_id,
                reference.version_id,
            )
        except (ConflictError, NotFoundError) as exc:
            raise DataRuntimeApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "Exact transformation code Artifact is unavailable",
            ) from exc
        content = await _collect_exact_stream(
            stream.chunks,
            integrity_code="OPERATION_INTEGRITY_ERROR",
            failure_message="Exact transformation code Artifact stream failed",
        )
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise DataRuntimeApplicationError(
                "SOURCE_ARTIFACT_INVALID",
                "Source ArtifactVersion failed digest verification",
            )
        return content, artifact

    async def _upload_staged_members(
        self,
        *,
        scope: InvocationScope,
        staging: CadL0StagingResult,
        raw_artifact: ArtifactRecord,
        fingerprint: str,
        operation_id: str,
    ) -> dict[str, ArtifactRecord]:
        published: dict[str, ArtifactRecord] = {SOURCE_L0_SOURCE_MEMBER_NAME: raw_artifact}
        for payload_name in PAYLOAD_SCHEMAS:
            path = staging.payload_paths[payload_name][0]
            logical_name = f"payloads/{payload_name}/part-00000.parquet"
            published[logical_name] = await self._upload_path(
                scope=scope,
                filename=f"{payload_name}.parquet",
                content_type=_PARQUET_MEDIA_TYPE,
                path=path,
                logical_name=logical_name,
                fingerprint=fingerprint,
                kind="data_package_member",
                version_metadata={
                    "logical_name": logical_name,
                    "row_count": staging.payload_row_counts[payload_name],
                },
            )
        published[_DESCRIPTION_MEMBER] = await self._upload(
            scope=scope,
            filename=_DESCRIPTION_MEMBER,
            content_type=_MARKDOWN_MEDIA_TYPE,
            content=staging.description_path.read_bytes(),
            logical_name=_DESCRIPTION_MEMBER,
            fingerprint=fingerprint,
            kind="data_package_member",
            version_metadata={"logical_name": _DESCRIPTION_MEMBER},
        )
        manifest = _FinalCadL0ManifestBuilder(
            staging=staging,
            raw_artifact=raw_artifact,
            description_artifact=published[_DESCRIPTION_MEMBER],
            payload_artifacts=published,
            operation_id=operation_id,
        ).build()
        published[_MANIFEST_MEMBER] = await self._upload(
            scope=scope,
            filename=_MANIFEST_MEMBER,
            content_type="application/json",
            content=(canonical_json(manifest) + "\n").encode("utf-8"),
            logical_name=_MANIFEST_MEMBER,
            fingerprint=fingerprint,
            kind="data_package_manifest",
            version_metadata={
                "logical_name": _MANIFEST_MEMBER,
            },
        )
        return published

    async def _upload_path(
        self,
        *,
        scope: InvocationScope,
        filename: str,
        content_type: str,
        path: Path,
        logical_name: str,
        fingerprint: str,
        kind: str,
        version_metadata: Mapping[str, Any],
    ) -> ArtifactRecord:
        return await self._artifacts.upload(
            owner_id=scope.owner_id,
            task_id=scope.identity.task_id or "",
            filename=filename,
            content_type=content_type,
            chunks=_file_chunks(path),
            idempotency_key=f"data-runtime:{fingerprint}:{logical_name}",
            kind=kind,
            run_id=scope.run_id,
            version_metadata=version_metadata,
        )

    async def _upload(
        self,
        *,
        scope: InvocationScope,
        filename: str,
        content_type: str,
        content: bytes,
        logical_name: str,
        fingerprint: str,
        kind: str,
        version_metadata: Mapping[str, Any],
    ) -> ArtifactRecord:
        return await self._artifacts.upload_bytes(
            owner_id=scope.owner_id,
            task_id=scope.identity.task_id or "",
            filename=filename,
            content_type=content_type,
            content=content,
            idempotency_key=f"data-runtime:{fingerprint}:{logical_name}",
            kind=kind,
            run_id=scope.run_id,
            version_metadata=version_metadata,
        )

    async def _import_result(self, published: PublishedImportResult) -> DataImportResult:
        source_member = next(
            (
                member
                for member in published.package.members
                if member.member_name == SOURCE_L0_SOURCE_MEMBER_NAME
                and member.member_kind is PackageMemberKind.SOURCE
            ),
            None,
        )
        manifest_member = next(
            (
                member
                for member in published.package.members
                if member.member_name == _MANIFEST_MEMBER
                and member.member_kind is PackageMemberKind.MANIFEST
            ),
            None,
        )
        if source_member is None or manifest_member is None:
            raise DataRuntimeApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "Published package has no exact source or manifest member",
            )
        content = await self._download_exact_member(manifest_member)
        try:
            manifest = json.loads(content)
            source_facts = manifest["source"]
            source_content = await self._download_exact_member(source_member)
            if (
                "artifact_version_ref" in source_facts
                or source_facts["member_name"] != SOURCE_L0_SOURCE_MEMBER_NAME
                or source_facts["name"] != published.package.package.source_filename
                or source_facts["byte_size"] != len(source_content)
                or source_facts["sha256"] != hashlib.sha256(source_content).hexdigest()
            ):
                raise ValueError("manifest source facts do not match source member")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise DataRuntimeApplicationError(
                "PACKAGE_MANIFEST_INVALID",
                "Published package manifest is invalid",
            ) from None
        return DataImportResult(
            published=published,
            source_artifact=source_member.artifact,
            manifest=MappingProxyType(manifest),
        )

    async def _package_by_alias(
        self,
        scope: InvocationScope,
        package_alias: str,
    ) -> PublishedDataPackage:
        entry = await self._repository.get_catalog_entry_by_alias(
            scope,
            CatalogResourceKind.PACKAGE,
            package_alias,
        )
        if entry is None or entry.package_id is None:
            raise DataRuntimeApplicationError(
                "DATA_PACKAGE_NOT_FOUND",
                "Data package was not found in this invocation scope",
            )
        return await self._package_for_catalog_entry(scope, entry)

    async def _operation_by_alias(
        self,
        scope: InvocationScope,
        operation_alias: str,
    ) -> PublishedCriticalOperation:
        entry = await self._repository.get_catalog_entry_by_alias(
            scope,
            CatalogResourceKind.OPERATION,
            operation_alias,
        )
        if entry is None or entry.operation_id is None:
            raise DataRuntimeApplicationError(
                "OPERATION_NOT_FOUND",
                "Operation was not found in this invocation scope",
            )
        return await self._operation_for_catalog_entry(scope, entry)

    async def _package_for_catalog_entry(
        self,
        scope: InvocationScope,
        entry: DataCatalogEntry,
    ) -> PublishedDataPackage:
        if entry.resource_kind is not CatalogResourceKind.PACKAGE or entry.package_id is None:
            raise DataRuntimeApplicationError(
                "CATALOG_INTEGRITY_ERROR",
                "Catalog package entry has an invalid target",
            )
        package = await self._repository.get_package(scope, entry.package_id)
        if package is None or package.catalog_entry != entry:
            raise DataRuntimeApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "Catalog package target is missing",
            )
        return package

    async def _operation_for_catalog_entry(
        self,
        scope: InvocationScope,
        entry: DataCatalogEntry,
    ) -> PublishedCriticalOperation:
        if entry.resource_kind is not CatalogResourceKind.OPERATION or entry.operation_id is None:
            raise DataRuntimeApplicationError(
                "CATALOG_INTEGRITY_ERROR",
                "Catalog operation entry has an invalid target",
            )
        operation = await self._repository.get_operation(scope, entry.operation_id)
        if operation is None or operation.catalog_entry != entry:
            raise DataRuntimeApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "Catalog operation target is missing",
            )
        return operation

    async def _operation_for_result_package(
        self,
        scope: InvocationScope,
        package_id: str,
    ) -> PublishedCriticalOperation:
        operation = await self._repository.get_operation_for_result_package(scope, package_id)
        if operation is not None:
            return operation
        raise DataRuntimeApplicationError(
            "PACKAGE_INTEGRITY_ERROR",
            "Package has no visible producing operation",
        )

    async def _download_exact_artifact(
        self,
        scope: InvocationScope,
        reference: ArtifactVersionRef,
        expected_media_type: str,
    ) -> bytes:
        try:
            downloaded = await self._exact_artifacts.read(
                scope,
                reference,
                expected_content_type=expected_media_type,
            )
        except ExactArtifactReadError as exc:
            raise DataRuntimeApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "Exact transformation code Artifact is unavailable or inconsistent",
            ) from exc
        return downloaded.content

    async def _download_exact_member(self, member: DataPackageMemberRecord) -> bytes:
        try:
            stream = await self._artifacts.stream_version(
                member.owner_id,
                member.artifact.artifact_id,
                member.artifact.version_id,
            )
        except (ConflictError, NotFoundError) as exc:
            raise DataRuntimeApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "Package member exact Artifact is unavailable",
            ) from exc
        if (
            stream.artifact.version_id != member.artifact.version_id
            or stream.artifact.sha256 != member.artifact.sha256
            or stream.artifact.task_id != member.task_id
            or stream.artifact.content_type != member.media_type
        ):
            raise DataRuntimeApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "Package member metadata does not match its exact Artifact version",
            )
        content = await _collect_exact_stream(
            stream.chunks,
            integrity_code="PACKAGE_INTEGRITY_ERROR",
            failure_message="Package member exact Artifact stream failed",
        )
        if hashlib.sha256(content).hexdigest() != member.artifact.sha256:
            raise DataRuntimeApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "Package member payload failed digest verification",
            )
        return content


async def _collect_exact_stream(
    chunks: AsyncIterable[bytes],
    *,
    integrity_code: str,
    failure_message: str,
) -> bytes:
    """Collect an already-authorized exact stream without leaking storage errors."""

    content = bytearray()
    iterator = chunks.__aiter__()
    while True:
        try:
            chunk = await anext(iterator)
        except StopAsyncIteration:
            return bytes(content)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DataRuntimeApplicationError(integrity_code, failure_message) from exc
        if not isinstance(chunk, bytes):
            raise DataRuntimeApplicationError(
                integrity_code,
                f"{failure_message}: stream returned invalid bytes",
            )
        content.extend(chunk)


def _validated_source(source: ValidatedSource) -> ValidatedSource:
    if not isinstance(source, ValidatedSource):
        raise DataRuntimeApplicationError(
            "SOURCE_INPUT_INVALID",
            "Source must be a validated exact source",
        )
    if source.media_type != "application/dxf":
        raise DataRuntimeApplicationError("SOURCE_INPUT_INVALID", "Source media type is invalid")
    return source


def _artifact_ref(artifact: ArtifactRecord) -> ArtifactVersionRef:
    return ArtifactVersionRef(
        artifact_id=artifact.artifact_id,
        version_id=artifact.version_id,
        sha256=artifact.sha256,
    )


def _artifact_binding(artifact: ArtifactRecord) -> dict[str, str]:
    return _artifact_ref(artifact).model_dump(mode="json")


class _FinalCadL0ManifestBuilder:
    """Build and validate the exact publication manifest from staged facts."""

    def __init__(
        self,
        *,
        staging: CadL0StagingResult,
        raw_artifact: ArtifactRecord,
        description_artifact: ArtifactRecord,
        payload_artifacts: Mapping[str, ArtifactRecord],
        operation_id: str,
    ) -> None:
        self._staging = staging
        self._raw_artifact = raw_artifact
        self._description_artifact = description_artifact
        self._payload_artifacts = payload_artifacts
        self._operation_id = operation_id

    def build(self) -> dict[str, Any]:
        facts = self._staging.manifest_facts
        source_facts = facts["source"]
        completeness = facts["completeness"]
        typed_facts = facts["typed_geometry"]
        entity_type_counts = dict(typed_facts["entity_type_counts"])
        unsupported_type_counts = dict(typed_facts["unsupported_entity_type_counts"])
        entity_count = sum(entity_type_counts.values())
        raw_only_entity_count = sum(unsupported_type_counts.values())

        payloads: dict[str, Any] = {}
        for payload_name, schema in PAYLOAD_SCHEMAS.items():
            logical_name = f"payloads/{payload_name}/part-00000.parquet"
            artifact = self._payload_artifacts[logical_name]
            payloads[payload_name] = {
                "format": "parquet",
                "media_type": _PARQUET_MEDIA_TYPE,
                "arrow_schema": str(schema),
                "row_count": self._staging.payload_row_counts[payload_name],
                "part_count": 1,
                "logical_byte_count": artifact.size_bytes,
                "members": [
                    {
                        "logical_name": logical_name,
                        "media_type": _PARQUET_MEDIA_TYPE,
                        "size_bytes": artifact.size_bytes,
                        "artifact_version_ref": _artifact_binding(artifact),
                    }
                ],
            }

        manifest = {
            "publication_state": "published",
            "package_kind": "source_l0",
            "name": _package_name(str(source_facts["name"])),
            # The raw Artifact is the first immutable publication for this import.
            # Reusing its durable timestamp keeps interrupted retries byte-identical.
            "created_at": self._raw_artifact.created_at,
            "extraction": {
                "operation_id": self._operation_id,
            },
            "description_member": {
                "logical_name": _DESCRIPTION_MEMBER,
                "media_type": _MARKDOWN_MEDIA_TYPE,
                "size_bytes": self._description_artifact.size_bytes,
                "artifact_version_ref": _artifact_binding(self._description_artifact),
            },
            "source": {
                "member_name": SOURCE_L0_SOURCE_MEMBER_NAME,
                "document_key": DOCUMENT_KEY,
                "name": source_facts["name"],
                "format": source_facts["format"],
                "byte_size": source_facts["byte_size"],
                "sha256": source_facts["sha256"],
                "dxf_version": source_facts["dxf_version"],
                "declared_codepage": source_facts["declared_codepage"],
                "decoded_encoding": source_facts["decoded_encoding"],
            },
            "observed_sections": list(facts["observed_sections"]),
            "payloads": payloads,
            "coverage": {
                "record_count": self._staging.payload_row_counts["records"],
                "source_group_pair_count": self._staging.payload_row_counts["raw_tags"],
                "entity_count": entity_count,
                "typed_geometry_entity_count": entity_count - raw_only_entity_count,
                "raw_only_entity_count": raw_only_entity_count,
                "parse_issue_count": completeness["parse_issue_count"],
            },
            "typed_geometry": {
                "supported_entity_types": list(typed_facts["supported_entity_types"]),
                "entity_type_counts": entity_type_counts,
                "typed_entity_count": entity_count - raw_only_entity_count,
                "raw_only_entity_count": raw_only_entity_count,
            },
            "unsupported": {
                "entity_count": raw_only_entity_count,
                "entity_type_counts": unsupported_type_counts,
            },
            "external_dependencies": {
                "count": len(facts["external_dependencies"]),
                "items": list(facts["external_dependencies"]),
            },
            "completeness": dict(completeness),
            "integrity": {
                "status": "verified",
                "source_artifact_size_and_digest_match": (
                    self._raw_artifact.size_bytes == source_facts["byte_size"]
                    and self._raw_artifact.sha256 == source_facts["sha256"]
                ),
                "blocking_parse_issue_count": completeness["blocking_parse_issue_count"],
                "all_parsed_tags_have_record": completeness["all_parsed_tags_have_record"],
                "all_six_payloads_have_exact_members": len(payloads) == 6,
                "payload_row_counts_match_staging": all(
                    payloads[name]["row_count"] == self._staging.payload_row_counts[name]
                    for name in PAYLOAD_SCHEMAS
                ),
            },
        }
        self._validate(manifest)
        return manifest

    def _validate(self, manifest: Mapping[str, Any]) -> None:
        integrity = manifest["integrity"]
        if (
            manifest["publication_state"] != "published"
            or manifest["package_kind"] != "source_l0"
            or manifest["name"] != _package_name(self._raw_artifact.filename)
            or manifest["created_at"] != self._raw_artifact.created_at
        ):
            self._invalid("final manifest package identity fields changed")
        if set(manifest["payloads"]) != set(PAYLOAD_SCHEMAS):
            self._invalid("final manifest must bind exactly six L0 payloads")
        for payload_name, schema in PAYLOAD_SCHEMAS.items():
            payload = manifest["payloads"][payload_name]
            staged = self._staging.manifest_facts["payloads"][payload_name]
            members = payload["members"]
            if len(members) != 1:
                self._invalid(f"final manifest payload {payload_name} is inconsistent")
            member = members[0]
            logical_name = f"payloads/{payload_name}/part-00000.parquet"
            artifact = self._payload_artifacts[logical_name]
            if (
                payload["media_type"] != _PARQUET_MEDIA_TYPE
                or payload["arrow_schema"] != str(schema)
                or staged["arrow_schema"] != str(schema)
                or payload["row_count"] != staged["row_count"]
                or payload["part_count"] != 1
                or member["logical_name"] != logical_name
                or member["media_type"] != _PARQUET_MEDIA_TYPE
                or member["size_bytes"] != artifact.size_bytes
                or ArtifactVersionRef.model_validate(member["artifact_version_ref"])
                != _artifact_ref(artifact)
            ):
                self._invalid(f"final manifest payload {payload_name} is inconsistent")
        source = manifest["source"]
        description = manifest["description_member"]
        coverage = manifest["coverage"]
        external = manifest["external_dependencies"]
        if (
            "artifact_version_ref" in source
            or source["member_name"] != SOURCE_L0_SOURCE_MEMBER_NAME
            or source["byte_size"] != self._raw_artifact.size_bytes
            or source["sha256"] != self._raw_artifact.sha256
        ):
            self._invalid("final manifest source binding is inconsistent")
        if (
            description["media_type"] != _MARKDOWN_MEDIA_TYPE
            or description["size_bytes"] != self._description_artifact.size_bytes
            or ArtifactVersionRef.model_validate(description["artifact_version_ref"])
            != _artifact_ref(self._description_artifact)
        ):
            self._invalid("final manifest description media type changed")
        if (
            integrity["status"] != "verified"
            or not integrity["source_artifact_size_and_digest_match"]
            or integrity["blocking_parse_issue_count"] != 0
            or not integrity["all_parsed_tags_have_record"]
            or not integrity["all_six_payloads_have_exact_members"]
            or not integrity["payload_row_counts_match_staging"]
            or manifest["completeness"]["status"] != "complete"
            or coverage["typed_geometry_entity_count"] + coverage["raw_only_entity_count"]
            != coverage["entity_count"]
            or external["count"] != len(external["items"])
        ):
            self._invalid("final manifest integrity checks did not pass")

    @staticmethod
    def _invalid(message: str) -> None:
        raise DataRuntimeApplicationError("FINAL_MANIFEST_INVALID", message)


def _package_members(
    *,
    scope: InvocationScope,
    package_id: str,
    artifacts: Mapping[str, ArtifactRecord],
    row_counts: Mapping[str, int],
    created_at: datetime,
) -> tuple[DataPackageMemberRecord, ...]:
    definitions: list[tuple[str, PackageMemberKind, str, int | None]] = [
        (
            SOURCE_L0_SOURCE_MEMBER_NAME,
            PackageMemberKind.SOURCE,
            "application/dxf",
            None,
        ),
        (_MANIFEST_MEMBER, PackageMemberKind.MANIFEST, "application/json", None),
        (
            _DESCRIPTION_MEMBER,
            PackageMemberKind.DESCRIPTION,
            _MARKDOWN_MEDIA_TYPE,
            None,
        ),
    ]
    definitions.extend(
        (
            f"payloads/{name}/part-00000.parquet",
            PackageMemberKind.PAYLOAD,
            _PARQUET_MEDIA_TYPE,
            row_counts[name],
        )
        for name in PAYLOAD_SCHEMAS
    )
    return tuple(
        DataPackageMemberRecord(
            member_id=stable_id(
                "package_member",
                {"package_id": package_id, "member_name": member_name},
            ),
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            package_id=package_id,
            member_name=member_name,
            member_kind=member_kind,
            artifact=_artifact_ref(artifacts[member_name]),
            media_type=media_type,
            row_count=row_count,
            created_at=created_at,
        )
        for member_name, member_kind, media_type, row_count in definitions
    )


def _failed_operation_error(
    published: PublishedCriticalOperation,
) -> DataRuntimeApplicationError:
    operation = published.operation
    return DataRuntimeApplicationError(
        operation.error_code or "CAD_L0_EXTRACTION_FAILED",
        operation.error_message or "CAD L0 extraction failed",
        operation_alias=published.catalog_entry.alias,
    )


def _package_name(filename: str) -> str:
    suffix = " — CAD L0"
    return f"{filename[: 255 - len(suffix)]}{suffix}"


def _source_ingestion_intent(filename: str) -> str:
    return f"Import {filename} as an immutable CAD L0 source package."


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _file_chunks(path: Path, *, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """Stream a staged immutable member without loading the whole Parquet file."""

    stream = path.open("rb")
    try:
        while chunk := await asyncio.to_thread(stream.read, chunk_size):
            yield chunk
    finally:
        stream.close()


__all__ = [
    "DataCatalog",
    "DataImportResult",
    "DataPackageCatalog",
    "DataRuntimeApplicationError",
    "DataRuntimeService",
    "DirectScopeResolver",
    "DirectSessionService",
    "OperationCatalog",
    "OperationCodeReference",
    "OperationInspection",
    "PackageCodeReference",
    "PackageDescription",
    "TransformationCode",
    "TransformationCodeReference",
]
