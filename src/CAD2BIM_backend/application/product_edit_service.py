"""Atomic mixed-action product editing through the existing IFC write owner."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import tempfile
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq

from CAD2BIM_backend.application.exact_payload_resolver import ExactParquetPayload
from CAD2BIM_backend.application.ifc_write_support import require_ifc_idempotency_key
from CAD2BIM_backend.application.product_batch_geometry import ProductGeometrySpool, compilation_blocks, fragment_specs_for_product
from CAD2BIM_backend.application.product_apply_report import empty_report_rows, add_check
from CAD2BIM_backend.application.product_realization_report import record_realization
from CAD2BIM_backend.application.product_edit_report import empty_edit_report, edit_check, terminal_edit_rows, stage_product_edit_report
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationStatus, DataTransformOperationDetail, IfcProductsEditOperationDetail,
    IFC_PRODUCTS_EDIT_INTENT_SUMMARY,
)
from CAD2BIM_backend.domain.geometry_frame import rigid_transform_from_axis_reference
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity, IfcModelIntegrityError, IfcCommittedVerificationError
from CAD2BIM_backend.domain.ifc_spatial_structure import IfcProjectWorldPlacement
from CAD2BIM_backend.domain.product_apply import ProductApplyInputBinding
from CAD2BIM_backend.domain.product_edit import (
    EditIfcProductsCommand, ReplaceProductEditRow, PlacementProductEditRow, DeleteProductEditRow,
    IfcProductsEditTarget, IfcProductsEditPreflightRequest, CompileIfcProductsEditCommand,
    IfcProductsEditRevisionEffect, ProductsEditResult, ProductsEditSummary, ProductsEditActionCounts,
)
from CAD2BIM_backend.domain.product_realization import ProductRealizationInvocation, ProductRealizationOutputNeed, MAX_PRODUCT_REALIZATION_BATCH_ITEMS
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION, PRODUCT_REALIZATION_EXECUTION, MAX_PRODUCT_BATCH_OUTPUT_BYTES
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    InspectExactIfcRevisionRequest, EditIfcProductsRevisionRequest, GeneratedIfcRevision,
)

_TOOL = "ifc_products_edit"
_INPUT_TYPES = {name: pa.string() for name in ("product_key", "target_global_id", "action", "family_version_ref",
    "parameters_json", "external_constraints_json", "placement_json")}
_INPUT_TYPES.update({name: pa.float64() for name in ("origin_x_m", "origin_y_m", "origin_z_m")})


def _replacement_requests(closure, group, check_budget):
    """Assemble bounded closure pages for one complete streaming Edit batch."""
    requests = []
    for offset in range(0, len(group), MAX_PRODUCT_REALIZATION_BATCH_ITEMS):
        check_budget()
        requests.extend(closure.requests(tuple(row.invocation for row in group[offset:offset+MAX_PRODUCT_REALIZATION_BATCH_ITEMS])))
    return tuple(requests)


class ProductEditCallError(RuntimeError):
    def __init__(self, code, message, *, phase="input"):
        self.code, self.phase = code, phase
        super().__init__(message)


class IfcProductsEditService:
    def __init__(self, *, families, data, payloads, inputs, realization_runtime,
                 revision_runtime, writer, coordinator, storage, family_catalog, references):
        self._families, self._data, self._payloads, self._inputs = families, data, payloads, inputs
        self._realizations, self._runtime, self._writer = realization_runtime, revision_runtime, writer
        self._coordinator, self._storage = coordinator, storage
        self._family_catalog, self._references = family_catalog, references

    async def recover(self, scope, *, origin_run_id, operation_id):
        if origin_run_id != scope.run_id or scope.catalog_frontier is not None:
            raise ProductEditCallError("PRODUCT_EDIT_RECOVERY_SCOPE_MISMATCH", "Recover in the original writable Run")
        original = await self._data.get_ifc_products_recovery_operation(scope,
            origin_run_id=origin_run_id, operation_id=operation_id)
        if original is None or original.operation.tool_name != _TOOL or not isinstance(original.operation.detail, IfcProductsEditOperationDetail):
            raise ProductEditCallError("OPERATION_NOT_FOUND", "The original Edit Operation is unavailable")
        try:
            await self._writer.require_recovery_allowed(_ifc_scope(scope))
            return await self.edit(scope, original.operation.detail.request, idempotency_key=original.operation.idempotency_key)
        except Exception as exc:
            exc.operation_id = operation_id
            raise

    async def edit(self, scope, command: EditIfcProductsCommand, *, idempotency_key: str):
        key = require_ifc_idempotency_key(idempotency_key)
        package = await self._data.get_package(scope, command.package_id)
        if package is None:
            raise ProductEditCallError("PACKAGE_NOT_FOUND", "The exact input Package is not visible")
        payload = self._payloads.transform_payloads(package).get(command.payload_name)
        if not isinstance(payload, ExactParquetPayload):
            raise ProductEditCallError("PRODUCT_INPUT_FORMAT_INVALID", "Edit requires one complete Parquet payload")
        producer = await self._data.get_operation_for_result_package(scope, command.package_id)
        source = producer.operation.detail.ifc_source if producer and isinstance(producer.operation.detail, DataTransformOperationDetail) else None
        if source is not None:
            if command.base_revision_id is not None and command.base_revision_id != source.revision_id:
                raise ProductEditCallError("IFC_BASE_SOURCE_MISMATCH", "Explicit base and system Package IFC source differ")
            command = command.model_copy(update={"base_revision_id": source.revision_id})
        if command.base_revision_id is None:
            raise ProductEditCallError("IFC_BASE_REFERENCE_REQUIRED", "Read the current exact model basis and provide its revision reference")
        detail = IfcProductsEditOperationDetail(request=command, input_binding=ProductApplyInputBinding(
            package_id=command.package_id, member_id=payload.member.member_id,
            payload_name=command.payload_name, logical_key_fields=payload.logical_key_fields))
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        ifc_scope = _ifc_scope(scope)
        operation_id = None

        async def execute():
            nonlocal operation_id
            state = await self._writer.recover_revision(ifc_scope, tool_name=_TOOL, idempotency_key=key,
                request_digest=digest, intent_summary=IFC_PRODUCTS_EDIT_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
            if state is not None:
                return await self._state_result(scope, state)
            prepared = await self._writer.prepare(ifc_scope, tool_name=_TOOL, idempotency_key=key,
                request_digest=digest, intent_summary=IFC_PRODUCTS_EDIT_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
            operation_id = prepared.operation_id
            claim = await self._writer.claim_execution(ifc_scope, prepared)
            deadline = monotonic() + claim.remaining_seconds
            guard = self._writer.guard_registration(ifc_scope, claim)
            rows, timings, validated, families, closures = empty_edit_report(), {}, [], {}, {}
            phase, generated, trusted_rows, active = "recovery", None, False, []

            def check_budget():
                if monotonic() >= deadline:
                    raise ProductEditCallError("PRODUCT_EDIT_DEADLINE", "The original invocation deadline expired", phase=phase)

            def summary(committed):
                counts = Counter(row["action"] for row in rows["products"]) if committed else Counter()
                return ProductsEditSummary(input_count=len(rows["products"]), committed_count=len(rows["products"]) if committed else 0,
                    committed_by_action=ProductsEditActionCounts(**{name: counts[name] for name in ("replace", "placement", "delete")}),
                    created_product_count=len({item["global_id"] for item in rows["changes"] if item["change_kind"] == "created"}) if committed else 0,
                    deleted_product_count=len({item["global_id"] for item in rows["changes"] if item["change_kind"] == "deleted"}) if committed else 0,
                    timings_seconds=dict(timings))

            async def close_failure(exc):
                code = getattr(exc, "code", "PRODUCT_EDIT_TECHNICAL_FAILURE")
                message = str(exc).encode("utf-8")[:1800].decode("utf-8", errors="ignore") or "Product Edit failed"
                for index in active:
                    field = "solve_status" if phase == "numeric_precheck" else "execution_status"
                    rows["products"][index][field] = "UNKNOWN"
                report = None
                if trusted_rows:
                    edit_check(rows, phase=getattr(exc, "phase", phase), check_kind="call_failure", status="FAIL",
                        error_code=code, message=message, recommended_action="Inspect the original checks and correct this condition before a new operation")
                    try:
                        report = await stage_product_edit_report(self._storage, ifc_scope, prepared, detail=detail,
                            summary=summary(False), rows=terminal_edit_rows(rows, committed=False), committed=False)
                    except Exception as report_error:
                        # The original rejection remains the replay authority.
                        # Report availability is supplementary bounded context.
                        unavailable = str(report_error).encode("utf-8")[:150].decode("utf-8", errors="ignore")
                        message += f"; diagnostic report unavailable: {unavailable}"
                alias = await self._writer.fail_prepared(ifc_scope, prepared, claim=claim,
                    error_code=code, error_message=message, product_report=report)
                return ProductsEditResult(operation_id=prepared.operation_id, operation_alias=alias, outcome="failed",
                    summary=report.summary if report else None,
                    report_package_alias=await self._report_alias(scope, report.package.package_id) if report else None,
                    error_code=code, error_phase="ifc_validation", message=message)

            try:
                async with self._runtime.invocation_scope(IFC_REVISION_INVOCATION_EXECUTION, deadline=deadline, on_guard_registered=guard):
                    phase = "commit"
                    resumed = await self._writer.resume_selected_revision(ifc_scope, prepared, claim)
                    if resumed is not None:
                        committed, selected = resumed
                        return ProductsEditResult(operation_id=prepared.operation_id, operation_alias=committed.operation_alias,
                            outcome="succeeded", summary=selected.product_report.summary,
                            report_package_alias=await self._report_alias(scope, selected.product_report.package.package_id),
                            ifc_revision_id=committed.exact.revision_id)
                    phase, started = "read_input", monotonic()
                    paths = await self._payloads.materialize_members((payload.member,), check_budget=check_budget,
                                                                     max_total_bytes=MAX_PRODUCT_BATCH_OUTPUT_BYTES)
                    path = paths[payload.member.member_id]
                    try:
                        table = pq.read_table(path)
                    finally:
                        path.unlink(missing_ok=True)
                    _validate_schema(table, payload.logical_key_fields)
                    records = table.to_pylist()
                    rows["products"] = [_initial_product(index, item) for index, item in enumerate(records)]
                    trusted_rows = True
                    counts = Counter(item.get("product_key") for item in records if isinstance(item.get("product_key"), str))
                    for index, item in enumerate(records):
                        check_budget()
                        try:
                            if counts[item["product_key"]] != 1 or not item["product_key"].strip():
                                raise ValueError("product_key must be nonblank and unique across the complete input")
                            _validate_action_fields(item)
                            common = dict(input_row_index=index, product_key=item["product_key"], target_global_id=item["target_global_id"])
                            if item["action"] == "replace":
                                reference = await self._references.expand(scope.identity, item["family_version_ref"], {"fv"})
                                if reference not in families:
                                    family = await self._family_catalog.resolve_exact_version(scope.identity, reference)
                                    version_id = family.version.family_version_id
                                    families[reference] = family
                                    if version_id not in closures:
                                        closures[version_id] = (family, await self._inputs.prepare_published_closure(scope, family_version=family))
                                family = families[reference]
                                version_id = family.version.family_version_id
                                invocation = ProductRealizationInvocation(public_parameters=json.loads(item["parameters_json"]),
                                    external_constraints=json.loads(item["external_constraints_json"]),
                                    product_origin=tuple(item[name] for name in ("origin_x_m", "origin_y_m", "origin_z_m")), length_unit=IfcLengthUnit())
                                invocation = closures[version_id][1].normalize_invocation(invocation)
                                row = ReplaceProductEditRow(**common, family_version_id=version_id, invocation=invocation)
                            elif item["action"] == "placement":
                                placement = IfcProjectWorldPlacement.model_validate(json.loads(item["placement_json"]))
                                row = PlacementProductEditRow(**common, placement=rigid_transform_from_axis_reference(
                                    placement.origin, placement.axis, placement.ref_direction))
                            else:
                                row = DeleteProductEditRow(**common)
                            validated.append(row)
                        except Exception as exc:
                            rows["products"][index]["precheck_status"] = "FAILED"
                            edit_check(rows, phase="input", check_kind="row_input", status="FAIL", input_row_index=index,
                                product_key=rows["products"][index]["product_key"], target_global_id=rows["products"][index]["target_global_id"],
                                error_code=getattr(exc, "code", "PRODUCT_EDIT_INPUT_INVALID"), message=str(exc)[:1800],
                                recommended_action="Correct this row using the exact Family and action contract")
                    timings[phase] = monotonic()-started
                    phase, started = "target_preflight", monotonic()
                    base = await self._writer.open_prepared_base(ifc_scope, prepared)
                    targets = []
                    for row in validated:
                        new_class = None
                        if row.action == "replace":
                            family = closures[row.family_version_id][0]
                            new_class = next(item.ifc_class.value for item in family.version.family_contract.components if item.parent_component_key is None)
                        targets.append(IfcProductsEditTarget(input_row_index=row.input_row_index, product_key=row.product_key,
                            target_global_id=row.target_global_id, action=row.action, replacement_ifc_class=new_class))
                    inspected = await self._runtime.execute(InspectExactIfcRevisionRequest(projection="edit_targets",
                        edit_targets_request=IfcProductsEditPreflightRequest(targets=tuple(targets))), source=base)
                    preflight = inspected.edit_targets
                    if preflight is None:
                        raise IfcModelIntegrityError("IFC runtime omitted complete Edit target preflight")
                    for fact in preflight.products:
                        rows["products"][fact.input_row_index]["before_ifc_class"] = fact.before_ifc_class
                    for check in preflight.checks:
                        values = check.model_dump(exclude={"check_index", "phase", "check_kind", "status"})
                        edit_check(rows, phase=check.phase, check_kind=check.check_kind, status=check.status, **values)
                    for row in validated:
                        if row.action != "replace":
                            rows["products"][row.input_row_index]["precheck_status"] = "PASSED" if preflight.accepted else "NOT_RUN"
                    for check in preflight.checks:
                        if check.status == "FAIL" and check.input_row_index is not None:
                            rows["products"][check.input_row_index]["precheck_status"] = "FAILED"
                    timings[phase] = monotonic()-started
                    if not preflight.accepted or len(validated) != len(records):
                        return await close_failure(ProductEditCallError("PRODUCT_EDIT_PRECHECK_REJECTED", "The batch has input or target dependency blockers", phase=phase))
                    groups = defaultdict(list)
                    for row in validated:
                        if row.action == "replace":
                            groups[row.family_version_id].append(row)
                    if groups:
                        await self._writer.preflight_prepared_product_body(prepared)
                    phase, started = "numeric_precheck", monotonic()
                    numeric, requests, observation_rows, numeric_bytes = {}, {}, empty_report_rows(), 0
                    observation_rows["products"] = [{"product_key": item["product_key"]} for item in records]
                    observed_checks, observed_interfaces = 0, 0
                    indices = {item["product_key"]: index for index, item in enumerate(records)}
                    def retain_numeric_observations():
                        nonlocal observed_checks, observed_interfaces
                        for check in observation_rows["checks"][observed_checks:]:
                            values = {key: value for key, value in check.items() if key not in {"check_index", "phase", "check_kind", "status"}}
                            index = values.get("input_row_index")
                            if index is not None:
                                values["target_global_id"] = records[index]["target_global_id"]
                            edit_check(rows, phase=check["phase"], check_kind=check["check_kind"],
                                status="NOT_RUN" if check["status"] == "NOT_COMPARED" else check["status"], **values)
                        rows["interfaces"].extend({**item, "input_row_index": indices[item["product_key"]]}
                            for item in observation_rows["interfaces"][observed_interfaces:])
                        observed_checks = len(observation_rows["checks"])
                        observed_interfaces = len(observation_rows["interfaces"])
                    async with self._realizations.invocation_scope(PRODUCT_REALIZATION_EXECUTION, deadline=deadline, on_guard_registered=guard):
                        for version_id, group in groups.items():
                            family, closure = closures[version_id]
                            batch = _replacement_requests(closure, group, check_budget)
                            requests[version_id], numeric[version_id] = batch, []
                            active = [row.input_row_index for row in group]
                            async for result in self._realizations.precheck_batch(batch, window_size=1024):
                                check_budget()
                                position = len(numeric[version_id])
                                if position >= len(group):
                                    raise IfcModelIntegrityError("numeric Worker returned an extra result")
                                index = group[position].input_row_index
                                numeric[version_id].append(result)
                                numeric_bytes += len(result.model_dump_json().encode())
                                if numeric_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                    raise ProductEditCallError("PRODUCT_EDIT_NUMERIC_LIMIT", "Complete numeric observations exceed their existing byte budget", phase=phase)
                                record_realization(observation_rows, index, family.version.family_contract, result, add_check=add_check)
                                retain_numeric_observations()
                                passed = result.proposal.status.value == "SOLVED" and result.frame_check_status == "PASS"
                                rows["products"][index].update(precheck_status="PASSED" if passed else "FAILED",
                                    solve_status="PASSED" if passed else "FAILED",
                                    normalized_parameters_json=observation_rows["products"][index].get("normalized_parameters_json"),
                                    solution_json=observation_rows["products"][index].get("solution_json"))
                                active.remove(index)
                            if len(numeric[version_id]) != len(group):
                                raise IfcModelIntegrityError("numeric Worker omitted product results")
                    active = []
                    timings[phase] = monotonic()-started
                    if any(row["precheck_status"] != "PASSED" for row in rows["products"]):
                        return await close_failure(ProductEditCallError("PRODUCT_EDIT_PRECHECK_REJECTED", "Global numeric precheck failed; no geometry built", phase=phase))
                    phase, started = "geometry_instantiation", monotonic()
                    with tempfile.TemporaryDirectory(prefix="cad2bim-product-edit-") as directory:
                        cache, sources, items = ProductGeometrySpool(Path(directory)), {}, []
                        async with self._realizations.invocation_scope(PRODUCT_REALIZATION_EXECUTION, deadline=deadline, on_guard_registered=guard):
                            for version_id, group in groups.items():
                                family, _ = closures[version_id]
                                batch = await self._inputs.load_geometry_materials(scope, family_version=family,
                                    requests=requests[version_id], prechecks=numeric[version_id])
                                received, active = 0, [row.input_row_index for row in group]
                                async for built in self._realizations.instantiate_batch(batch, numeric[version_id],
                                        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS, source_cache=cache, window_size=1024):
                                    check_budget()
                                    if received >= len(group) or built.instance_geometries is None:
                                        raise IfcModelIntegrityError("Geometry Worker returned an invalid product result")
                                    index = group[received].input_row_index
                                    specs = fragment_specs_for_product(family, batch[received], built, cache, sources, namespace=version_id+":")
                                    items.append((index, built.realization, specs))
                                    active.remove(index)
                                    received += 1
                                if received != len(group):
                                    raise IfcModelIntegrityError("Geometry Worker omitted product results")
                        active = []
                        timings[phase] = monotonic()-started
                        phase, started = "ifc_edit", monotonic()
                        active = list(range(len(records)))
                        generated = await self._runtime.execute(EditIfcProductsRevisionRequest(
                            command=CompileIfcProductsEditCommand(rows=tuple(validated),
                                families={key: family.version.family_contract for key, (family, _) in closures.items()}, preflight=preflight),
                            blocks=compilation_blocks(rows, items, sources, check_budget)), source=base)
                        timings[phase] = monotonic()-started
                    if not isinstance(generated, GeneratedIfcRevision) or not isinstance(generated.effect, IfcProductsEditRevisionEffect):
                        raise IfcModelIntegrityError("IFC runtime omitted the complete Edit effect")
                    effect = generated.effect
                    if {item.input_row_index for item in effect.products} != set(range(len(records))) or len(effect.products) != len(records):
                        raise IfcModelIntegrityError("Edit effect differs from the complete input row set")
                    for fact in effect.products:
                        if fact.execution_status != "PASSED" or fact.target_global_id != records[fact.input_row_index]["target_global_id"]:
                            raise IfcModelIntegrityError("Edit effect target or execution outcome differs")
                        rows["products"][fact.input_row_index].update(execution_status=fact.execution_status,
                            before_ifc_class=fact.before_ifc_class, after_ifc_class=fact.after_ifc_class, result_global_id=fact.result_global_id)
                    rows["changes"] = [item.model_dump(mode="json") for item in effect.changes]
                    for check in effect.checks:
                        edit_check(rows, phase=check.phase, check_kind=check.check_kind, status=check.status,
                                   **check.model_dump(exclude={"check_index", "phase", "check_kind", "status"}))
                    timings.update({f"ifc_worker.{name}": value for name, value in effect.timings.items()})
                    active = []
                    phase = "stage_report"
                    success = await stage_product_edit_report(self._storage, ifc_scope, prepared, detail=detail,
                        summary=summary(True), rows=terminal_edit_rows(rows, committed=True, bindings=effect.bindings), committed=True)
                    failed_rows = terminal_edit_rows(rows, committed=False)
                    edit_check(failed_rows, phase="commit", check_kind="current_cas", status="FAIL", error_code="IFC_CURRENT_CONFLICT",
                        message="CURRENT changed after the exact Edit base", recommended_action="Reread the affected products and dependencies")
                    rejected = await stage_product_edit_report(self._storage, ifc_scope, prepared, detail=detail,
                        summary=summary(False), rows=failed_rows, committed=False)
                    phase = "commit"
                    committed = await self._writer.commit(ifc_scope, prepared, generated, claim=claim,
                        product_report=success, product_failure_report=rejected)
                    return ProductsEditResult(operation_id=prepared.operation_id, operation_alias=committed.operation_alias,
                        outcome="succeeded", summary=success.summary,
                        report_package_alias=await self._report_alias(scope, success.package.package_id), ifc_revision_id=committed.exact.revision_id)
            except (asyncio.CancelledError, IfcCommittedVerificationError):
                raise
            except Exception as exc:
                if phase == "commit":
                    state = await self._writer.recover_revision(ifc_scope, tool_name=_TOOL, idempotency_key=key,
                        request_digest=digest, intent_summary=IFC_PRODUCTS_EDIT_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
                    if state is not None:
                        return await self._state_result(scope, state)
                    await self._writer.freeze_indeterminate(ifc_scope, prepared, reason_code="PRODUCT_EDIT_COMMIT_UNCERTAIN", claim=claim)
                    return ProductsEditResult(operation_id=prepared.operation_id, outcome="indeterminate")
                return await close_failure(exc)
            finally:
                try:
                    if generated is not None:
                        await generated.candidate.aclose()
                finally:
                    await self._writer.retire_execution(ifc_scope, claim)

        async def identified():
            try:
                return await execute()
            except Exception as exc:
                if operation_id is not None:
                    exc.operation_id = operation_id
                raise
        return await self._coordinator.invoke(ifc_scope, logical_operation_identity=f"{_TOOL}:{key}:{digest}",
            operation=identified, deadline=monotonic()+IFC_REVISION_INVOCATION_EXECUTION.total_seconds)

    async def _report_alias(self, scope, package_id):
        package = await self._data.get_package(scope, package_id)
        if package is None:
            raise IfcModelIntegrityError("the original Edit report is not visible")
        return package.catalog_entry.alias

    async def _state_result(self, scope, state):
        return ProductsEditResult(operation_id=state.prepared.operation_id, operation_alias=state.operation_alias,
            outcome=state.status.value, summary=state.product_summary,
            report_package_alias=await self._report_alias(scope, state.report_package_id) if state.report_package_id else None,
            ifc_revision_id=state.prepared.after_revision_id if state.status is CriticalOperationStatus.SUCCEEDED else None,
            error_code=state.error_code, error_phase=state.error_phase.value if state.error_phase else None, message=state.error_message)


def _ifc_scope(scope):
    return IfcRunIdentity(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
                          task_id=scope.identity.task_id, run_id=scope.run_id)


def _validate_schema(table, logical_keys):
    if tuple(logical_keys) != ("product_key",):
        raise ProductEditCallError("PRODUCT_INPUT_KEY_INVALID", "Declare logical_key_fields=[product_key]")
    if not {"product_key", "target_global_id", "action"} <= set(table.column_names):
        raise ProductEditCallError("PRODUCT_INPUT_SCHEMA_INVALID", "Edit input requires product_key, target_global_id and action")
    for field in table.schema:
        if field.name not in _INPUT_TYPES or field.type != _INPUT_TYPES[field.name]:
            raise ProductEditCallError("PRODUCT_INPUT_SCHEMA_INVALID", f"Unsupported physical column {field.name}: {field.type}")


def _validate_action_fields(item):
    action = item["action"]
    specific = {"replace": {"family_version_ref", "origin_x_m", "origin_y_m", "origin_z_m", "parameters_json", "external_constraints_json"},
                "placement": {"placement_json"}, "delete": set()}
    if action not in specific:
        raise ValueError("action must be replace, placement or delete")
    fields = specific[action]
    if any(item.get(name) is None for name in fields):
        raise ValueError(f"{action} requires all of {sorted(fields)}")
    if any(value is not None for name, value in item.items() if name not in fields | {"product_key", "target_global_id", "action"}):
        raise ValueError(f"{action} has a non-null unrelated field")


def _initial_product(index, item):
    return {"input_row_index": index,
        **{name: item.get(name) if isinstance(item.get(name), str) else None
           for name in ("product_key", "target_global_id", "action", "family_version_ref")},
        "precheck_status": "NOT_RUN", "solve_status": "NOT_RUN" if item.get("action") == "replace" else "NOT_APPLICABLE",
        "execution_status": "NOT_RUN", "commit_status": "NOT_COMMITTED"}
