"""Bounded reads over immutable JSON Package Payloads.

Unlike exploratory SQL, JSON continuations are self-contained and bind exact
Package, PAYLOAD, and SCHEMA ArtifactVersions.  No process-local snapshot is
needed because published Package members are immutable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactPayloadResolutionError,
    ExactPayloadResolver,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_json import (
    JsonChild,
    JsonChildWithPreview,
    JsonChildWithoutPreview,
    JsonChildrenQuery,
    JsonChildrenResult,
    JsonErrorCode,
    JsonKind,
    JsonNodeType,
    JsonReadRequest,
    JsonReadResult,
    JsonRecord,
    JsonRecordsResult,
    JsonValueQuery,
    JsonValueResult,
)
from CAD2BIM_backend.domain.data_query import PageInfo
from CAD2BIM_backend.domain.data_runtime import (
    DataPackageMemberRecord,
    PackageMemberKind,
    PublishedDataPackage,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.data_query import CursorCodec, CursorCodecError


_TOOL_NAME = "data_read_json"
_JSON_MEDIA_TYPE = "application/json"
_JSON_SCHEMA_MEDIA_TYPE = "application/schema+json"
_MAX_PAYLOAD_BYTES = 100 * 1024 * 1024
_MAX_SCHEMA_BYTES = 64 * 1024
_MAX_VALUE_BYTES = 512 * 1024
_MAX_PREVIEW_BYTES = 4 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ERROR_BYTES = 2_000

JsonContainer: TypeAlias = dict[str, object] | list[object]


class DataJsonApplicationError(RuntimeError):
    """A bounded JSON read failure suitable for entry-adapter projection."""

    def __init__(
        self,
        code: str | JsonErrorCode,
        message: str,
        *,
        phase: Literal["read"] = "read",
        retryable: bool = False,
    ) -> None:
        encoded = (message.strip() or "JSON read failed").encode("utf-8")
        bounded = encoded[:_MAX_ERROR_BYTES].decode("utf-8", errors="ignore")
        super().__init__(bounded)
        self.code = code.value if isinstance(code, JsonErrorCode) else code
        self.phase = phase
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DataJsonPolicy:
    timeout_seconds: float = 15.0
    response_bytes: int = _MAX_RESPONSE_BYTES
    value_bytes: int = _MAX_VALUE_BYTES
    preview_bytes: int = _MAX_PREVIEW_BYTES

    def __post_init__(self) -> None:
        if (
            self.timeout_seconds <= 0
            or self.response_bytes < 1
            or self.value_bytes < 1
            or self.preview_bytes < 1
        ):
            raise ValueError("JSON read policy values must be positive")
        if (
            self.timeout_seconds > 15.0
            or self.response_bytes > _MAX_RESPONSE_BYTES
            or self.value_bytes > _MAX_VALUE_BYTES
            or self.preview_bytes > _MAX_PREVIEW_BYTES
        ):
            raise ValueError("JSON read policy cannot weaken the approved hard limits")


@dataclass(frozen=True, slots=True)
class _ExactJsonPayload:
    package: PublishedDataPackage
    payload: DataPackageMemberRecord
    schema: DataPackageMemberRecord
    json_kind: JsonKind


@dataclass(frozen=True, slots=True)
class _CursorState:
    package_alias: str
    payload_name: str
    json_kind: JsonKind
    mode: Literal["children", "records"]
    pointer: str
    page_size: int
    position: int
    returned: int
    total: int
    claims: Mapping[str, object]


class DataJsonService:
    """Read complete JSON values or deterministic pages from exact members."""

    def __init__(
        self,
        payloads: ExactPayloadResolver,
        artifacts: ArtifactService,
        cursor_codec: CursorCodec,
        policy: DataJsonPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._payloads = payloads
        self._artifacts = artifacts
        self._cursor_codec = cursor_codec
        self._policy = policy or DataJsonPolicy()
        self._clock = clock

    async def read_json(
        self,
        scope: InvocationScope,
        request: JsonReadRequest,
        *,
        deadline: float | None = None,
    ) -> JsonReadResult:
        effective_deadline = min(
            deadline if deadline is not None else float("inf"),
            self._clock() + self._policy.timeout_seconds,
        )
        timeout = max(0.0, effective_deadline - self._clock())
        try:
            async with asyncio.timeout(timeout):
                result = await self._read(scope, request, effective_deadline)
        except TimeoutError as exc:
            raise DataJsonApplicationError("QUERY_TIMEOUT", "The JSON read timed out") from exc
        self._check_deadline(effective_deadline)
        return result

    async def read(
        self,
        scope: InvocationScope,
        request: JsonReadRequest,
        *,
        deadline: float | None = None,
    ) -> JsonReadResult:
        """Compatibility spelling for application adapters."""

        return await self.read_json(scope, request, deadline=deadline)

    async def _read(
        self,
        scope: InvocationScope,
        request: JsonReadRequest,
        deadline: float,
    ) -> JsonReadResult:
        if request.query is not None:
            query = request.query
            pointer = (
                query.pointer if isinstance(query, (JsonValueQuery, JsonChildrenQuery)) else ""
            )
            if isinstance(query, (JsonValueQuery, JsonChildrenQuery)):
                _pointer_tokens(pointer)
            mode = query.mode
            page_size = query.page_size if not isinstance(query, JsonValueQuery) else 0
            position = 0
            cursor_state = None
            package_alias = query.package_alias
            payload_name = query.payload_name
        else:
            cursor_state = self._decode_cursor(scope, request.cursor or "")
            mode = cursor_state.mode
            pointer = cursor_state.pointer
            page_size = cursor_state.page_size
            position = cursor_state.position
            package_alias = cursor_state.package_alias
            payload_name = cursor_state.payload_name

        exact = await self._resolve(scope, package_alias, payload_name, cursor_state is not None)
        if cursor_state is not None:
            self._validate_cursor_resource(cursor_state, exact)
        payload_bytes, schema_bytes = await asyncio.gather(
            self._read_member(
                exact.payload,
                media_type=_JSON_MEDIA_TYPE,
                maximum=_MAX_PAYLOAD_BYTES,
                deadline=deadline,
            ),
            self._read_member(
                exact.schema,
                media_type=_JSON_SCHEMA_MEDIA_TYPE,
                maximum=_MAX_SCHEMA_BYTES,
                deadline=deadline,
            ),
        )
        value = await asyncio.to_thread(_parse_json_bytes, payload_bytes)
        await asyncio.to_thread(_parse_json_bytes, schema_bytes)
        self._check_deadline(deadline)
        if _json_text(value).encode("utf-8") != payload_bytes:
            raise _integrity("A published JSON Payload is not deterministically canonical")
        if exact.json_kind is JsonKind.RECORDS and not isinstance(value, list):
            raise _integrity("A records JSON Payload does not contain a top-level array")

        if mode == "value":
            assert isinstance(request.query, JsonValueQuery)
            target = _resolve_pointer(value, pointer)
            return self._value_result(exact, pointer, target)
        if mode == "children":
            target = _resolve_pointer(value, pointer)
            if not isinstance(target, (dict, list)):
                raise DataJsonApplicationError(
                    JsonErrorCode.READ_MODE_MISMATCH,
                    "children mode requires an object or array node",
                )
            total = len(target)
            self._validate_cursor_counts(cursor_state, total)
            return self._children_result(
                scope,
                exact,
                pointer,
                target,
                page_size,
                position,
                deadline,
            )
        if exact.json_kind is not JsonKind.RECORDS or not isinstance(value, list):
            raise DataJsonApplicationError(
                JsonErrorCode.READ_MODE_MISMATCH,
                "records mode requires a Payload declared with json_kind=records",
            )
        total = len(value)
        self._validate_cursor_counts(cursor_state, total)
        return self._records_result(
            scope,
            exact,
            value,
            page_size,
            position,
            deadline,
        )

    async def _resolve(
        self,
        scope: InvocationScope,
        package_alias: str,
        payload_name: str,
        continuation: bool,
    ) -> _ExactJsonPayload:
        try:
            package = await self._payloads.package_by_alias(scope, package_alias)
        except ExactPayloadResolutionError as exc:
            if continuation:
                raise _cursor_invalid("The cursor's exact Package is no longer visible") from exc
            raise DataJsonApplicationError(exc.code, str(exc)) from exc

        logical_matches = [
            member
            for member in package.members
            if member.member_kind is PackageMemberKind.PAYLOAD
            and member.logical_payload_name == payload_name
        ]
        if not logical_matches:
            if continuation:
                raise _cursor_invalid("The cursor's exact Payload is no longer visible")
            raise DataJsonApplicationError("PAYLOAD_NOT_FOUND", "The JSON Payload was not found")
        json_matches = [m for m in logical_matches if m.media_type == _JSON_MEDIA_TYPE]
        if not json_matches:
            if continuation:
                raise _cursor_invalid("The cursor's exact Payload format changed")
            raise DataJsonApplicationError(
                "PAYLOAD_FORMAT_UNSUPPORTED",
                "The selected Payload is not JSON; use data_query_rows for Parquet",
            )
        if len(logical_matches) != 1 or len(json_matches) != 1:
            raise _integrity("The Package has duplicate logical Payload metadata")
        payload = json_matches[0]
        expected_payload_name = f"payloads/{payload_name}.json"
        if payload.member_name != expected_payload_name or payload.logical_byte_count is None:
            raise _integrity("The JSON PAYLOAD member metadata is invalid")
        try:
            json_kind = JsonKind(payload.json_kind.value if payload.json_kind is not None else "")
        except ValueError as exc:
            raise _integrity("The JSON PAYLOAD member has invalid json_kind metadata") from exc

        expected_schema_name = f"schemas/{payload_name}.schema.json"
        schemas = [
            member
            for member in package.members
            if member.member_kind is PackageMemberKind.SCHEMA
            and member.member_name == expected_schema_name
        ]
        if len(schemas) != 1:
            raise _integrity("The JSON Payload does not have exactly one fixed SCHEMA member")
        return _ExactJsonPayload(package, payload, schemas[0], json_kind)

    async def _read_member(
        self,
        member: DataPackageMemberRecord,
        *,
        media_type: str,
        maximum: int,
        deadline: float,
    ) -> bytes:
        self._check_deadline(deadline)
        try:
            stream = await self._artifacts.stream_version(
                member.owner_id,
                member.artifact.artifact_id,
                member.artifact.version_id,
            )
        except (ConflictError, NotFoundError) as exc:
            raise _integrity("An exact JSON Package member is unavailable") from exc
        artifact = stream.artifact
        if (
            artifact.owner_id != member.owner_id
            or artifact.task_id != member.task_id
            or artifact.artifact_id != member.artifact.artifact_id
            or artifact.version_id != member.artifact.version_id
            or artifact.sha256 != member.artifact.sha256
            or artifact.content_type != media_type
            or member.media_type != media_type
            or artifact.size_bytes > maximum
        ):
            raise _integrity("A JSON Package member does not match its exact Artifact version")
        if (
            member.member_kind is PackageMemberKind.PAYLOAD
            and member.logical_byte_count != artifact.size_bytes
        ):
            raise _integrity("JSON Payload size metadata differs from its exact Artifact")
        chunks: list[bytes] = []
        size = 0
        digest = hashlib.sha256()
        try:
            async for chunk in stream.chunks:
                self._check_deadline(deadline)
                if not isinstance(chunk, bytes):
                    raise _integrity("An exact JSON Artifact stream returned invalid bytes")
                size += len(chunk)
                if size > maximum or size > artifact.size_bytes:
                    raise _integrity("An exact JSON Artifact exceeds its published size")
                digest.update(chunk)
                chunks.append(chunk)
        except DataJsonApplicationError:
            raise
        except Exception as exc:
            raise _integrity("An exact JSON Artifact stream failed") from exc
        if size != artifact.size_bytes or digest.hexdigest() != member.artifact.sha256:
            raise _integrity("An exact JSON Artifact failed size or digest verification")
        return b"".join(chunks)

    def _value_result(
        self,
        exact: _ExactJsonPayload,
        pointer: str,
        value: object,
    ) -> JsonValueResult:
        text = _json_text(value)
        size = len(text.encode("utf-8"))
        if size > self._policy.value_bytes:
            suffix = (
                "Use children mode for this container."
                if isinstance(value, (dict, list))
                else "Publish the oversized scalar as Parquet or redesign the JSON Payload."
            )
            raise DataJsonApplicationError(
                JsonErrorCode.VALUE_TOO_LARGE,
                f"The exact JSON node exceeds 512 KiB. {suffix}",
            )
        result = JsonValueResult(
            package_alias=exact.package.catalog_entry.alias,
            payload_name=exact.payload.logical_payload_name or "",
            json_kind=exact.json_kind,
            pointer=pointer,
            node_type=_node_type(value),
            size_bytes=size,
            value_json=text,
        )
        if _response_size(result) > self._policy.response_bytes:
            raise DataJsonApplicationError(
                JsonErrorCode.VALUE_TOO_LARGE,
                "The exact JSON node cannot fit in the bounded 1 MiB response",
            )
        return result

    def _children_result(
        self,
        scope: InvocationScope,
        exact: _ExactJsonPayload,
        pointer: str,
        value: JsonContainer,
        page_size: int,
        position: int,
        deadline: float,
    ) -> JsonChildrenResult:
        entries = _children(value, pointer)
        if position < 0 or position > len(entries):
            raise _cursor_invalid("The cursor child position is invalid")
        items: list[JsonChild] = []
        for selector, child_pointer, child in entries[position : position + page_size]:
            self._check_deadline(deadline)
            text = _json_text(child)
            size = len(text.encode("utf-8"))
            common = {
                "pointer": child_pointer,
                "selector": selector,
                "node_type": _node_type(child),
                "size_bytes": size,
                "child_count": len(child) if isinstance(child, (dict, list)) else 0,
            }
            item: JsonChild
            if size <= self._policy.preview_bytes:
                item = JsonChildWithPreview(preview_json=text, **common)
            else:
                item = JsonChildWithoutPreview(**common)
            candidate = (*items, item)
            result = self._children_page(
                scope, exact, pointer, page_size, position, candidate, len(entries)
            )
            if _response_size(result) > self._policy.response_bytes:
                if not items:
                    raise DataJsonApplicationError(
                        JsonErrorCode.VALUE_TOO_LARGE,
                        "One complete JSON child descriptor cannot fit in the 1 MiB response",
                    )
                break
            items.append(item)
        result = self._children_page(
            scope, exact, pointer, page_size, position, tuple(items), len(entries)
        )
        if not items and position < len(entries):
            raise DataJsonApplicationError(
                JsonErrorCode.VALUE_TOO_LARGE,
                "The next complete JSON child cannot fit in the bounded response",
            )
        return result

    def _children_page(
        self,
        scope: InvocationScope,
        exact: _ExactJsonPayload,
        pointer: str,
        page_size: int,
        position: int,
        items: Sequence[JsonChild],
        total: int,
    ) -> JsonChildrenResult:
        page = self._page(scope, exact, "children", pointer, page_size, position, len(items), total)
        return JsonChildrenResult(
            package_alias=exact.package.catalog_entry.alias,
            payload_name=exact.payload.logical_payload_name or "",
            json_kind=exact.json_kind,
            pointer=pointer,
            children=tuple(items),
            page=page,
        )

    def _records_result(
        self,
        scope: InvocationScope,
        exact: _ExactJsonPayload,
        value: list[object],
        page_size: int,
        position: int,
        deadline: float,
    ) -> JsonRecordsResult:
        if position < 0 or position > len(value):
            raise _cursor_invalid("The cursor record position is invalid")
        records: list[JsonRecord] = []
        for index in range(position, min(position + page_size, len(value))):
            self._check_deadline(deadline)
            text = _json_text(value[index])
            if len(text.encode("utf-8")) > self._policy.value_bytes:
                raise DataJsonApplicationError(
                    JsonErrorCode.VALUE_TOO_LARGE,
                    f"JSON record {index} exceeds the complete 512 KiB record limit",
                )
            record = JsonRecord(record_index=index, record_json=text)
            candidate = (*records, record)
            result = self._records_page(scope, exact, page_size, position, candidate, len(value))
            if _response_size(result) > self._policy.response_bytes:
                if not records:
                    raise DataJsonApplicationError(
                        JsonErrorCode.VALUE_TOO_LARGE,
                        f"JSON record {index} cannot fit in the bounded 1 MiB response",
                    )
                break
            records.append(record)
        result = self._records_page(scope, exact, page_size, position, tuple(records), len(value))
        if not records and position < len(value):
            raise DataJsonApplicationError(
                JsonErrorCode.VALUE_TOO_LARGE,
                "The next complete JSON record cannot fit in the bounded response",
            )
        return result

    def _records_page(
        self,
        scope: InvocationScope,
        exact: _ExactJsonPayload,
        page_size: int,
        position: int,
        records: Sequence[JsonRecord],
        total: int,
    ) -> JsonRecordsResult:
        page = self._page(scope, exact, "records", "", page_size, position, len(records), total)
        return JsonRecordsResult(
            package_alias=exact.package.catalog_entry.alias,
            payload_name=exact.payload.logical_payload_name or "",
            json_kind=JsonKind.RECORDS,
            records=tuple(records),
            page=page,
        )

    def _page(
        self,
        scope: InvocationScope,
        exact: _ExactJsonPayload,
        mode: Literal["children", "records"],
        pointer: str,
        page_size: int,
        position: int,
        count: int,
        total: int,
    ) -> PageInfo:
        next_position = position + count
        remaining = total - next_position
        if remaining < 0:
            raise _cursor_invalid("The cursor page counts are invalid")
        next_cursor = None
        if remaining:
            claims = {
                "tool": _TOOL_NAME,
                "scope": _scope_digest(scope),
                "package": {
                    "alias": exact.package.catalog_entry.alias,
                    "id": exact.package.package.package_id,
                },
                "payload_name": exact.payload.logical_payload_name,
                "payload": _member_claim(exact.payload),
                "schema": _member_claim(exact.schema),
                "json_kind": exact.json_kind.value,
                "mode": mode,
                "pointer": pointer,
                "page_size": page_size,
                "position": next_position,
                "returned": next_position,
                "total": total,
            }
            try:
                next_cursor = self._cursor_codec.encode(claims)
            except CursorCodecError as exc:
                raise _cursor_invalid("The JSON continuation cannot be encoded safely") from exc
        return PageInfo(
            total_matched_count=total,
            returned_count=count,
            remaining_count=remaining,
            complete=remaining == 0,
            next_cursor=next_cursor,
        )

    def _decode_cursor(self, scope: InvocationScope, token: str) -> _CursorState:
        try:
            claims = self._cursor_codec.decode(token)
            required = {
                "tool",
                "scope",
                "package",
                "payload_name",
                "payload",
                "schema",
                "json_kind",
                "mode",
                "pointer",
                "page_size",
                "position",
                "returned",
                "total",
            }
            if set(claims) != required:
                raise ValueError
            package = claims["package"]
            if (
                claims["tool"] != _TOOL_NAME
                or claims["scope"] != _scope_digest(scope)
                or not isinstance(package, dict)
                or set(package) != {"alias", "id"}
                or not isinstance(package["alias"], str)
                or not isinstance(package["id"], str)
                or not isinstance(claims["payload_name"], str)
                or claims["mode"] not in {"children", "records"}
                or not isinstance(claims["pointer"], str)
                or not _strict_int(claims["page_size"])
                or not _strict_int(claims["position"])
                or not _strict_int(claims["returned"])
                or not _strict_int(claims["total"])
            ):
                raise ValueError
            mode = claims["mode"]
            page_size = claims["page_size"]
            position = claims["position"]
            returned = claims["returned"]
            total = claims["total"]
            if (
                (mode == "children" and not 1 <= page_size <= 100)
                or (mode == "records" and not 1 <= page_size <= 200)
                or min(position, returned, total) < 0
                or returned != position
                or position > total
            ):
                raise ValueError
            _pointer_tokens(claims["pointer"])
            json_kind = JsonKind(claims["json_kind"])
            _validate_member_claim(claims["payload"])
            _validate_member_claim(claims["schema"])
        except DataJsonApplicationError as exc:
            raise _cursor_invalid("The JSON cursor is invalid") from exc
        except (CursorCodecError, KeyError, TypeError, ValueError) as exc:
            raise _cursor_invalid("The JSON cursor is invalid") from exc
        return _CursorState(
            package_alias=package["alias"],
            payload_name=claims["payload_name"],
            json_kind=json_kind,
            mode=mode,
            pointer=claims["pointer"],
            page_size=page_size,
            position=position,
            returned=returned,
            total=total,
            claims=claims,
        )

    @staticmethod
    def _validate_cursor_counts(state: _CursorState | None, total: int) -> None:
        if state is not None and state.total != total:
            raise _cursor_invalid("The immutable JSON cursor total no longer matches")

    @staticmethod
    def _validate_cursor_resource(state: _CursorState, exact: _ExactJsonPayload) -> None:
        claims = state.claims
        expected_package = {
            "alias": exact.package.catalog_entry.alias,
            "id": exact.package.package.package_id,
        }
        if (
            claims.get("package") != expected_package
            or claims.get("payload_name") != exact.payload.logical_payload_name
            or claims.get("payload") != _member_claim(exact.payload)
            or claims.get("schema") != _member_claim(exact.schema)
            or state.json_kind is not exact.json_kind
        ):
            raise _cursor_invalid("The JSON cursor exact resource binding does not match")

    def _check_deadline(self, deadline: float) -> None:
        if self._clock() > deadline:
            raise DataJsonApplicationError("QUERY_TIMEOUT", "The JSON read timed out")


def _parse_json_bytes(value: bytes) -> object:
    try:
        return json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_mapping_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _integrity("A published JSON Package member is not valid exact UTF-8 JSON") from exc


def _mapping_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant {value}")


def _pointer_tokens(pointer: str) -> tuple[str, ...]:
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise DataJsonApplicationError(
            JsonErrorCode.POINTER_INVALID,
            "An RFC 6901 JSON Pointer must be empty or start with '/'",
        )
    tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        index = 0
        while index < len(raw):
            if raw[index] == "~":
                if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                    raise DataJsonApplicationError(
                        JsonErrorCode.POINTER_INVALID,
                        "An RFC 6901 escape must be exactly '~0' or '~1'",
                    )
                index += 2
            else:
                index += 1
        tokens.append(raw.replace("~1", "/").replace("~0", "~"))
    return tuple(tokens)


def _resolve_pointer(value: object, pointer: str) -> object:
    current = value
    for token in _pointer_tokens(pointer):
        if isinstance(current, dict):
            if token not in current:
                raise DataJsonApplicationError(
                    JsonErrorCode.POINTER_INVALID,
                    "The RFC 6901 JSON Pointer target does not exist",
                )
            current = current[token]
        elif isinstance(current, list):
            if token == "0":
                index = 0
            elif token and token[0] in "123456789" and token.isascii() and token.isdecimal():
                index = int(token)
            else:
                raise DataJsonApplicationError(
                    JsonErrorCode.POINTER_INVALID,
                    "The RFC 6901 array token is not a canonical existing index",
                )
            if index >= len(current):
                raise DataJsonApplicationError(
                    JsonErrorCode.POINTER_INVALID,
                    "The RFC 6901 JSON Pointer target does not exist",
                )
            current = current[index]
        else:
            raise DataJsonApplicationError(
                JsonErrorCode.POINTER_INVALID,
                "The RFC 6901 JSON Pointer traverses through a scalar",
            )
    return current


def _children(value: JsonContainer, pointer: str) -> list[tuple[str, str, object]]:
    if isinstance(value, dict):
        return [
            (key, _append_pointer(pointer, _escape_pointer_token(key)), child)
            for key, child in value.items()
        ]
    return [
        (str(index), _append_pointer(pointer, str(index)), child)
        for index, child in enumerate(value)
    ]


def _escape_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _append_pointer(pointer: str, token: str) -> str:
    return f"{pointer}/{token}"


def _json_text(value: object) -> str:
    try:
        return canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise _integrity("A published JSON value is outside canonical JSON") from exc


def _node_type(value: object) -> JsonNodeType:
    if value is None:
        return JsonNodeType.NULL
    if isinstance(value, bool):
        return JsonNodeType.BOOLEAN
    if isinstance(value, int):
        return JsonNodeType.INTEGER
    if isinstance(value, float):
        return JsonNodeType.NUMBER
    if isinstance(value, str):
        return JsonNodeType.STRING
    if isinstance(value, list):
        return JsonNodeType.ARRAY
    if isinstance(value, dict):
        return JsonNodeType.OBJECT
    raise _integrity("A published JSON value has an unsupported node type")


def _response_size(value: JsonReadResult) -> int:
    return len(value.model_dump_json().encode("utf-8"))


def _scope_digest(scope: InvocationScope) -> str:
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
    return hashlib.sha256(canonical_json(authority).encode("utf-8")).hexdigest()


def _member_claim(member: DataPackageMemberRecord) -> dict[str, str]:
    return {
        "name": member.member_name,
        "artifact": member.artifact.artifact_id,
        "version": member.artifact.version_id,
        "sha256": member.artifact.sha256,
    }


def _validate_member_claim(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "artifact", "version", "sha256"}
        or any(not isinstance(item, str) or not item for item in value.values())
    ):
        raise ValueError("invalid exact member claim")


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _integrity(message: str) -> DataJsonApplicationError:
    return DataJsonApplicationError("PACKAGE_INTEGRITY_ERROR", message)


def _cursor_invalid(message: str) -> DataJsonApplicationError:
    return DataJsonApplicationError(JsonErrorCode.CURSOR_INVALID, message)


__all__ = [
    "DataJsonApplicationError",
    "DataJsonPolicy",
    "DataJsonService",
]
