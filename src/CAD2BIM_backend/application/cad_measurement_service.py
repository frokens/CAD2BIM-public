"""M3 application orchestration for bounded CAD analysis and atomic measurement publication."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Literal, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactParquetPayload,
    ExactPayloadResolutionError,
    ExactPayloadResolver,
)
from CAD2BIM_backend.application.package_synopsis import (
    PackageSynopsis,
    PackageSynopsisCompiler,
    PackageSynopsisError,
)
from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_COMPUTE_BUDGET,
    CAD_MEASUREMENT_PAYLOAD_NAMES,
    CAD_TOLERANCE_ANALYZER_BUDGET,
    CadAnalyzeToleranceCandidatesCommand,
    CadComputeMeasurementsCommand,
    CadMeasurementErrorCode,
    CadToleranceAnalysisResult,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest, stable_id
from CAD2BIM_backend.domain.control_plane import ArtifactRecord
from CAD2BIM_backend.domain.data_runtime import (
    ArrowColumnRecord,
    ArtifactVersionRef,
    CadMeasurementOperationDetail,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataPackageKind,
    DataPackageMemberRecord,
    DataPackageRecord,
    PackageMemberKind,
    PublishedCriticalOperation,
    PublishedDataPackage,
    PublishedPackageResult,
    TransformInputBindingRecord,
    TransformOutputRecord,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.cad_measurement.schemas import (
    MEASUREMENT_LOGICAL_KEYS,
    MEASUREMENT_PAYLOAD_SCHEMAS,
)
from CAD2BIM_backend.infrastructure.cad_measurement.supervised_worker import (
    CadMeasurementWorkerComputeResult,
    CadMeasurementWorkerError,
    SubprocessCadMeasurementWorker,
)
from CAD2BIM_backend.ports.data_runtime import DataRuntimeRepository


_TOOL_NAME = "cad_compute_measurements"
_CODE_MEMBER = "code/transformation.py"
_DESCRIPTION_MEMBER = "description.md"
_MANIFEST_MEMBER = "manifest.json"
_PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
_PYTHON_MEDIA_TYPE = "text/x-python"
_MARKDOWN_MEDIA_TYPE = "text/markdown"
_JSON_MEDIA_TYPE = "application/json"
_INPUT_PAYLOAD_NAMES = ("entities", "geometry_elements", "relationships")
_OUTPUT_DESCRIPTIONS = {
    "atomic_edges": "Deterministic LINE and straight-polyline atomic edge facts.",
    "endpoints": "Exact atomic-edge endpoint, tangent and quantization facts.",
    "endpoint_pairs": "Complete endpoint proximity candidates within the explicit threshold.",
    "endpoint_on_path": "Complete endpoint-to-other-path projection candidates.",
    "planar_intersections": "XY intersection facts with retained Z measurements.",
    "point_to_path": "Complete POINT-scope to path candidates and ambiguity measurements.",
    "curve_turns": "Adjacent source-edge three-dimensional and XY turn measurements.",
    "entity_bounds": "Three-dimensional bounds for every selected source entity.",
    "local_frames": "Deterministic local frames for measured geometric anchors.",
    "tolerance_diagnostics": "Coverage, threshold, blocker and completeness diagnostics.",
}
_RETRYABLE_CODES = {
    CadMeasurementErrorCode.CAPACITY_BUSY.value,
    CadMeasurementErrorCode.SERVICE_UNAVAILABLE.value,
    CadMeasurementErrorCode.PUBLICATION_UNCERTAIN.value,
}
MeasurementPhase = Literal["preflight", "execution", "output_validation", "publication"]


class CadMeasurementApplicationError(RuntimeError):
    """One bounded M3 failure suitable for a provider adapter."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: MeasurementPhase,
        retryable: bool | None = None,
        operation_alias: str | None = None,
    ) -> None:
        super().__init__(_bounded_text(message, 2_000))
        self.code = code
        self.phase = phase
        self.retryable = code in _RETRYABLE_CODES if retryable is None else retryable
        self.operation_alias = operation_alias


@dataclass(frozen=True, slots=True)
class CadMeasurementApplicationResult:
    published: PublishedPackageResult
    output_members: tuple[DataPackageMemberRecord, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    package: PublishedDataPackage
    payloads: tuple[ExactParquetPayload, ...]


@dataclass(frozen=True, slots=True)
class _SerializedOutput:
    name: str
    table: pa.Table
    path: Path
    parquet_bytes: int


class CadMeasurementService:
    """Resolve one L0 source, share the pure kernel, and publish only compute results."""

    def __init__(
        self,
        repository: DataRuntimeRepository,
        artifacts: ArtifactService,
        payloads: ExactPayloadResolver,
        synopsis_compiler: PackageSynopsisCompiler | None = None,
        worker: SubprocessCadMeasurementWorker | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._payloads = payloads
        self._synopsis = synopsis_compiler or PackageSynopsisCompiler()
        self._worker = worker or SubprocessCadMeasurementWorker()

    async def close(self) -> None:
        await self._worker.close()

    async def analyze(
        self,
        scope: InvocationScope,
        command: CadAnalyzeToleranceCandidatesCommand,
        *,
        deadline: float | None = None,
    ) -> CadToleranceAnalysisResult:
        if not isinstance(command, CadAnalyzeToleranceCandidatesCommand):
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.INVALID_ARGUMENT.value,
                "The tolerance analysis command is invalid",
                phase="preflight",
            )
        deadline = deadline or monotonic() + CAD_TOLERANCE_ANALYZER_BUDGET.total_timeout_seconds
        command = _canonical_analyzer_command(command)
        resolved = await self._resolve_source(scope, command.package_alias)
        paths: dict[str, Path] = {}
        try:
            paths, tables = await self._materialize_tables(scope, resolved, deadline)
            analysis = await self._worker.analyze(tables, command, deadline=deadline)
            result = _analysis_result(
                analysis.payload,
                command=command,
                tables=tables,
            )
            if (
                len(result.model_dump_json().encode("utf-8"))
                > CAD_TOLERANCE_ANALYZER_BUDGET.response_bytes
            ):
                raise CadMeasurementApplicationError(
                    CadMeasurementErrorCode.OUTPUT_BUDGET_EXCEEDED.value,
                    "The bounded tolerance analysis response exceeds 256 KiB",
                    phase="output_validation",
                )
            return result
        except CadMeasurementApplicationError:
            raise
        except CadMeasurementWorkerError as exc:
            raise _worker_error(exc) from None
        finally:
            await _remove_paths(paths)

    async def compute(
        self,
        scope: InvocationScope,
        command: CadComputeMeasurementsCommand,
        *,
        deadline: float | None = None,
    ) -> CadMeasurementApplicationResult:
        if not isinstance(command, CadComputeMeasurementsCommand):
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.INVALID_ARGUMENT.value,
                "The CAD measurement command is invalid",
                phase="preflight",
            )
        deadline = deadline or monotonic() + CAD_MEASUREMENT_COMPUTE_BUDGET.total_timeout_seconds
        command = _canonical_compute_command(command)
        synopsis = self._compile_synopsis(command.output_package.description_markdown)
        resolved = await self._resolve_source(scope, command.package_alias)
        fingerprint = _request_fingerprint(scope, command, resolved)
        replay = await self._repository.get_operation_replay(scope, (_TOOL_NAME,), fingerprint)
        if replay is not None:
            return await self._resolve_replay(scope, replay)

        implementation = _implementation_bundle_bytes()
        operation_id = stable_id(
            "operation",
            {"run_id": scope.run_id, "fingerprint": fingerprint},
        )
        detail = _operation_detail(command, resolved)
        paths: dict[str, Path] = {}
        code_artifact: ArtifactRecord | None = None
        started_at: datetime | None = None
        worker_result: CadMeasurementWorkerComputeResult | None = None
        try:
            paths, tables = await self._materialize_tables(scope, resolved, deadline)
            code_artifact = await self._upload_bytes(
                scope,
                filename="cad_measurement.py",
                media_type=_PYTHON_MEDIA_TYPE,
                content=implementation,
                logical_name=_CODE_MEMBER,
                fingerprint=fingerprint,
                kind="cad_measurement_code",
            )
            try:
                worker_result = await self._worker.compute(tables, command, deadline=deadline)
                started_at = worker_result.started_at
            except CadMeasurementWorkerError as exc:
                started_at = exc.started_at if exc.execution_started else None
                raise _worker_error(exc) from None
            serialized = _worker_outputs(worker_result)
            return await self._publish_success(
                scope=scope,
                command=command,
                synopsis=synopsis,
                resolved=resolved,
                detail=detail,
                fingerprint=fingerprint,
                operation_id=operation_id,
                code_artifact=code_artifact,
                started_at=started_at,
                serialized=serialized,
            )
        except asyncio.CancelledError as cancelled:
            worker_failure = getattr(cancelled, "worker_failure", None)
            if isinstance(worker_failure, CadMeasurementWorkerError):
                started_at = worker_failure.started_at if worker_failure.execution_started else None
            if bool(getattr(cancelled, "cad_measurement_commit_attempted", False)):
                try:
                    await asyncio.shield(self._reconcile_publication(scope, fingerprint, deadline))
                except CadMeasurementApplicationError:
                    pass
                raise
            if started_at is not None and code_artifact is not None:
                try:
                    await asyncio.shield(
                        self._record_failure(
                            scope=scope,
                            operation_id=operation_id,
                            fingerprint=fingerprint,
                            synopsis=synopsis,
                            code_artifact=code_artifact,
                            detail=detail,
                            started_at=started_at,
                            code=CadMeasurementErrorCode.TIMEOUT.value,
                            message="CAD measurement execution was cancelled after it started",
                            phase="execution",
                        )
                    )
                except CadMeasurementApplicationError:
                    pass
            raise
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, CadMeasurementApplicationError)
                else CadMeasurementApplicationError(
                    CadMeasurementErrorCode.WORKER_FAILED.value,
                    "The CAD measurement request failed unexpectedly",
                    phase="execution" if started_at is not None else "preflight",
                )
            )
            if error.code == CadMeasurementErrorCode.PUBLICATION_UNCERTAIN.value:
                return await self._reconcile_publication(scope, fingerprint, deadline)
            if started_at is None or code_artifact is None:
                raise error from None
            return await self._record_failure(
                scope=scope,
                operation_id=operation_id,
                fingerprint=fingerprint,
                synopsis=synopsis,
                code_artifact=code_artifact,
                detail=detail,
                started_at=started_at,
                code=error.code,
                message=str(error),
                phase=error.phase,
            )
        finally:
            if worker_result is not None:
                await asyncio.shield(worker_result.cleanup())
            await _remove_paths(paths)

    async def _resolve_source(
        self,
        scope: InvocationScope,
        package_alias: str,
    ) -> _ResolvedSource:
        try:
            package = await self._payloads.package_by_alias(scope, package_alias)
            if package.package.package_kind is not DataPackageKind.SOURCE_L0:
                raise CadMeasurementApplicationError(
                    CadMeasurementErrorCode.PACKAGE_KIND_MISMATCH.value,
                    "CAD measurement accepts only a source_l0 Package; use data_describe_package for derived data",
                    phase="preflight",
                )
            payloads = self._payloads.parquet_payloads(package)
            selected = tuple(payloads[name] for name in _INPUT_PAYLOAD_NAMES)
            rows = sum(int(item.member.row_count or 0) for item in selected)
            if rows > CAD_MEASUREMENT_COMPUTE_BUDGET.input_rows:
                raise CadMeasurementApplicationError(
                    CadMeasurementErrorCode.CAD_MEASUREMENT_INPUT_BUDGET_EXCEEDED.value,
                    "The three exact L0 inputs exceed the 2,000,000-row M3 budget",
                    phase="preflight",
                )
            return _ResolvedSource(package=package, payloads=selected)
        except CadMeasurementApplicationError:
            raise
        except (ExactPayloadResolutionError, KeyError) as exc:
            code = getattr(exc, "code", CadMeasurementErrorCode.PACKAGE_INTEGRITY_ERROR.value)
            raise CadMeasurementApplicationError(code, str(exc), phase="preflight") from None

    async def _materialize_tables(
        self,
        scope: InvocationScope,
        resolved: _ResolvedSource,
        deadline: float,
    ) -> tuple[dict[str, Path], dict[str, pa.Table]]:
        del scope

        def check_budget() -> None:
            if monotonic() >= deadline:
                raise CadMeasurementApplicationError(
                    CadMeasurementErrorCode.TIMEOUT.value,
                    "The M3 request deadline expired during input materialization",
                    phase="preflight",
                )

        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                paths = await self._payloads.materialize_members(
                    tuple(item.member for item in resolved.payloads),
                    check_budget=check_budget,
                    max_total_bytes=CAD_MEASUREMENT_COMPUTE_BUDGET.input_parquet_bytes,
                )
            tables: dict[str, pa.Table] = {}
            for item in resolved.payloads:
                async with asyncio.timeout(max(0.0, deadline - monotonic())):
                    tables[item.payload_name] = await asyncio.to_thread(
                        pq.read_table,
                        paths[item.member.member_id],
                    )
            if sum(table.nbytes for table in tables.values()) > (
                CAD_MEASUREMENT_COMPUTE_BUDGET.input_arrow_bytes
            ):
                raise CadMeasurementApplicationError(
                    CadMeasurementErrorCode.CAD_MEASUREMENT_INPUT_BUDGET_EXCEEDED.value,
                    "The three exact L0 inputs exceed the 512 MiB Arrow budget",
                    phase="preflight",
                )
            return paths, tables
        except CadMeasurementApplicationError:
            raise
        except TimeoutError:
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.TIMEOUT.value,
                "The M3 total deadline expired during input materialization",
                phase="preflight",
            ) from None
        except ExactPayloadResolutionError as exc:
            raise CadMeasurementApplicationError(exc.code, str(exc), phase="preflight") from None
        except (OSError, pa.ArrowException) as exc:
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.PACKAGE_INTEGRITY_ERROR.value,
                "The exact source_l0 measurement inputs could not be read",
                phase="preflight",
            ) from exc

    async def _publish_success(
        self,
        *,
        scope: InvocationScope,
        command: CadComputeMeasurementsCommand,
        synopsis: PackageSynopsis,
        resolved: _ResolvedSource,
        detail: CadMeasurementOperationDetail,
        fingerprint: str,
        operation_id: str,
        code_artifact: ArtifactRecord,
        started_at: datetime,
        serialized: tuple[_SerializedOutput, ...],
    ) -> CadMeasurementApplicationResult:
        payload_artifacts: dict[str, ArtifactRecord] = {}
        for output in serialized:
            payload_artifacts[output.name] = await self._upload_path(
                scope,
                filename=f"{output.name}.parquet",
                media_type=_PARQUET_MEDIA_TYPE,
                path=output.path,
                logical_name=_payload_member_name(output.name),
                fingerprint=fingerprint,
                kind="data_package_member",
            )
        description_artifact = await self._upload_bytes(
            scope,
            filename=_DESCRIPTION_MEMBER,
            media_type=_MARKDOWN_MEDIA_TYPE,
            content=command.output_package.description_markdown.encode("utf-8"),
            logical_name=_DESCRIPTION_MEMBER,
            fingerprint=fingerprint,
            kind="data_package_member",
        )
        created_at = code_artifact.created_at
        package_id = stable_id(
            "data_package",
            {"run_id": scope.run_id, "fingerprint": fingerprint},
        )
        manifest = _manifest(
            command=command,
            resolved=resolved,
            detail=detail,
            operation_id=operation_id,
            package_id=package_id,
            created_at=created_at,
            code_artifact=code_artifact,
            description_artifact=description_artifact,
            payload_artifacts=payload_artifacts,
            serialized=serialized,
        )
        manifest_artifact = await self._upload_bytes(
            scope,
            filename=_MANIFEST_MEMBER,
            media_type=_JSON_MEDIA_TYPE,
            content=(canonical_json(manifest) + "\n").encode("utf-8"),
            logical_name=_MANIFEST_MEMBER,
            fingerprint=fingerprint,
            kind="data_package_manifest",
        )
        package = DataPackageRecord(
            package_id=package_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            created_by_run_id=scope.run_id,
            name=command.output_package.name,
            package_kind=DataPackageKind.DERIVED,
            source_filename=None,
            description_title=synopsis.description_title,
            summary=synopsis.summary,
            section_titles=synopsis.section_titles,
            idempotency_key=f"cad-measurement-package:{fingerprint}",
            request_fingerprint=fingerprint,
            created_at=created_at,
        )
        common = {
            "owner_id": scope.owner_id,
            "tenant_id": scope.identity.tenant_id,
            "task_id": scope.identity.task_id or "",
            "package_id": package_id,
            "created_at": created_at,
        }
        by_name = {item.name: item for item in serialized}
        output_members = tuple(
            DataPackageMemberRecord(
                member_id=_member_id(package_id, _payload_member_name(name)),
                member_name=_payload_member_name(name),
                member_kind=PackageMemberKind.PAYLOAD,
                artifact=_artifact_ref(payload_artifacts[name]),
                media_type=_PARQUET_MEDIA_TYPE,
                row_count=by_name[name].table.num_rows,
                logical_payload_name=name,
                payload_description=_OUTPUT_DESCRIPTIONS[name],
                logical_key_fields=MEASUREMENT_LOGICAL_KEYS[name],
                arrow_schema=_arrow_schema(by_name[name].table.schema),
                logical_byte_count=by_name[name].table.nbytes,
                **common,
            )
            for name in CAD_MEASUREMENT_PAYLOAD_NAMES
        )
        members = (
            *output_members,
            DataPackageMemberRecord(
                member_id=_member_id(package_id, _CODE_MEMBER),
                member_name=_CODE_MEMBER,
                member_kind=PackageMemberKind.CODE,
                artifact=_artifact_ref(code_artifact),
                media_type=_PYTHON_MEDIA_TYPE,
                **common,
            ),
            DataPackageMemberRecord(
                member_id=_member_id(package_id, _DESCRIPTION_MEMBER),
                member_name=_DESCRIPTION_MEMBER,
                member_kind=PackageMemberKind.DESCRIPTION,
                artifact=_artifact_ref(description_artifact),
                media_type=_MARKDOWN_MEDIA_TYPE,
                **common,
            ),
            DataPackageMemberRecord(
                member_id=_member_id(package_id, _MANIFEST_MEMBER),
                member_name=_MANIFEST_MEMBER,
                member_kind=PackageMemberKind.MANIFEST,
                artifact=_artifact_ref(manifest_artifact),
                media_type=_JSON_MEDIA_TYPE,
                **common,
            ),
        )
        operation = CriticalOperationRecord(
            operation_id=operation_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            run_id=scope.run_id,
            operation_kind=CriticalOperationKind.DATA_TRANSFORM,
            tool_name=_TOOL_NAME,
            intent_summary=_intent_summary(synopsis),
            source_filename=None,
            idempotency_key=f"cad-measurement:{fingerprint}",
            request_fingerprint=fingerprint,
            status=CriticalOperationStatus.SUCCEEDED,
            result_package_id=package_id,
            code_artifact=_artifact_ref(code_artifact),
            detail=detail,
            started_at=started_at,
            finished_at=_now(),
        )
        try:
            published = await self._repository.publish_package_result(
                scope,
                package,
                members,
                operation,
            )
        except asyncio.CancelledError as cancelled:
            # Only the repository call can make publication outcome uncertain.
            # Artifact staging before this point is immutable but not catalog-visible.
            setattr(cancelled, "cad_measurement_commit_attempted", True)
            raise
        except Exception as exc:
            replay = await self._repository.get_operation_replay(scope, (_TOOL_NAME,), fingerprint)
            if replay is not None:
                return await self._resolve_replay(scope, replay)
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.PUBLICATION_UNCERTAIN.value,
                "The CAD measurement Package publication could not be reconciled",
                phase="publication",
                retryable=True,
            ) from exc
        return await self._resolve_replay(scope, published.operation)

    async def _record_failure(
        self,
        *,
        scope: InvocationScope,
        operation_id: str,
        fingerprint: str,
        synopsis: PackageSynopsis,
        code_artifact: ArtifactRecord,
        detail: CadMeasurementOperationDetail,
        started_at: datetime,
        code: str,
        message: str,
        phase: MeasurementPhase,
    ) -> CadMeasurementApplicationResult:
        operation = CriticalOperationRecord(
            operation_id=operation_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            run_id=scope.run_id,
            operation_kind=CriticalOperationKind.DATA_TRANSFORM,
            tool_name=_TOOL_NAME,
            intent_summary=_intent_summary(synopsis),
            source_filename=None,
            idempotency_key=f"cad-measurement:{fingerprint}",
            request_fingerprint=fingerprint,
            status=CriticalOperationStatus.FAILED,
            error_code=code,
            error_message=_bounded_text(message, 2_000),
            error_phase=(
                CriticalOperationErrorPhase.OUTPUT_VALIDATION
                if phase in {"output_validation", "publication"}
                else CriticalOperationErrorPhase.EXECUTION
            ),
            code_artifact=_artifact_ref(code_artifact),
            detail=detail,
            started_at=started_at,
            finished_at=_now(),
        )
        try:
            published = await self._repository.record_failed_operation(scope, operation)
        except Exception as exc:
            replay = await self._repository.get_operation_replay(scope, (_TOOL_NAME,), fingerprint)
            if replay is None:
                raise CadMeasurementApplicationError(
                    CadMeasurementErrorCode.PUBLICATION_UNCERTAIN.value,
                    "The failed CAD measurement Operation could not be reconciled",
                    phase="publication",
                    retryable=True,
                ) from exc
            published = replay
        return await self._resolve_replay(scope, published)

    async def _resolve_replay(
        self,
        scope: InvocationScope,
        replay: PublishedCriticalOperation,
    ) -> CadMeasurementApplicationResult:
        operation = replay.operation
        if operation.status is CriticalOperationStatus.FAILED:
            raise CadMeasurementApplicationError(
                operation.error_code or CadMeasurementErrorCode.WORKER_FAILED.value,
                operation.error_message or "The previous CAD measurement failed",
                phase=(
                    operation.error_phase.value
                    if operation.error_phase is not None
                    else "execution"
                ),
                retryable=False,
                operation_alias=replay.catalog_entry.alias,
            )
        package = await self._repository.get_package(scope, operation.result_package_id or "")
        if package is None or not isinstance(operation.detail, CadMeasurementOperationDetail):
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.PACKAGE_INTEGRITY_ERROR.value,
                "The successful measurement Operation has no exact visible result Package",
                phase="publication",
            )
        payload_members = tuple(
            item for item in package.members if item.member_kind is PackageMemberKind.PAYLOAD
        )
        supporting_members = {
            item.member_name: item.member_kind
            for item in package.members
            if item.member_kind is not PackageMemberKind.PAYLOAD
        }
        names = tuple(item.logical_payload_name for item in payload_members)
        if (
            len(package.members) != len(CAD_MEASUREMENT_PAYLOAD_NAMES) + 3
            or len(payload_members) != len(CAD_MEASUREMENT_PAYLOAD_NAMES)
            or len(set(names)) != len(names)
            or set(names) != set(CAD_MEASUREMENT_PAYLOAD_NAMES)
            or supporting_members
            != {
                _CODE_MEMBER: PackageMemberKind.CODE,
                _DESCRIPTION_MEMBER: PackageMemberKind.DESCRIPTION,
                _MANIFEST_MEMBER: PackageMemberKind.MANIFEST,
            }
        ):
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.PACKAGE_INTEGRITY_ERROR.value,
                "The successful measurement Package does not contain exactly the fixed ten Payloads",
                phase="publication",
            )
        payloads = {item.logical_payload_name: item for item in payload_members}
        return CadMeasurementApplicationResult(
            published=PublishedPackageResult(package=package, operation=replay),
            output_members=tuple(payloads[name] for name in CAD_MEASUREMENT_PAYLOAD_NAMES),
        )

    async def _reconcile_publication(
        self,
        scope: InvocationScope,
        fingerprint: str,
        deadline: float,
    ) -> CadMeasurementApplicationResult:
        reconcile_deadline = min(deadline, monotonic() + 2.0)
        while monotonic() < reconcile_deadline:
            try:
                remaining = max(0.0, reconcile_deadline - monotonic())
                async with asyncio.timeout(remaining):
                    replay = await self._repository.get_operation_replay(
                        scope,
                        (_TOOL_NAME,),
                        fingerprint,
                    )
            except Exception:
                replay = None
            if replay is not None:
                return await self._resolve_replay(scope, replay)
            await asyncio.sleep(min(0.05, max(0.0, reconcile_deadline - monotonic())))
        raise CadMeasurementApplicationError(
            CadMeasurementErrorCode.PUBLICATION_UNCERTAIN.value,
            "The CAD measurement publication outcome could not be reconciled; retry the exact request",
            phase="publication",
            retryable=True,
        )

    def _compile_synopsis(self, markdown: str) -> PackageSynopsis:
        try:
            return self._synopsis.compile(markdown)
        except PackageSynopsisError as exc:
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.INVALID_ARGUMENT.value,
                str(exc),
                phase="preflight",
            ) from None

    async def _upload_bytes(
        self,
        scope: InvocationScope,
        *,
        filename: str,
        media_type: str,
        content: bytes,
        logical_name: str,
        fingerprint: str,
        kind: str,
    ) -> ArtifactRecord:
        try:
            return await self._artifacts.upload_bytes(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename=filename,
                content_type=media_type,
                content=content,
                idempotency_key=f"cad-measurement:{fingerprint}:{logical_name}",
                kind=kind,
                run_id=scope.run_id,
                version_metadata={"logical_name": logical_name},
            )
        except Exception as exc:
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.SERVICE_UNAVAILABLE.value,
                "An exact CAD measurement Artifact could not be stored",
                phase="publication" if logical_name != _CODE_MEMBER else "preflight",
                retryable=True,
            ) from exc

    async def _upload_path(
        self,
        scope: InvocationScope,
        *,
        filename: str,
        media_type: str,
        path: Path,
        logical_name: str,
        fingerprint: str,
        kind: str,
    ) -> ArtifactRecord:
        try:
            return await self._artifacts.upload(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename=filename,
                content_type=media_type,
                chunks=_file_chunks(path),
                idempotency_key=f"cad-measurement:{fingerprint}:{logical_name}",
                kind=kind,
                run_id=scope.run_id,
                version_metadata={"logical_name": logical_name},
            )
        except Exception as exc:
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.SERVICE_UNAVAILABLE.value,
                "An exact CAD measurement Payload Artifact could not be stored",
                phase="publication",
                retryable=True,
            ) from exc


def _analysis_result(
    analysis: Mapping[str, object],
    *,
    command: CadAnalyzeToleranceCandidatesCommand,
    tables: Mapping[str, pa.Table],
) -> CadToleranceAnalysisResult:
    payload = dict(analysis)
    document_keys = {
        str(value)
        for value in tables["entities"].column("document_key").to_pylist()
        if value is not None
    }
    if len(document_keys) != 1:
        raise CadMeasurementApplicationError(
            CadMeasurementErrorCode.PACKAGE_INTEGRITY_ERROR.value,
            "The source_l0 entities do not identify exactly one document",
            phase="preflight",
        )
    payload.update(
        {
            "package_alias": command.package_alias,
            "document_key": document_keys.pop(),
            "source_unit_to_m": command.source_unit_to_m,
        }
    )
    try:
        return CadToleranceAnalysisResult.model_validate(payload)
    except Exception as exc:
        raise CadMeasurementApplicationError(
            CadMeasurementErrorCode.OUTPUT_INVALID.value,
            "The tolerance analyzer result does not match the bounded response contract",
            phase="output_validation",
        ) from exc


def _worker_outputs(
    result: CadMeasurementWorkerComputeResult,
) -> tuple[_SerializedOutput, ...]:
    if (
        set(result.tables) != set(CAD_MEASUREMENT_PAYLOAD_NAMES)
        or set(result.parquet_paths) != set(CAD_MEASUREMENT_PAYLOAD_NAMES)
        or set(result.parquet_bytes) != set(CAD_MEASUREMENT_PAYLOAD_NAMES)
    ):
        raise CadMeasurementApplicationError(
            CadMeasurementErrorCode.OUTPUT_INVALID.value,
            "The measurement worker did not return the exact fixed ten Payloads",
            phase="output_validation",
        )
    total_rows = 0
    total_arrow_bytes = 0
    total_parquet_bytes = 0
    outputs: list[_SerializedOutput] = []
    for name in CAD_MEASUREMENT_PAYLOAD_NAMES:
        table = result.tables[name]
        if table.schema != MEASUREMENT_PAYLOAD_SCHEMAS[name]:
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.OUTPUT_INVALID.value,
                f"Measurement Payload {name} has an invalid Arrow schema",
                phase="output_validation",
            )
        total_rows += table.num_rows
        total_arrow_bytes += table.nbytes
        path = result.parquet_paths[name]
        parquet_bytes = result.parquet_bytes[name]
        try:
            actual_bytes = path.stat().st_size
        except OSError as exc:
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.OUTPUT_INVALID.value,
                f"Measurement Payload {name} is missing its staged Parquet file",
                phase="output_validation",
            ) from exc
        if parquet_bytes != actual_bytes:
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.OUTPUT_INVALID.value,
                f"Measurement Payload {name} staged Parquet size does not match",
                phase="output_validation",
            )
        if parquet_bytes > (CAD_MEASUREMENT_COMPUTE_BUDGET.output_payload_parquet_bytes or 0):
            raise CadMeasurementApplicationError(
                CadMeasurementErrorCode.OUTPUT_BUDGET_EXCEEDED.value,
                f"Measurement Payload {name} exceeds the 100 MiB Parquet budget",
                phase="output_validation",
            )
        total_parquet_bytes += parquet_bytes
        outputs.append(
            _SerializedOutput(
                name=name,
                table=table,
                path=path,
                parquet_bytes=parquet_bytes,
            )
        )
    if total_rows > (CAD_MEASUREMENT_COMPUTE_BUDGET.output_rows or 0):
        message = "Measurement outputs exceed the 2,000,000-row aggregate budget"
    elif total_arrow_bytes > (CAD_MEASUREMENT_COMPUTE_BUDGET.output_arrow_bytes or 0):
        message = "Measurement outputs exceed the 512 MiB Arrow aggregate budget"
    elif total_parquet_bytes > (CAD_MEASUREMENT_COMPUTE_BUDGET.output_parquet_bytes or 0):
        message = "Measurement outputs exceed the 256 MiB Parquet aggregate budget"
    else:
        return tuple(outputs)
    raise CadMeasurementApplicationError(
        CadMeasurementErrorCode.OUTPUT_BUDGET_EXCEEDED.value,
        message,
        phase="output_validation",
    )


def _operation_detail(
    command: CadComputeMeasurementsCommand,
    resolved: _ResolvedSource,
) -> CadMeasurementOperationDetail:
    return CadMeasurementOperationDetail(
        inputs=tuple(
            TransformInputBindingRecord(
                ordinal=index,
                input_name=item.payload_name,
                package_id=resolved.package.package.package_id,
                member_id=item.member.member_id,
                payload_name=item.payload_name,
            )
            for index, item in enumerate(resolved.payloads, 1)
        ),
        source_unit_to_m=command.source_unit_to_m,
        path_layers=command.path_layers,
        point_scopes=command.point_scopes,
        tolerance_spec=command.tolerance_spec,
        outputs=tuple(
            TransformOutputRecord(
                payload_name=name,
                format="parquet",
                description=_OUTPUT_DESCRIPTIONS[name],
                logical_key_fields=MEASUREMENT_LOGICAL_KEYS[name],
            )
            for name in CAD_MEASUREMENT_PAYLOAD_NAMES
        ),
        output_package_name=command.output_package.name,
    )


def _request_fingerprint(
    scope: InvocationScope,
    command: CadComputeMeasurementsCommand,
    resolved: _ResolvedSource,
) -> str:
    return sha256_digest(
        {
            "contract": "m3-cad-measurement-v1",
            "run_id": scope.run_id,
            "source_package_id": resolved.package.package.package_id,
            "inputs": [
                {
                    "payload_name": item.payload_name,
                    "member_id": item.member.member_id,
                    "artifact": item.member.artifact.model_dump(mode="json"),
                }
                for item in resolved.payloads
            ],
            "command": command.model_dump(mode="json", exclude={"package_alias"}),
        }
    )


def _canonical_compute_command(
    command: CadComputeMeasurementsCommand,
) -> CadComputeMeasurementsCommand:
    scopes = tuple(
        item.model_copy(update={"point_layers": tuple(sorted(item.point_layers))})
        for item in sorted(command.point_scopes, key=lambda item: item.scope_key)
    )
    return command.model_copy(
        update={
            "path_layers": tuple(sorted(command.path_layers)),
            "point_scopes": scopes,
        }
    )


def _canonical_analyzer_command(
    command: CadAnalyzeToleranceCandidatesCommand,
) -> CadAnalyzeToleranceCandidatesCommand:
    groups = tuple(
        item.model_copy(update={"point_layers": tuple(sorted(item.point_layers))})
        for item in sorted(command.point_groups, key=lambda item: item.scope_key)
    )
    trials = tuple(
        item.model_copy(
            update={
                "point_search_radii": tuple(
                    sorted(item.point_search_radii, key=lambda radius: radius.scope_key)
                )
            }
        )
        for item in command.trials
    )
    return command.model_copy(
        update={
            "path_layers": tuple(sorted(command.path_layers)),
            "point_groups": groups,
            "trials": trials,
        }
    )


def _manifest(
    *,
    command: CadComputeMeasurementsCommand,
    resolved: _ResolvedSource,
    detail: CadMeasurementOperationDetail,
    operation_id: str,
    package_id: str,
    created_at: datetime,
    code_artifact: ArtifactRecord,
    description_artifact: ArtifactRecord,
    payload_artifacts: Mapping[str, ArtifactRecord],
    serialized: tuple[_SerializedOutput, ...],
) -> dict[str, object]:
    by_name = {item.name: item for item in serialized}
    return {
        "publication_state": "published",
        "package_id": package_id,
        "package_kind": "derived",
        "name": command.output_package.name,
        "created_at": created_at,
        "description": {
            "member_name": _DESCRIPTION_MEMBER,
            "artifact_version_ref": _artifact_ref(description_artifact).model_dump(mode="json"),
        },
        "transformation": {
            "operation_id": operation_id,
            "tool_name": _TOOL_NAME,
            "engine": "cad_measurement",
            "code_member_name": _CODE_MEMBER,
            "code_artifact_version_ref": _artifact_ref(code_artifact).model_dump(mode="json"),
            "source_package_id": resolved.package.package.package_id,
            "inputs": [item.model_dump(mode="json") for item in detail.inputs],
            "source_unit_to_m": command.source_unit_to_m,
            "path_layers": list(command.path_layers),
            "point_scopes": [item.model_dump(mode="json") for item in command.point_scopes],
            "tolerance_spec": command.tolerance_spec.model_dump(mode="json"),
        },
        "payloads": {
            name: {
                "format": "parquet",
                "media_type": _PARQUET_MEDIA_TYPE,
                "description": _OUTPUT_DESCRIPTIONS[name],
                "logical_key_fields": list(MEASUREMENT_LOGICAL_KEYS[name]),
                "row_count": by_name[name].table.num_rows,
                "logical_byte_count": by_name[name].table.nbytes,
                "arrow_schema": [
                    item.model_dump(mode="json")
                    for item in _arrow_schema(by_name[name].table.schema)
                ],
                "members": [
                    {
                        "logical_name": _payload_member_name(name),
                        "size_bytes": payload_artifacts[name].size_bytes,
                        "artifact_version_ref": _artifact_ref(payload_artifacts[name]).model_dump(
                            mode="json"
                        ),
                    }
                ],
            }
            for name in CAD_MEASUREMENT_PAYLOAD_NAMES
        },
    }


def _worker_error(exc: CadMeasurementWorkerError) -> CadMeasurementApplicationError:
    return CadMeasurementApplicationError(
        exc.code,
        str(exc),
        phase=exc.phase,
        retryable=exc.retryable,
    )


def _implementation_bundle_bytes() -> bytes:
    """Return a deterministic, executable audit bundle for the exact M3 implementation."""

    from CAD2BIM_backend.infrastructure import cad_measurement

    root = Path(cad_measurement.__file__ or "").parent
    try:
        sources = {
            path.name: path.read_bytes()
            for path in sorted(root.glob("*.py"), key=lambda item: item.name)
        }
    except OSError as exc:
        raise CadMeasurementApplicationError(
            CadMeasurementErrorCode.SERVICE_UNAVAILABLE.value,
            "The exact server-owned CAD measurement implementation is unavailable",
            phase="preflight",
            retryable=True,
        ) from exc
    if not sources or any(not content for content in sources.values()):
        raise CadMeasurementApplicationError(
            CadMeasurementErrorCode.SERVICE_UNAVAILABLE.value,
            "The exact server-owned CAD measurement implementation bundle is invalid",
            phase="preflight",
            retryable=True,
        )
    lines = [
        '"""CAD measurement source snapshot; integrity belongs to its exact ArtifactVersion."""',
        "from base64 import b85decode",
        "from zlib import decompress",
        "FILES = {",
    ]
    for name, content in sources.items():
        encoded = base64.b85encode(zlib.compress(content, level=9)).decode("ascii")
        lines.append(f"    {name!r}: {encoded!r},")
    lines.extend(
        [
            "}",
            "def sources():",
            "    return {name: decompress(b85decode(encoded)).decode('utf-8')",
            "            for name, encoded in FILES.items()}",
            "if __name__ == '__main__':",
            "    print('\\n'.join(sources()))",
            "",
        ]
    )
    result = "\n".join(lines).encode("utf-8")
    if len(result) > 65_536:
        raise CadMeasurementApplicationError(
            CadMeasurementErrorCode.SERVICE_UNAVAILABLE.value,
            "The exact CAD measurement audit bundle exceeds the code-read contract",
            phase="preflight",
            retryable=True,
        )
    return result


def _payload_member_name(name: str) -> str:
    return f"payloads/{name}/part-00000.parquet"


def _member_id(package_id: str, member_name: str) -> str:
    return stable_id("package_member", {"package_id": package_id, "name": member_name})


def _artifact_ref(artifact: ArtifactRecord) -> ArtifactVersionRef:
    return ArtifactVersionRef(
        artifact_id=artifact.artifact_id,
        version_id=artifact.version_id,
        sha256=artifact.sha256,
    )


def _arrow_schema(schema: pa.Schema) -> tuple[ArrowColumnRecord, ...]:
    return tuple(
        ArrowColumnRecord(name=field.name, type=str(field.type), nullable=field.nullable)
        for field in schema
    )


def _intent_summary(synopsis: PackageSynopsis) -> str:
    return f"{synopsis.description_title} — {synopsis.summary}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bounded_text(value: str, maximum: int) -> str:
    normalized = " ".join(value.split()) or "CAD measurement failed"
    encoded = normalized.encode("utf-8")
    if len(encoded) <= maximum:
        return normalized
    return encoded[:maximum].decode("utf-8", errors="ignore")


async def _file_chunks(path: Path):
    with path.open("rb") as stream:
        while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
            yield chunk


async def _remove_paths(paths: Mapping[str, Path]) -> None:
    for path in paths.values():
        try:
            await asyncio.to_thread(os.remove, path)
        except FileNotFoundError:
            pass


__all__ = [
    "CadMeasurementApplicationError",
    "CadMeasurementApplicationResult",
    "CadMeasurementService",
]
