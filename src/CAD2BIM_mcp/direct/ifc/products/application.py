"""Shared public-reference adapter for the existing product Apply core."""

from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
from CAD2BIM_backend.application.product_apply_service import ProductApplyCallError
from CAD2BIM_backend.domain.product_apply import ApplyIfcProductsCommand
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError
from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.application_support import ifc_direct_error
from CAD2BIM_mcp.direct.operation.application import project_ifc_apply_operation


def products_apply_error(error, *, operation_ref=None):
    """Preserve original public failure facts without inferring a failed write."""
    if isinstance(error, ProductApplyCallError):
        projected = DirectApplicationError(error.code, str(error),
            phase="preflight" if error.phase == "input" else "reconcile",
            operation_alias=getattr(error, "operation_alias", None),
            diagnostic=getattr(error, "diagnostic", None))
    elif isinstance(error, DataRuntimeApplicationError):
        projected = _direct_error(error, phase="preflight" if operation_ref is None else "read")
    elif isinstance(error, ProductFamilyCatalogError):
        projected = DirectApplicationError(error.code, str(error), phase="preflight")
    elif isinstance(error, ReferenceIndexError):
        projected = DirectApplicationError("REFERENCE_INVALID", str(error), phase="preflight")
    else:
        projected = ifc_direct_error(error)
    projected.operation_ref = operation_ref or projected.operation_ref
    if projected.operation_status is None:
        projected.operation_status = "unknown"
    return projected


async def execute_products_apply(scope, request, *, request_identity, core, data_runtime,
                                 family_catalog, references):
    """Resolve only public references; all business execution remains in Core/Writer."""
    operation_ref = request.operation_ref if request.mode == "recover" else None
    result = None
    try:
        if request.mode == "recover":
            origin_run_id, operation_id = references.operation_locator(scope.identity, operation_ref)
            result = await core.recover(scope, origin_run_id=origin_run_id, operation_id=operation_id)
            inspection = await data_runtime.get_ifc_products_recovery_operation(scope,
                origin_run_id=origin_run_id, operation_id=operation_id)
        else:
            family = await family_catalog.resolve_exact_version(scope.identity, request.family_version_alias,
                verify_assets=False)
            package = await data_runtime.resolve_package(scope, request.package_alias)
            command = ApplyIfcProductsCommand(family_version_id=family.version.family_version_id,
                package_id=package.package.package_id, payload_name=request.payload_name)
            result = await core.apply(scope, command, idempotency_key=request_identity)
            # Keep the settled action locatable even if its report or exact
            # committed metadata cannot be read for the response.
            operation_ref = references.operation_reference(scope.identity,
                origin_run_id=scope.run_id, operation_id=result.operation_id)
            inspection = await data_runtime.get_operation(scope, result.operation_alias)
        record = inspection.operation.operation
        family_version_ref = await family_catalog.version_ref_for_visible_id(scope.identity,
            record.detail.request.family_version_id, verify_assets=False)
        return project_ifc_apply_operation(inspection, family_version_ref=family_version_ref,
            operation_ref=operation_ref, same_run=record.run_id == scope.run_id)
    except Exception as exc:
        operation_id = getattr(exc, "operation_id", None)
        if operation_ref is None and operation_id is not None:
            operation_ref = references.operation_reference(scope.identity,
                origin_run_id=scope.run_id, operation_id=operation_id)
        error = products_apply_error(exc, operation_ref=operation_ref)
        if result is not None:
            error.operation_alias = result.operation_alias
            # The Core already completed; a later read/projection error cannot change its result.
            error.operation_status = ("succeeded" if result.ifc_revision_id is not None else "failed")
        raise error from None


class IfcProductsApplyApplicationMixin:
    async def apply_ifc_products(self, request, *, request_identity):
        try:
            scope = await self._resolve_scope(request.direct_session_alias, "ifc_products_apply")
            if self._products_apply_service is None or self.reference_service is None:
                raise DirectApplicationError("SYSTEM_PAUSED", "The product Apply service is unavailable",
                    phase="preflight")
            with self._scope_binder(scope.identity):
                return await execute_products_apply(scope, request, request_identity=request_identity,
                    core=self._products_apply_service, data_runtime=self._application,
                    family_catalog=self._require_product_family_catalog_service(), references=self.reference_service)
        except DirectApplicationError:
            raise
        except Exception as exc:
            raise products_apply_error(exc) from None


async def execute_products_edit(scope, request, *, request_identity, core, data_runtime, references):
    """Resolve public resource locators, then delegate the whole batch to Core."""
    from CAD2BIM_backend.domain.product_edit import EditIfcProductsCommand
    from CAD2BIM_mcp.direct.operation.application import project_ifc_products_edit_operation

    operation_ref = request.operation_ref if request.mode == "recover" else None
    result = None
    try:
        if request.mode == "recover":
            origin_run_id, operation_id = references.operation_locator(scope.identity, operation_ref)
            result = await core.recover(scope, origin_run_id=origin_run_id, operation_id=operation_id)
            inspection = await data_runtime.get_ifc_products_recovery_operation(scope,
                origin_run_id=origin_run_id, operation_id=operation_id)
        else:
            package = await data_runtime.resolve_package(scope, request.package_alias)
            base_revision_id = None
            if request.base_revision_ref is not None:
                origin_run_id, _model_id, base_revision_id, _revision_no = references.ifc_revision_locator(
                    scope.identity, request.base_revision_ref)
                if origin_run_id != scope.run_id:
                    raise ReferenceIndexError("The edit base belongs to another Run; read this Scope's actual IFC basis")
            command = EditIfcProductsCommand(package_id=package.package.package_id,
                payload_name=request.payload_name, base_revision_id=base_revision_id)
            result = await core.edit(scope, command, idempotency_key=request_identity)
            operation_ref = references.operation_reference(scope.identity,
                origin_run_id=scope.run_id, operation_id=result.operation_id)
            inspection = await data_runtime.get_ifc_products_recovery_operation(scope,
                origin_run_id=scope.run_id, operation_id=result.operation_id)
        return project_ifc_products_edit_operation(inspection, operation_ref=operation_ref,
            same_run=inspection.operation.operation.run_id == scope.run_id)
    except Exception as exc:
        operation_id = getattr(exc, "operation_id", None)
        if operation_ref is None and operation_id is not None:
            operation_ref = references.operation_reference(scope.identity,
                origin_run_id=scope.run_id, operation_id=operation_id)
        if isinstance(exc, DirectApplicationError):
            error = exc
        elif isinstance(exc, (ValueError, TypeError)) and result is not None:
            error = DirectApplicationError("OPERATION_INTEGRITY_ERROR", "The original edit result could not be projected",
                phase="output_validation")
        elif hasattr(exc, "code") and hasattr(exc, "phase"):
            phase = "preflight" if exc.phase in {"input", "input_validation"} else exc.phase
            error = DirectApplicationError(exc.code, str(exc), phase=phase,
                retryable=bool(getattr(exc, "retryable", False)),
                operation_alias=getattr(exc, "operation_alias", None), diagnostic=getattr(exc, "diagnostic", None),
                operation_ref=getattr(exc, "operation_ref", None), operation_status=getattr(exc, "operation_status", None))
        else:
            error = products_apply_error(exc, operation_ref=operation_ref)
        error.operation_ref = operation_ref or error.operation_ref
        if result is not None:
            error.operation_alias = result.operation_alias
            error.operation_status = result.outcome
        elif error.operation_status is None:
            error.operation_status = "unknown"
        raise error from None


class IfcProductsEditApplicationMixin:
    async def edit_ifc_products(self, request, *, request_identity):
        try:
            scope = await self._resolve_scope(request.direct_session_alias, "ifc_products_edit")
            if self._products_edit_service is None or self.reference_service is None:
                raise DirectApplicationError("SYSTEM_PAUSED", "The product edit service is unavailable", phase="preflight")
            with self._scope_binder(scope.identity):
                return await execute_products_edit(scope, request, request_identity=request_identity,
                    core=self._products_edit_service, data_runtime=self._application, references=self.reference_service)
        except DirectApplicationError:
            raise
        except Exception as exc:
            raise products_apply_error(exc) from None


async def execute_products_read(scope, request, *, core, family_catalog, references):
    """Resolve an opaque Operation locator and project only public read facts."""
    from pydantic import TypeAdapter
    from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadQuery
    from CAD2BIM_mcp.direct.ifc.products.contracts import (
        IfcProductsReadResult, ProductsReadOperation,
        ProductsReadInputNavigation, ProductsReadReportNavigation,
    )
    from CAD2BIM_mcp.direct.operation.contracts import ProductApplyCommittedRevision

    operation_ref = request.operation_ref
    try:
        locator = (references.operation_locator(scope.identity, operation_ref)
            if operation_ref is not None else None)
        query = TypeAdapter(IfcProductsReadQuery).validate_python(
            request.model_dump(exclude={"direct_session_alias", "operation_ref"}))
        outcome = await core.read(scope, query, operation_locator=locator)
        operation = None
        if outcome.operation is not None:
            inspection = outcome.operation
            record = inspection.operation.operation
            detail = record.detail
            family_ref = await family_catalog.version_ref_for_visible_id(
                scope.identity, detail.request.family_version_id, verify_assets=False)
            if operation_ref is None:
                operation_ref = references.operation_reference(scope.identity,
                    origin_run_id=record.run_id, operation_id=record.operation_id)
            exact = inspection.committed_ifc_revision
            committed = (ProductApplyCommittedRevision(revision_no=exact.revision_no,
                size_bytes=exact.artifact.size_bytes) if record.status.value == "succeeded" else None)

            def navigation(alias, *, exists):
                return ({"detail_access": "available", "package_alias": alias}
                    if alias is not None else {"detail_access": "not_in_current_catalog" if exists else "unavailable"})

            operation = ProductsReadOperation(operation_ref=operation_ref,
                outcome=record.status.value, family_version_ref=family_ref,
                committed_revision=committed,
                input=ProductsReadInputNavigation(payload_name=detail.input_binding.payload_name,
                    logical_key_fields=detail.input_binding.logical_key_fields,
                    details=navigation(outcome.input_alias, exists=True)),
                report=ProductsReadReportNavigation(details=navigation(outcome.report_alias,
                    exists=record.result_package_id is not None)))
        from CAD2BIM_mcp.direct.ifc.model.application import source_revision_result
        return IfcProductsReadResult(**outcome.facts.model_dump(), operation=operation,
            source_revision=source_revision_result(getattr(outcome, "source_exact", None),
                references=references, identity=scope.identity))
    except Exception as exc:
        raise products_read_error(exc, operation_ref=operation_ref) from None


def products_read_error(error, *, operation_ref=None):
    from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadError
    from CAD2BIM_backend.domain.ifc_model import IfcModelNotInitializedError, IfcModelIntegrityError
    from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError

    if isinstance(error, DirectApplicationError):
        projected = error
    elif isinstance(error, IfcProductsReadError):
        projected = DirectApplicationError(error.code, str(error), phase="read",
            diagnostic={"detail": error.diagnostic} if error.diagnostic is not None else None)
    elif isinstance(error, DataRuntimeApplicationError):
        projected = _direct_error(error, phase="read")
    elif isinstance(error, ProductFamilyCatalogError):
        projected = DirectApplicationError(error.code, str(error), phase="read")
    elif isinstance(error, ReferenceIndexError):
        projected = DirectApplicationError("REFERENCE_INVALID", str(error), phase="read")
    elif isinstance(error, IfcRevisionRuntimeError):
        projected = DirectApplicationError(error.code, error.message, phase="read",
            retryable=error.retryable, diagnostic=error.diagnostic)
    elif isinstance(error, IfcModelNotInitializedError):
        projected = DirectApplicationError("IFC_MODEL_NOT_INITIALIZED",
            "The calling Run has no current IFC model to read", phase="read")
    elif isinstance(error, IfcModelIntegrityError):
        projected = DirectApplicationError("IFC_MODEL_INTEGRITY_ERROR",
            "The selected IFC revision failed integrity verification", phase="read")
    elif isinstance(error, TimeoutError):
        projected = DirectApplicationError("IFC_PRODUCTS_READ_TIMEOUT",
            "The product read deadline expired; narrow the selection before retrying", phase="read")
    else:
        projected = DirectApplicationError("SYSTEM_PAUSED",
            "Product reading is unavailable pending server-side verification", phase="read")
    projected.operation_ref = operation_ref or projected.operation_ref
    return projected


class IfcProductsReadApplicationMixin:
    async def read_ifc_products(self, request):
        try:
            scope = await self._resolve_scope(request.direct_session_alias, "ifc_products_read")
            if self._products_read_service is None:
                raise DirectApplicationError("SYSTEM_PAUSED", "The product read service is unavailable", phase="read")
            with self._scope_binder(scope.identity):
                return await execute_products_read(scope, request,
                    core=self._products_read_service,
                    family_catalog=self._require_product_family_catalog_service(),
                    references=self.reference_service)
        except DirectApplicationError:
            raise
        except Exception as exc:
            raise products_read_error(exc, operation_ref=request.operation_ref) from None
