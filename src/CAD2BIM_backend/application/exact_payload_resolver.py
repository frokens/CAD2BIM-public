"""Resolve immutable Package payloads and materialize their exact bytes.

The resolver is shared application infrastructure for bounded queries and data
transforms.  It deliberately knows nothing about Direct sessions, cursors, SQL,
or provider schemas; callers supply an already-authorized ``InvocationScope``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_runtime import (
    CatalogResourceKind,
    DataPackageKind,
    DataPackageMemberRecord,
    PackageMemberKind,
    PublishedDataPackage,
    SOURCE_L0_LOGICAL_KEYS,
    SOURCE_L0_PAYLOAD_ORDER,
    source_l0_payload_member_name,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.ports.data_runtime import DataRuntimeRepository
from CAD2BIM_backend.ports.arrow_inputs import ArrowInput


PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
JSON_MEDIA_TYPE = "application/json"
JSON_SCHEMA_MEDIA_TYPE = "application/schema+json"
_JSON_PAYLOAD_BYTES = 100 * 1024 * 1024
_JSON_MAX_DEPTH = 64
_JSON_INPUT_FORMAT_KEY = b"cad2bim.input_format"
_JSON_INPUT_KIND_KEY = b"cad2bim.json_kind"


class ExactPayloadResolutionError(RuntimeError):
    """Failure while resolving or verifying an exact immutable payload."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExactParquetPayload:
    """One logical Parquet payload bound to one exact Package member."""

    payload_name: str
    logical_key_fields: tuple[str, ...]
    member: DataPackageMemberRecord


@dataclass(frozen=True, slots=True)
class ExactJsonPayload:
    """One logical JSON payload and its exact colocated JSON Schema member."""

    payload_name: str
    json_kind: str
    member: DataPackageMemberRecord
    schema_member: DataPackageMemberRecord


ExactTransformPayload = ExactParquetPayload | ExactJsonPayload


@dataclass(frozen=True, slots=True)
class ExactPayloadInput:
    """One exact Payload projected to a Worker-local input name."""

    input_name: str
    payload: ExactTransformPayload


def exact_payload_members(
    inputs: Sequence[ExactPayloadInput],
) -> tuple[DataPackageMemberRecord, ...]:
    """Return each exact payload/schema member once in deterministic order."""

    members: list[DataPackageMemberRecord] = []
    seen: set[str] = set()
    for item in inputs:
        candidates = [item.payload.member]
        if isinstance(item.payload, ExactJsonPayload):
            candidates.append(item.payload.schema_member)
        for member in candidates:
            if member.member_id not in seen:
                seen.add(member.member_id)
                members.append(member)
    return tuple(members)


def exact_payload_arrow_inputs(
    inputs: Sequence[ExactPayloadInput],
    materialized: Mapping[str, Path],
) -> tuple[ArrowInput, ...]:
    """Open already verified exact Payload files as bounded Arrow readers."""

    result: list[ArrowInput] = []
    try:
        for item in inputs:
            if isinstance(item.payload, ExactJsonPayload):
                reader, row_count = _json_input_reader(item.payload, materialized)
                result.append(
                    ArrowInput(
                        input_name=item.input_name,
                        reader=reader,
                        input_format="json",
                        json_kind=item.payload.json_kind,
                        row_count=row_count,
                    )
                )
                continue
            path = materialized[item.payload.member.member_id]
            parquet = pq.ParquetFile(path)
            if parquet.metadata.num_rows != item.payload.member.row_count:
                raise ValueError("Parquet row count does not match Package metadata")
            reader = pa.RecordBatchReader.from_batches(
                parquet.schema_arrow,
                parquet.iter_batches(batch_size=65_536),
            )
            result.append(
                ArrowInput(
                    input_name=item.input_name,
                    reader=reader,
                    row_count=parquet.metadata.num_rows,
                )
            )
        return tuple(result)
    except ExactPayloadResolutionError:
        raise
    except (OSError, KeyError, ValueError, pa.ArrowException) as exc:
        raise ExactPayloadResolutionError(
            "PACKAGE_INTEGRITY_ERROR",
            "An exact Payload cannot be opened as its declared format",
        ) from exc


class ExactPayloadResolver:
    """Resolve Catalog aliases and verify exact ArtifactVersion materialization."""

    def __init__(
        self,
        repository: DataRuntimeRepository,
        artifacts: ArtifactService,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts

    async def package_by_alias(
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
            raise ExactPayloadResolutionError("PACKAGE_NOT_FOUND", "The data package was not found")
        package = await self._repository.get_package(scope, entry.package_id)
        if package is None:
            raise ExactPayloadResolutionError("PACKAGE_NOT_FOUND", "The data package was not found")
        return package

    def parquet_payloads(
        self,
        package: PublishedDataPackage,
    ) -> Mapping[str, ExactParquetPayload]:
        """Return logical Parquet bindings after validating member metadata."""

        if package.package.package_kind is DataPackageKind.SOURCE_L0:
            return self._source_l0_payloads(package)
        if package.package.package_kind is DataPackageKind.DERIVED:
            return self._derived_payloads(package)
        raise ExactPayloadResolutionError(
            "PACKAGE_KIND_MISMATCH", "The package kind has no queryable Parquet payload contract"
        )

    def transform_payloads(
        self,
        package: PublishedDataPackage,
    ) -> Mapping[str, ExactTransformPayload]:
        """Return every exact Payload format accepted by ``data_transform``."""

        if package.package.package_kind is DataPackageKind.SOURCE_L0:
            return dict(self.parquet_payloads(package))
        if package.package.package_kind is not DataPackageKind.DERIVED:
            raise ExactPayloadResolutionError(
                "PACKAGE_KIND_MISMATCH",
                "The package kind has no transform Payload contract",
            )
        parquet = dict(self._derived_payloads(package, require_payload=False))
        json_payloads = self._derived_json_payloads(package)
        if set(parquet).intersection(json_payloads):
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR",
                "A derived package has duplicate logical Payload names",
            )
        return dict(sorted({**parquet, **json_payloads}.items()))

    async def payload_by_exact_member(
        self,
        scope: InvocationScope,
        *,
        package_id: str,
        payload_member_id: str,
        schema_member_id: str | None = None,
    ) -> tuple[PublishedDataPackage, ExactTransformPayload]:
        """Reopen one persisted exact Payload without consulting a Catalog alias.

        Query repair and restart recovery retain immutable Package/member
        identities.  This path reauthorizes that Package through the same
        invocation Scope, then applies the normal format and metadata checks
        before accepting the exact member pair.
        """

        package = await self._repository.get_package(scope, package_id)
        if package is None or package.package.package_id != package_id:
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR",
                "An exact query input Package is unavailable",
            )
        return self.payload_from_exact_package(
            package,
            package_id=package_id,
            payload_member_id=payload_member_id,
            schema_member_id=schema_member_id,
        )

    def payload_from_exact_package(
        self,
        package: PublishedDataPackage,
        *,
        package_id: str,
        payload_member_id: str,
        schema_member_id: str | None = None,
    ) -> tuple[PublishedDataPackage, ExactTransformPayload]:
        """Validate an Operation-authorized exact Package without alias lookup."""
        if package.package.package_id != package_id:
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR",
                "An exact input Package does not match its Operation binding",
            )
        matches = [
            payload
            for payload in self.transform_payloads(package).values()
            if payload.member.member_id == payload_member_id
        ]
        if len(matches) != 1:
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR",
                "An exact query input Payload is unavailable",
            )
        payload = matches[0]
        expected_schema_member_id = (
            payload.schema_member.member_id if isinstance(payload, ExactJsonPayload) else None
        )
        if schema_member_id != expected_schema_member_id:
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR",
                "An exact query input Payload schema binding is invalid",
            )
        return package, payload

    async def materialize_members(
        self,
        members: Sequence[DataPackageMemberRecord],
        *,
        check_budget: Callable[[], None],
        filename_suffix: str = ".parquet",
        max_total_bytes: int | None = None,
    ) -> dict[str, Path]:
        """Copy exact member bytes into private files and verify identity and size."""

        if max_total_bytes is not None and max_total_bytes < 0:
            raise ValueError("max_total_bytes must be non-negative")
        await self._preflight_total_bytes(
            members,
            check_budget=check_budget,
            max_total_bytes=max_total_bytes,
        )

        result: dict[str, Path] = {}
        current_path: Path | None = None
        streamed_bytes = 0
        try:
            for member in members:
                check_budget()
                try:
                    stream = await self._artifacts.stream_version(
                        member.owner_id,
                        member.artifact.artifact_id,
                        member.artifact.version_id,
                    )
                except (ConflictError, NotFoundError) as exc:
                    raise ExactPayloadResolutionError(
                        "PACKAGE_INTEGRITY_ERROR",
                        "A package member exact Artifact version is unavailable",
                    ) from exc
                if (
                    stream.artifact.version_id != member.artifact.version_id
                    or stream.artifact.sha256 != member.artifact.sha256
                    or stream.artifact.task_id != member.task_id
                    or stream.artifact.content_type != member.media_type
                ):
                    raise ExactPayloadResolutionError(
                        "PACKAGE_INTEGRITY_ERROR",
                        "A package member does not match its exact Artifact version",
                    )
                fd, raw_path = tempfile.mkstemp(
                    prefix="cad2bim-exact-payload-", suffix=filename_suffix
                )
                current_path = Path(raw_path)
                try:
                    os.chmod(raw_path, 0o600)
                    digest = hashlib.sha256()
                    with os.fdopen(fd, "wb") as handle:
                        chunks = stream.chunks.__aiter__()
                        while True:
                            check_budget()
                            try:
                                chunk = await anext(chunks)
                            except StopAsyncIteration:
                                break
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                raise ExactPayloadResolutionError(
                                    "PACKAGE_INTEGRITY_ERROR",
                                    "A package member exact Artifact stream failed",
                                ) from exc
                            if not isinstance(chunk, bytes):
                                raise ExactPayloadResolutionError(
                                    "PACKAGE_INTEGRITY_ERROR",
                                    "A package member exact Artifact stream returned invalid bytes",
                                )
                            check_budget()
                            streamed_bytes += len(chunk)
                            if max_total_bytes is not None and streamed_bytes > max_total_bytes:
                                raise ExactPayloadResolutionError(
                                    "TRANSFORM_INPUT_BUDGET_EXCEEDED",
                                    "Exact input Artifact bytes exceed the configured budget",
                                )
                            digest.update(chunk)
                            handle.write(chunk)
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                if digest.hexdigest() != member.artifact.sha256:
                    raise ExactPayloadResolutionError(
                        "PACKAGE_INTEGRITY_ERROR",
                        "A package member failed digest verification",
                    )
                # Member names repeat across input Packages; member identity is
                # the collision-free key required by multi-Package transforms.
                result[member.member_id] = current_path
                current_path = None
            return result
        except (Exception, asyncio.CancelledError):
            if current_path is not None:
                current_path.unlink(missing_ok=True)
            self.remove_paths(result.values())
            raise

    async def _preflight_total_bytes(
        self,
        members: Sequence[DataPackageMemberRecord],
        *,
        check_budget: Callable[[], None],
        max_total_bytes: int | None,
    ) -> None:
        if max_total_bytes is None:
            return
        total_bytes = 0
        for member in members:
            check_budget()
            try:
                artifact = await self._artifacts.get_version_metadata(
                    member.owner_id,
                    member.artifact.artifact_id,
                    member.artifact.version_id,
                )
            except (ConflictError, NotFoundError) as exc:
                raise ExactPayloadResolutionError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "A package member exact Artifact version is unavailable",
                ) from exc
            if (
                artifact.owner_id != member.owner_id
                or artifact.task_id != member.task_id
                or artifact.artifact_id != member.artifact.artifact_id
                or artifact.version_id != member.artifact.version_id
                or artifact.sha256 != member.artifact.sha256
                or artifact.content_type != member.media_type
            ):
                raise ExactPayloadResolutionError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "A package member does not match its exact Artifact version",
                )
            total_bytes += artifact.size_bytes
            if total_bytes > max_total_bytes:
                raise ExactPayloadResolutionError(
                    "TRANSFORM_INPUT_BUDGET_EXCEEDED",
                    "Exact input Artifact bytes exceed the configured budget",
                )

    @staticmethod
    def remove_paths(paths: Iterable[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)

    def _source_l0_payloads(
        self,
        package: PublishedDataPackage,
    ) -> Mapping[str, ExactParquetPayload]:
        result: dict[str, ExactParquetPayload] = {}
        for payload_name in SOURCE_L0_PAYLOAD_ORDER:
            member_name = source_l0_payload_member_name(payload_name)
            matches = [member for member in package.members if member.member_name == member_name]
            if len(matches) != 1:
                raise ExactPayloadResolutionError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "The source_l0 package member set is invalid",
                )
            member = matches[0]
            self._validate_payload_member(package, member)
            declared_name = getattr(member, "logical_payload_name", None)
            declared_keys = tuple(getattr(member, "logical_key_fields", ()))
            if declared_name not in {None, payload_name} or (
                declared_keys and declared_keys != SOURCE_L0_LOGICAL_KEYS[payload_name]
            ):
                raise ExactPayloadResolutionError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "A source_l0 payload has inconsistent logical metadata",
                )
            result[payload_name] = ExactParquetPayload(
                payload_name=payload_name,
                logical_key_fields=SOURCE_L0_LOGICAL_KEYS[payload_name],
                member=member,
            )
        return result

    def _derived_payloads(
        self,
        package: PublishedDataPackage,
        *,
        require_payload: bool = True,
    ) -> Mapping[str, ExactParquetPayload]:
        result: dict[str, ExactParquetPayload] = {}
        payload_members = [
            member
            for member in package.members
            if member.member_kind is PackageMemberKind.PAYLOAD
            and member.media_type == PARQUET_MEDIA_TYPE
        ]
        if require_payload and not payload_members:
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR",
                "The derived package has no queryable Parquet payload",
            )
        for member in payload_members:
            self._validate_payload_member(package, member)
            payload_name = getattr(member, "logical_payload_name", None)
            logical_keys = tuple(getattr(member, "logical_key_fields", ()))
            if not payload_name or not logical_keys or payload_name in result:
                raise ExactPayloadResolutionError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "A derived Parquet payload has invalid logical metadata",
                )
            result[payload_name] = ExactParquetPayload(
                payload_name=payload_name,
                logical_key_fields=logical_keys,
                member=member,
            )
        return dict(sorted(result.items()))

    def _derived_json_payloads(
        self,
        package: PublishedDataPackage,
    ) -> Mapping[str, ExactJsonPayload]:
        payload_members = [
            member
            for member in package.members
            if member.member_kind is PackageMemberKind.PAYLOAD
            and member.media_type == JSON_MEDIA_TYPE
        ]
        schema_members = {
            member.member_name: member
            for member in package.members
            if member.member_kind is PackageMemberKind.SCHEMA
        }
        if len(schema_members) != len(
            [member for member in package.members if member.member_kind is PackageMemberKind.SCHEMA]
        ):
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR",
                "A derived package has duplicate JSON Schema members",
            )
        result: dict[str, ExactJsonPayload] = {}
        used_schema_names: set[str] = set()
        for member in payload_members:
            self._validate_json_payload_member(package, member)
            payload_name = member.logical_payload_name
            assert payload_name is not None
            if payload_name in result:
                raise ExactPayloadResolutionError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "A derived package has duplicate JSON Payload metadata",
                )
            schema_name = f"schemas/{payload_name}.schema.json"
            schema_member = schema_members.get(schema_name)
            if schema_member is None:
                raise ExactPayloadResolutionError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "A JSON Payload has no exact JSON Schema member",
                )
            self._validate_schema_member(package, schema_member)
            used_schema_names.add(schema_name)
            result[payload_name] = ExactJsonPayload(
                payload_name=payload_name,
                json_kind=str(member.json_kind.value),
                member=member,
                schema_member=schema_member,
            )
        if set(schema_members) != used_schema_names:
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR",
                "A derived package contains an unbound JSON Schema member",
            )
        return result

    @staticmethod
    def _validate_payload_member(
        package: PublishedDataPackage,
        member: DataPackageMemberRecord,
    ) -> None:
        if (
            member.member_kind is not PackageMemberKind.PAYLOAD
            or member.media_type != PARQUET_MEDIA_TYPE
            or member.row_count is None
            or member.package_id != package.package.package_id
            or member.task_id != package.package.task_id
            or member.tenant_id != package.package.tenant_id
            or member.owner_id != package.package.owner_id
        ):
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR", "A Parquet payload binding is invalid"
            )

    @staticmethod
    def _validate_json_payload_member(
        package: PublishedDataPackage,
        member: DataPackageMemberRecord,
    ) -> None:
        if (
            member.member_kind is not PackageMemberKind.PAYLOAD
            or member.media_type != JSON_MEDIA_TYPE
            or member.logical_payload_name is None
            or member.member_name != f"payloads/{member.logical_payload_name}.json"
            or member.json_kind is None
            or member.row_count is not None
            or member.logical_key_fields
            or member.arrow_schema
            or member.logical_byte_count is None
            or member.package_id != package.package.package_id
            or member.task_id != package.package.task_id
            or member.tenant_id != package.package.tenant_id
            or member.owner_id != package.package.owner_id
        ):
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR", "A JSON payload binding is invalid"
            )

    @staticmethod
    def _validate_schema_member(
        package: PublishedDataPackage,
        member: DataPackageMemberRecord,
    ) -> None:
        if (
            member.member_kind is not PackageMemberKind.SCHEMA
            or member.media_type != JSON_SCHEMA_MEDIA_TYPE
            or member.package_id != package.package.package_id
            or member.task_id != package.package.task_id
            or member.tenant_id != package.package.tenant_id
            or member.owner_id != package.package.owner_id
        ):
            raise ExactPayloadResolutionError(
                "PACKAGE_INTEGRITY_ERROR", "A JSON Schema binding is invalid"
            )


def _json_input_reader(
    payload: ExactJsonPayload,
    materialized: Mapping[str, Path],
) -> tuple[pa.RecordBatchReader, int]:
    try:
        payload_bytes = materialized[payload.member.member_id].read_bytes()
        schema_bytes = materialized[payload.schema_member.member_id].read_bytes()
        if len(payload_bytes) != payload.member.logical_byte_count:
            raise ValueError("JSON logical byte count does not match its exact Artifact")
        schema = _parse_exact_json_schema(schema_bytes.decode("utf-8"), payload.json_kind)
        value = _load_exact_json(payload_bytes.decode("utf-8"))
        jsonschema.Draft202012Validator(schema).validate(value)
        if _exact_json_depth(value) > _JSON_MAX_DEPTH or len(payload_bytes) > _JSON_PAYLOAD_BYTES:
            raise ValueError("JSON Payload exceeds its published depth or byte budget")
        if canonical_json(value).encode("utf-8") != payload_bytes:
            raise ValueError("JSON Payload bytes are not in canonical publication form")
        metadata = {
            _JSON_INPUT_FORMAT_KEY: b"json",
            _JSON_INPUT_KIND_KEY: payload.json_kind.encode("ascii"),
        }
        if payload.json_kind == "document":
            schema_arrow = pa.schema(
                [pa.field("json_value", pa.string(), nullable=False)],
                metadata=metadata,
            )
            table = pa.Table.from_arrays(
                [pa.array([payload_bytes.decode("utf-8")])], schema=schema_arrow
            )
        else:
            if not isinstance(value, list):
                raise ValueError("records JSON Payload root is not an array")
            schema_arrow = pa.schema(
                [
                    pa.field("record_index", pa.int64(), nullable=False),
                    pa.field("json_value", pa.string(), nullable=False),
                ],
                metadata=metadata,
            )
            table = pa.Table.from_arrays(
                [
                    pa.array(range(len(value)), type=pa.int64()),
                    pa.array([canonical_json(record) for record in value], type=pa.string()),
                ],
                schema=schema_arrow,
            )
        return pa.RecordBatchReader.from_batches(table.schema, table.to_batches()), table.num_rows
    except (
        OSError,
        UnicodeError,
        KeyError,
        ValueError,
        RecursionError,
        jsonschema.SchemaError,
        jsonschema.ValidationError,
    ) as exc:
        raise ExactPayloadResolutionError(
            "PACKAGE_INTEGRITY_ERROR",
            "An exact JSON Payload or its JSON Schema is invalid",
        ) from exc


def _parse_exact_json_schema(schema_json: str, json_kind: str) -> object:
    schema = _load_exact_json(schema_json)
    if _exact_json_depth(schema) > _JSON_MAX_DEPTH:
        raise ValueError("JSON Schema nesting exceeds the product limit")
    if (
        isinstance(schema, dict)
        and schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
        != "https://json-schema.org/draft/2020-12/schema"
    ):
        raise ValueError("JSON Schema draft is not supported")
    if json_kind == "records" and (
        not isinstance(schema, dict) or schema.get("type") != "array" or "items" not in schema
    ):
        raise ValueError("records JSON Schema must declare an array root")
    jsonschema.Draft202012Validator.check_schema(schema)
    _validate_exact_local_schema_references(schema)
    return schema


def _validate_exact_local_schema_references(schema: object) -> None:
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef"}:
                    if not isinstance(value, str) or not (
                        value == "#" or value.startswith("#/$defs/")
                    ):
                        raise ValueError("JSON Schema reference is not local")
                    _resolve_exact_local_schema_pointer(schema, value)
                pending.append(value)
        elif isinstance(node, list):
            pending.extend(node)


def _resolve_exact_local_schema_pointer(schema: object, reference: str) -> None:
    if reference == "#":
        return
    current = schema
    for raw_token in reference[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ValueError("JSON Schema local reference does not resolve")


def _load_exact_json(value: str) -> object:
    def reject_constant(constant: str) -> object:
        raise ValueError(f"non-finite JSON number is not allowed: {constant}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key is not allowed")
            result[key] = item
        return result

    try:
        return json.loads(
            value,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid bounded JSON text") from exc


def _exact_json_depth(value: object) -> int:
    maximum = 1
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        node, depth = pending.pop()
        maximum = max(maximum, depth)
        if maximum > _JSON_MAX_DEPTH:
            return maximum
        if isinstance(node, dict):
            pending.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return maximum


__all__ = [
    "ExactJsonPayload",
    "ExactParquetPayload",
    "ExactPayloadInput",
    "ExactPayloadResolutionError",
    "ExactPayloadResolver",
    "ExactTransformPayload",
    "JSON_MEDIA_TYPE",
    "JSON_SCHEMA_MEDIA_TYPE",
    "PARQUET_MEDIA_TYPE",
    "exact_payload_arrow_inputs",
    "exact_payload_members",
]
