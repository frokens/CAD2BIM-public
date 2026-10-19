"""Strict single-Family Package installation through the existing IFC writer."""

from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile
from time import monotonic

from CAD2BIM_backend.application.product_realization_report import record_realization

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from CAD2BIM_backend.application.exact_payload_resolver import ExactParquetPayload
from CAD2BIM_backend.application.ifc_write_support import require_ifc_idempotency_key
from CAD2BIM_backend.application.product_batch_geometry import ProductGeometrySpool, compilation_blocks, fragment_specs_for_product
from CAD2BIM_backend.application.product_apply_report import (
    add_check, empty_report_rows, report_for_commit, stage_product_apply_report,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationStatus, IFC_PRODUCTS_APPLY_INTENT_SUMMARY, IfcProductsApplyOperationDetail,
)
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity, IfcModelConflictError, IfcModelIntegrityError, IfcCommittedVerificationError
from CAD2BIM_backend.domain.ifc_product_compilation import (
    CompileIfcProductsCommand, IfcProductRevisionEffect,
)
from CAD2BIM_backend.domain.product_apply import (
    ApplyIfcProductsCommand, ProductApplyInputBinding, ProductApplyResult, ProductApplySummary,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation, ProductRealizationOutputNeed,
)
from CAD2BIM_backend.domain.runtime_execution import (
    IFC_REVISION_INVOCATION_EXECUTION, MAX_PRODUCT_BATCH_OUTPUT_BYTES, PRODUCT_REALIZATION_EXECUTION,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    CompileIfcProductsRevisionRequest, GeneratedIfcRevision,
)


_INPUT_TYPES = {"product_key": pa.string(), "origin_x_m": pa.float64(),
                "origin_y_m": pa.float64(), "origin_z_m": pa.float64(),
                "parameters_json": pa.string(), "external_constraints_json": pa.string()}
_NUMERIC_GROUP_ITEMS = 1024
# Amortize native startup within the existing request/output byte budgets.
_GEOMETRY_GROUP_ITEMS = 1024


class ProductApplyCallError(RuntimeError):
    def __init__(self, code, message, *, phase="input", normal=False):
        self.code, self.phase, self.normal = code, phase, normal
        super().__init__(message)




class IfcProductsApplyService:
    """Application Core shared by internal engineering and future entry adapters."""

    def __init__(self, *, families, data, payloads, inputs, realization_runtime,
                 revision_runtime, writer, coordinator, storage):
        self._families, self._data, self._payloads = families, data, payloads
        self._inputs, self._realizations = inputs, realization_runtime
        self._runtime, self._writer = revision_runtime, writer
        self._coordinator, self._storage = coordinator, storage

    async def recover(self, scope, *, origin_run_id: str, operation_id: str):
        """Continue the original authorized action without a new request identity."""
        if origin_run_id != scope.run_id or scope.catalog_frontier is not None:
            raise ProductApplyCallError("PRODUCT_APPLY_RECOVERY_SCOPE_MISMATCH",
                "Recovery requires write authority for the original Run", normal=True)
        original = await self._data.get_ifc_products_recovery_operation(scope,
            origin_run_id=origin_run_id, operation_id=operation_id)
        if original is None:
            raise ProductApplyCallError("OPERATION_NOT_FOUND",
                "The original Apply Operation is not available in this scope", normal=True)
        record = original.operation
        if (record.tool_name != "ifc_products_apply"
                or not isinstance(record.detail, IfcProductsApplyOperationDetail)):
            raise ProductApplyCallError("PRODUCT_APPLY_RECOVERY_OPERATION_INVALID",
                "Recovery requires an original current Apply Operation", normal=True)
        ifc_scope = IfcRunIdentity(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id, run_id=scope.run_id)
        try:
            await self._writer.require_recovery_allowed(ifc_scope)
            return await self.apply(scope, record.detail.request, idempotency_key=record.idempotency_key)
        except Exception as exc:
            exc.operation_id = operation_id
            raise

    async def apply(self, scope, command: ApplyIfcProductsCommand, *, idempotency_key: str):
        key = require_ifc_idempotency_key(idempotency_key)
        package = await self._data.get_package(scope, command.package_id)
        if package is None:
            raise ProductApplyCallError("PACKAGE_NOT_FOUND", "The exact input Package is not visible")
        payload = self._payloads.transform_payloads(package).get(command.payload_name)
        if not isinstance(payload, ExactParquetPayload):
            raise ProductApplyCallError("PRODUCT_INPUT_FORMAT_INVALID", "Apply requires one exact Parquet payload")
        detail = IfcProductsApplyOperationDetail(request=command, input_binding=ProductApplyInputBinding(
            package_id=command.package_id, member_id=payload.member.member_id,
            payload_name=command.payload_name, logical_key_fields=payload.logical_key_fields,
        ))
        ifc_scope = IfcRunIdentity(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
                                   task_id=scope.identity.task_id, run_id=scope.run_id)
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        accepted_operation_id = None

        async def execute():
            nonlocal accepted_operation_id
            recovered = await self._recover(ifc_scope, detail, key, digest)
            if recovered is not None:
                return await self._result_from_state(scope, recovered)
            prepared = await self._writer.prepare(ifc_scope, tool_name="ifc_products_apply",
                idempotency_key=key, request_digest=digest, intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY,
                operation_detail=detail.persisted_payload())
            accepted_operation_id = prepared.operation_id
            claim = await self._writer.claim_execution(ifc_scope, prepared)
            guard = self._writer.guard_registration(ifc_scope, claim)
            deadline = monotonic() + claim.remaining_seconds
            rows, timings = empty_report_rows(), {}
            counts = {"input_count": payload.member.row_count or 0, "solve_executed_count": 0,
                      "solve_unconfirmed_count": 0, "build_unconfirmed_count": 0,
                      "solved_count": 0, "interface_constraints_evaluated_product_count": 0,
                      "build_executed_count": 0, "built_count": 0}
            phase, generated, failure_report = "recovery", None, None
            active_indices, active_position, active_window_offset = [], 0, 0

            def set_active_window(received, window_size):
                nonlocal active_indices, active_position, active_window_offset
                active_window_offset = received // window_size * window_size
                active_indices = indices[active_window_offset:active_window_offset + window_size]
                active_position = received - active_window_offset

            def check_budget():
                if monotonic() >= deadline:
                    raise ProductApplyCallError("PRODUCT_APPLY_DEADLINE", "Original invocation deadline exceeded", phase=phase)

            def summary(outcome):
                return ProductApplySummary(**counts, outcome=outcome,
                    committed_count=counts["input_count"] if outcome == "committed" else 0,
                    not_executed_count=counts["input_count"]-counts["solve_executed_count"]-counts["solve_unconfirmed_count"],
                    timings_seconds=dict(timings))

            async def close_failure(exc, *, normal=False):
                nonlocal failure_report
                code = getattr(exc, "code", "PRODUCT_APPLY_TECHNICAL_FAILURE")
                body_configuration_error = phase == "body_preflight" and code in {
                    "IFC_REPRESENTATION_CONTEXT_NOT_FOUND", "IFC_REPRESENTATION_CONTEXT_INCOMPATIBLE",
                }
                normal = normal or body_configuration_error
                message = str(exc)[:1800] or "Product Apply failed"
                action = ("Configure a compatible Body/Model/MODEL_VIEW context with ifc_model_configure_geometry_basis"
                          if body_configuration_error else
                          "Inspect the reported input or Family condition" if normal else
                          "Inspect the original Operation and the reported resource or backend failure")
                observed_index = getattr(exc, "item_index", None)
                failure_row = (indices[observed_index] if phase in {"numeric_precheck", "geometry_instantiation"}
                               and isinstance(observed_index, int) and 0 <= observed_index < len(indices) else None)
                add_check(rows, phase=phase if body_configuration_error else getattr(exc, "phase", phase),
                          check_kind="call_failure", status="FAIL", input_row_index=failure_row,
                          product_key=rows["products"][failure_row]["product_key"] if failure_row is not None else None,
                          error_code=code, message=message, error_origin=type(exc).__name__,
                          recommended_action=action,
                          diagnostic_json=canonical_json(getattr(exc, "diagnostic", None)))
                failure_summary = summary("rejected" if normal else "technical_failure")
                if failure_report is None:
                    try:
                        failure_report = await stage_product_apply_report(self._storage, ifc_scope, prepared,
                            detail=detail, summary=failure_summary,
                            rows=report_for_commit(rows, (), committed=False))
                    except Exception as report_error:
                        message = (message + "; diagnostic report unavailable: " + str(report_error))[:1800]
                operation_alias = await self._writer.fail_prepared(ifc_scope, prepared, claim=claim,
                    error_code=code, error_message=message, product_report=failure_report)
                report_alias = None
                if failure_report is not None:
                    report_alias = await self._report_alias(scope, failure_report.package.package_id)
                return ProductApplyResult(summary=failure_report.summary if failure_report is not None else None,
                    operation_id=prepared.operation_id, operation_alias=operation_alias, report_package_alias=report_alias,
                    error_code=code, message=message)

            try:
                async with self._runtime.invocation_scope(IFC_REVISION_INVOCATION_EXECUTION, deadline=deadline,
                                                          on_guard_registered=guard):
                    phase = "commit"
                    resumed = await self._writer.resume_selected_revision(ifc_scope, prepared, claim)
                    if resumed is not None:
                        committed, selected = resumed
                        if selected.product_report is None:
                            raise IfcModelIntegrityError("selected Apply lost its complete report")
                        return ProductApplyResult(summary=selected.product_report.summary,
                            operation_id=prepared.operation_id, operation_alias=committed.operation_alias,
                            report_package_alias=await self._report_alias(scope, selected.product_report.package.package_id),
                            ifc_revision_id=committed.exact.revision_id)
                    phase = "read_input"
                    started = monotonic()
                    materialized = await self._payloads.materialize_members((payload.member,), check_budget=check_budget,
                                                                           max_total_bytes=MAX_PRODUCT_BATCH_OUTPUT_BYTES)
                    input_path = materialized[payload.member.member_id]
                    try:
                        parquet = pq.ParquetFile(input_path)
                        counts["input_count"] = parquet.metadata.num_rows
                        table = parquet.read(columns=[name for name in _INPUT_TYPES if name in parquet.schema_arrow.names])
                    finally:
                        input_path.unlink(missing_ok=True)
                    _initialize_products(rows, table, input_count=counts["input_count"])
                    _validate_input_schema(table, payload.logical_key_fields)
                    table = table.select(tuple(_INPUT_TYPES))
                    timings[phase] = monotonic()-started
                    phase = "body_preflight"
                    started = monotonic()
                    base = await self._writer.open_prepared_base(ifc_scope, prepared)
                    await self._writer.preflight_prepared_product_body(prepared)
                    timings[phase] = monotonic()-started
                    family = await self._families.get_product_family_version_by_id(
                        scope.identity, family_version_id=command.family_version_id)
                    if family is None:
                        raise ProductApplyCallError("FAMILY_NOT_FOUND", "The exact published Family is unavailable", phase="resource")
                    phase = "normalize_input"
                    started = monotonic()
                    closure = await self._inputs.prepare_published_closure(scope, family_version=family)
                    indices, invocations = _prepare_rows(rows, table, closure)
                    timings[phase] = monotonic()-started
                    requests, prechecks, numeric_bytes = [], [], 0
                    phase = "numeric_precheck"
                    started = monotonic()
                    for offset in range(0, len(invocations), _NUMERIC_GROUP_ITEMS):
                        check_budget()
                        requests.extend(closure.requests(tuple(invocations[offset:offset+_NUMERIC_GROUP_ITEMS])))
                    async with self._realizations.invocation_scope(PRODUCT_REALIZATION_EXECUTION, deadline=deadline,
                                                                  on_guard_registered=guard):
                        if requests:
                            set_active_window(0, _NUMERIC_GROUP_ITEMS)
                            received = 0
                            async for result in self._realizations.precheck_batch(requests, window_size=_NUMERIC_GROUP_ITEMS):
                                check_budget()
                                if received >= len(requests):
                                    raise IfcModelIntegrityError("numeric Worker returned an extra product result")
                                index = indices[received]
                                prechecks.append(result)
                                counts["solve_executed_count"] += 1
                                record_realization(rows, index, family.version.family_contract, result, add_check=add_check)
                                if result.proposal.status.value == "SOLVED":
                                    counts["solved_count"] += 1
                                    if result.execution_stage == "interface_constraints_evaluated":
                                        counts["interface_constraints_evaluated_product_count"] += 1
                                numeric_bytes += len(result.model_dump_json().encode())
                                if numeric_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                    raise ProductApplyCallError("PRODUCT_APPLY_NUMERIC_LIMIT", "Complete numeric results exceed their byte budget", phase=phase)
                                received += 1
                                set_active_window(received, _NUMERIC_GROUP_ITEMS)
                            if received != len(requests):
                                raise IfcModelIntegrityError("numeric Worker omitted product results")
                            active_indices = []
                    timings[phase] = monotonic()-started
                    if len(indices) != table.num_rows or any(
                            result.proposal.status.value != "SOLVED" or result.frame_check_status != "PASS"
                            for result in prechecks):
                        return await close_failure(ProductApplyCallError("PRODUCT_PRECHECK_REJECTED",
                            "Strict precheck rejected at least one input; no geometry was instantiated", phase=phase, normal=True), normal=True)
                    phase = "geometry_instantiation"
                    components_by_product = {}
                    for row in rows["components"]:
                        components_by_product.setdefault(row["product_key"], []).append(row)
                    started = monotonic()
                    with tempfile.TemporaryDirectory(prefix="cad2bim-product-apply-") as directory:
                        root, sources, items = Path(directory), {}, []
                        source_cache = ProductGeometrySpool(root)
                        async with self._realizations.invocation_scope(PRODUCT_REALIZATION_EXECUTION, deadline=deadline,
                                                                      on_guard_registered=guard):
                            check_budget()
                            group = await self._inputs.load_geometry_materials(scope, family_version=family,
                                requests=requests,
                                prechecks=prechecks)
                            received = 0
                            set_active_window(0, _GEOMETRY_GROUP_ITEMS)
                            async for built in self._realizations.instantiate_batch(group,
                                    prechecks,
                                    output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS, source_cache=source_cache,
                                    window_size=_GEOMETRY_GROUP_ITEMS):
                                if received >= len(group):
                                    raise IfcModelIntegrityError("Geometry Worker returned an extra product")
                                index, request = indices[received], group[received]
                                counts["build_executed_count"] += 1
                                if built.instance_geometries is None:
                                    raise IfcModelIntegrityError("instantiation omitted local fragment bytes")
                                counts["built_count"] += 1
                                rows["products"][index]["build_status"] = "BUILT"
                                for component in components_by_product[rows["products"][index]["product_key"]]:
                                    component["build_status"] = "BUILT"
                                fragment_specs = fragment_specs_for_product(family, request, built, source_cache, sources)
                                items.append((index, built.realization, fragment_specs))
                                received += 1
                                set_active_window(received, _GEOMETRY_GROUP_ITEMS)
                            if received != len(group):
                                raise IfcModelIntegrityError("Geometry Worker omitted product results")
                            active_indices = []
                            del group
                        timings[phase] = monotonic()-started
                        phase = "ifc_compilation"
                        started = monotonic()
                        generated = await self._runtime.execute(CompileIfcProductsRevisionRequest(
                            command=CompileIfcProductsCommand(family_contract=family.version.family_contract),
                            blocks=compilation_blocks(rows, items, sources, check_budget)), source=base)
                        timings[phase] = monotonic()-started
                    if not isinstance(generated, GeneratedIfcRevision) or not isinstance(generated.effect, IfcProductRevisionEffect):
                        raise IfcModelIntegrityError("IFC Worker omitted the actual component identity map")
                    effect = generated.effect
                    expected = {(row["product_key"], row["component_key"]) for row in rows["components"]}
                    actual = {(binding.product_key, binding.component_key) for binding in effect.bindings}
                    if (actual != expected or len(actual) != len(effect.bindings)
                            or len({binding.global_id for binding in effect.bindings}) != len(effect.bindings)):
                        raise IfcModelIntegrityError("IFC bindings differ from the complete input component set")
                    timings.update({f"ifc_worker.{name}": value for name, value in effect.timings.items()})
                    phase = "stage_report"
                    committed_summary = summary("committed")
                    success_report = await stage_product_apply_report(self._storage, ifc_scope, prepared,
                        detail=detail, summary=committed_summary, rows=report_for_commit(rows, effect.bindings, committed=True))
                    failed_rows = report_for_commit(rows, (), committed=False)
                    add_check(failed_rows, phase="commit", check_kind="current_cas", status="FAIL",
                              error_code="IFC_CURRENT_CONFLICT", message="CURRENT changed before the atomic commit",
                              error_origin="IfcModelRepository", recommended_action="Inspect the original Operation and current model")
                    failure_report = await stage_product_apply_report(self._storage, ifc_scope, prepared,
                        detail=detail, summary=summary("technical_failure"), rows=failed_rows)
                    phase = "commit"
                    committed = await self._writer.commit(ifc_scope, prepared, generated, claim=claim,
                        product_report=success_report, product_failure_report=failure_report)
                    return ProductApplyResult(summary=success_report.summary, operation_id=prepared.operation_id,
                        operation_alias=committed.operation_alias,
                        report_package_alias=await self._report_alias(scope, success_report.package.package_id),
                        ifc_revision_id=committed.exact.revision_id)
            except asyncio.CancelledError:
                raise
            except IfcCommittedVerificationError:
                raise
            except Exception as exc:
                if active_indices:
                    observed = getattr(exc, "item_index", None)
                    confirmed = counts["solve_executed_count"] if phase == "numeric_precheck" else counts["build_executed_count"]
                    unresolved = (indices[confirmed:observed+1]
                                  if isinstance(observed, int) and confirmed <= observed < len(indices)
                                  else active_indices[active_position:])
                    count_name = "solve_unconfirmed_count" if phase == "numeric_precheck" else "build_unconfirmed_count"
                    counts[count_name] += len(unresolved)
                    field = "solve_status" if phase == "numeric_precheck" else "build_status"
                    for index in unresolved:
                        rows["products"][index][field] = "UNCONFIRMED"
                        add_check(rows, phase=phase, check_kind="execution_coverage", status="NOT_CHECKED",
                                  input_row_index=index, product_key=rows["products"][index]["product_key"],
                                  message="Worker failure identifies an observation range; no terminal result was received")
                if phase == "commit":
                    recovered = await self._recover(ifc_scope, detail, key, digest)
                    if recovered is not None:
                        return await self._result_from_state(scope, recovered)
                    if not isinstance(exc, IfcModelConflictError):
                        await self._writer.freeze_indeterminate(ifc_scope, prepared,
                            reason_code="PRODUCT_APPLY_COMMIT_UNCERTAIN", claim=claim)
                        raise
                else:
                    failure_report = None
                return await close_failure(exc, normal=getattr(exc, "normal", False))
            finally:
                try:
                    if generated is not None:
                        await generated.candidate.aclose()
                finally:
                    await self._writer.retire_execution(ifc_scope, claim)

        async def execute_identified():
            try:
                return await execute()
            except Exception as exc:
                if accepted_operation_id is not None:
                    exc.operation_id = accepted_operation_id
                raise

        return await self._coordinator.invoke(ifc_scope,
            logical_operation_identity=f"ifc_products_apply:{key}:{digest}", operation=execute_identified,
            deadline=monotonic()+IFC_REVISION_INVOCATION_EXECUTION.total_seconds)

    async def _recover(self, scope, detail, key, digest):
        return await self._writer.recover_revision(scope, tool_name="ifc_products_apply", idempotency_key=key,
            request_digest=digest, intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY, operation_detail=detail.persisted_payload())

    async def _report_alias(self, scope, package_id):
        package = await self._data.get_package(scope, package_id)
        if package is None:
            raise IfcModelIntegrityError("settled Apply report is not visible in its original Scope")
        return package.catalog_entry.alias

    async def _result_from_state(self, scope, state):
        alias = await self._report_alias(scope, state.report_package_id) if state.report_package_id is not None else None
        if state.status is CriticalOperationStatus.SUCCEEDED and (state.product_summary is None or alias is None):
            raise IfcModelIntegrityError("successful Apply lost its original report")
        return ProductApplyResult(summary=state.product_summary, operation_id=state.prepared.operation_id,
            operation_alias=state.operation_alias,
            report_package_alias=alias,
            ifc_revision_id=state.prepared.after_revision_id if state.status is CriticalOperationStatus.SUCCEEDED else None,
            error_code=state.error_code, message=state.error_message)


def _initialize_products(rows, table, *, input_count=None):
    keys = table.column("product_key").to_pylist() if "product_key" in table.column_names else [None]*(table.num_rows if input_count is None else input_count)
    for index, key in enumerate(keys):
        rows["products"].append({"input_row_index": index, "product_key": key if isinstance(key, str) else None,
            "solve_status": "NOT_EXECUTED", "build_status": "NOT_EXECUTED", "commit_status": "NOT_COMMITTED"})


def _validate_input_schema(table, logical_keys):
    if tuple(logical_keys) != ("product_key",):
        raise ProductApplyCallError("PRODUCT_INPUT_KEY_INVALID", "Input must declare logical_key_fields=[product_key]", normal=True)
    for name, kind in _INPUT_TYPES.items():
        if name not in table.column_names or table.schema.field(name).type != kind:
            raise ProductApplyCallError("PRODUCT_INPUT_SCHEMA_INVALID", f"Input column {name} must have physical type {kind}", normal=True)


def _prepare_rows(rows, table, closure):
    records = table.to_pylist()
    counts = Counter(row["product_key"] for row in records if isinstance(row["product_key"], str))
    indices, invocations = [], []
    for index, row in enumerate(records):
        field = "product_key"
        try:
            key = row[field]
            if not isinstance(key, str) or not key.strip() or counts[key] > 1:
                raise ValueError("product_key must be nonblank and unique across the complete payload")
            parameters, constraints = None, None
            for field, expected in (("parameters_json", dict), ("external_constraints_json", list)):
                value = row[field]
                if not isinstance(value, str) or not value:
                    raise ValueError(f"{field} must be nonempty JSON text")
                decoded = json.loads(value)
                if not isinstance(decoded, expected):
                    raise ValueError(f"{field} has the wrong JSON root type")
                if field == "parameters_json": parameters = decoded
                else: constraints = decoded
            field = "invocation"
            invocation = ProductRealizationInvocation(public_parameters=parameters,
                product_origin=(row["origin_x_m"], row["origin_y_m"], row["origin_z_m"]),
                length_unit=IfcLengthUnit(), external_constraints=constraints)
            invocation = closure.normalize_invocation(invocation)
        except (ValueError, ValidationError) as exc:
            product = rows["products"][index]
            product.update(solve_status="INPUT_INVALID", error_code="PRODUCT_INPUT_INVALID", error_phase="input")
            add_check(rows, phase="input", check_kind="input_validation", status="FAIL", input_row_index=index,
                product_key=product["product_key"], field_path=field, error_code="PRODUCT_INPUT_INVALID",
                message=str(exc)[:1800], error_origin="ProductRealizationInputService",
                recommended_action="Inspect this input field and the exact Family parameter contract")
            continue
        indices.append(index); invocations.append(invocation)
    return indices, invocations
