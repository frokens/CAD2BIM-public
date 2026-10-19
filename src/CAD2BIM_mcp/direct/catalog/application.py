"""Direct application behavior owned by this capability family."""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING

from pydantic import ValidationError

from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationKind,
    CriticalOperationStatus,
    DataPackageKind,
    IfcProductsApplyOperationDetail,
    PackageMemberKind,
    PublishedCriticalOperation,
    PublishedDataPackage,
)
from CAD2BIM_backend.domain.data_query import (
    DataDescribePackageQuery,
    DataDescribePackageResult as BackendDataDescribePackageResult,
    JsonPayloadSummary,
)
from CAD2BIM_mcp.direct.catalog.contracts import (
    DataPackagePrimaryResult,
    DataPayloadCard,
    DerivedCatalogPackageCard,
    DirectDataDescribePackageResult,
    DirectJsonPayloadSummary,
    DirectPackageListResult,
    JsonDataPayloadCard,
    PackageDescriptionResult,
    ParquetDataPayloadCard,
    SourceL0CatalogPackageCard,
)
from CAD2BIM_mcp.direct.directory_budget import bounded_keyset_directory
from CAD2BIM_mcp.direct.errors import DirectApplicationError

from CAD2BIM_mcp.direct.application_support import (
    _direct_error,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.data_runtime_service import (
        PackageDescription,
    )


_L0_PAYLOAD_ORDER = (
    "records",
    "raw_tags",
    "entities",
    "geometry_elements",
    "xdata_tags",
    "relationships",
)


class CatalogApplicationMixin:
    async def list_data_packages(
        self,
        direct_session_alias: str,
        before_sequence: int | None = None,
        page_size: int = 20,
    ) -> DirectPackageListResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        try:
            catalog = await self._run_runtime_read(
                direct_session_alias,
                "catalog",
                lambda scope: self._application.list_package_catalog(
                    scope, before_sequence=before_sequence, page_size=page_size
                ),
            )
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc) from None
        try:
            cards = tuple(
                _catalog_package_card(
                    item,
                    catalog.producing_operations.get(item.package.package_id),
                )
                for item in catalog.packages
            )
            return _bounded_package_list_result(
                cards,
                complete=catalog.complete,
                next_before_sequence=catalog.next_before_sequence,
            )
        except (KeyError, TypeError, ValueError):
            raise DirectApplicationError(
                "CATALOG_INTEGRITY_ERROR",
                "The Direct Data Catalog could not be projected",
            ) from None

    async def get_package_description(
        self,
        direct_session_alias: str,
        package_alias: str,
    ) -> PackageDescriptionResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        try:
            description = await self._run_runtime_read(
                direct_session_alias,
                "description",
                lambda scope: self._application.get_package_description(scope, package_alias),
            )
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc) from None
        return _description_result(description)

    async def describe_package(
        self,
        direct_session_alias: str,
        package_alias: str,
        payload_name: str | None = None,
    ) -> DirectDataDescribePackageResult:
        service = self._require_query_service()
        deadline = monotonic() + 10.0
        try:
            async with asyncio.timeout(10.0):
                request = DataDescribePackageQuery(
                    package_alias=package_alias,
                    payload_name=payload_name,
                )
                scope = await self._resolve_scope(direct_session_alias, "describe_package")
                with self._scope_binder(scope.identity):
                    result = await service.describe_package(scope, request)
            if monotonic() > deadline:
                raise DirectApplicationError("QUERY_TIMEOUT", "The query timed out")
        except TimeoutError:
            raise DirectApplicationError("QUERY_TIMEOUT", "The query timed out") from None
        except ValidationError:
            raise DirectApplicationError(
                "INVALID_ARGUMENT",
                "The package description query is invalid",
            ) from None
        except Exception as exc:
            self._raise_query_error(exc)
        try:
            return _describe_package_result(result)
        except (TypeError, ValueError):
            raise DirectApplicationError(
                "CATALOG_INTEGRITY_ERROR",
                "The Package payload description could not be projected",
            ) from None


def _describe_package_result(
    result: BackendDataDescribePackageResult,
) -> DirectDataDescribePackageResult:
    return DirectDataDescribePackageResult(
        package=result.package,
        producing_operation_alias=result.producing_operation_alias,
        payloads=tuple(
            DirectJsonPayloadSummary(
                payload_name=item.payload_name,
                size_bytes=item.size_bytes,
                json_kind=item.json_kind,
            )
            if isinstance(item, JsonPayloadSummary)
            else item
            for item in result.payloads
        ),
        payload_schema=result.payload_schema,
    )


def _catalog_payload_cards(package: PublishedDataPackage) -> tuple[DataPayloadCard, ...]:
    record = package.package
    if record.package_kind is DataPackageKind.SOURCE_L0:
        payload_by_name = {
            PurePosixPath(member.member_name).parts[-2]: ParquetDataPayloadCard(
                payload_name=PurePosixPath(member.member_name).parts[-2],
                row_count=int(member.row_count or 0),
            )
            for member in package.members
            if member.member_kind is PackageMemberKind.PAYLOAD
        }
        if set(payload_by_name) != set(_L0_PAYLOAD_ORDER):
            raise ValueError("source_l0 package does not contain the exact six payloads")
        return tuple(payload_by_name[name] for name in _L0_PAYLOAD_ORDER)

    payloads: list[DataPayloadCard] = []
    for member in package.members:
        if member.member_kind is not PackageMemberKind.PAYLOAD:
            continue
        if member.media_type == "application/vnd.apache.parquet":
            if member.row_count is None:
                raise ValueError("derived Parquet payload metadata is incomplete")
            payloads.append(
                ParquetDataPayloadCard(
                    payload_name=member.logical_payload_name or "",
                    row_count=member.row_count,
                )
            )
        elif member.media_type == "application/json":
            if member.logical_byte_count is None or member.json_kind is None:
                raise ValueError("derived JSON payload metadata is incomplete")
            payloads.append(
                JsonDataPayloadCard(
                    payload_name=member.logical_payload_name or "",
                    size_bytes=member.logical_byte_count,
                    json_kind=member.json_kind.value,
                )
            )
        else:
            raise ValueError("derived payload format is unsupported")
    if not payloads or any(not item.payload_name for item in payloads):
        raise ValueError("derived package payload metadata is incomplete")
    if len({item.payload_name for item in payloads}) != len(payloads):
        raise ValueError("derived package payload names are not unique")
    return tuple(sorted(payloads, key=lambda item: item.payload_name))


def _catalog_package_card(
    package: PublishedDataPackage,
    producing_operation: PublishedCriticalOperation | None,
):
    record = package.package
    if producing_operation is None and record.package_kind is not DataPackageKind.SOURCE_L0:
        raise ValueError("Package has no producing Operation")
    operation = None if producing_operation is None else producing_operation.operation
    expected_kind = (CriticalOperationKind.SOURCE_INGESTION if record.package_kind is DataPackageKind.SOURCE_L0
                     else CriticalOperationKind.DATA_TRANSFORM)
    if operation is not None:
        if (record.package_kind is DataPackageKind.DERIVED
                and isinstance(operation.detail, IfcProductsApplyOperationDetail)):
            valid_producer = (operation.operation_kind is CriticalOperationKind.IFC_MODEL_WRITE
                and operation.status in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED})
        else:
            valid_producer = (operation.operation_kind is expected_kind
                and operation.status is CriticalOperationStatus.SUCCEEDED)
        if not valid_producer or operation.result_package_id != record.package_id:
            raise ValueError("Package producing Operation is inconsistent")
    common = {
        "producing_operation_alias": None if producing_operation is None else producing_operation.catalog_entry.alias,
        "package_alias": package.catalog_entry.alias,
        "sequence": package.catalog_entry.ordinal,
        "name": record.name,
        "created_at": record.created_at,
        "description_title": record.description_title or "",
        "summary": record.summary,
        "section_titles": record.section_titles,
        "payloads": _catalog_payload_cards(package),
    }
    if record.package_kind is DataPackageKind.SOURCE_L0:
        if record.source_filename is None:
            raise ValueError("source_l0 Catalog metadata is inconsistent")
        return SourceL0CatalogPackageCard(
            **common,
            package_kind="source_l0",
            source_filename=record.source_filename,
        )
    return DerivedCatalogPackageCard(
        **common,
        package_kind="derived",
    )


def _primary_result(package: PublishedDataPackage) -> DataPackagePrimaryResult:
    return DataPackagePrimaryResult(
        result_kind="data_package",
        package_alias=package.catalog_entry.alias,
        package_name=package.package.name,
    )


def _bounded_package_list_result(
    cards: tuple,
    *,
    complete: bool,
    next_before_sequence: int | None,
) -> DirectPackageListResult:
    return bounded_keyset_directory(
        cards,
        complete=complete,
        next_before_sequence=next_before_sequence,
        sequence_of=lambda card: card.sequence,
        build_result=lambda selected, page_complete, continuation: DirectPackageListResult(
            packages=selected,
            complete=page_complete,
            next_before_sequence=continuation,
        ),
        item_label="Package",
    )


def _description_result(description: PackageDescription) -> PackageDescriptionResult:
    return PackageDescriptionResult(
        package_alias=description.package.catalog_entry.alias,
        package_name=description.package.package.name,
        markdown=description.markdown,
    )
