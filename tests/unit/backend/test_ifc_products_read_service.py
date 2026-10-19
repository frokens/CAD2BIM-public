from contextlib import nullcontext
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from CAD2BIM_backend.application.data_runtime_service import OperationInspection
from CAD2BIM_backend.application.ifc_products_read_service import IfcProductsReadService
from CAD2BIM_backend.application.product_apply_report import REPORT_SCHEMAS
from CAD2BIM_backend.domain.data_runtime import CriticalOperationStatus, IfcProductsApplyOperationDetail
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.domain.ifc_products_read import (
    IfcProductReadFacts, IfcProductsReadAppliedQuery, IfcProductsReadCurrentQuery,
    IfcProductsReadError, IfcProductsReadIfcQuery, IfcProductsReadRuntimeResult,
)
from tests.unit.backend.test_product_realization_preview_service import _scope


ROOT = "1" * 22
CHILD = "2" * 22


def product(key="product-1", *, committed=True, index=0):
    return dict(input_row_index=index, product_key=key, solve_status="SOLVED", build_status="BUILT",
        commit_status="COMMITTED" if committed else "NOT_COMMITTED", root_global_id=ROOT if committed else None)


class Payloads:
    """Materialization spy; the production service still scans real Parquet bytes."""
    def __init__(self, tmp_path, rows):
        self.directory, self.rows, self.calls = tmp_path, rows, []

    def payload_from_exact_package(self, package, **kwargs):
        assert kwargs["package_id"] == package.package.package_id
        assert any(member.member_id == kwargs["payload_member_id"] for member in package.members)

    async def materialize_members(self, members, *, check_budget, max_total_bytes):
        check_budget()
        self.calls.append((tuple(member.logical_payload_name for member in members), max_total_bytes))
        paths = {}
        for member in members:
            name = member.logical_payload_name
            path = self.directory / f"{name}.parquet"
            pq.write_table(pa.Table.from_pylist(self.rows[name], schema=REPORT_SCHEMAS[name]), path)
            paths[member.member_id] = path
        return paths

    @staticmethod
    def remove_paths(paths):
        for path in paths:
            path.unlink()


def fixture(tmp_path, *, rows=None, status=CriticalOperationStatus.SUCCEEDED, report=True):
    scope = _scope()
    ifc_scope = IfcRunIdentity(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id, run_id=scope.run_id)
    exact = SimpleNamespace(model_id="model-original", scope=ifc_scope)
    detail = IfcProductsApplyOperationDetail.model_validate({
        "request": {"family_version_id": "family-version", "package_id": "input-package", "payload_name": "products"},
        "input_binding": {"package_id": "input-package", "member_id": "input-member", "payload_name": "products",
            "logical_key_fields": ["product_key"]},
    })
    report_package = SimpleNamespace(package=SimpleNamespace(package_id="report-package"),
        members=tuple(SimpleNamespace(member_id=name, logical_payload_name=name) for name in ("products", "components")),
        catalog_entry=SimpleNamespace(alias="data_002")) if report else None
    inspection = OperationInspection(operation=SimpleNamespace(operation=SimpleNamespace(
        detail=detail, status=status)), result_package=report_package, input_packages={},
        committed_ifc_revision=exact if status is CriticalOperationStatus.SUCCEEDED else None)

    class Data:
        async def get_operation_by_locator(self, call_scope, **locator):
            assert locator == {"origin_run_id": "origin-run", "operation_id": "operation-original"}
            return inspection

    class Repository:
        entries = (SimpleNamespace(package_id="other-package", alias="data_002"),
            SimpleNamespace(package_id="input-package", alias="data_009"))

        async def list_package_catalog(self, call_scope, **kwargs):
            assert call_scope is scope
            return SimpleNamespace(entries=self.entries, complete=True)

    class Runtime:
        entries = 0

        def invocation_scope(self, *args, **kwargs):
            self.entries += 1
            return nullcontext()

    class Writer:
        pins = 0
        reads = []
        current = exact

        async def pin_current(self, call_scope):
            assert call_scope == ifc_scope
            self.pins += 1
            return self.current

        async def read_projection(self, pinned, *, request):
            self.reads.append((pinned, request))
            return SimpleNamespace(products=IfcProductsReadRuntimeResult(products=tuple(
                IfcProductReadFacts(global_id=guid, status="not_found", match_count=0)
                for guid in request.products_request.global_ids)))

    report_rows = rows if rows is not None else {"products": [product()], "components": [
        dict(product_key="product-1", component_key="root", parent_component_key=None,
            ifc_class="IfcElementAssembly", project_world_transform_json="[]", build_status="BUILT",
            commit_status="COMMITTED", global_id=ROOT),
        dict(product_key="product-1", component_key="child", parent_component_key="root",
            ifc_class="IfcValve", project_world_transform_json="[]", build_status="BUILT",
            commit_status="COMMITTED", global_id=CHILD)]}
    payloads, repository, writer, runtime = Payloads(tmp_path, report_rows), Repository(), Writer(), Runtime()
    service = IfcProductsReadService(data_runtime=Data(), repository=repository,
        payloads=payloads, writer=writer, runtime=runtime)
    return service, scope, inspection, payloads, repository, writer, runtime


async def read(service, scope, query):
    return await service.read(scope, query, operation_locator=("origin-run", "operation-original"))


@pytest.mark.asyncio
async def test_applied_uses_exact_report_and_current_catalog_identity_without_runtime(tmp_path):
    service, scope, _, payloads, _, writer, runtime = fixture(tmp_path)
    result = await read(service, scope, IfcProductsReadAppliedQuery(product_keys=("product-1",)))
    assert result.facts.items[0].global_id == ROOT
    assert result.facts.items[0].apply_match.input_row_index == 0
    assert result.input_alias == "data_009" and result.report_alias is None
    assert payloads.calls == [(("products",), 256 * 1024 * 1024)]
    assert writer.pins == 0 and runtime.entries == 0
    assert result.source_exact is None
    assert not tuple(tmp_path.glob("*.parquet"))


@pytest.mark.asyncio
async def test_duplicate_report_keys_remain_ambiguous_and_do_not_locate_current(tmp_path):
    rows = {"products": [product(index=0), product(index=1)], "components": []}
    service, scope, _, _, _, writer, _ = fixture(tmp_path, rows=rows)
    result = await read(service, scope, IfcProductsReadCurrentQuery(view="current", product_keys=("product-1",)))
    match = result.facts.items[0].apply_match
    assert (match.status, match.report_match_count, match.root_global_id) == ("ambiguous", 2, None)
    assert writer.pins == 0


@pytest.mark.asyncio
async def test_root_component_roles_merge_and_child_keeps_original_input_relation(tmp_path):
    service, scope, _, _, _, writer, runtime = fixture(tmp_path)
    result = await read(service, scope, IfcProductsReadIfcQuery(global_ids=(ROOT, CHILD)))
    root, child = (item.apply_match for item in result.facts.items)
    assert root.roles == ("root", "component") and root.report_match_count == 1
    assert child.roles == ("component",) and child.parent_component_key == "root"
    assert child.input_row_index == 0 and child.root_global_id == ROOT
    assert writer.pins == 1 and len(writer.reads) == 1 and runtime.entries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE])
async def test_unsettled_apply_keeps_mapping_unknown_without_current_lookup(tmp_path, status):
    service, scope, _, _, _, writer, _ = fixture(tmp_path, status=status, report=False)
    result = await read(service, scope, IfcProductsReadCurrentQuery(view="current", product_keys=("product-1",)))
    match = result.facts.items[0].apply_match
    assert match.status == "unknown" and match.report_match_count is None and match.root_global_id is None
    assert writer.pins == 0


@pytest.mark.asyncio
async def test_confirmed_failure_keeps_diagnostics_but_no_guid_or_current_lookup(tmp_path):
    # Both rows passed precheck; a later build failure left one result unconfirmed.
    first = product(committed=False)
    second = {**product("product-2", committed=False, index=1),
        "build_status": "UNCONFIRMED"}
    rows = {"products": [first, second], "components": [
        dict(product_key="product-1", component_key="root", parent_component_key=None,
            ifc_class="IfcElementAssembly", project_world_transform_json="[]", build_status="BUILT",
            commit_status="NOT_COMMITTED", global_id=None),
        dict(product_key="product-2", component_key="root", parent_component_key=None,
            ifc_class="IfcElementAssembly", project_world_transform_json="[]", build_status="NOT_EXECUTED",
            commit_status="NOT_COMMITTED", global_id=None)]}
    service, scope, _, _, _, writer, _ = fixture(tmp_path, status=CriticalOperationStatus.FAILED, rows=rows)
    result = await read(service, scope, IfcProductsReadCurrentQuery(view="current",
        product_keys=("product-1", "product-2")))
    assert [item.apply_match.solve_status for item in result.facts.items] == ["SOLVED", "SOLVED"]
    assert [item.apply_match.build_status for item in result.facts.items] == ["BUILT", "UNCONFIRMED"]
    assert all(item.apply_match.status == "not_committed" and item.apply_match.root_global_id is None
        and item.global_id is None and item.current is None for item in result.facts.items)
    assert writer.pins == 0
    current = await read(service, scope, IfcProductsReadIfcQuery(global_ids=(ROOT,)))
    assert current.facts.items[0].apply_match.status == "not_committed"
    assert current.facts.items[0].current.status == "not_found" and writer.pins == 1


@pytest.mark.asyncio
async def test_source_not_queried_differs_from_no_match_in_supplied_operation(tmp_path):
    service, scope, _, _, _, writer, _ = fixture(tmp_path)
    query = IfcProductsReadIfcQuery(global_ids=("3" * 22,))
    direct = await service.read(scope, query)
    supplied = await read(service, scope, query)
    assert direct.facts.items[0].apply_match.status == "not_checked"
    assert direct.source_exact is writer.reads[0][0]
    assert supplied.facts.items[0].apply_match.status == "not_found"
    assert supplied.source_exact is writer.reads[1][0]
    assert writer.pins == 2


@pytest.mark.asyncio
async def test_same_guid_in_different_model_is_rejected_before_inspection(tmp_path):
    service, scope, _, _, _, writer, _ = fixture(tmp_path)
    writer.current = SimpleNamespace(model_id="another-model", scope=writer.current.scope)
    with pytest.raises(IfcProductsReadError, match="another model") as caught:
        await read(service, scope, IfcProductsReadIfcQuery(global_ids=(ROOT,)))
    assert caught.value.code == "IFC_PRODUCTS_READ_MODEL_MISMATCH" and not writer.reads


@pytest.mark.asyncio
async def test_report_scan_deadline_propagates_without_an_empty_success(tmp_path, monkeypatch):
    import CAD2BIM_backend.application.ifc_products_read_service as module
    service, scope, *_ = fixture(tmp_path)
    monkeypatch.setattr(module, "REPORT_SCAN_SECONDS", -1.0)
    with pytest.raises(IfcProductsReadError) as caught:
        await read(service, scope, IfcProductsReadAppliedQuery(product_keys=("product-1",)))
    assert caught.value.code == "IFC_PRODUCTS_READ_BUDGET"


@pytest.mark.asyncio
async def test_legal_long_product_key_overflows_combined_facts_with_actionable_budget_code(tmp_path):
    key = "long-legal-key-" + "x" * 140_000
    rows = {"products": [product(key)], "components": []}
    service, scope, *_ = fixture(tmp_path, rows=rows)
    query = IfcProductsReadAppliedQuery(product_keys=(key,))
    with pytest.raises(IfcProductsReadError) as caught:
        await read(service, scope, query)
    assert caught.value.code == "IFC_PRODUCTS_READ_LIMIT_EXCEEDED"
    assert "fewer" in str(caught.value).lower() or "query" in str(caught.value).lower()
