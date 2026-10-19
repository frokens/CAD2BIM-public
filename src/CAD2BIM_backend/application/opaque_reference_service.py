"""Project authenticated owner references through a durable short navigation index.

The index never authorizes an object. Consumers still decode the original
authenticated reference and perform their existing scope, type and state checks.
Pagination claims are deliberately excluded.
"""

from collections.abc import Collection, Mapping
from datetime import datetime
import re

from CAD2BIM_backend.application.catalog_scope import catalog_scope_digest
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError, OpaqueCursorCodecPort
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError, ReferenceIndexPort


REFERENCE_KINDS = frozenset({"gr", "gv", "fr", "fv", "gd", "fd", "sc", "gq", "gi", "or"})
SHORT_REFERENCE_PATTERN = rf"^({'|'.join(sorted(REFERENCE_KINDS))})_[A-Za-z0-9_-]{{22}}$"
_SHORT_REFERENCE = re.compile(SHORT_REFERENCE_PATTERN)
_CODE_KINDS = frozenset({
    "geometry_draft", "product_family_draft",
    "geometry_authoring_attempt", "geometry_resource_program", "geometry_query_attempt",
    "product_family_authoring_attempt", "product_family_realization_program",
})


def encode_operation_reference(codec, context, *, origin_run_id: str, operation_id: str) -> str:
    return codec.encode({"domain": "operation_reference", "scope": catalog_scope_digest(context),
        "origin_run_id": origin_run_id, "operation_id": operation_id})


def reference_kind(claims: Mapping[str, object]) -> str | None:
    domain = claims.get("domain")
    if domain == "operation_reference":
        return "or"
    if domain == "geometry_catalog_reference":
        return {"resource_root": "gr", "resource_version": "gv"}.get(claims.get("reference_kind"))
    if domain == "product_family_catalog_reference":
        return {"family_root": "fr", "family_version": "fv"}.get(claims.get("reference_kind"))
    if domain == "code_discovery_locator" and claims.get("code_kind") in _CODE_KINDS:
        return "sc"
    if domain == "geometry_query_attempt_alias":
        return "gq"
    if domain == "geometry_inspection_reference":
        return "gi"
    if domain == "authoring_draft":
        return {"geometry": "gd", "family": "fd"}.get(claims.get("root_kind"))
    return None


class OpaqueReferenceService:
    def __init__(self, repository: ReferenceIndexPort, codec: OpaqueCursorCodecPort) -> None:
        self._repository = repository
        self._codec = codec

    def operation_reference(self, context, *, origin_run_id: str, operation_id: str) -> str:
        return encode_operation_reference(self._codec, context,
            origin_run_id=origin_run_id, operation_id=operation_id)

    def operation_locator(self, context, reference: str) -> tuple[str, str]:
        try:
            claims = self._codec.decode(reference)
            if (claims.get("domain") != "operation_reference"
                    or claims.get("scope") != catalog_scope_digest(context)
                    or not isinstance(claims.get("origin_run_id"), str)
                    or not claims["origin_run_id"]
                    or not isinstance(claims.get("operation_id"), str)
                    or not claims["operation_id"]):
                raise ValueError("operation reference authority differs")
            return claims["origin_run_id"], claims["operation_id"]
        except (OpaqueCursorCodecError, KeyError, TypeError, ValueError):
            raise ReferenceIndexError("The Operation reference is unavailable in this scope") from None

    def ifc_revision_reference(self, context, exact) -> str:
        if exact.scope.tenant_id != context.tenant_id or exact.scope.task_id != context.task_id:
            raise ReferenceIndexError("The exact IFC read basis differs from the authorized scope")
        return self._codec.encode({"domain": "ifc_revision_reference", "scope": catalog_scope_digest(context),
            "origin_run_id": exact.scope.run_id, "model_id": exact.model_id,
            "revision_id": exact.revision_id, "revision_no": exact.revision_no})

    def ifc_revision_locator(self, context, reference: str) -> tuple[str, str, str, int]:
        try:
            claims = self._codec.decode(reference)
            if (claims.get("domain") != "ifc_revision_reference"
                    or claims.get("scope") != catalog_scope_digest(context)
                    or any(not isinstance(claims.get(key), str) or not claims[key]
                        for key in ("origin_run_id", "model_id", "revision_id"))
                    or type(claims.get("revision_no")) is not int or claims["revision_no"] < 1):
                raise ValueError("IFC revision reference authority differs")
            return claims["origin_run_id"], claims["model_id"], claims["revision_id"], claims["revision_no"]
        except (OpaqueCursorCodecError, KeyError, TypeError, ValueError):
            raise ReferenceIndexError("The exact IFC revision reference is unavailable in this scope") from None

    def ifc_products_recovery_cursor(self, scope, *, tool_name: str, created_at: datetime, operation_id: str) -> str:
        if tool_name not in {"ifc_products_apply", "ifc_products_edit"}:
            raise ReferenceIndexError("The product recovery tool filter is unsupported")
        return self._codec.encode({"domain": "ifc_products_recovery", "scope": catalog_scope_digest(scope.identity),
            "run_id": scope.run_id, "tool_name": tool_name,
            "created_at": created_at.isoformat(), "operation_id": operation_id})

    def parse_ifc_products_recovery_cursor(self, scope, cursor: str, *, tool_name: str) -> tuple[datetime, str]:
        try:
            claims = self._codec.decode(cursor)
            created_at = datetime.fromisoformat(claims["created_at"])
            operation_id = claims["operation_id"]
            if (tool_name not in {"ifc_products_apply", "ifc_products_edit"}
                    or claims.get("domain") != "ifc_products_recovery"
                    or claims.get("tool_name") != tool_name
                    or claims.get("scope") != catalog_scope_digest(scope.identity)
                    or claims.get("run_id") != scope.run_id
                    or created_at.tzinfo is None or created_at.utcoffset() is None
                    or not isinstance(operation_id, str) or not operation_id):
                raise ValueError("Product recovery cursor authority differs")
            return created_at, operation_id
        except (OpaqueCursorCodecError, KeyError, TypeError, ValueError):
            raise ReferenceIndexError("The product recovery cursor is unavailable in this scope or tool filter") from None

    async def shorten(
        self, context: TenantExecutionContext, value: str, allowed_kinds: Collection[str]
    ) -> str:
        if not value.startswith("cur."):
            return value
        try:
            kind = reference_kind(self._codec.decode(value))
        except (OpaqueCursorCodecError, TypeError, ValueError):
            raise ReferenceIndexError("The server reference could not be authenticated") from None
        if kind is None:
            return value  # Other owner kinds and page cursors retain their current protocol.
        if kind not in allowed_kinds:
            raise ReferenceIndexError("The server reference has the wrong type for its field")
        return await self._repository.get_or_create_reference(
            context, scope_digest=catalog_scope_digest(context), reference_kind=kind,
            sealed_reference=value,
        )

    async def expand(
        self, context: TenantExecutionContext, value: str, allowed_kinds: Collection[str]
    ) -> str:
        if value.startswith("cur."):
            return value  # Authenticated references retain their domain validation path.
        match = _SHORT_REFERENCE.fullmatch(value)
        if match is None or match.group(1) not in allowed_kinds:
            raise ReferenceIndexError("The reference type or spelling is invalid")
        indexed = await self._repository.resolve_reference(
            context, scope_digest=catalog_scope_digest(context), handle=value,
        )
        if indexed is None:
            raise ReferenceIndexError("The reference is not available in the current scope")
        kind, sealed = indexed
        try:
            authenticated_kind = reference_kind(self._codec.decode(sealed))
        except (OpaqueCursorCodecError, TypeError, ValueError):
            raise ReferenceIndexError("The stored reference could not be authenticated") from None
        if kind != match.group(1) or authenticated_kind != kind:
            raise ReferenceIndexError("The stored reference type is inconsistent")
        return sealed
