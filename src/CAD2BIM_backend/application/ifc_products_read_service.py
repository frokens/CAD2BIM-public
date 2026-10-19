"""Selected report snapshots and CURRENT facts, without a second result store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic

import pyarrow.parquet as pq

from CAD2BIM_backend.application.data_runtime_service import OperationInspection
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationStatus, IfcProductsApplyOperationDetail,
)
from CAD2BIM_backend.domain.ifc_model import ExactIfcRevision, IfcModelIntegrityError, IfcRunIdentity
from CAD2BIM_backend.domain.ifc_products_read import (
    IfcApplyProductMatch, IfcProductsReadError, IfcProductsReadFacts,
    IfcProductsReadItem, IfcProductsReadQuery, IfcProductsReadRuntimeRequest,
)
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_EXECUTION
from CAD2BIM_backend.ports.ifc_revision_runtime import InspectExactIfcRevisionRequest


MAX_REPORT_INPUT_BYTES = 256 * 1024 * 1024
MAX_REPORT_SCAN_ROWS = 5_000_000
REPORT_SCAN_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ProductsReadOutcome:
    facts: IfcProductsReadFacts
    operation: OperationInspection | None
    input_alias: str | None
    report_alias: str | None
    source_exact: ExactIfcRevision | None = None


@dataclass(slots=True)
class _Matches:
    count: int = 0
    first: dict | None = None

    def add(self, row):
        self.count += 1
        self.first = row if self.count == 1 else None


class IfcProductsReadService:
    def __init__(self, *, data_runtime, repository, payloads, writer, runtime):
        self._data, self._repository, self._payloads = data_runtime, repository, payloads
        self._writer, self._runtime = writer, runtime

    async def read(self, scope, query: IfcProductsReadQuery, *, operation_locator=None):
        deadline = monotonic() + IFC_REVISION_EXECUTION.total_seconds
        try:
            async with asyncio.timeout(IFC_REVISION_EXECUTION.total_seconds):
                return await self._read(scope, query, operation_locator, deadline)
        except TimeoutError as exc:
            raise IfcProductsReadError("IFC_PRODUCTS_READ_BUDGET",
                "The product read exceeded its invocation deadline") from exc

    async def _read(self, scope, query, operation_locator, deadline):
        inspection = None
        if operation_locator is not None:
            origin_run_id, operation_id = operation_locator
            inspection = await self._data.get_operation_by_locator(
                scope, origin_run_id=origin_run_id, operation_id=operation_id)
            if not isinstance(inspection.operation.operation.detail, IfcProductsApplyOperationDetail):
                raise IfcProductsReadError("IFC_PRODUCTS_READ_OPERATION_KIND",
                    "The source Operation must be a current product Apply Operation")
        if query.mode == "from_apply" and inspection is None:
            raise IfcProductsReadError("IFC_PRODUCTS_READ_OPERATION_REQUIRED",
                "from_apply requires the original Apply Operation")

        products, roots, components = await self._report(inspection, query, deadline)
        aliases = await self._current_aliases(scope, inspection, deadline)
        items = []
        if query.mode == "from_apply":
            for key in query.product_keys:
                match = self._product_match(inspection, products.get(key, _Matches()))
                items.append(IfcProductsReadItem(product_key=key,
                    global_id=match.root_global_id, apply_match=match))
            view = query.view
            global_ids = tuple(dict.fromkeys(item.global_id for item in items if item.global_id))
        else:
            view = "current"
            global_ids = query.global_ids
            for guid in global_ids:
                match = self._guid_match(inspection, guid, products, roots, components)
                items.append(IfcProductsReadItem(global_id=guid,
                    product_key=match.product_key, apply_match=match))

        source_exact = None
        if view == "current" and global_ids:
            ifc_scope = IfcRunIdentity(owner_id=scope.owner_id,
                tenant_id=scope.identity.tenant_id, task_id=scope.identity.task_id,
                run_id=scope.run_id)
            async with self._runtime.invocation_scope(IFC_REVISION_EXECUTION, deadline=deadline):
                exact = await self._writer.pin_current(ifc_scope)
                if inspection is not None and self._succeeded(inspection):
                    original = inspection.committed_ifc_revision
                    if original is None:
                        raise IfcModelIntegrityError("A successful Apply has no exact committed metadata")
                    if original.model_id != exact.model_id or original.scope != exact.scope:
                        raise IfcProductsReadError("IFC_PRODUCTS_READ_MODEL_MISMATCH",
                            "The original Apply belongs to another model; use applied for its historical report")
                result = await self._writer.read_projection(exact,
                    request=InspectExactIfcRevisionRequest(projection="products",
                        products_request=IfcProductsReadRuntimeRequest(
                            global_ids=global_ids, groups=query.groups)))
                if result.products is None:
                    raise IfcModelIntegrityError("IFC inspection omitted the requested product facts")
                found = {product.global_id: product for product in result.products.products}
                if set(found) != set(global_ids) or len(found) != len(result.products.products):
                    raise IfcModelIntegrityError("IFC product facts do not match the fixed selectors")
                items = [item.model_copy(update={"current": found.get(item.global_id)}) for item in items]
                source_exact = exact
        return ProductsReadOutcome(
            facts=IfcProductsReadFacts(view=view, items=tuple(items)), operation=inspection,
            input_alias=aliases[0], report_alias=aliases[1], source_exact=source_exact)

    @staticmethod
    def _succeeded(inspection):
        return inspection.operation.operation.status is CriticalOperationStatus.SUCCEEDED

    @staticmethod
    def _check(deadline):
        if monotonic() >= deadline:
            raise IfcProductsReadError("IFC_PRODUCTS_READ_BUDGET",
                "Report reading exceeded its deadline; use existing bounded Data queries for details")

    async def _current_aliases(self, scope, inspection, deadline):
        if inspection is None:
            return None, None
        binding = inspection.operation.operation.detail.input_binding
        package_ids = {binding.package_id}
        report_id = None if inspection.result_package is None else inspection.result_package.package.package_id
        if report_id:
            package_ids.add(report_id)
        aliases, before = {}, None
        while package_ids - aliases.keys():
            self._check(deadline)
            page = await self._repository.list_package_catalog(scope, before_sequence=before, page_size=50)
            for entry in page.entries:
                if entry.package_id in package_ids:
                    aliases[entry.package_id] = entry.alias
            if page.complete:
                break
            before = page.next_before_sequence
        return aliases.get(binding.package_id), aliases.get(report_id)

    async def _report(self, inspection, query, deadline):
        products, roots, components = {}, {}, {}
        if inspection is None or inspection.result_package is None:
            return products, roots, components
        package = inspection.result_package
        names = ("products",) if query.mode == "from_apply" else ("components", "products")
        selected = {}
        for name in names:
            members = [member for member in package.members if member.logical_payload_name == name]
            if len(members) != 1:
                raise IfcModelIntegrityError("The Apply report is missing a unique required table")
            member = members[0]
            self._payloads.payload_from_exact_package(package,
                package_id=package.package.package_id, payload_member_id=member.member_id)
            selected[name] = member
        scan_deadline = min(deadline, monotonic() + REPORT_SCAN_SECONDS)
        paths = await self._payloads.materialize_members(tuple(selected.values()),
            check_budget=lambda: self._check(scan_deadline), max_total_bytes=MAX_REPORT_INPUT_BYTES)
        scanned = 0
        try:
            keys = set(query.product_keys) if query.mode == "from_apply" else set()
            guids = set(query.global_ids) if query.mode == "from_ifc" else set()
            for name in names:
                columns = (["product_key", "global_id", "component_key", "parent_component_key", "commit_status"]
                    if name == "components" else ["input_row_index", "product_key", "solve_status",
                        "build_status", "commit_status", "root_global_id"])
                parquet = pq.ParquetFile(paths[selected[name].member_id])
                if not set(columns).issubset(parquet.schema_arrow.names):
                    raise IfcModelIntegrityError("The Apply report table lacks required current columns")
                for batch in parquet.iter_batches(batch_size=4096, columns=columns):
                    self._check(scan_deadline)
                    scanned += batch.num_rows
                    if scanned > MAX_REPORT_SCAN_ROWS:
                        raise IfcProductsReadError("IFC_PRODUCTS_READ_REPORT_LIMIT",
                            "The report scan exceeds five million rows; use existing Data queries")
                    for row in batch.to_pylist():
                        if name == "components":
                            guid = row["global_id"]
                            if guid in guids:
                                matches = components.setdefault(guid, _Matches())
                                matches.add(row)
                                # More candidates only establish ambiguity; do not accumulate their keys.
                                if matches.count == 1:
                                    keys.add(row["product_key"])
                        else:
                            key, guid = row["product_key"], row["root_global_id"]
                            if key in keys:
                                products.setdefault(key, _Matches()).add(row)
                            if guid in guids:
                                roots.setdefault(guid, _Matches()).add(row)
                    await asyncio.sleep(0)
        finally:
            self._payloads.remove_paths(paths.values())
        return products, roots, components

    def _product_match(self, inspection, rows):
        operation = inspection.operation.operation
        if operation.status in {CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE}:
            return IfcApplyProductMatch(status="unknown", report_match_count=None)
        if inspection.result_package is None:
            return IfcApplyProductMatch(status="report_unavailable", report_match_count=None)
        if rows.count != 1:
            return IfcApplyProductMatch(status="ambiguous" if rows.count else "not_found",
                report_match_count=rows.count)
        row = rows.first
        submitted = self._succeeded(inspection)
        if row["commit_status"] != ("COMMITTED" if submitted else "NOT_COMMITTED"):
            raise IfcModelIntegrityError("Apply row commit status contradicts the original Operation")
        guid = row["root_global_id"]
        if submitted != (guid is not None):
            raise IfcModelIntegrityError("Apply root GUID contradicts the original whole-batch outcome")
        return IfcApplyProductMatch(status="matched" if submitted else "not_committed",
            report_match_count=1, input_row_index=row["input_row_index"], product_key=row["product_key"],
            root_global_id=guid, roles=("root",) if submitted else (),
            solve_status=row["solve_status"], build_status=row["build_status"], commit_status=row["commit_status"])

    def _guid_match(self, inspection, guid, products, roots, components):
        if inspection is None:
            return IfcApplyProductMatch(status="not_checked", report_match_count=None)
        status = inspection.operation.operation.status
        if status in {CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE}:
            return IfcApplyProductMatch(status="unknown", report_match_count=None)
        if status is CriticalOperationStatus.FAILED:
            return IfcApplyProductMatch(status="not_committed", report_match_count=0)
        if inspection.result_package is None:
            raise IfcModelIntegrityError("A successful Apply has no report")
        root, component = roots.get(guid, _Matches()), components.get(guid, _Matches())
        if root.count > 1 or component.count > 1:
            return IfcApplyProductMatch(status="ambiguous", report_match_count=max(root.count, component.count))
        if not root.count and not component.count:
            return IfcApplyProductMatch(status="not_found", report_match_count=0)
        component_row = component.first
        if component_row is not None and component_row["commit_status"] != "COMMITTED":
            raise IfcModelIntegrityError("The selected component GUID has no committed report binding")
        key = component_row["product_key"] if component_row else root.first["product_key"]
        rows = products.get(key, root if root.count else _Matches())
        match = self._product_match(inspection, rows)
        if match.status != "matched":
            return match
        if root.count and root.first["product_key"] != key:
            return IfcApplyProductMatch(status="ambiguous", report_match_count=2)
        return match.model_copy(update={
            "component_key": component_row["component_key"] if component_row else None,
            "parent_component_key": component_row["parent_component_key"] if component_row else None,
            "roles": tuple(role for role, present in (("root", bool(root.count)), ("component", bool(component.count))) if present),
        })
