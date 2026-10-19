from __future__ import annotations

import base64
import secrets
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError


def reference_scope(**changes):
    return TenantExecutionContext(
        tenant_id="tenant_a", principal_id="principal_a", task_id="task_a",
        actor_kind=TenantActorKind.USER, command_id="command_a",
    ).model_copy(update=changes)


def reference_codec():
    return CursorCodec(base64.urlsafe_b64encode(b"r" * 32).rstrip(b"=").decode())


class MemoryReferenceIndex:
    """Test double only; PostgreSQL tests exercise durability and authorization."""
    def __init__(self):
        self.rows = {}
        self.writes = 0

    async def get_or_create_reference(self, context, *, scope_digest, reference_kind, sealed_reference):
        for handle, (scope, kind, value) in self.rows.items():
            if scope == scope_digest and value == sealed_reference:
                assert kind == reference_kind
                return handle
        handle = reference_kind + "_" + secrets.token_urlsafe(16)
        self.rows[handle] = scope_digest, reference_kind, sealed_reference
        self.writes += 1
        return handle

    async def resolve_reference(self, context, *, scope_digest, handle):
        row = self.rows.get(handle)
        return (row[1], row[2]) if row and row[0] == scope_digest else None


@pytest.mark.parametrize("tool_name", ["ifc_products_apply", "ifc_products_edit"])
def test_products_recovery_cursor_binds_filter_keyset_scope_run_without_index_state(tool_name):
    index = MemoryReferenceIndex()
    service = OpaqueReferenceService(index, reference_codec())
    scope = SimpleNamespace(identity=reference_scope(), run_id="run-original")
    created_at = datetime(2026, 10, 14, tzinfo=UTC)
    cursor = service.ifc_products_recovery_cursor(scope, tool_name=tool_name, created_at=created_at, operation_id="original-operation")
    assert service.parse_ifc_products_recovery_cursor(scope, cursor, tool_name=tool_name) == (created_at, "original-operation")
    for invalid_scope in (SimpleNamespace(identity=reference_scope(), run_id="other-run"),
                          SimpleNamespace(identity=reference_scope(task_id="other-task"), run_id=scope.run_id)):
        with pytest.raises(ReferenceIndexError):
            service.parse_ifc_products_recovery_cursor(invalid_scope, cursor, tool_name=tool_name)
    with pytest.raises(ReferenceIndexError):
        service.parse_ifc_products_recovery_cursor(scope, cursor[:-2] + "xx", tool_name=tool_name)
    with pytest.raises(ReferenceIndexError):
        service.parse_ifc_products_recovery_cursor(scope, cursor,
            tool_name="ifc_products_edit" if tool_name == "ifc_products_apply" else "ifc_products_apply")
    assert index.writes == 0


@pytest.mark.asyncio
async def test_exact_ifc_read_reference_retains_same_basis_without_new_short_index():
    index, context = MemoryReferenceIndex(), reference_scope()
    service = OpaqueReferenceService(index, reference_codec())
    exact = SimpleNamespace(scope=SimpleNamespace(tenant_id=context.tenant_id, task_id=context.task_id,
        run_id="actual-original-run"), model_id="actual-model", revision_id="actual-revision", revision_no=7)
    reference = service.ifc_revision_reference(context, exact)
    assert service.ifc_revision_locator(context, reference) == ("actual-original-run", "actual-model", "actual-revision", 7)
    assert await service.shorten(context, reference, {"or"}) == reference
    assert index.writes == 0
    for other in (reference_scope(task_id="other"), reference_scope(principal_id="other"), reference_scope(tenant_id="other")):
        with pytest.raises(ReferenceIndexError):
            service.ifc_revision_locator(other, reference)
    with pytest.raises(ReferenceIndexError):
        service.ifc_revision_locator(context, service.operation_reference(context, origin_run_id="run", operation_id="operation"))
    with pytest.raises(ReferenceIndexError):
        service.ifc_revision_reference(reference_scope(task_id="other"), exact)


@pytest.mark.asyncio
@pytest.mark.parametrize(("claims", "kind"), [
    ({"domain":"geometry_catalog_reference", "reference_kind":"resource_root"}, "gr"),
    ({"domain":"geometry_catalog_reference", "reference_kind":"resource_version"}, "gv"),
    ({"domain":"product_family_catalog_reference", "reference_kind":"family_root"}, "fr"),
    ({"domain":"product_family_catalog_reference", "reference_kind":"family_version"}, "fv"),
    ({"domain":"code_discovery_locator", "code_kind":"geometry_authoring_attempt"}, "sc"),
    ({"domain":"code_discovery_locator", "code_kind":"product_family_realization_program"}, "sc"),
    ({"domain":"geometry_query_attempt_alias"}, "gq"),
    ({"domain":"geometry_inspection_reference"}, "gi"),
])
async def test_short_handle_reuses_exact_authenticated_reference(claims, kind):
    index, codec, scope = MemoryReferenceIndex(), reference_codec(), reference_scope()
    service = OpaqueReferenceService(index, codec)
    old = codec.encode({**claims, "exact": "object-1"})
    handle = await service.shorten(scope, old, (kind,))
    assert len(handle) == 25 and handle.startswith(kind + "_")
    assert await service.shorten(scope.model_copy(update={"command_id":"another"}), old, (kind,)) == handle
    assert index.writes == 1
    restarted = OpaqueReferenceService(index, reference_codec())
    assert await restarted.expand(scope, handle, (kind,)) == old
    assert await restarted.expand(scope, old, (kind,)) == old
    newer = codec.encode({**claims, "exact": "object-2"})
    assert await restarted.shorten(scope, newer, (kind,)) != handle
    assert await restarted.expand(scope, handle, (kind,)) == old


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,claims", [
    ("gv", {"domain":"geometry_catalog_reference", "reference_kind":"resource_version"}),
    ("gi", {"domain":"geometry_inspection_reference"}),
])
@pytest.mark.parametrize("changed", [
    {"tenant_id":"tenant_b"}, {"task_id":"task_b"}, {"principal_id":"principal_b"},
])
async def test_short_handle_cannot_cross_scope(changed, kind, claims):
    service = OpaqueReferenceService(MemoryReferenceIndex(), reference_codec())
    old = reference_codec().encode(claims)
    handle = await service.shorten(reference_scope(), old, (kind,))
    with pytest.raises(ReferenceIndexError, match="current scope"):
        await service.expand(reference_scope(**changed), handle, (kind,))


@pytest.mark.asyncio
async def test_wrong_type_typo_and_index_corruption_are_rejected():
    index, codec, scope = MemoryReferenceIndex(), reference_codec(), reference_scope()
    service = OpaqueReferenceService(index, codec)
    old = codec.encode({"domain":"geometry_catalog_reference", "reference_kind":"resource_version"})
    handle = await service.shorten(scope, old, ("gv",))
    for invalid, kinds in [(handle, ("fv",)), (handle[:-1], ("gv",)),
                           (handle[:-1] + ("A" if handle[-1] != "A" else "B"), ("gv",))]:
        with pytest.raises(ReferenceIndexError):
            await service.expand(scope, invalid, kinds)
    row = index.rows[handle]
    index.rows[handle] = row[0], row[1], row[2][:-1]
    with pytest.raises(ReferenceIndexError, match="authenticated"):
        await service.expand(scope, handle, ("gv",))


@pytest.mark.asyncio
async def test_page_cursors_and_unselected_code_owners_are_not_indexed():
    index, codec, scope = MemoryReferenceIndex(), reference_codec(), reference_scope()
    service = OpaqueReferenceService(index, codec)
    for claims in [{"domain":"geometry_catalog_list_cursor"},
                   {"domain":"code_discovery_locator", "code_kind":"data_query_sql_attempt"}]:
        old = codec.encode(claims)
        assert await service.shorten(scope, old, ("sc",)) == old
    assert index.writes == 0


@pytest.mark.asyncio
async def test_operation_reference_preserves_origin_run_and_rejects_other_task():
    service = OpaqueReferenceService(MemoryReferenceIndex(), reference_codec())
    scope = reference_scope()
    sealed = service.operation_reference(scope, origin_run_id="run-original", operation_id="operation-original")
    handle = await service.shorten(scope, sealed, ("or",))
    assert handle.startswith("or_")
    restored = await service.expand(scope, handle, ("or",))
    assert service.operation_locator(scope, restored) == ("run-original", "operation-original")
    with pytest.raises(ReferenceIndexError):
        service.operation_locator(reference_scope(task_id="different-task"), restored)


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["geometry_preview_capability_v1", "geometry_preview_capability_v2"])
async def test_retired_preview_capability_is_not_shortened_and_old_index_rows_are_rejected(domain):
    index, codec, scope = MemoryReferenceIndex(), reference_codec(), reference_scope()
    service = OpaqueReferenceService(index, codec)
    retired = codec.encode({"domain": domain, "target_kind": "product_realization_preview"})
    assert await service.shorten(scope, retired, ("pp",)) == retired
    assert index.writes == 0
    # The signed envelope stays generic; the Preview owner rejects its old domain.
    assert await service.expand(scope, retired, ("pp",)) == retired
    from CAD2BIM_backend.application.catalog_scope import catalog_scope_digest
    old_handle = await index.get_or_create_reference(
        scope, scope_digest=catalog_scope_digest(scope), reference_kind="pp", sealed_reference=retired,
    )
    with pytest.raises(ReferenceIndexError, match="type or spelling is invalid"):
        await service.expand(scope, old_handle, ("pp",))
