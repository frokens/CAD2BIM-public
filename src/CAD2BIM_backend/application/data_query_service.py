"""Bounded, provider-neutral queries over immutable Data Packages.

The service deliberately owns no transport concerns and creates no durable
resources.  Direct MCP and the future Agent gateway both supply an
``InvocationScope`` and receive the same domain DTOs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import heapq
import math
import re
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from typing import Any, TypeAlias, TypeVar

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import ValidationError

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactParquetPayload,
    ExactPayloadResolutionError,
    ExactPayloadResolver,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_query import (
    AppidProfileBucket,
    AssignmentKeyProfileBucket,
    BinaryCellValue,
    BinaryTypedValue,
    CadEntitiesQuery,
    CadEntityCountResult,
    CadEntityQuery,
    CadEntityRowsResult,
    CadGetEntityRequest,
    CadGetEntityResult,
    CadQueryEntitiesRequest,
    CadQueryEntitiesResult,
    CadQueryXdataRequest,
    CadQueryXdataResult,
    CadXdataProfileResult,
    CadXdataQuery,
    CadXdataRowsResult,
    ColumnSchema,
    DataDescribePackageQuery,
    DataDescribePackageResult,
    DataQueryRowsRequest,
    DataQueryRowsResult,
    DataRowsQuery,
    EntityCountBy,
    EntityCountScope,
    EntityDetail,
    EntityQueryMode,
    EntityScope,
    EntitySection,
    EntitySummary,
    FilterCondition,
    FilterOperator,
    GenericQueryRow,
    GeometryElementRow,
    GroupCodeProfileBucket,
    IntegerTypedValue,
    JsonPayloadSchema,
    JsonPayloadSummary,
    LayerCountBucket,
    LayerEntityTypeCountBucket,
    NullTypedValue,
    NumberTypedValue,
    NumericInterpretation,
    NumericProfile,
    PackageSummary,
    PageInfo,
    ParquetPayloadSchema,
    ParquetPayloadSummary,
    PayloadSummary,
    RawTagRow,
    RelationshipRow,
    StringTypedValue,
    TypedValue,
    ValueEnumeration,
    ValueFrequency,
    XdataProfileDimension,
    XdataProfileScope,
    XdataQueryMode,
    XdataQueryRow,
)
from CAD2BIM_backend.domain.data_runtime import (
    DataPackageMemberRecord,
    DataPackageKind,
    JsonPayloadKind,
    PackageMemberKind,
    PublishedDataPackage,
    SOURCE_L0_LOGICAL_KEYS,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.data_query import CursorCodec, CursorCodecError
from CAD2BIM_backend.ports.data_runtime import DataRuntimeRepository


_NUMERIC_LEXEME = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_BATCH_SIZE = 65_536
_CLOCK_CHECK_INTERVAL = 4_096
_JSON_MEDIA_TYPE = "application/json"
_JSON_SCHEMA_MEDIA_TYPE = "application/schema+json"
_MAX_JSON_SCHEMA_BYTES = 65_536

_ENTITY_SUMMARY_COLUMNS = (
    "entity_key",
    "record_key",
    "entity_type",
    "layer",
    "handle",
    "entity_scope",
    "typed_geometry_supported",
    "parse_disposition",
)
_XDATA_ENTITY_COLUMNS = ("entity_key", "entity_scope", "layer", "entity_type")
_XDATA_PROFILE_COLUMNS = (
    "xdata_tag_key",
    "entity_key",
    "appid",
    "appid_group_ordinal",
    "tag_ordinal",
    "is_appid_marker",
    "group_code",
    "typed_value_kind",
    "string_value",
    "int_value",
    "float_value",
    "binary_value",
)


class DataQueryApplicationError(RuntimeError):
    """Bounded query failure suitable for an MCP ``ToolError`` projection."""

    def __init__(self, code: str, message: str) -> None:
        bounded = message.strip()[:2_000] or "Data query failed"
        super().__init__(bounded)
        self.code = code


@dataclass(frozen=True, slots=True)
class DataQueryPolicy:
    scan_row_budget: int = 2_000_000
    decoded_projection_byte_budget: int = 256 * 1024 * 1024
    describe_timeout_seconds: float = 10.0
    query_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.scan_row_budget < 1 or self.decoded_projection_byte_budget < 1:
            raise ValueError("query budgets must be positive")
        if self.describe_timeout_seconds <= 0 or self.query_timeout_seconds <= 0:
            raise ValueError("query timeouts must be positive")


@dataclass(slots=True)
class _Budget:
    policy: DataQueryPolicy
    deadline: float
    clock: Callable[[], float]
    scanned_rows: int = 0
    decoded_bytes: int = 0
    cancelled: bool = False
    pending_thread: asyncio.Task[Any] | None = None

    def charge(self, batch: pa.RecordBatch) -> None:
        self.scanned_rows += batch.num_rows
        self.decoded_bytes += batch.nbytes
        exceeded = []
        if self.scanned_rows > self.policy.scan_row_budget:
            exceeded.append("scan_rows")
        if self.decoded_bytes > self.policy.decoded_projection_byte_budget:
            exceeded.append("decoded_projection_bytes")
        if exceeded:
            raise DataQueryApplicationError(
                "QUERY_BUDGET_EXCEEDED",
                f"The query exceeded its scan budget; exceeded={','.join(exceeded)}; "
                f"scanned_rows={self.scanned_rows}; scan_row_budget={self.policy.scan_row_budget}; "
                f"decoded_bytes={self.decoded_bytes}; "
                f"decoded_projection_byte_budget={self.policy.decoded_projection_byte_budget}; "
                "matching_total=unknown",
            )
        self.check_time()

    def check_time(self) -> None:
        if self.cancelled or self.clock() > self.deadline:
            raise DataQueryApplicationError("QUERY_TIMEOUT", "The query timed out")


@dataclass(frozen=True, slots=True)
class _Continuation:
    query: Any
    last_key: tuple[Any, ...] | None
    cumulative: int
    expected_total: int | None
    claims: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class _Assignment:
    key: str
    value: _RawTypedValue


@dataclass(frozen=True, slots=True)
class _JsonPayload:
    payload_name: str
    json_kind: JsonPayloadKind
    member: DataPackageMemberRecord
    schema_member: DataPackageMemberRecord


T = TypeVar("T")
_RawTypedValue: TypeAlias = tuple[str, Any]


def _enforce_hard_timeout(*, describe: bool = False):
    """Bound the whole async service call, while ``_Budget`` bounds CPU scans."""

    def decorator(method):
        @wraps(method)
        async def wrapped(self: DataQueryService, *args, **kwargs):
            timeout = (
                self._policy.describe_timeout_seconds
                if describe
                else self._policy.query_timeout_seconds
            )
            deadline = self._clock() + timeout
            try:
                async with asyncio.timeout(timeout):
                    result = await method(self, *args, **kwargs)
            except TimeoutError as exc:
                raise DataQueryApplicationError("QUERY_TIMEOUT", "The query timed out") from exc
            # An event-loop timeout cannot pre-empt synchronous Arrow work. Every
            # scan checks its own deadline; this final check also covers methods
            # that only awaited metadata and performed no scan.
            if self._clock() > deadline:
                raise DataQueryApplicationError("QUERY_TIMEOUT", "The query timed out")
            return result

        return wrapped

    return decorator


class DataQueryService:
    """Describe exact Package members and query Parquet through one bounded runtime."""

    def __init__(
        self,
        repository: DataRuntimeRepository,
        artifacts: ArtifactService,
        cursor_codec: CursorCodec,
        policy: DataQueryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._payload_resolver = ExactPayloadResolver(repository, artifacts)
        self._cursor_codec = cursor_codec
        self._policy = policy or DataQueryPolicy()
        self._clock = clock

    @_enforce_hard_timeout(describe=True)
    async def describe_package(
        self,
        scope: InvocationScope,
        query: DataDescribePackageQuery,
    ) -> DataDescribePackageResult:
        budget = self._budget(describe=True)
        package = await self._package_by_alias(scope, query.package_alias)
        payload_bindings = self._payload_bindings(package)
        payloads = tuple(self._payload_summary(binding) for binding in payload_bindings.values())
        payload_schema = None
        if query.payload_name is not None:
            binding = payload_bindings.get(query.payload_name)
            if binding is None:
                raise DataQueryApplicationError(
                    "PAYLOAD_NOT_FOUND", "The requested payload was not found"
                )
            if isinstance(binding, _JsonPayload):
                payload_schema = JsonPayloadSchema(
                    payload_name=binding.payload_name,
                    json_kind=binding.json_kind,
                    schema_json=await self._read_exact_json_schema(binding.schema_member, budget),
                )
            else:
                member = binding.member
                paths = await self._materialize_members((member,), budget)
                try:
                    payload_schema = await self._run_thread(budget,
                        self._payload_schema,
                        paths[member.member_id],
                        member,
                        query.payload_name,
                        binding.logical_key_fields,
                        budget,
                    )
                finally:
                    self._remove_paths(paths.values(), budget)
        record = package.package
        producer = await self._repository.get_operation_for_result_package(scope, record.package_id)
        if producer is None and record.package_kind.value != "source_l0":
            raise DataQueryApplicationError("PACKAGE_INTEGRITY_ERROR", "Package has no visible producing operation")
        return DataDescribePackageResult(
            producing_operation_alias=None if producer is None else producer.catalog_entry.alias,
            package=PackageSummary(
                package_alias=package.catalog_entry.alias,
                name=record.name,
                package_kind=record.package_kind.value,
                source_filename=record.source_filename,
            ),
            payloads=payloads,
            payload_schema=payload_schema,
        )

    @_enforce_hard_timeout()
    async def query_rows(
        self,
        scope: InvocationScope,
        request: DataQueryRowsRequest,
    ) -> DataQueryRowsResult:
        continuation = self._continuation(
            scope, request.query, request.cursor, "data_query_rows", DataRowsQuery
        )
        query: DataRowsQuery = continuation.query
        budget = self._budget()
        package = await self._package_by_alias(
            scope, query.package_alias, continuation=continuation
        )
        payload_bindings = self._payload_bindings(package)
        binding = payload_bindings.get(query.payload_name)
        if binding is None:
            if continuation.claims is not None:
                raise DataQueryApplicationError(
                    "CURSOR_RESOURCE_MISMATCH", "The cursor payload no longer resolves exactly"
                )
            raise DataQueryApplicationError("PAYLOAD_NOT_FOUND", "The payload was not found")
        if isinstance(binding, _JsonPayload):
            raise DataQueryApplicationError(
                "PAYLOAD_FORMAT_UNSUPPORTED",
                "data_query_rows supports Parquet payloads only; use data_read_json for JSON",
            )
        member = binding.member
        logical_keys = binding.logical_key_fields
        self._require_cursor_shape(continuation, "rows", logical_keys)
        self._validate_resources(continuation, package, (member,))
        paths = await self._materialize_members((member,), budget)
        try:
            total, rows = await self._run_thread(budget,
                self._scan_member_page,
                paths[member.member_id],
                member,
                query,
                logical_keys,
                continuation.last_key,
                budget,
            )
        finally:
            self._remove_paths(paths.values(), budget)
        self._validate_cursor_total(continuation, total)
        page = self._page(
            scope,
            "data_query_rows",
            "rows",
            query,
            package,
            (member,),
            logical_keys,
            rows,
            total,
            continuation.cumulative,
        )
        return DataQueryRowsResult(
            package_alias=query.package_alias,
            payload_name=query.payload_name,
            logical_key_fields=logical_keys,
            rows=tuple(
                GenericQueryRow(
                    key_values={key: _cell(row[key]) for key in logical_keys},
                    values={field: _cell(row[field]) for field in query.select},
                )
                for row in rows
            ),
            page=page,
        )

    @_enforce_hard_timeout()
    async def query_entities(
        self,
        scope: InvocationScope,
        request: CadQueryEntitiesRequest,
    ) -> CadQueryEntitiesResult:
        continuation = self._continuation(
            scope, request.query, request.cursor, "cad_query_entities", CadEntitiesQuery
        )
        query: CadEntitiesQuery = continuation.query
        entity_mode = (
            "rows" if query.mode is EntityQueryMode.ROWS else f"count_by:{query.count_by.value}"
        )
        entity_ordering = (
            ("entity_key",)
            if query.mode is EntityQueryMode.ROWS
            else _entity_bucket_keys(query.count_by)
        )
        self._require_cursor_shape(continuation, entity_mode, entity_ordering)
        budget = self._budget()
        package = await self._package_by_alias(
            scope,
            query.package_alias,
            continuation=continuation,
            require_source_l0=True,
        )
        payloads = self._payload_members(package)
        names = ("entities",)
        if query.mode is EntityQueryMode.COUNT_BY and query.count_by in {
            EntityCountBy.LAYER,
            EntityCountBy.LAYER_AND_ENTITY_TYPE,
        }:
            names = ("entities", "records", "raw_tags")
        members = tuple(payloads[name] for name in names)
        self._validate_resources(continuation, package, members)
        paths = await self._materialize_members(members, budget)
        try:
            entity_rows = await self._run_thread(budget,
                self._read_member_rows,
                paths[payloads["entities"].member_id],
                payloads["entities"],
                _ENTITY_SUMMARY_COLUMNS,
                budget,
            )
            matched = _filter_rows(
                entity_rows,
                lambda row: _entity_matches(row, query),
                budget,
            )
            if query.mode is EntityQueryMode.ROWS:
                ordered = sorted(matched, key=lambda row: row["entity_key"])
                budget.check_time()
                page_rows = _after_and_slice(
                    ordered, ("entity_key",), continuation.last_key, query.page_size
                )
                total = len(ordered)
                self._validate_cursor_total(continuation, total)
                page = self._page(
                    scope,
                    "cad_query_entities",
                    "rows",
                    query,
                    package,
                    members,
                    ("entity_key",),
                    page_rows,
                    total,
                    continuation.cumulative,
                )
                return CadEntityRowsResult(
                    package_alias=query.package_alias,
                    rows=tuple(_entity_summary(row) for row in page_rows),
                    page=page,
                )

            declarations: Counter[str] = Counter()
            if query.count_by in {EntityCountBy.LAYER, EntityCountBy.LAYER_AND_ENTITY_TYPE}:
                declarations = await self._run_thread(budget,
                    self._declared_names,
                    paths,
                    payloads,
                    record_type="LAYER",
                    budget=budget,
                )
                if query.filters.layers is not None:
                    allowed = set(query.filters.layers)
                    declarations = Counter(
                        {name: count for name, count in declarations.items() if name in allowed}
                    )
            buckets = _entity_count_buckets(query, matched, declarations)
            budget.check_time()
            key_fields = _entity_bucket_keys(query.count_by)
            page_rows = _after_and_slice(
                buckets, key_fields, continuation.last_key, query.page_size
            )
            total = len(buckets)
            self._validate_cursor_total(continuation, total)
            used_layers = {row.get("layer") or "" for row in matched}
            layer_scope = query.count_by in {
                EntityCountBy.LAYER,
                EntityCountBy.LAYER_AND_ENTITY_TYPE,
            }
            summary = EntityCountScope(
                entity_scope=query.entity_scope,
                matched_entity_count=len(matched),
                declared_layer_count=len(declarations) if layer_scope else None,
                used_layer_count=len(used_layers) if layer_scope else None,
                undeclared_used_layer_count=(
                    len(used_layers - set(declarations)) if layer_scope else None
                ),
            )
            page = self._page(
                scope,
                "cad_query_entities",
                f"count_by:{query.count_by.value}",
                query,
                package,
                members,
                key_fields,
                page_rows,
                total,
                continuation.cumulative,
            )
            return CadEntityCountResult(
                package_alias=query.package_alias,
                count_by=query.count_by,
                scope=summary,
                counts=tuple(_entity_bucket_model(query.count_by, row) for row in page_rows),
                page=page,
            )
        finally:
            self._remove_paths(paths.values(), budget)

    @_enforce_hard_timeout()
    async def get_entity(
        self,
        scope: InvocationScope,
        request: CadGetEntityRequest,
    ) -> CadGetEntityResult:
        continuation = self._continuation(
            scope, request.query, request.cursor, "cad_get_entity", CadEntityQuery
        )
        query: CadEntityQuery = continuation.query
        self._require_cursor_shape(
            continuation,
            query.section.value,
            (SOURCE_L0_LOGICAL_KEYS[query.section.value][0],),
        )
        budget = self._budget()
        package = await self._package_by_alias(
            scope,
            query.package_alias,
            continuation=continuation,
            require_source_l0=True,
        )
        payloads = self._payload_members(package)
        members = (payloads["entities"], payloads[query.section.value])
        self._validate_resources(continuation, package, members)
        paths = await self._materialize_members(members, budget)
        try:
            entities = await self._run_thread(budget,
                self._read_member_rows,
                paths[payloads["entities"].member_id],
                payloads["entities"],
                None,
                budget,
            )
            entity = None
            for index, row in enumerate(entities):
                _check_periodically(index, budget)
                if row["entity_key"] == query.entity_key:
                    entity = row
                    break
            budget.check_time()
            if entity is None:
                raise DataQueryApplicationError("ENTITY_NOT_FOUND", "The CAD entity was not found")
            match_fields: Mapping[str, tuple[Any, ...]]
            if query.section in {
                EntitySection.GEOMETRY_ELEMENTS,
                EntitySection.XDATA_TAGS,
            }:
                match_fields = {"entity_key": (query.entity_key,)}
            elif query.section is EntitySection.RAW_TAGS:
                match_fields = {"record_key": (entity["record_key"],)}
            else:
                identities = (query.entity_key, entity["record_key"])
                match_fields = {
                    "subject_key": identities,
                    "resolved_target_key": identities,
                }
            section_rows = await self._run_thread(budget,
                self._read_member_rows_matching_any,
                paths[payloads[query.section.value].member_id],
                payloads[query.section.value],
                match_fields,
                budget,
            )
            if query.section is EntitySection.GEOMETRY_ELEMENTS:
                selected = section_rows
                key_fields = ("geometry_element_key",)
            elif query.section is EntitySection.XDATA_TAGS:
                selected = self._xdata_rows_with_projection(
                    section_rows,
                    {query.entity_key: entity},
                    budget,
                )
                key_fields = ("xdata_tag_key",)
            elif query.section is EntitySection.RAW_TAGS:
                selected = section_rows
                key_fields = ("raw_tag_key",)
            else:
                selected = section_rows
                key_fields = ("relationship_key",)
            ordered = sorted(selected, key=lambda row: _row_key(row, key_fields))
            budget.check_time()
            page_rows = _after_and_slice(
                ordered, key_fields, continuation.last_key, query.page_size
            )
            total = len(ordered)
            self._validate_cursor_total(continuation, total)
            page = self._page(
                scope,
                "cad_get_entity",
                query.section.value,
                query,
                package,
                members,
                key_fields,
                page_rows,
                total,
                continuation.cumulative,
            )
            return CadGetEntityResult(
                package_alias=query.package_alias,
                entity=EntityDetail(**entity),
                section=query.section,
                items=tuple(_section_model(query.section, row) for row in page_rows),
                page=page,
            )
        finally:
            self._remove_paths(paths.values(), budget)

    @_enforce_hard_timeout()
    async def query_xdata(
        self,
        scope: InvocationScope,
        request: CadQueryXdataRequest,
    ) -> CadQueryXdataResult:
        continuation = self._continuation(
            scope, request.query, request.cursor, "cad_query_xdata", CadXdataQuery
        )
        query: CadXdataQuery = continuation.query
        xdata_mode = (
            "rows"
            if query.mode is XdataQueryMode.ROWS
            else f"profile:{query.profile_dimension.value}"
        )
        xdata_ordering = (
            ("xdata_tag_key",)
            if query.mode is XdataQueryMode.ROWS
            else _profile_key_fields(query.profile_dimension)
        )
        self._require_cursor_shape(continuation, xdata_mode, xdata_ordering)
        budget = self._budget()
        package = await self._package_by_alias(
            scope,
            query.package_alias,
            continuation=continuation,
            require_source_l0=True,
        )
        payloads = self._payload_members(package)
        names = ["entities", "xdata_tags"]
        if (
            query.mode is XdataQueryMode.PROFILE
            and query.profile_dimension is XdataProfileDimension.APPID
        ):
            names.extend(("records", "raw_tags"))
        members = tuple(payloads[name] for name in names)
        self._validate_resources(continuation, package, members)
        paths = await self._materialize_members(members, budget)
        try:
            entity_rows = await self._run_thread(budget,
                self._read_member_rows,
                paths[payloads["entities"].member_id],
                payloads["entities"],
                _XDATA_ENTITY_COLUMNS,
                budget,
            )
            entities = {
                row["entity_key"]: row
                for row in _filter_rows(
                    entity_rows,
                    lambda row: _xdata_entity_matches(row, query),
                    budget,
                )
            }
            xdata_columns = None if query.mode is XdataQueryMode.ROWS else _XDATA_PROFILE_COLUMNS
            raw_xdata = await self._run_thread(budget,
                self._read_member_rows,
                paths[payloads["xdata_tags"].member_id],
                payloads["xdata_tags"],
                xdata_columns,
                budget,
            )
            raw_xdata = _filter_rows(
                raw_xdata,
                lambda row: row["entity_key"] in entities,
                budget,
            )
            projected = self._xdata_rows_with_projection(
                raw_xdata,
                entities,
                budget,
                include_typed_value=query.mode is XdataQueryMode.ROWS,
            )
            if query.mode is XdataQueryMode.ROWS:
                assignment_value_filter = None
                if (
                    query.xdata_filters is not None
                    and query.xdata_filters.assignment_values is not None
                ):
                    assignment_value_filter = {
                        _typed_identity(_raw_from_model(item))
                        for item in query.xdata_filters.assignment_values
                    }
                selected = _filter_rows(
                    projected,
                    lambda row: _xdata_row_matches(
                        row,
                        query,
                        assignment_value_filter,
                    ),
                    budget,
                )
                ordered = sorted(selected, key=lambda row: row["xdata_tag_key"])
                budget.check_time()
                page_rows = _after_and_slice(
                    ordered, ("xdata_tag_key",), continuation.last_key, query.page_size
                )
                total = len(ordered)
                self._validate_cursor_total(continuation, total)
                page = self._page(
                    scope,
                    "cad_query_xdata",
                    "rows",
                    query,
                    package,
                    members,
                    ("xdata_tag_key",),
                    page_rows,
                    total,
                    continuation.cumulative,
                )
                return CadXdataRowsResult(
                    package_alias=query.package_alias,
                    rows=tuple(_xdata_model(row) for row in page_rows),
                    page=page,
                )

            profiles, profile_scope = await self._run_thread(budget,
                self._xdata_profiles,
                query,
                projected,
                entities,
                paths,
                payloads,
                budget,
            )
            key_fields = _profile_key_fields(query.profile_dimension)
            page_rows = _after_and_slice(
                profiles, key_fields, continuation.last_key, query.page_size
            )
            total = len(profiles)
            self._validate_cursor_total(continuation, total)
            page = self._page(
                scope,
                "cad_query_xdata",
                f"profile:{query.profile_dimension.value}",
                query,
                package,
                members,
                key_fields,
                page_rows,
                total,
                continuation.cumulative,
            )
            return CadXdataProfileResult(
                package_alias=query.package_alias,
                profile_dimension=query.profile_dimension,
                scope=profile_scope,
                profiles=tuple(_profile_model(query.profile_dimension, row) for row in page_rows),
                page=page,
            )
        finally:
            self._remove_paths(paths.values(), budget)

    def _budget(self, *, describe: bool = False) -> _Budget:
        timeout = (
            self._policy.describe_timeout_seconds
            if describe
            else self._policy.query_timeout_seconds
        )
        return _Budget(self._policy, self._clock() + timeout, self._clock)

    async def _package_by_alias(
        self,
        scope: InvocationScope,
        alias: str,
        *,
        continuation: _Continuation | None = None,
        require_source_l0: bool = False,
    ) -> PublishedDataPackage:
        try:
            package = await self._payload_resolver.package_by_alias(scope, alias)
        except ExactPayloadResolutionError as exc:
            code = (
                "CURSOR_RESOURCE_MISMATCH"
                if exc.code == "PACKAGE_NOT_FOUND" and continuation and continuation.claims
                else exc.code
            )
            raise DataQueryApplicationError(code, str(exc)) from exc
        if require_source_l0 and package.package.package_kind is not DataPackageKind.SOURCE_L0:
            raise DataQueryApplicationError(
                "PACKAGE_KIND_MISMATCH",
                "This CAD query requires a source_l0 package; use Data tools for derived packages",
            )
        return package

    def _payload_members(self, package: PublishedDataPackage) -> dict[str, DataPackageMemberRecord]:
        return {name: binding.member for name, binding in self._payload_bindings(package).items()}

    def _payload_bindings(
        self,
        package: PublishedDataPackage,
    ) -> Mapping[str, ExactParquetPayload | _JsonPayload]:
        if package.package.package_kind is DataPackageKind.SOURCE_L0:
            return self._parquet_payload_bindings(package)
        if package.package.package_kind is not DataPackageKind.DERIVED:
            raise DataQueryApplicationError(
                "PACKAGE_KIND_MISMATCH", "The package kind has no queryable payload contract"
            )

        payload_members = tuple(
            member for member in package.members if member.member_kind is PackageMemberKind.PAYLOAD
        )
        if not payload_members:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "The derived package has no queryable payload"
            )
        unsupported = tuple(
            member
            for member in payload_members
            if member.media_type not in {_JSON_MEDIA_TYPE, "application/vnd.apache.parquet"}
        )
        if unsupported:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "A derived package payload format is unsupported"
            )

        result: dict[str, ExactParquetPayload | _JsonPayload] = {}
        if any(member.media_type == "application/vnd.apache.parquet" for member in payload_members):
            result.update(self._parquet_payload_bindings(package))

        json_payloads: dict[str, DataPackageMemberRecord] = {}
        for member in payload_members:
            if member.media_type != _JSON_MEDIA_TYPE:
                continue
            payload_name = member.logical_payload_name
            json_kind = member.json_kind
            if (
                not payload_name
                or payload_name in result
                or payload_name in json_payloads
                or member.member_name != f"payloads/{payload_name}.json"
                or not member.logical_byte_count
                or member.row_count is not None
                or member.logical_key_fields
                or member.arrow_schema
                or json_kind not in {JsonPayloadKind.DOCUMENT, JsonPayloadKind.RECORDS}
                or not self._member_belongs_to_package(package, member)
            ):
                raise DataQueryApplicationError(
                    "PACKAGE_INTEGRITY_ERROR", "A derived JSON payload has invalid metadata"
                )
            json_payloads[payload_name] = member

        schema_members: dict[str, DataPackageMemberRecord] = {}
        for member in package.members:
            if member.member_kind is not PackageMemberKind.SCHEMA:
                continue
            prefix = "schemas/"
            suffix = ".schema.json"
            if (
                not member.member_name.startswith(prefix)
                or not member.member_name.endswith(suffix)
                or member.media_type != _JSON_SCHEMA_MEDIA_TYPE
                or not self._member_belongs_to_package(package, member)
            ):
                raise DataQueryApplicationError(
                    "PACKAGE_INTEGRITY_ERROR", "A JSON schema member has invalid metadata"
                )
            payload_name = member.member_name[len(prefix) : -len(suffix)]
            if not payload_name or payload_name in schema_members:
                raise DataQueryApplicationError(
                    "PACKAGE_INTEGRITY_ERROR", "A JSON schema member is ambiguous"
                )
            schema_members[payload_name] = member

        if set(schema_members) != set(json_payloads):
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "Every JSON payload must have exactly one matching schema member",
            )
        for payload_name, member in json_payloads.items():
            result[payload_name] = _JsonPayload(
                payload_name=payload_name,
                json_kind=member.json_kind,
                member=member,
                schema_member=schema_members[payload_name],
            )
        return dict(sorted(result.items()))

    def _parquet_payload_bindings(
        self,
        package: PublishedDataPackage,
    ) -> Mapping[str, ExactParquetPayload]:
        try:
            return self._payload_resolver.parquet_payloads(package)
        except ExactPayloadResolutionError as exc:
            raise DataQueryApplicationError(exc.code, str(exc)) from exc

    @staticmethod
    def _member_belongs_to_package(
        package: PublishedDataPackage,
        member: DataPackageMemberRecord,
    ) -> bool:
        record = package.package
        return (
            member.package_id == record.package_id
            and member.task_id == record.task_id
            and member.tenant_id == record.tenant_id
            and member.owner_id == record.owner_id
        )

    def _payload_summary(
        self,
        binding: ExactParquetPayload | _JsonPayload,
    ) -> PayloadSummary:
        if isinstance(binding, _JsonPayload):
            size_bytes = binding.member.logical_byte_count
            if size_bytes is None:
                raise DataQueryApplicationError(
                    "PACKAGE_INTEGRITY_ERROR", "A JSON payload is missing its byte count"
                )
            return JsonPayloadSummary(
                payload_name=binding.payload_name,
                size_bytes=size_bytes,
                json_kind=binding.json_kind,
            )
        return ParquetPayloadSummary(
            payload_name=binding.payload_name,
            row_count=self._required_row_count(binding.member),
            logical_key_fields=binding.logical_key_fields,
        )

    @staticmethod
    def _required_row_count(member: DataPackageMemberRecord) -> int:
        if member.row_count is None:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "A Parquet payload is missing its row count"
            )
        return member.row_count

    async def _materialize_members(
        self,
        members: Sequence[DataPackageMemberRecord],
        budget: _Budget,
    ) -> dict[str, Path]:
        try:
            return await self._payload_resolver.materialize_members(
                members,
                check_budget=budget.check_time,
            )
        except ExactPayloadResolutionError as exc:
            raise DataQueryApplicationError(exc.code, str(exc)) from exc

    async def _read_exact_json_schema(
        self,
        member: DataPackageMemberRecord,
        budget: _Budget,
    ) -> str:
        budget.check_time()
        try:
            stream = await self._artifacts.stream_version(
                member.owner_id,
                member.artifact.artifact_id,
                member.artifact.version_id,
            )
        except (ConflictError, NotFoundError) as exc:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "The JSON schema exact Artifact version is unavailable",
            ) from exc
        artifact = stream.artifact
        if (
            artifact.owner_id != member.owner_id
            or artifact.task_id != member.task_id
            or artifact.artifact_id != member.artifact.artifact_id
            or artifact.version_id != member.artifact.version_id
            or artifact.sha256 != member.artifact.sha256
            or artifact.content_type != member.media_type
            or not 1 <= artifact.size_bytes <= _MAX_JSON_SCHEMA_BYTES
        ):
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "The JSON schema member does not match its Artifact"
            )

        digest = hashlib.sha256()
        content = bytearray()
        try:
            chunks = stream.chunks.__aiter__()
            while True:
                budget.check_time()
                try:
                    chunk = await anext(chunks)
                except StopAsyncIteration:
                    break
                if not isinstance(chunk, bytes):
                    raise TypeError("Artifact stream returned non-bytes")
                content.extend(chunk)
                if len(content) > _MAX_JSON_SCHEMA_BYTES:
                    raise ValueError("JSON schema exceeds byte limit")
                digest.update(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "The JSON schema exact Artifact stream failed"
            ) from exc
        if len(content) != artifact.size_bytes or digest.hexdigest() != member.artifact.sha256:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "The JSON schema exact bytes failed integrity validation"
            )
        try:
            schema_json = bytes(content).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "The JSON schema is not valid UTF-8"
            ) from exc
        if not schema_json:
            raise DataQueryApplicationError("PACKAGE_INTEGRITY_ERROR", "The JSON schema is empty")
        budget.check_time()
        return schema_json

    @staticmethod
    def _remove_paths(paths: Iterable[Path], budget: _Budget | None = None) -> None:
        owned_paths = tuple(paths)
        pending = None if budget is None else budget.pending_thread
        if pending is None:
            ExactPayloadResolver.remove_paths(owned_paths)
            return

        def cleanup(completed: asyncio.Task[Any]) -> None:
            try:
                completed.result()
            except BaseException:
                pass
            finally:
                ExactPayloadResolver.remove_paths(owned_paths)

        pending.add_done_callback(cleanup)

    @staticmethod
    async def _run_thread(scan_budget: _Budget, function: Callable, *args, **kwargs):
        def run():
            scan_budget.check_time()
            return function(*args, **kwargs)

        task = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            scan_budget.cancelled = True
            scan_budget.pending_thread = task
            raise

    def _open_parquet(self, path: Path, member: DataPackageMemberRecord) -> pq.ParquetFile:
        try:
            parquet = pq.ParquetFile(path)
        except (OSError, pa.ArrowException) as exc:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "A package payload is not valid Parquet"
            ) from exc
        if parquet.metadata.num_rows != self._required_row_count(member):
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "Parquet row count differs from the package member"
            )
        declared_schema = tuple(getattr(member, "arrow_schema", ()))
        if declared_schema:
            actual_schema = tuple(
                (field.name, str(field.type), field.nullable) for field in parquet.schema_arrow
            )
            expected_schema = tuple(
                (column.name, column.type, column.nullable) for column in declared_schema
            )
            if actual_schema != expected_schema:
                raise DataQueryApplicationError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "Parquet schema differs from the package member metadata",
                )
        return parquet

    def _payload_schema(
        self,
        path: Path,
        member: DataPackageMemberRecord,
        payload_name: str,
        logical_key_fields: tuple[str, ...],
        budget: _Budget,
    ) -> ParquetPayloadSchema:
        budget.check_time()
        parquet = self._open_parquet(path, member)
        result = ParquetPayloadSchema(
            payload_name=payload_name,
            logical_key_fields=logical_key_fields,
            columns=tuple(
                ColumnSchema(
                    name=field.name,
                    type=str(field.type),
                    nullable=field.nullable,
                )
                for field in parquet.schema_arrow
            ),
        )
        budget.check_time()
        return result

    def _read_member_rows(
        self,
        path: Path,
        member: DataPackageMemberRecord,
        columns: Sequence[str] | None,
        budget: _Budget,
    ) -> list[dict[str, Any]]:
        return self._read_rows(self._open_parquet(path, member), columns, budget)

    def _read_member_rows_matching_any(
        self,
        path: Path,
        member: DataPackageMemberRecord,
        match_fields: Mapping[str, tuple[Any, ...]],
        budget: _Budget,
    ) -> list[dict[str, Any]]:
        """Scan every row for budget accounting but decode only matching rows."""

        parquet = self._open_parquet(path, member)
        rows: list[dict[str, Any]] = []
        try:
            for batch in parquet.iter_batches(batch_size=_BATCH_SIZE, use_threads=False):
                budget.charge(batch)
                mask: pa.Array | None = None
                for field_name, values in match_fields.items():
                    field = batch.schema.field(field_name)
                    candidate = pc.is_in(
                        batch.column(batch.schema.get_field_index(field_name)),
                        value_set=pa.array(values, type=field.type),
                    )
                    candidate = pc.fill_null(candidate, False)
                    mask = candidate if mask is None else pc.or_(mask, candidate)
                if mask is None:
                    raise DataQueryApplicationError(
                        "PACKAGE_INTEGRITY_ERROR", "No entity match fields were provided"
                    )
                rows.extend(batch.filter(mask).to_pylist())
                budget.check_time()
        except DataQueryApplicationError:
            raise
        except (KeyError, OSError, pa.ArrowException) as exc:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "A Parquet payload could not be scanned"
            ) from exc
        return rows

    def _scan_member_page(
        self,
        path: Path,
        member: DataPackageMemberRecord,
        query: DataRowsQuery,
        logical_keys: tuple[str, ...],
        last_key: tuple[Any, ...] | None,
        budget: _Budget,
    ) -> tuple[int, list[dict[str, Any]]]:
        parquet = self._open_parquet(path, member)
        self._validate_row_query(query, parquet.schema_arrow, logical_keys)
        columns = tuple(
            dict.fromkeys((*logical_keys, *query.select, *(item.field for item in query.filters)))
        )
        try:
            return self._scan_generic_page(parquet, columns, query, logical_keys, last_key, budget)
        except DataQueryApplicationError as exc:
            if exc.code != "QUERY_BUDGET_EXCEEDED":
                raise
            raise DataQueryApplicationError(
                exc.code,
                f"{exc}. Required logical-key fields in this projection: {', '.join(logical_keys)}; "
                "select cannot remove these fields; page_size only limits returned rows. "
                "For a query that still exceeds the scan budget, use data_query_sql with the "
                "same filters and only the necessary result columns.",
            ) from None

    def _read_rows(
        self,
        parquet: pq.ParquetFile,
        columns: Sequence[str] | None,
        budget: _Budget,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            for batch in parquet.iter_batches(
                batch_size=_BATCH_SIZE,
                columns=columns,
                use_threads=False,
            ):
                budget.charge(batch)
                rows.extend(batch.to_pylist())
                budget.check_time()
        except DataQueryApplicationError:
            raise
        except (OSError, pa.ArrowException) as exc:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "A Parquet payload could not be scanned"
            ) from exc
        return rows

    def _scan_generic_page(
        self,
        parquet: pq.ParquetFile,
        columns: Sequence[str],
        query: DataRowsQuery,
        logical_keys: tuple[str, ...],
        last_key: tuple[Any, ...] | None,
        budget: _Budget,
    ) -> tuple[int, list[dict[str, Any]]]:
        total = 0
        candidates: list[tuple[_HeapKey, int, dict[str, Any]]] = []
        sequence = 0
        try:
            for batch in parquet.iter_batches(
                batch_size=_BATCH_SIZE, columns=columns, use_threads=False
            ):
                budget.charge(batch)
                batch_rows = batch.to_pylist()
                budget.check_time()
                for index, row in enumerate(batch_rows):
                    _check_periodically(index, budget)
                    if not all(_filter_matches(row, condition) for condition in query.filters):
                        continue
                    total += 1
                    key = _row_key(row, logical_keys)
                    if last_key is not None and key <= last_key:
                        continue
                    item = (_HeapKey(key), sequence, row)
                    sequence += 1
                    if len(candidates) < query.page_size:
                        heapq.heappush(candidates, item)
                    elif key < candidates[0][0].key:
                        heapq.heapreplace(candidates, item)
                budget.check_time()
        except DataQueryApplicationError:
            raise
        except (OSError, pa.ArrowException) as exc:
            raise DataQueryApplicationError(
                "PACKAGE_INTEGRITY_ERROR", "A Parquet payload could not be scanned"
            ) from exc
        return total, [item[2] for item in sorted(candidates, key=lambda item: item[0].key)]

    @staticmethod
    def _validate_row_query(
        query: DataRowsQuery,
        schema: pa.Schema,
        logical_keys: tuple[str, ...],
    ) -> None:
        fields = {field.name: field for field in schema}
        for name in (*query.select, *logical_keys):
            if name not in fields:
                raise DataQueryApplicationError("FIELD_NOT_FOUND", f"Field {name!r} was not found")
        for condition in query.filters:
            field = fields.get(condition.field)
            if field is None:
                raise DataQueryApplicationError(
                    "FIELD_NOT_FOUND", f"Field {condition.field!r} was not found"
                )
            _validate_filter_type(field.type, condition)

    def _continuation(
        self,
        scope: InvocationScope,
        query: T | None,
        cursor: str | None,
        tool: str,
        query_type: type[T],
    ) -> _Continuation:
        if query is not None:
            return _Continuation(query, None, 0, None, None)
        if cursor is None:
            raise DataQueryApplicationError("INVALID_ARGUMENT", "query XOR cursor is required")
        try:
            claims = self._cursor_codec.decode(cursor)
        except CursorCodecError as exc:
            raise DataQueryApplicationError("CURSOR_INVALID", "The cursor is invalid") from exc
        if not isinstance(claims.get("tool"), str):
            raise DataQueryApplicationError("CURSOR_INVALID", "The cursor is invalid")
        if claims["tool"] != tool:
            raise DataQueryApplicationError(
                "CURSOR_TOOL_MISMATCH", "The cursor belongs to a different tool"
            )
        if claims.get("scope") != _scope_digest(scope):
            raise DataQueryApplicationError(
                "CURSOR_SCOPE_MISMATCH", "The cursor belongs to a different invocation scope"
            )
        try:
            decoded_query = query_type.model_validate(claims["query"])
            expected_fingerprint = _query_fingerprint(decoded_query)
            last_key_value = claims["last_key"]
            last_key = tuple(last_key_value) if isinstance(last_key_value, list) else None
            cumulative = claims["cumulative"]
            total = claims["total"]
            if (
                claims.get("query_fingerprint") != expected_fingerprint
                or not isinstance(cumulative, int)
                or isinstance(cumulative, bool)
                or cumulative < 0
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
                or last_key is None
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise DataQueryApplicationError("CURSOR_INVALID", "The cursor is invalid") from exc
        return _Continuation(decoded_query, last_key, cumulative, total, claims)

    def _validate_resources(
        self,
        continuation: _Continuation,
        package: PublishedDataPackage,
        members: Sequence[DataPackageMemberRecord],
    ) -> None:
        if continuation.claims is None:
            return
        expected = _resource_claim(package, members)
        if continuation.claims.get("resource") != expected:
            raise DataQueryApplicationError(
                "CURSOR_RESOURCE_MISMATCH", "The cursor resource binding no longer matches"
            )

    @staticmethod
    def _require_cursor_shape(
        continuation: _Continuation,
        mode: str,
        ordering: tuple[str, ...],
    ) -> None:
        claims = continuation.claims
        if claims is None:
            return
        if (
            claims.get("mode") != mode
            or claims.get("ordering") != list(ordering)
            or claims.get("page_size") != continuation.query.page_size
            or continuation.last_key is None
            or len(continuation.last_key) != len(ordering)
        ):
            raise DataQueryApplicationError("CURSOR_INVALID", "The cursor is invalid")

    @staticmethod
    def _validate_cursor_total(continuation: _Continuation, total: int) -> None:
        if continuation.expected_total is not None and continuation.expected_total != total:
            raise DataQueryApplicationError(
                "CURSOR_RESOURCE_MISMATCH", "The immutable query result changed"
            )

    def _page(
        self,
        scope: InvocationScope,
        tool: str,
        mode: str,
        query: Any,
        package: PublishedDataPackage,
        members: Sequence[DataPackageMemberRecord],
        key_fields: tuple[str, ...],
        rows: Sequence[Mapping[str, Any]],
        total: int,
        cumulative: int,
    ) -> PageInfo:
        returned = len(rows)
        next_cumulative = cumulative + returned
        remaining = total - next_cumulative
        if remaining < 0:
            raise DataQueryApplicationError("CURSOR_INVALID", "Cursor page counts are invalid")
        next_cursor = None
        if remaining:
            if not rows:
                raise DataQueryApplicationError("CURSOR_INVALID", "Cursor cannot advance")
            try:
                next_cursor = self._cursor_codec.encode(
                    {
                        "tool": tool,
                        "mode": mode,
                        "scope": _scope_digest(scope),
                        "resource": _resource_claim(package, members),
                        "query": query.model_dump(mode="json", exclude_none=False),
                        "query_fingerprint": _query_fingerprint(query),
                        "ordering": list(key_fields),
                        "page_size": query.page_size,
                        "last_key": list(_row_key(rows[-1], key_fields)),
                        "cumulative": next_cumulative,
                        "total": total,
                    }
                )
            except CursorCodecError as exc:
                raise DataQueryApplicationError(
                    "INVALID_ARGUMENT",
                    "The continuation exceeds cursor capacity; use smaller IN batches "
                    "or data_query_sql with the same filters",
                ) from exc
        return PageInfo(
            total_matched_count=total,
            returned_count=returned,
            remaining_count=remaining,
            complete=remaining == 0,
            next_cursor=next_cursor,
        )

    def _declared_names(
        self,
        paths: Mapping[str, Path],
        payloads: Mapping[str, DataPackageMemberRecord],
        *,
        record_type: str,
        budget: _Budget,
    ) -> Counter[str]:
        records = self._read_rows(
            self._open_parquet(paths[payloads["records"].member_id], payloads["records"]),
            ("record_key", "section", "record_type"),
            budget,
        )
        keys: set[str] = set()
        for index, row in enumerate(records):
            _check_periodically(index, budget)
            if row["section"] == "TABLES" and row["record_type"] == record_type:
                keys.add(row["record_key"])
        budget.check_time()
        tags = self._read_rows(
            self._open_parquet(paths[payloads["raw_tags"].member_id], payloads["raw_tags"]),
            ("record_key", "group_code", "string_value"),
            budget,
        )
        declarations: Counter[str] = Counter()
        for index, row in enumerate(tags):
            _check_periodically(index, budget)
            if (
                row["record_key"] in keys
                and row["group_code"] == 2
                and row["string_value"] is not None
            ):
                declarations[row["string_value"]] += 1
        budget.check_time()
        return declarations

    def _xdata_rows_with_projection(
        self,
        raw_rows: Sequence[dict[str, Any]],
        entities: Mapping[str, Mapping[str, Any]],
        budget: _Budget,
        *,
        include_typed_value: bool = True,
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
        for index, row in enumerate(raw_rows):
            _check_periodically(index, budget)
            groups[(row["entity_key"], row["appid"], row["appid_group_ordinal"])].append(row)
        assignments: dict[str, _Assignment] = {}
        for index, ((_entity_key, appid, _ordinal), rows) in enumerate(groups.items()):
            _check_periodically(index, budget)
            keyed = 0
            nonmarkers = [row for row in rows if not row["is_appid_marker"]]
            for row in nonmarkers:
                if row["group_code"] == 1000 and isinstance(row.get("string_value"), str):
                    key, separator, value = row["string_value"].partition("=")
                    key = key.strip()
                    if separator and key:
                        assignments[row["xdata_tag_key"]] = _Assignment(
                            key, ("string", value.strip())
                        )
                        keyed += 1
            if len(nonmarkers) == 1 and keyed == 0:
                row = nonmarkers[0]
                assignments[row["xdata_tag_key"]] = _Assignment(
                    appid,
                    _raw_typed_value(row),
                )
        result: list[dict[str, Any]] = []
        for index, row in enumerate(raw_rows):
            _check_periodically(index, budget)
            entity = entities[row["entity_key"]]
            assignment = assignments.get(row["xdata_tag_key"])
            row["layer"] = entity.get("layer")
            row["entity_type"] = entity["entity_type"]
            if include_typed_value:
                row["_typed_value"] = _raw_typed_value(row)
            row["assignment_key"] = assignment.key if assignment else None
            row["_assignment_value"] = assignment.value if assignment else None
            result.append(row)
        budget.check_time()
        return result

    def _xdata_profiles(
        self,
        query: CadXdataQuery,
        rows: Sequence[dict[str, Any]],
        entities: Mapping[str, Mapping[str, Any]],
        paths: Mapping[str, Path],
        payloads: Mapping[str, DataPackageMemberRecord],
        budget: _Budget,
    ) -> tuple[list[dict[str, Any]], XdataProfileScope]:
        nonmarkers = _filter_rows(rows, lambda row: not row["is_appid_marker"], budget)
        projected = _filter_rows(
            nonmarkers,
            lambda row: row["assignment_key"] is not None,
            budget,
        )
        entities_with: set[str] = set()
        for index, row in enumerate(rows):
            _check_periodically(index, budget)
            entities_with.add(row["entity_key"])
        scope = XdataProfileScope(
            matched_entity_count=len(entities),
            entities_with_xdata_count=len(entities_with),
            raw_non_marker_tag_count=len(nonmarkers),
            projected_assignment_count=len(projected),
            unprojected_non_marker_tag_count=len(nonmarkers) - len(projected),
        )
        if query.profile_dimension is XdataProfileDimension.APPID:
            declarations = self._declared_names(paths, payloads, record_type="APPID", budget=budget)
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for index, row in enumerate(rows):
                _check_periodically(index, budget)
                grouped[row["appid"]].append(row)
            names = set(grouped)
            if query.include_declared_unused_appids:
                names.update(declarations)
            profiles = []
            for index, appid in enumerate(sorted(names)):
                _check_periodically(index, budget)
                values = grouped.get(appid, [])
                profiles.append(
                    {
                        "appid": appid,
                        "declared_in_appid_table": appid in declarations,
                        "declaration_count": declarations[appid],
                        "entity_count": len({row["entity_key"] for row in values}),
                        "appid_group_count": len(
                            {(row["entity_key"], row["appid_group_ordinal"]) for row in values}
                        ),
                        "marker_count": sum(row["is_appid_marker"] for row in values),
                        "payload_tag_count": sum(not row["is_appid_marker"] for row in values),
                    }
                )
            budget.check_time()
            return profiles, scope
        if query.profile_dimension is XdataProfileDimension.GROUP_CODE:
            grouped_codes: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for index, row in enumerate(rows):
                _check_periodically(index, budget)
                grouped_codes[row["group_code"]].append(row)
            profiles = []
            for index, code in enumerate(sorted(grouped_codes)):
                _check_periodically(index, budget)
                values = grouped_codes[code]
                profiles.append(
                    {
                        "group_code": code,
                        "is_appid_marker_code": code == 1001,
                        "tag_count": len(values),
                        "entity_count": len({row["entity_key"] for row in values}),
                        "typed_value_kind_counts": dict(
                            sorted(Counter(_raw_typed_value(row)[0] for row in values).items())
                        ),
                    }
                )
            budget.check_time()
            return profiles, scope

        allowed = set(query.assignment_keys) if query.assignment_keys is not None else None
        assignments: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for index, row in enumerate(projected):
            _check_periodically(index, budget)
            key = row["assignment_key"]
            if allowed is None or key in allowed:
                assignments[key].append(row)
        profiles = []
        for index, (key, values) in enumerate(sorted(assignments.items())):
            _check_periodically(index, budget)
            profiles.append(_assignment_profile(key, values, len(entities), budget))
        budget.check_time()
        return profiles, scope


@dataclass(frozen=True, slots=True)
class _HeapKey:
    """Reverse ordering lets a min-heap retain the smallest requested keys."""

    key: tuple[Any, ...]

    def __lt__(self, other: _HeapKey) -> bool:
        return self.key > other.key


def _scope_digest(scope: InvocationScope) -> str:
    # command_id identifies one call and must not prevent a later continuation.
    authority = {
        "tenant_id": scope.identity.tenant_id,
        "principal_id": scope.identity.principal_id,
        "task_id": scope.identity.task_id,
        "actor_kind": scope.identity.actor_kind.value,
        "owner_id": scope.owner_id,
        "run_id": scope.run_id,
        "execution_mode": scope.execution_mode.value,
        "catalog_frontier": scope.catalog_frontier,
    }
    return hashlib.sha256(canonical_json(authority).encode()).hexdigest()


def _resource_claim(
    package: PublishedDataPackage,
    members: Sequence[DataPackageMemberRecord],
) -> dict[str, Any]:
    return {
        "package_id": package.package.package_id,
        "package_alias": package.catalog_entry.alias,
        "members": [
            {
                "member_name": member.member_name,
                "artifact_id": member.artifact.artifact_id,
                "version_id": member.artifact.version_id,
                "sha256": member.artifact.sha256,
            }
            for member in sorted(members, key=lambda item: item.member_name)
        ],
    }


def _query_fingerprint(query: Any) -> str:
    value = query.model_dump(mode="json", exclude_none=False)
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _check_periodically(index: int, budget: _Budget) -> None:
    if index % _CLOCK_CHECK_INTERVAL == 0:
        budget.check_time()


def _filter_rows(
    rows: Sequence[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    budget: _Budget,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        _check_periodically(index, budget)
        if predicate(row):
            selected.append(row)
    budget.check_time()
    return selected


def _row_key(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row[field] for field in fields)


def _after_and_slice(
    rows: Sequence[dict[str, Any]],
    key_fields: tuple[str, ...],
    last_key: tuple[Any, ...] | None,
    page_size: int,
) -> list[dict[str, Any]]:
    start = 0
    if last_key is not None:
        start = bisect_right(rows, last_key, key=lambda row: _row_key(row, key_fields))
    return list(rows[start : start + page_size])


def _cell(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return BinaryCellValue(
            kind="binary_base64",
            base64=base64.b64encode(bytes(value)).decode("ascii"),
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise DataQueryApplicationError(
            "PACKAGE_INTEGRITY_ERROR", "A payload contains a non-finite number"
        )
    if isinstance(value, (list, tuple)):
        return [_cell(item) for item in value]
    if isinstance(value, dict):
        return {key: _cell(item) for key, item in value.items()}
    return value


def _validate_filter_type(data_type: pa.DataType, condition: FilterCondition) -> None:
    if condition.op in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
        return
    values = condition.value if isinstance(condition.value, tuple) else (condition.value,)
    for value in values:
        valid = False
        if pa.types.is_boolean(data_type):
            valid = type(value) is bool
        elif pa.types.is_integer(data_type):
            valid = type(value) is int
        elif pa.types.is_floating(data_type):
            valid = type(value) in {int, float} and math.isfinite(float(value))
        elif pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
            valid = type(value) is str
        elif pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type):
            valid = isinstance(value, BinaryCellValue)
        if not valid:
            raise DataQueryApplicationError(
                "FILTER_TYPE_MISMATCH",
                f"Filter value is incompatible with field {condition.field!r}",
            )
    if condition.op in {
        FilterOperator.LT,
        FilterOperator.LTE,
        FilterOperator.GT,
        FilterOperator.GTE,
    }:
        if pa.types.is_boolean(data_type) or pa.types.is_binary(data_type):
            raise DataQueryApplicationError(
                "FILTER_TYPE_MISMATCH", "The selected field does not support ordered comparison"
            )


def _filter_matches(row: Mapping[str, Any], condition: FilterCondition) -> bool:
    actual = row.get(condition.field)
    if condition.op is FilterOperator.IS_NULL:
        return actual is None
    if condition.op is FilterOperator.IS_NOT_NULL:
        return actual is not None
    expected: Any = condition.value
    if isinstance(expected, BinaryCellValue):
        expected = base64.b64decode(expected.base64)
    if isinstance(expected, tuple):
        expected = tuple(
            base64.b64decode(value.base64) if isinstance(value, BinaryCellValue) else value
            for value in expected
        )
    if actual is None:
        return False
    if condition.op is FilterOperator.EQ:
        return actual == expected
    if condition.op is FilterOperator.IN:
        return actual in expected
    if condition.op is FilterOperator.LT:
        return actual < expected
    if condition.op is FilterOperator.LTE:
        return actual <= expected
    if condition.op is FilterOperator.GT:
        return actual > expected
    return actual >= expected


def _entity_matches(row: Mapping[str, Any], query: CadEntitiesQuery) -> bool:
    if (
        query.entity_scope is EntityScope.TOP_LEVEL_MODELSPACE
        and row["entity_scope"] != EntityScope.TOP_LEVEL_MODELSPACE.value
    ):
        return False
    filters = query.filters
    return (
        (filters.layers is None or row.get("layer") in filters.layers)
        and (filters.entity_types is None or row["entity_type"] in filters.entity_types)
        and (filters.entity_keys is None or row["entity_key"] in filters.entity_keys)
        and (filters.handles is None or row.get("handle") in filters.handles)
        and (
            filters.typed_geometry_supported is None
            or row["typed_geometry_supported"] is filters.typed_geometry_supported
        )
    )


def _xdata_entity_matches(row: Mapping[str, Any], query: CadXdataQuery) -> bool:
    filters = query.entity_filters
    if (
        filters.entity_scope is EntityScope.TOP_LEVEL_MODELSPACE
        and row["entity_scope"] != EntityScope.TOP_LEVEL_MODELSPACE.value
    ):
        return False
    return (
        (filters.layers is None or row.get("layer") in filters.layers)
        and (filters.entity_types is None or row["entity_type"] in filters.entity_types)
        and (filters.entity_keys is None or row["entity_key"] in filters.entity_keys)
    )


def _entity_summary(row: Mapping[str, Any]) -> EntitySummary:
    return EntitySummary(
        entity_key=row["entity_key"],
        record_key=row["record_key"],
        entity_type=row["entity_type"],
        layer=row.get("layer"),
        handle=row.get("handle"),
        entity_scope=row["entity_scope"],
        typed_geometry_supported=row["typed_geometry_supported"],
        parse_disposition=row["parse_disposition"],
    )


def _entity_bucket_keys(count_by: EntityCountBy) -> tuple[str, ...]:
    return {
        EntityCountBy.LAYER: ("layer",),
        EntityCountBy.ENTITY_TYPE: ("entity_type",),
        EntityCountBy.LAYER_AND_ENTITY_TYPE: ("layer", "entity_type"),
    }[count_by]


def _entity_count_buckets(
    query: CadEntitiesQuery,
    rows: Sequence[Mapping[str, Any]],
    declarations: Counter[str],
) -> list[dict[str, Any]]:
    if query.count_by is EntityCountBy.LAYER:
        counts = Counter((row.get("layer") or "") for row in rows)
        names = set(counts)
        if query.include_declared_empty_layers:
            names.update(declarations)
        return [
            {
                "layer": name,
                "declared_in_layer_table": name in declarations,
                "declaration_count": declarations[name],
                "entity_count": counts[name],
            }
            for name in sorted(names)
        ]
    if query.count_by is EntityCountBy.ENTITY_TYPE:
        counts = Counter(row["entity_type"] for row in rows)
        return [
            {"entity_type": name, "entity_count": count} for name, count in sorted(counts.items())
        ]
    counts = Counter(((row.get("layer") or ""), row["entity_type"]) for row in rows)
    return [
        {
            "layer": layer,
            "entity_type": entity_type,
            "declared_in_layer_table": layer in declarations,
            "entity_count": count,
        }
        for (layer, entity_type), count in sorted(counts.items())
    ]


def _entity_bucket_model(count_by: EntityCountBy, row: Mapping[str, Any]) -> Any:
    if count_by is EntityCountBy.LAYER:
        return LayerCountBucket(**row)
    if count_by is EntityCountBy.ENTITY_TYPE:
        from CAD2BIM_backend.domain.data_query import EntityTypeCountBucket

        return EntityTypeCountBucket(**row)
    return LayerEntityTypeCountBucket(**row)


def _raw_typed_value(row: Mapping[str, Any]) -> _RawTypedValue:
    kind = row.get("typed_value_kind") or row.get("value_kind")
    if kind in {None, "null"}:
        return ("null", None)
    if kind in {"string", "unparsed"}:
        return ("string", row.get("string_value") or row.get("raw_lexeme") or "")
    if kind in {"integer", "int"}:
        value = row.get("int_value")
        return ("null", None) if value is None else ("integer", value)
    if kind in {"number", "float"}:
        value = row.get("float_value")
        return ("null", None) if value is None else ("number", value)
    if kind == "binary":
        value = row.get("binary_value")
        return ("null", None) if value is None else ("binary", bytes(value))
    raise DataQueryApplicationError(
        "PACKAGE_INTEGRITY_ERROR", "An XDATA row has an unsupported typed value kind"
    )


def _typed_model(value: _RawTypedValue) -> TypedValue:
    kind, raw = value
    if kind == "null":
        return NullTypedValue(kind="null")
    if kind == "string":
        return StringTypedValue(kind="string", value=raw)
    if kind == "integer":
        return IntegerTypedValue(kind="integer", value=raw)
    if kind == "number":
        return NumberTypedValue(kind="number", value=raw)
    if kind == "binary":
        return BinaryTypedValue(
            kind="binary",
            base64=base64.b64encode(raw).decode("ascii"),
        )
    raise DataQueryApplicationError(
        "PACKAGE_INTEGRITY_ERROR", "An XDATA value has an unsupported typed value kind"
    )


def _typed_value(row: Mapping[str, Any]) -> TypedValue:
    return _typed_model(_raw_typed_value(row))


def _raw_from_model(value: TypedValue) -> _RawTypedValue:
    if isinstance(value, NullTypedValue):
        return ("null", None)
    if isinstance(value, StringTypedValue):
        return ("string", value.value)
    if isinstance(value, IntegerTypedValue):
        return ("integer", value.value)
    if isinstance(value, NumberTypedValue):
        return ("number", value.value)
    return ("binary", base64.b64decode(value.base64))


def _typed_identity(value: _RawTypedValue) -> str:
    kind, raw = value
    if kind == "binary":
        raw = base64.b64encode(raw).decode("ascii")
    return canonical_json({"kind": kind, "value": raw})


def _xdata_row_matches(
    row: Mapping[str, Any],
    query: CadXdataQuery,
    assignment_value_filter: set[str] | None,
) -> bool:
    filters = query.xdata_filters
    if filters is None:
        return True
    if filters.appids is not None and row["appid"] not in filters.appids:
        return False
    if filters.group_codes is not None and row["group_code"] not in filters.group_codes:
        return False
    if filters.assignment_keys is not None and row["assignment_key"] not in filters.assignment_keys:
        return False
    if assignment_value_filter is not None:
        value = row["_assignment_value"]
        if value is None or _typed_identity(value) not in assignment_value_filter:
            return False
    return True


def _xdata_model(row: Mapping[str, Any]) -> XdataQueryRow:
    return XdataQueryRow(
        xdata_tag_key=row["xdata_tag_key"],
        entity_key=row["entity_key"],
        layer=row.get("layer"),
        entity_type=row["entity_type"],
        appid=row["appid"],
        appid_group_ordinal=row["appid_group_ordinal"],
        tag_ordinal=row["tag_ordinal"],
        is_appid_marker=row["is_appid_marker"],
        group_code=row["group_code"],
        raw_lexeme=row.get("raw_lexeme"),
        typed_value=_typed_model(row["_typed_value"]),
        assignment_key=row.get("assignment_key"),
        assignment_value=(
            _typed_model(row["_assignment_value"])
            if row.get("_assignment_value") is not None
            else None
        ),
        source_raw_tag_key=row["source_raw_tag_key"],
    )


def _section_model(section: EntitySection, row: Mapping[str, Any]) -> Any:
    if section is EntitySection.GEOMETRY_ELEMENTS:
        return GeometryElementRow(**row)
    if section is EntitySection.XDATA_TAGS:
        return _xdata_model(row)
    if section is EntitySection.RELATIONSHIPS:
        return RelationshipRow(**row)
    values = dict(row)
    values["raw_value_bytes"] = _cell(values["raw_value_bytes"])
    values["binary_value"] = (
        _cell(values["binary_value"]) if values["binary_value"] is not None else None
    )
    return RawTagRow(**values)


def _profile_key_fields(dimension: XdataProfileDimension) -> tuple[str, ...]:
    return {
        XdataProfileDimension.APPID: ("appid",),
        XdataProfileDimension.GROUP_CODE: ("group_code",),
        XdataProfileDimension.ASSIGNMENT_KEY: ("assignment_key",),
    }[dimension]


def _profile_model(dimension: XdataProfileDimension, row: Mapping[str, Any]) -> Any:
    if dimension is XdataProfileDimension.APPID:
        return AppidProfileBucket(**row)
    if dimension is XdataProfileDimension.GROUP_CODE:
        return GroupCodeProfileBucket(**row)
    return AssignmentKeyProfileBucket(**row)


def _assignment_profile(
    key: str,
    rows: Sequence[Mapping[str, Any]],
    matched_entity_count: int,
    budget: _Budget,
) -> dict[str, Any]:
    values: list[_RawTypedValue] = []
    entity_counts: Counter[str] = Counter()
    frequencies: Counter[_RawTypedValue] = Counter()
    source_appids: set[str] = set()
    for index, row in enumerate(rows):
        _check_periodically(index, budget)
        value = row["_assignment_value"]
        values.append(value)
        entity_counts[row["entity_key"]] += 1
        frequencies[value] += 1
        source_appids.add(row["appid"])
    ordered = sorted(
        frequencies,
        key=lambda value: (
            -frequencies[value],
            value[0],
            _typed_identity(value),
        ),
    )
    budget.check_time()
    complete = len(ordered) <= 100
    shown = ordered if complete else ordered[:20]
    shown_occurrences = sum(frequencies[value] for value in shown)
    enumeration = ValueEnumeration(
        value_enumeration_complete=complete,
        returned_value_count=len(shown),
        other_distinct_value_count=len(ordered) - len(shown),
        other_occurrence_count=len(values) - shown_occurrences,
        items=tuple(
            ValueFrequency(
                value=_typed_model(value),
                occurrence_count=frequencies[value],
            )
            for value in shown
        ),
    )
    kinds = Counter(value[0] for value in values)
    return {
        "assignment_key": key,
        "source_appids": tuple(sorted(source_appids)),
        "occurrence_count": len(rows),
        "entity_count": len(entity_counts),
        "missing_entity_count": matched_entity_count - len(entity_counts),
        "duplicate_entity_count": sum(count > 1 for count in entity_counts.values()),
        "empty_value_count": sum(kind == "string" and value == "" for kind, value in values),
        "distinct_value_count": len(ordered),
        "value_kind_counts": dict(sorted(kinds.items())),
        "values": enumeration,
        "numeric_profile": _numeric_profile(values, budget),
    }


def _numeric_profile(values: Sequence[_RawTypedValue], budget: _Budget) -> NumericProfile:
    parsed: list[Decimal] = []
    numeric_string_seen = False
    nonnumeric = 0
    for index, (kind, value) in enumerate(values):
        _check_periodically(index, budget)
        if kind == "null":
            continue
        if kind == "integer":
            parsed.append(Decimal(value))
        elif kind == "number":
            parsed.append(Decimal(str(value)))
        elif kind == "string":
            if value == "":
                continue
            if _NUMERIC_LEXEME.fullmatch(value):
                try:
                    parsed.append(Decimal(value))
                    numeric_string_seen = True
                except InvalidOperation:
                    nonnumeric += 1
            else:
                nonnumeric += 1
        else:
            nonnumeric += 1
    budget.check_time()
    if not parsed or nonnumeric:
        return NumericProfile(
            applicable=False,
            interpretation=NumericInterpretation.NOT_APPLICABLE,
            numeric_value_count=len(parsed),
            non_numeric_non_empty_count=nonnumeric,
        )
    return NumericProfile(
        applicable=True,
        interpretation=(
            NumericInterpretation.STRICT_NUMERIC_LEXEME
            if numeric_string_seen
            else NumericInterpretation.NATIVE_NUMERIC
        ),
        numeric_value_count=len(parsed),
        minimum=_decimal_text(min(parsed)),
        maximum=_decimal_text(max(parsed)),
        non_numeric_non_empty_count=0,
    )


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


__all__ = ["DataQueryApplicationError", "DataQueryPolicy", "DataQueryService"]
