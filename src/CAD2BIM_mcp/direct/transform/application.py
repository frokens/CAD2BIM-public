"""Direct application behavior owned by this capability family."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from typing import TYPE_CHECKING

from pydantic import ValidationError

from CAD2BIM_backend.domain.data_runtime import (
    CadMeasurementOperationDetail as DomainCadMeasurementOperationDetail,
    CriticalOperationKind,
    CriticalOperationStatus,
    DataPackageKind,
    DataTransformOperationDetail as DomainDataTransformOperationDetail,
    PackageMemberKind,
)
from CAD2BIM_backend.domain.data_transform import PackageCodeReference
from CAD2BIM_mcp.direct.transform.contracts import (
    DataRepairTransformResult,
    DataRepairTransformToolResult,
    DataTransformOutputResult,
    DataTransformResult,
    DataTransformToolResult,
    JsonDataTransformOutputResult,
    MultiOutputDataTransformResult,
    MultiOutputDataRepairTransformResult,
)
from CAD2BIM_mcp.direct.transform.repair_inputs import (
    DirectFailedOperationRepairTransformInput,
    DirectRepairTransformInput,
    DirectSucceededPackageRepairTransformInput,
    to_domain_repair_transform_command,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError

from CAD2BIM_mcp.direct.application_support import (
    _direct_error,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.data_transform_service import DataTransformService
    from CAD2BIM_backend.application.data_transform_service import (
        DataTransformApplicationResult,
    )
    from CAD2BIM_backend.domain.data_transform import DataTransformCommand
    from CAD2BIM_backend.application.cad_measurement_service import (
        CadMeasurementApplicationResult,
    )


from CAD2BIM_backend.domain.runtime_execution import DATA_SQL_EXECUTION, DATA_PYTHON_EXECUTION

_TRANSFORM_TIMEOUT_SECONDS = DATA_SQL_EXECUTION.total_seconds
_PYTHON_TRANSFORM_TIMEOUT_SECONDS = DATA_PYTHON_EXECUTION.total_seconds


def _repair_request_payload(request: DirectRepairTransformInput) -> dict[str, object]:
    return {
        "mode": "repair",
        "request": request.model_dump(mode="json", exclude={"direct_session_alias"}),
    }


class TransformApplicationMixin:
    async def repair_transform(
        self,
        request: DirectRepairTransformInput,
        *,
        request_identity: str,
    ) -> DataRepairTransformToolResult:
        """Resolve the selected source's exact Operation, patch it, and rerun it."""

        from CAD2BIM_backend.application.data_transform_service import (
            DataTransformApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        service = self._require_transform_service()
        started = monotonic()
        maximum_deadline = started + _PYTHON_TRANSFORM_TIMEOUT_SECONDS
        request_payload = _repair_request_payload(request)
        try:
            async with asyncio.timeout(max(0.0, maximum_deadline - monotonic())):
                scope = await self._resolve_scope(
                    request.direct_session_alias,
                    "data_derive",
                )
                with self._scope_binder(scope.identity):
                    resumed = await service.resume_by_identity(
                        scope,
                        request_identity,
                        request_payload,
                        deadline=maximum_deadline,
                    )
                    if resumed is not None:
                        parent_alias = resumed.repair_parent_operation_alias
                        if parent_alias is None:
                            raise DirectApplicationError(
                                "OPERATION_INTEGRITY_ERROR",
                                "The repaired Operation has no original parent alias",
                                phase="publication",
                            )
                        return _data_repair_transform_result(
                            resumed, repair_parent_operation_alias=parent_alias
                        )
                    if isinstance(request, DirectFailedOperationRepairTransformInput):
                        parent_alias = request.source.operation_alias
                        parent = await self._application.get_operation(scope, parent_alias)
                        if parent.operation.operation.status is not CriticalOperationStatus.FAILED:
                            raise DirectApplicationError(
                                "TRANSFORM_REPAIR_PARENT_INVALID",
                                "failed_operation must select a terminal failed Data Transform Operation",
                                phase="preflight",
                            )
                    elif isinstance(request, DirectSucceededPackageRepairTransformInput):
                        source_code = await self._application.get_transformation_code(
                            scope,
                            PackageCodeReference(
                                reference_kind="package",
                                package_alias=request.source.package_alias,
                            ),
                        )
                        parent_alias = source_code.operation.catalog_entry.alias
                        parent = await self._application.get_operation(scope, parent_alias)
                        if (
                            parent.operation.operation.status
                            is not CriticalOperationStatus.SUCCEEDED
                        ):
                            raise DirectApplicationError(
                                "TRANSFORM_REPAIR_PARENT_INVALID",
                                "succeeded_package must resolve to a succeeded Data Transform Operation",
                                phase="preflight",
                            )
                    else:  # pragma: no cover - closed Provider union
                        raise DirectApplicationError(
                            "INVALID_ARGUMENT",
                            "The repair source branch is invalid",
                            phase="preflight",
                        )

                    if parent.operation.operation.tool_name not in {
                        "data_derive",
                    }:
                        raise DirectApplicationError(
                            "TRANSFORM_REPAIR_PARENT_INVALID",
                            "Repair sources must be produced by a Data Transform Operation",
                            phase="preflight",
                        )
        except TimeoutError:
            raise DirectApplicationError(
                "TRANSFORM_TIMEOUT",
                "The repair deadline expired before execution could start",
                retryable=False,
                phase="preflight",
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except DataTransformApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                operation_alias=exc.operation_alias,
                phase=exc.phase,
                diagnostic=_transform_application_diagnostic(exc),
            ) from None

        detail = parent.operation.operation.detail
        timeout_seconds = (
            _PYTHON_TRANSFORM_TIMEOUT_SECONDS
            if isinstance(detail, DomainDataTransformOperationDetail)
            and detail.engine == "python_arrow"
            else _TRANSFORM_TIMEOUT_SECONDS
        )
        deadline = started + timeout_seconds
        command = to_domain_repair_transform_command(
            request,
            parent_operation_id=parent.operation.operation.operation_id,
        )
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                with self._scope_binder(scope.identity):
                    result = await service.repair_transform(
                        scope=scope,
                        parent_operation=parent.operation,
                        # OperationInspection indexes inputs by request-local
                        # input_name for human inspection.  Repair reconstruction
                        # needs the current Catalog aliases instead; never infer
                        # those aliases from an internal package id.
                        catalog_packages={
                            package.catalog_entry.alias: package
                            for package in parent.input_packages.values()
                        },
                        command=command,
                        request_identity=request_identity,
                        request_payload=request_payload,
                        repair_parent_operation_alias=parent_alias,
                        deadline=deadline,
                    )
            return _data_repair_transform_result(
                result,
                repair_parent_operation_alias=parent_alias,
            )
        except TimeoutError:
            raise DirectApplicationError(
                "TRANSFORM_PUBLICATION_UNCERTAIN",
                "The Direct repair deadline expired; reconcile with the same invocation identity",
                retryable=True,
                phase="publication",
            ) from None
        except DataTransformApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                operation_alias=exc.operation_alias,
                phase=exc.phase,
                diagnostic=_transform_application_diagnostic(exc),
            ) from None
        except (KeyError, TypeError, ValueError, ValidationError):
            raise DirectApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The repaired Data Transform result could not be projected",
                phase="publication",
            ) from None

    async def transform_data(
        self,
        direct_session_alias: str,
        command: DataTransformCommand,
        *,
        request_identity: str,
    ) -> DataTransformToolResult:
        from CAD2BIM_backend.application.data_transform_service import (
            DataTransformApplicationError,
        )
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        service = self._require_transform_service()
        timeout_seconds = (
            _PYTHON_TRANSFORM_TIMEOUT_SECONDS
            if command.execution.engine == "python_arrow"
            else _TRANSFORM_TIMEOUT_SECONDS
        )
        timeout_label = str(int(timeout_seconds))
        deadline = monotonic() + timeout_seconds
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                scope = await self._resolve_scope(direct_session_alias, "data_derive")
        except TimeoutError:
            raise DirectApplicationError(
                "TRANSFORM_TIMEOUT",
                f"The {timeout_label}-second transform deadline expired before execution could start",
                retryable=False,
                phase="preflight",
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None

        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                with self._scope_binder(scope.identity):
                    result = await service.transform(
                        scope,
                        command,
                        request_identity=request_identity,
                        deadline=deadline,
                    )
            return _data_transform_result(result)
        except TimeoutError:
            raise DirectApplicationError(
                "TRANSFORM_PUBLICATION_UNCERTAIN",
                f"The {timeout_label}-second Direct transform boundary expired; reconcile with the same invocation identity",
                retryable=True,
                phase="publication",
            ) from None
        except DataTransformApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                operation_alias=exc.operation_alias,
                phase=exc.phase,
                diagnostic=_transform_application_diagnostic(exc),
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except (KeyError, TypeError, ValueError):
            raise DirectApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The Data Transform result could not be projected",
                phase="publication",
            ) from None

    def _require_transform_service(self) -> DataTransformService:
        if self._transform_service is None:
            raise DirectApplicationError(
                "TRANSFORM_RUNTIME_UNAVAILABLE",
                "The M2b Data Transform runtime is not configured",
                phase="preflight",
            )
        return self._transform_service


def _data_transform_result(
    result: DataTransformApplicationResult | CadMeasurementApplicationResult,
) -> DataTransformToolResult:
    package = result.published.package
    operation = result.published.operation
    members = result.output_members
    if (
        package.package.package_kind is not DataPackageKind.DERIVED
        or operation.operation.status is not CriticalOperationStatus.SUCCEEDED
        or operation.operation.operation_kind is not CriticalOperationKind.DATA_TRANSFORM
        or operation.operation.result_package_id != package.package.package_id
        or not 1 <= len(members) <= 16
    ):
        raise ValueError("published Data Transform result is inconsistent")
    outputs: list[DataTransformOutputResult | JsonDataTransformOutputResult] = []
    for member in members:
        if (
            member.package_id != package.package.package_id
            or member.member_kind is not PackageMemberKind.PAYLOAD
            or member.logical_payload_name is None
        ):
            raise ValueError("published Data Transform output member is inconsistent")
        if member.media_type == "application/vnd.apache.parquet":
            if member.row_count is None or not member.logical_key_fields:
                raise ValueError("published Parquet output metadata is incomplete")
            outputs.append(
                DataTransformOutputResult(
                    payload_name=member.logical_payload_name,
                    format="parquet",
                    row_count=member.row_count,
                    logical_key_fields=member.logical_key_fields,
                )
            )
        elif member.media_type == "application/json":
            if member.json_kind is None or member.logical_byte_count is None:
                raise ValueError("published JSON output metadata is incomplete")
            outputs.append(
                JsonDataTransformOutputResult(
                    payload_name=member.logical_payload_name,
                    format="json",
                    json_kind=member.json_kind.value,
                    size_bytes=member.logical_byte_count,
                )
            )
        else:
            raise ValueError("published Data Transform output format is unsupported")
    if len(outputs) != len(members) or len({item.payload_name for item in outputs}) != len(outputs):
        raise ValueError("published Data Transform outputs are inconsistent")
    detail = operation.operation.detail
    if not isinstance(
        detail,
        (DomainDataTransformOperationDetail, DomainCadMeasurementOperationDetail),
    ) or tuple(item.payload_name for item in outputs) != tuple(
        item.payload_name for item in detail.outputs
    ):
        raise ValueError("published Data Transform output order does not match its declaration")
    common = {
        "operation_alias": operation.catalog_entry.alias,
        "package_alias": package.catalog_entry.alias,
        "package_name": package.package.name,
    }
    if isinstance(detail, DomainDataTransformOperationDetail) and detail.ifc_source is not None:
        common["ifc_source"] = {"revision_no": detail.ifc_source.revision_no}
    if len(outputs) == 1:
        return DataTransformResult(**common, output=outputs[0])
    return MultiOutputDataTransformResult(**common, outputs=tuple(outputs))


def _data_repair_transform_result(
    result: DataTransformApplicationResult,
    *,
    repair_parent_operation_alias: str,
) -> DataRepairTransformToolResult:
    projected = _data_transform_result(result)
    operation = result.published.operation.operation
    detail = operation.detail
    if (
        operation.tool_name != "data_derive"
        or not isinstance(detail, DomainDataTransformOperationDetail)
        or operation.code_artifact is None
        or operation.repair_parent_operation_id is None
    ):
        raise ValueError("published repaired Data Transform result is inconsistent")
    common = {
        **projected.model_dump(mode="python"),
        "repair_parent_operation_alias": repair_parent_operation_alias,
        "engine": detail.engine,
        "code_sha256": operation.code_artifact.sha256,
    }
    if isinstance(projected, DataTransformResult):
        return DataRepairTransformResult(**common)
    return MultiOutputDataRepairTransformResult(**common)


def _transform_application_diagnostic(error: object) -> dict[str, object] | None:
    explicit = getattr(error, "diagnostic", None)
    if isinstance(explicit, dict):
        return explicit
    if getattr(error, "phase", None) != "code_validation":
        return None
    try:
        parsed = json.loads(str(error))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): value for key, value in parsed.items()}
