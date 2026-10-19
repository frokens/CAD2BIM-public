"""Fixed-command child program for one process-private IFC revision call."""

from __future__ import annotations

import hashlib
import os
import time
_MODULE_ENTERED_NS = time.monotonic_ns()
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import ValidationError
from CAD2BIM_backend.infrastructure.worker_runtime.timing import WorkerTiming, timed_phase, sample_worker_memory

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcGeometryBasisBusinessError,
    IfcGeometryBasisIntegrityError,
)
from CAD2BIM_backend.domain.ifc_model import InitializeIfcModelCommand, IfcModelValidationError
from CAD2BIM_backend.domain.ifc_product_compilation import CompileIfcProductsCommand, IfcProductCompilationBlock, IfcProductCompilationError
from CAD2BIM_backend.domain.product_edit import CompileIfcProductsEditCommand, IfcProductsEditPreflightRequest
from CAD2BIM_backend.domain.ifc_project import IfcProjectBusinessError, UpdateIfcProjectCommand
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    IfcSpatialNotEmptyError,
    IfcSpatialStructureBusinessError,
    IfcSpatialStructureIntegrityError,
    IfcSpatialStructureListQuery,
)
from CAD2BIM_backend.infrastructure.ifc.codec import (
    IfcOpenShellModelCodec,
)
from CAD2BIM_backend.infrastructure.ifc_revision_runtime.protocol import (
    MAX_IFC_REVISION_CONTROL_BYTES,
    MAX_IFC_REVISION_INPUT_BYTES,
    MAX_IFC_REVISION_RESULT_BYTES,
    MAX_IFC_PRODUCT_BLOCK_BYTES, MAX_IFC_PRODUCT_STREAM_BYTES,
)
from CAD2BIM_backend.infrastructure.worker_runtime.protocol import (
    MAX_STATUS_BYTES,
    open_binary_fd,
    read_json_frame,
    write_json_frame,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError

if TYPE_CHECKING:
    from CAD2BIM_backend.infrastructure.ifc.product_compilation import IfcOpenShellProductCompiler


_CONTROL_ENV = "CAD2BIM_IFC_REVISION_CONTROL_FD"
_RESULT_ENV = "CAD2BIM_IFC_REVISION_RESULT_FD"
_TERMINAL_ENV = "CAD2BIM_IFC_REVISION_TERMINAL_FD"
_CANDIDATE_WRITE_ENV = "CAD2BIM_IFC_REVISION_CANDIDATE_WRITE_FD"
_CANDIDATE_READ_ENV = "CAD2BIM_IFC_REVISION_CANDIDATE_READ_FD"
_BUSINESS_ERRORS = (
    IfcGeometryBasisBusinessError,
    IfcProjectBusinessError,
    IfcSpatialStructureBusinessError,
)


def _descriptor(name: str) -> int:
    value = os.environ.get(name, "")
    if not value.isdigit():
        raise RuntimeError(f"required worker descriptor {name} is unavailable")
    return int(value)


def _read_source(control: dict[str, Any]) -> tuple[bytes, str]:
    source = control.get("source")
    if not isinstance(source, dict):
        raise ValueError("this IFC revision command requires an exact source")
    descriptor = source.get("fd")
    expected_size = source.get("size_bytes")
    expected_sha256 = source.get("sha256")
    if not isinstance(descriptor, int) or isinstance(descriptor, bool):
        raise ValueError("source descriptor is invalid")
    if not isinstance(expected_size, int) or not 1 <= expected_size <= MAX_IFC_REVISION_INPUT_BYTES:
        raise ValueError("source size is invalid")
    if not isinstance(expected_sha256, str):
        raise ValueError("source digest is invalid")
    # The exact source capability stays alive until this command ends. Reading
    # a duplicate preserves it for the native IFC reader; both share an offset.
    with open_binary_fd(os.dup(descriptor), "rb") as stream:
        stream.seek(0)
        content = stream.read(MAX_IFC_REVISION_INPUT_BYTES + 1)
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if len(content) != expected_size or actual_sha256 != expected_sha256:
        raise ValueError("staged exact source does not match its declared metadata")
    return content, actual_sha256


def _save_candidate(content: bytes) -> None:
    if len(content) > MAX_IFC_REVISION_INPUT_BYTES:
        raise IfcRevisionRuntimeError(
            "IFC_REVISION_CANDIDATE_TOO_LARGE",
            f"IFC revision candidate is {len(content)} bytes; limit is {MAX_IFC_REVISION_INPUT_BYTES} bytes",
            phase="candidate_save", execution_started=True,
            diagnostic={"actual_bytes": len(content), "limit_bytes": MAX_IFC_REVISION_INPUT_BYTES},
        )
    write_descriptor = _descriptor(_CANDIDATE_WRITE_ENV)
    with open_binary_fd(write_descriptor, "wb") as stream:
        for offset in range(0, len(content), 1024 * 1024):
            stream.write(content[offset : offset + 1024 * 1024])
        stream.flush()
        os.fsync(stream.fileno())


def _write_candidate(content: bytes) -> bytes:
    _save_candidate(content)
    read_descriptor = _descriptor(_CANDIDATE_READ_ENV)
    with open_binary_fd(read_descriptor, "rb") as stream:
        reopened = stream.read(MAX_IFC_REVISION_INPUT_BYTES + 1)
    if reopened != content:
        raise RuntimeError("reopened candidate differs from compiler output")
    return reopened


def _execute(control: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source = control.get("source")
    descriptor = source.get("fd") if isinstance(source, dict) else None
    if isinstance(descriptor, int) and not isinstance(descriptor, bool) and descriptor >= 0:
        with open_binary_fd(descriptor, "rb"):
            return _execute_command(control)
    return _execute_command(control)


def _execute_command(control: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    kind = control.get("kind")
    codec = IfcOpenShellModelCodec()
    source_sha256: str | None = None

    if kind == "initialize":
        if control.get("source") is not None:
            raise ValueError("initialize must not receive an exact source")
        command = InitializeIfcModelCommand.model_validate(control.get("command"))
        project_global_id = control.get("project_global_id")
        header_timestamp = datetime.fromisoformat(str(control.get("header_timestamp", "")))
        candidate = codec.create_revision1(
            project_global_id=project_global_id,
            project_name=command.project_name,
            header_timestamp=header_timestamp,
        )
        reopened = _write_candidate(candidate.content)
        inspection = codec.inspect_revision1(
            reopened,
            expected_project_global_id=project_global_id,
            expected_project_name=command.project_name,
        )
        effect: dict[str, Any] | None = None
    elif kind == "inspect_exact":
        with timed_phase("ifc_inspect_read_bytes"):
            content, source_sha256 = _read_source(control)
        expected_global_id = control.get("expected_project_global_id")
        expected_name = control.get("expected_project_name")
        if (expected_global_id is None) != (expected_name is None):
            raise ValueError("revision-1 inspection expectations must be provided together")
        with timed_phase("ifc_inspect_parse"):
            model = codec.private_copy_from_source_fd(
                content, descriptor=control["source"]["fd"],
            ).model
        with timed_phase("ifc_inspect_facts"):
            inspection = (
                codec.inspect_from_model(content, model)
                if expected_global_id is None
                else codec.inspect_revision1_from_model(
                    content, model,
                    expected_project_global_id=expected_global_id,
                    expected_project_name=expected_name,
                )
            )
        projection = control.get("projection", "model")
        result = {"inspection": inspection.model_dump(mode="json")}
        if projection == "contexts":
            from CAD2BIM_backend.infrastructure.ifc.geometry_basis import IfcOpenShellGeometryBasisCompiler

            result["contexts"] = IfcOpenShellGeometryBasisCompiler(codec).inspect_contexts_from_model(model).model_dump(mode="json")
        elif projection == "spatial":
            from CAD2BIM_backend.infrastructure.ifc.spatial_structure import IfcOpenShellSpatialStructureCompiler

            query = IfcSpatialStructureListQuery.model_validate(control.get("spatial_query"))
            from CAD2BIM_backend.domain.ifc_spatial_structure import project_spatial_catalog
            result["spatial_page"] = project_spatial_catalog(IfcOpenShellSpatialStructureCompiler(codec).inspect_spatial_structure_from_model(model), query).model_dump(mode="json")
        elif projection == "host_facts":
            from CAD2BIM_backend.domain.product_realization_host import CurrentIfcHostFactRequest
            from CAD2BIM_backend.infrastructure.ifc.product_realization_host import IfcOpenShellProductRealizationHostFactExtractor

            host_request = CurrentIfcHostFactRequest.model_validate(control.get("host_request"))
            result["host_facts"] = [item.model_dump(mode="json") for item in
                IfcOpenShellProductRealizationHostFactExtractor(codec).extract_from_model(model, host_request.selectors, ifc_sha256=source_sha256)]
        elif projection == "body":
            from CAD2BIM_backend.infrastructure.ifc.product_compilation import preflight_product_body

            result["body"] = preflight_product_body(model, inspection)[1].model_dump(mode="json")
        elif projection == "products":
            from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadRuntimeRequest
            from CAD2BIM_backend.infrastructure.ifc.product_read import read_products

            request = IfcProductsReadRuntimeRequest.model_validate(control.get("products_request"))
            deadline = control.get("deadline_monotonic")
            def check_budget():
                if deadline is not None and time.monotonic() >= deadline:
                    raise IfcRevisionRuntimeError("IFC_REVISION_TIMEOUT", "IFC revision exceeded its original time budget",
                        phase="inspection", execution_started=True)
            result["products"] = read_products(model, request, check_budget=check_budget).model_dump(mode="json")
        elif projection == "edit_targets":
            from CAD2BIM_backend.infrastructure.ifc.product_edit import preflight_edit_targets
            request = IfcProductsEditPreflightRequest.model_validate(control.get("edit_targets_request"))
            with timed_phase("ifc_edit_target_preflight", flush=True):
                preflight = preflight_edit_targets(model, request)
            result["edit_targets"] = preflight.model_dump(mode="json")
        elif projection != "model":
            raise ValueError("unknown IFC inspection projection")
        return (
            result,
            {"source_sha256": source_sha256},
        )
    elif kind in {"compile_products", "edit_products"}:
        from CAD2BIM_backend.infrastructure.ifc.product_compilation import IfcOpenShellProductCompiler

        started = time.monotonic()
        content, source_sha256 = _read_source(control)
        private = codec.private_copy_from_source_fd(
            content, descriptor=control["source"]["fd"],
        )
        if kind == "edit_products":
            from CAD2BIM_backend.infrastructure.ifc.product_edit import IfcOpenShellProductEditor
            command = CompileIfcProductsEditCommand.model_validate(control.get("command"))
            with timed_phase("ifc_edit_editor_prepare", flush=True):
                compiler = IfcOpenShellProductEditor(private.model, command)
        else:
            command = CompileIfcProductsCommand.model_validate(control.get("command"))
            compiler = IfcOpenShellProductCompiler(private.model, command)
        initialization = time.monotonic() - started
        _consume_product_blocks(control, compiler)
        if kind == "edit_products":
            compiler.check_complete()
        saved_at = time.monotonic()
        with timed_phase("ifc_serialize"):
            serialized = private.to_bytes()
        with timed_phase("ifc_save_exact_bytes"):
            _save_candidate(serialized)
        with timed_phase("ifc_saved_reopen_inspect"):
            read_descriptor = _descriptor(_CANDIDATE_READ_ENV)
            try:
                with timed_phase("ifc_saved_parse"):
                    saved_model = codec.private_copy_from_saved_file(
                        serialized, path=Path("candidate.ifc"), descriptor=read_descriptor,
                    ).model
            finally:
                os.close(read_descriptor)
            # The saved-file codec has compared every actual byte to this
            # compiler output before opening the same private candidate file.
            reopened = serialized
            with timed_phase("ifc_saved_facts"):
                inspection = codec.inspect_from_model(reopened, saved_model)
            with timed_phase("ifc_saved_product_links"):
                compiler.verify_saved(saved_model)
        effect = {"kind": "products_edit" if kind == "edit_products" else "products", **compiler.effect(timings={
            "initialization": initialization, "save_readback": time.monotonic() - saved_at,
            "worker_total": time.monotonic() - started,
        }).model_dump(mode="json")}
    elif kind == "configure_geometry_basis":
        from CAD2BIM_backend.infrastructure.ifc.geometry_basis import IfcOpenShellGeometryBasisCompiler

        content, source_sha256 = _read_source(control)
        command = ConfigureIfcGeometryBasisCommand.model_validate(control.get("command"))
        compiler = IfcOpenShellGeometryBasisCompiler(codec)
        candidate = compiler.apply(content, command)
        reopened = _write_candidate(candidate.content)
        inspection = codec.inspect(reopened)
        compiler.inspect_contexts(reopened)
        compiler.inspect_geometry_basis(reopened)
        effect = None
    elif kind == "update_project":
        from CAD2BIM_backend.infrastructure.ifc.project import IfcOpenShellProjectCompiler

        content, source_sha256 = _read_source(control)
        command = UpdateIfcProjectCommand.model_validate(control.get("command"))
        compiler = IfcOpenShellProjectCompiler(codec)
        candidate = compiler.apply(content, command)
        reopened = _write_candidate(candidate.content)
        inspection = codec.inspect(reopened)
        project = compiler.inspect_project(reopened)
        effect = {"kind": "project", "project": project.model_dump(mode="json")}
    elif kind == "apply_spatial_structure":
        from CAD2BIM_backend.infrastructure.ifc.spatial_structure import IfcOpenShellSpatialStructureCompiler

        content, source_sha256 = _read_source(control)
        command = ApplyIfcSpatialStructureCommand.model_validate(control.get("command"))
        compiler = IfcOpenShellSpatialStructureCompiler(codec)
        compilation = compiler.apply(content, command)
        reopened = _write_candidate(compilation.candidate.content)
        inspection = codec.inspect(reopened)
        catalog = compiler.inspect_spatial_structure(reopened)
        affected = set(compilation.affected_global_ids)
        effect = {
            "kind": "spatial",
            "project_global_id": catalog.project_global_id,
            "created": [item.model_dump(mode="json") for item in compilation.created],
            "affected_global_ids": list(compilation.affected_global_ids),
            "spatial_summary": [
                item.model_dump(mode="json")
                for item in catalog.items
                if item.global_id in affected
            ],
        }
    else:
        raise ValueError("unsupported IFC revision worker command")

    result: dict[str, Any] = {
        "candidate": {
            "size_bytes": inspection.size_bytes,
            "sha256": inspection.sha256,
        },
        "inspection": inspection.model_dump(mode="json"),
        "effect": effect,
    }
    return result, {"source_sha256": source_sha256}


def _diagnostic(exc: BaseException) -> dict[str, Any] | None:
    if isinstance(exc, (IfcProductCompilationError, IfcRevisionRuntimeError)):
        return exc.diagnostic
    if isinstance(exc, IfcSpatialNotEmptyError):
        return exc.blockers.model_dump(mode="json")
    if isinstance(exc, ValidationError):
        return {
            "validation_errors": exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        }
    return None


def _consume_product_blocks(control: dict[str, Any], compiler: IfcOpenShellProductCompiler) -> None:
    descriptor = control.get("product_blocks_fd")
    if not isinstance(descriptor, int) or isinstance(descriptor, bool):
        raise ValueError("product block descriptor is invalid")
    class CountedReader:
        size = 0
        def __init__(self, stream):
            self.stream = stream
        def read(self, size):
            value = self.stream.read(size)
            self.size += len(value)
            if self.size > MAX_IFC_PRODUCT_STREAM_BYTES:
                raise ValueError("product block stream exceeds its cumulative byte budget")
            return value
    with open_binary_fd(descriptor, "rb") as stream:
        reader = CountedReader(stream)
        count = 0
        while True:
            with timed_phase("ifc_block_receive", block_index=count):
                frame = read_json_frame(reader, limit=MAX_IFC_PRODUCT_BLOCK_BYTES)
            if frame == {"status": "end", "count": count}:
                if (not count and control.get("kind") != "edit_products") or reader.read(1):
                    raise ValueError("product block stream is empty or has trailing bytes")
                return
            if set(frame) != {"status", "index", "block"} or frame["status"] != "block" or frame["index"] != count:
                raise ValueError("product block sequence is invalid")
            with timed_phase("ifc_block_decode", block_index=count):
                block = IfcProductCompilationBlock.model_validate(frame["block"])
            compiler.consume(block)
            products, fragments = len(block.items), sum(len(item.fragment_sources) for item in block.items)
            del block, frame
            sample_worker_memory("ifc_block_released", block_index=count,
                                 product_count=products, fragment_count=fragments)
            count += 1


def _fail(
    terminal_stream: Any,
    exc: BaseException,
    *,
    execution_started: bool,
    inspected_source: bool = False,
) -> NoReturn:
    business = isinstance(exc, _BUSINESS_ERRORS)
    if inspected_source:
        from CAD2BIM_backend.domain.product_realization_host import ProductRealizationHostError
        from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadError

        business = business or isinstance(exc, (ProductRealizationHostError, IfcProductsReadError))
    integrity = inspected_source and isinstance(exc, (
        IfcModelValidationError, IfcGeometryBasisIntegrityError, IfcSpatialStructureIntegrityError,
    ))
    invalid_control = isinstance(exc, (TypeError, ValueError, ValidationError))
    write_json_frame(
        terminal_stream,
        {
            "status": "failed",
            "code": str(
                getattr(
                    exc,
                    "code",
                    "IFC_REVISION_INPUT_INVALID"
                    if isinstance(exc, (TypeError, ValueError, ValidationError))
                    else "IFC_REVISION_WORKER_FAILED",
                )
            ),
            "message": str(exc),
            "phase": getattr(exc, "phase", "integrity" if integrity else "business" if business else "execution"),
            "execution_started": execution_started,
            "retryable": getattr(exc, "retryable", not business and not invalid_control and not isinstance(exc, IfcProductCompilationError)),
            "diagnostic": _diagnostic(exc),
        },
        limit=MAX_STATUS_BYTES,
    )
    raise SystemExit(2)


def _main() -> None:
    terminal_descriptor = _descriptor(_TERMINAL_ENV)
    control: dict[str, Any] = {}
    with open_binary_fd(terminal_descriptor, "wb") as terminal_stream:
        try:
            with open_binary_fd(_descriptor(_CONTROL_ENV), "rb") as control_stream:
                control = read_json_frame(
                    control_stream,
                    limit=MAX_IFC_REVISION_CONTROL_BYTES,
                )
            result, attestation = _execute(control)
            with open_binary_fd(_descriptor(_RESULT_ENV), "wb") as result_stream:
                write_json_frame(
                    result_stream,
                    result,
                    limit=MAX_IFC_REVISION_RESULT_BYTES,
                )
            write_json_frame(
                terminal_stream,
                {
                    "status": "succeeded",
                    "kind": control.get("kind"),
                    "source_sha256": attestation["source_sha256"],
                },
                limit=MAX_STATUS_BYTES,
            )
        except SystemExit:
            raise
        except BaseException as exc:
            _fail(terminal_stream, exc, execution_started=True, inspected_source=control.get("kind") == "inspect_exact")


def main() -> None:
    timing = WorkerTiming("ifc_revision", _MODULE_ENTERED_NS)
    try:
        _main()
    finally:
        timing.close()


if __name__ == "__main__":
    main()
