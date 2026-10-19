"""One-shot isolated Product Realization Program child process."""

from __future__ import annotations

import time
_MODULE_ENTERED_NS = time.monotonic_ns()
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.product_realization import (
    MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
    MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
    MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES,
    MAX_PRODUCT_REALIZATION_SOURCE_BUNDLE_BYTES,
    PRODUCT_REALIZATION_RSS_BYTES,
    PRODUCT_REALIZATION_SCRATCH_BYTES,
    PRODUCT_REALIZATION_THREADS,
    ProductGeometryInstanceReceipt,
    ProductGeometryInterfaceEvaluation,
    RejectedProductRealizationProposal,
    ProductRealizationViolation,
    ProductRealizationInterfaceContractError,
    ProductRealizationAuthorProposal,
    ProductRealizationOutputNeed,
    ProductRealizationRequest,
    ProductRealizationResult,
    RigidTransform3D,
    ResolvedMappedGeometryBinding,
    SolvedProductRealizationProposal,
    compose_rigid_transform,
    complete_product_realization_instantiation,
    finalize_product_realization,
    _PreparedProductRealizationRequest,
    _prepare_geometry_binding_parameters,
    product_realization_structure,
    validate_product_realization_proposal,
)
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometryFormat, GeometryRuntimeValidationError
from CAD2BIM_backend.domain.geometry_interface import (
    scale_geometry_interfaces,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
from CAD2BIM_backend.infrastructure.worker_runtime.timing import WorkerTiming, timed_phase
from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import (
    MAX_PRODUCT_REALIZATION_INSTANCE_GEOMETRY_BYTES,
    ProductRealizationPreviewGeometryMetadata,
)
from CAD2BIM_backend.infrastructure.worker_runtime import open_binary_fd
from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import (
    CompiledPythonBundle,
    PythonBundleValidationError,
    compile_python_bundle,
)
from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import (
    RestrictedImportGuard,
    diagnostic_json,
    install_readonly_audit_hook,
    runtime_code_diagnostic,
)


_ALLOWED_IMPORTS = frozenset({"math"})
_PROPOSAL_ADAPTER = TypeAdapter(ProductRealizationAuthorProposal)
# Private per-Worker optimization only; eviction never changes admission/results.
_LOCAL_RESULT_CACHE_BYTES = 16 * 1024 * 1024
_LOCAL_RESULT_CACHE_ITEMS = 128


def main() -> int:
    timing = WorkerTiming("product_realization", _MODULE_ENTERED_NS)
    try:
        return _batch_main()
    finally:
        timing.close()




def _require_modeling_environment(*, native=True):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_environment import ModelingEnvironmentError, require_modeling_environment
    try:
        require_modeling_environment()
        if native:
            with timed_phase("geometry_native_dependencies"):
                _load_geometry_dependencies()
    except ModelingEnvironmentError as exc:
        raise _ChildError("REALIZATION_RUNTIME_UNAVAILABLE", str(exc), "preflight") from exc


def _load_geometry_dependencies():
    # Actual modeling initializes its required stack before the author audit.
    # Codec's no-summary byte validation intentionally no longer does this.
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_environment import load_modeling_dependencies
    load_modeling_dependencies()
    global read_geometry_payload, summarize_shape, save_user_shape, serialize_user_shape, reopen_user_shape, validate_user_shape
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec import read_geometry_payload, summarize_shape
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import save_user_shape, serialize_user_shape, reopen_user_shape, validate_user_shape


def _batch_main() -> int:
    numeric_only = os.environ.pop("CAD2BIM_PRODUCT_NUMERIC_ONLY", "0") == "1"
    instantiate_only = os.environ.pop("CAD2BIM_PRODUCT_INSTANTIATE_ONLY", "0") == "1"
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import (
        MAX_PRODUCT_REALIZATION_CONTROL_BYTES, MAX_PRODUCT_BATCH_OUTPUT_BYTES,
        MAX_PRODUCT_BATCH_RESULT_FRAME_BYTES, restore_batch_header, restore_batch_invocation,
    )
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import read_json_frame, write_json_frame, write_all, encode_json_frame, WorkerProtocolError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_family_program  # noqa: F401
    request_stream = open_binary_fd(_descriptor("CAD2BIM_PRODUCT_REQUEST_FD"), "rb")
    source_stream = open_binary_fd(_descriptor("CAD2BIM_PRODUCT_SOURCE_FD"), "rb")
    terminal = open_binary_fd(_descriptor("CAD2BIM_PRODUCT_TERMINAL_FD"), "wb")
    inputs = open_binary_fd(_descriptor("CAD2BIM_PRODUCT_BATCH_INPUT_FD"), "rb")
    outputs = open_binary_fd(_descriptor("CAD2BIM_PRODUCT_BATCH_OUTPUT_FD"), "wb")
    root = Path(os.environ.pop("CAD2BIM_PRODUCT_BINDING_ROOT"))
    started, item_index, output_bytes = False, -1, 0
    try:
        try:
            payload = request_stream.read(MAX_PRODUCT_REALIZATION_CONTROL_BYTES + 1025)
            if len(payload) > MAX_PRODUCT_REALIZATION_CONTROL_BYTES + 1024:
                raise ValueError("product batch closure is oversized")
            base, header = restore_batch_header(payload,
                source_stream.read(MAX_PRODUCT_REALIZATION_SOURCE_BUNDLE_BYTES + 1), root)
        except (TypeError, ValueError, KeyError) as exc:
            raise _ChildError("REALIZATION_BINDING_INVALID", _bounded(str(exc), 2000), "input_validation") from exc
        _require_modeling_environment(native=not numeric_only)
        prepared_request = _PreparedProductRealizationRequest(base)
        with timed_phase("prepare_family_program", entry_count=len(header["entry_names"])):
            if instantiate_only:
                program = None
            else:
                program = _compile_family(base.source_bundle, "cad2bim_realization_program", header["entry_names"][0],
                                          required_entry_names=header["entry_names"])
        try:
            prepared = _GeometryBindingLifecycle(base.geometry_bindings)
            parameter_rules = None if instantiate_only else prepared.parameter_rules
        except GeometryRuntimeValidationError as exc:
            raise _ChildError("REALIZATION_BINDING_INVALID", _bounded(str(exc), 2000), "input_validation") from exc
        guard = RestrictedImportGuard(allowed_import_roots=_ALLOWED_IMPORTS,
            policy={"threads": PRODUCT_REALIZATION_THREADS, "rss_bytes": PRODUCT_REALIZATION_RSS_BYTES,
                    "scratch_bytes": PRODUCT_REALIZATION_SCRATCH_BYTES}, worker_label="product realization batch")
        install_readonly_audit_hook(guard)
        ready_frame = encode_json_frame({"status": "ready"}, limit=1024)
        finished_frame = encode_json_frame({"status": "finished", "item_count": header["item_count"]}, limit=1024)
        output_bytes = len(ready_frame) + len(finished_frame)
        write_all(outputs, ready_frame)
        window_ends, cumulative = set(), 0
        for count in header.get("window_sizes", [header["item_count"]])[:-1]:
            cumulative += count
            window_ends.add(cumulative)
        for item_index in range(header["item_count"] + 1):
            try:
                frame = read_json_frame(inputs, limit=MAX_PRODUCT_REALIZATION_CONTROL_BYTES)
                if item_index in window_ends and "finish" not in frame:
                    if frame != {"window_end": item_index} or type(frame["window_end"]) is not int:
                        raise ValueError("product window boundary is invalid")
                    with timed_phase("window_boundary", completed_count=item_index):
                        write_json_frame(outputs, {"status": "window_finished", "item_count": item_index}, limit=1024)
                    output_bytes = len(finished_frame)
                    frame = read_json_frame(inputs, limit=MAX_PRODUCT_REALIZATION_CONTROL_BYTES)
                if "finish" in frame:
                    if (frame != {"finish": item_index}
                            or type(frame["finish"]) is not int or inputs.read(1)):
                        raise ValueError("product batch finish frame is invalid")
                    break
                if item_index == header["item_count"]:
                    raise ValueError("product batch requires its finish frame")
                with timed_phase("batch_input_normalize", item_index=item_index):
                    request, need = restore_batch_invocation(prepared_request, frame, item_index)
                entry = "standard_model" if request.invocation.realization_kind == "standard_model" else "realize"
                if program is not None and entry not in program.entry_names:
                    raise ValueError("batch entry was not prepared")
                if instantiate_only != ("precheck" in frame):
                    raise ValueError("batch instantiation proposal state is invalid")
                if not instantiate_only and need not in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES):
                    raise ValueError("numeric evaluation cannot request geometry without its checked proposal")
                if numeric_only and need not in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES):
                    raise ValueError("numeric batch cannot request native geometry")
                reused = frame.get("reused_fragments", {})
                if not isinstance(reused, dict) or ("reused_fragments" in frame and (
                        not instantiate_only or need is not ProductRealizationOutputNeed.LOCAL_FRAGMENTS)):
                    raise ValueError("reused sources require local-fragment instantiation")
                reused = {key: ProductRealizationPreviewGeometryMetadata.model_validate(value)
                          for key, value in reused.items()}
                if any(value.format is not CanonicalGeometryFormat.OCCT_BREP_V1 for value in reused.values()):
                    raise ValueError("reused sources must be canonical OCCT BREP")
            except (TypeError, ValueError, KeyError, WorkerProtocolError) as exc:
                raise _ChildError("REALIZATION_BINDING_INVALID", _bounded(str(exc), 2000), "input_validation") from exc
            started = True
            with timed_phase("batch_item", item_index=item_index):
                context = _ProductRealizationContext(request, prepared=prepared,
                    parameter_rules=parameter_rules,
                    include_instances=need not in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES),
                    capture_construction=need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
                    include_summary=need is ProductRealizationOutputNeed.PREVIEW_WORLD,
                    defer_validation_to_codec=need is not ProductRealizationOutputNeed.SOLVE_ONLY,
                    reused_fragments=reused)
                checked = None
                if instantiate_only:
                    with timed_phase("instantiate_precheck_decode"):
                        checked = ProductRealizationResult.model_validate(frame["precheck"])
                result, preview = _evaluate_invocation(
                    request, program, context, output_need=need,
                    precheck=checked,
                )
                with timed_phase("batch_result_projection"):
                    result_json = result.model_dump(mode="json")
                    if len(result.model_dump_json().encode("utf-8")) > 1024 * 1024:
                        raise _ChildError("REALIZATION_OUTPUT_INVALID", "batch result exceeds its per-instance byte budget", "output_validation")
                geometries, payloads = [], []
                if preview is not None:
                    geometries.append({"fragment_key": None,
                        "geometry": ProductRealizationPreviewGeometryMetadata.from_brep(*preview).model_dump(mode="json")})
                    payloads.append(preview[0])
                if context._instance_payloads is not None and isinstance(result.proposal, SolvedProductRealizationProposal):
                    for receipt in result.instance_receipts:
                        if receipt.fragment_key in context._reused_fragments:
                            continue
                        content = context._instance_payloads[receipt.fragment_key]
                        geometries.append({"fragment_key": receipt.fragment_key,
                            "geometry": ProductRealizationPreviewGeometryMetadata.from_brep(content, receipt.geometry_summary).model_dump(mode="json")})
                        payloads.append(content)
                constructions, construction_payloads = [], []
                for fragment_key, construction in context._instance_constructions.items():
                    with timed_phase("geometry_construction_record_payload", fragment_key=fragment_key):
                        content = construction.model_dump_json().encode("utf-8")
                    constructions.append({"fragment_key": fragment_key, "byte_length": len(content),
                                          "sha256": hashlib.sha256(content).hexdigest()})
                    construction_payloads.append(content)
                response = {"status": "succeeded", "item_index": item_index,
                            "output_need": need.value,
                            "result": result_json, "geometries": geometries,
                            "constructions": constructions}
                if context._reused_fragments:
                    response["reused_fragments"] = [receipt.fragment_key for receipt in result.instance_receipts
                                                    if receipt.fragment_key in context._reused_fragments]
                try:
                    with timed_phase("batch_output_encode", item_index=item_index):
                        response_frame = encode_json_frame(response, limit=MAX_PRODUCT_BATCH_RESULT_FRAME_BYTES)
                except WorkerProtocolError as exc:
                    raise _ChildError("REALIZATION_OUTPUT_INVALID", str(exc), "output_validation") from exc
                output_bytes += len(response_frame) + sum(map(len, payloads)) + sum(map(len, construction_payloads))
                if output_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                    raise _ChildError("REALIZATION_OUTPUT_INVALID", "product batch output exceeds its aggregate byte budget", "output_validation")
                with timed_phase("batch_output_write", item_index=item_index):
                    write_all(outputs, response_frame)
                    for content in payloads:
                        write_all(outputs, content)
                    for content in construction_payloads:
                        write_all(outputs, content)
                    outputs.flush()
                # Drop all invocation-owned shapes/bytes before waiting for the
                # next Host frame; prepared templates never enter author code.
                del context, checked, result, preview, payloads, geometries, constructions, construction_payloads, response, result_json, response_frame
                if "content" in locals():
                    del content
            from CAD2BIM_backend.infrastructure.worker_runtime.timing import sample_worker_memory
            sample_worker_memory("batch_item_released", item_index=item_index)
        finished_frame = encode_json_frame({"status": "finished", "item_count": item_index}, limit=1024)
        write_all(outputs, finished_frame)
        _write_json(terminal, {"status": "succeeded", "phase": "execution", "execution_started": started,
                               "item_count": item_index})
        return 0
    except BaseException as exc:
        if isinstance(exc, PythonBundleValidationError):
            failure = _ChildError("REALIZATION_SOURCE_INVALID", str(exc), "code_validation", diagnostic=exc.diagnostic)
        else:
            failure = exc if isinstance(exc, _ChildError) else _ChildError("REALIZATION_RUNTIME_FAILED", str(exc), "execution" if started else "preflight")
        _write_failure(terminal, failure, started=started)
        try:
            write_json_frame(outputs, {**_failure_payload(failure), "item_index": item_index}, limit=4096)
        except BaseException:
            pass
        return 1


def _evaluate_invocation(request, program, context, *, output_need, precheck=None):
    """Single and batch share author execution, placement, finalizer and failures."""
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_family_program
    entry_name = "standard_model" if request.invocation.realization_kind == "standard_model" else "realize"
    if precheck is not None:
        try:
            return _instantiate_invocation(request, precheck, context, output_need=output_need)
        except _ChildError:
            raise
        except ModelingCapabilityError as exc:
            raise _ChildError("REALIZATION_EXECUTION_FAILED", _bounded(str(exc), 2000), "geometry_build") from exc
        except Exception as exc:
            raise _ChildError("REALIZATION_OUTPUT_INVALID", _output_validation_message(exc), "geometry_instantiation") from exc
    try:
        with timed_phase("author_entry"):
            raw_proposal = execute_family_program(program, context, entry_name=entry_name)
    except _ChildError:
        raise
    except BaseException as exc:
        diagnostic = runtime_code_diagnostic(
            program.sources["main.py"], entry_name, exc,
            virtual_sources=program.sources, virtual_filename_prefix="<cad2bim-bundle:",
        )
        raise _ChildError("REALIZATION_EXECUTION_FAILED", str(exc), "execution", diagnostic=diagnostic) from exc
    try:
        try:
            author_proposal = _PROPOSAL_ADAPTER.validate_python(raw_proposal)
            proposal = validate_product_realization_proposal(request, author_proposal, _parameter_rules=context.parameter_rules)
        except ValueError as exc:
            # This boundary contains only deterministic proposal-contract checks.
            proposal = RejectedProductRealizationProposal(rejection=ProductRealizationViolation(
                violation_key="proposal_contract_rejected", message=_bounded(_output_validation_message(exc), 512)))
        evaluations = ()
        if isinstance(proposal, SolvedProductRealizationProposal) and output_need is not ProductRealizationOutputNeed.SOLVE_ONLY:
            evaluations = context.evaluate_interfaces(proposal)
        with timed_phase("domain_finalize"):
            result = finalize_product_realization(request, proposal, (), interface_evaluations=evaluations,
                output_need=ProductRealizationOutputNeed.SOLVE_ONLY if output_need is ProductRealizationOutputNeed.SOLVE_ONLY or isinstance(proposal, RejectedProductRealizationProposal) else ProductRealizationOutputNeed.INTERFACES,
                _parameter_rules=context.parameter_rules, _proposal_validated=True)
        return result, None
    except _ChildError:
        raise
    except PythonBundleValidationError:
        # The child entry owns code-validation classification for exact source.
        raise
    except ProductRealizationInterfaceContractError as exc:
        raise _ChildError("REALIZATION_BINDING_INVALID", str(exc), "interface_contract") from exc
    except ModelingCapabilityError as exc:
        raise _ChildError(
            "REALIZATION_EXECUTION_FAILED", _bounded(str(exc), 2000), "execution",
        ) from exc
    except Exception as exc:
        raise _ChildError("REALIZATION_OUTPUT_INVALID", _output_validation_message(exc), "output_validation") from exc


def _instantiate_invocation(request, precheck, context, *, output_need):
    from CAD2BIM_backend.domain.product_realization import validate_product_realization_instantiation_precheck
    try:
        with timed_phase("instantiate_precheck_integrity"):
            validate_product_realization_instantiation_precheck(request, precheck)
            if set(context._reused_fragments) - {item.fragment_key for item in precheck.proposal.fragments}:
                raise ValueError("reused source references an unknown fragment")
    except (ValueError, TypeError) as exc:
        raise _ChildError("REALIZATION_BINDING_INVALID", _output_validation_message(exc), "input_validation") from exc
    context.build_fragments(precheck.proposal, precheck.interface_evaluations)
    result = complete_product_realization_instantiation(precheck, context.receipts)
    context.place_finalized_geometry(result, materialize_world=output_need is ProductRealizationOutputNeed.PREVIEW_WORLD)
    return result, context.compose_preview_geometry() if output_need is ProductRealizationOutputNeed.PREVIEW_WORLD else None


class _PreparedGeometryBindings:
    """Host-only templates and unexecuted code for selected exact bindings."""

    def __init__(self, bindings, *, parameter_rules=None) -> None:
        self.parameter_rules = (
            _prepare_geometry_binding_parameters(bindings)
            if parameter_rules is None
            else parameter_rules
        )
        self.function_programs = {}
        self.function_geometries = {}
        self.mapped_shapes = {}
        self.mapped_summaries = {}
        self.load(tuple(enumerate(bindings)))

    def load(self, indexed_bindings) -> None:
        with timed_phase("prepare_exact_geometry", binding_count=len(indexed_bindings)):
            for index, binding in indexed_bindings:
                if isinstance(binding, ResolvedMappedGeometryBinding):
                    with timed_phase("reopen_mapped_template", binding_key=binding.binding_key):
                        shape = read_geometry_payload(binding.geometry)
                    self.mapped_shapes[binding.binding_key] = shape
                    self.mapped_summaries[binding.binding_key] = binding.geometry.summary
                else:
                    geometries = {}
                    for item in binding.geometry_inputs:
                        with timed_phase("reopen_named_template", input_key=item.input_key):
                            shape = reopen_user_shape(item.payload)
                            validate_user_shape(shape)
                        geometries[item.input_key] = shape
                    self.function_geometries[binding.binding_key] = geometries
                    with timed_phase("compile_geometry", binding_key=binding.binding_key):
                        self.function_programs[binding.binding_key] = _compile_geometry(
                            binding.source_bundle, f"cad2bim_bound_geometry_{index:03d}", "build")


class _GeometryBindingLifecycle:
    """Prepare selected Geometry after preflight, reusing it across a batch."""

    def __init__(self, bindings) -> None:
        self._bindings = tuple(bindings)
        self._parameter_rules = None
        self._prepared = None
        self._loaded_keys: set[str] = set()
        self.interface_programs = {}
        self._local_results = OrderedDict()
        self._local_result_bytes = 0

    @property
    def parameter_rules(self):
        # Numeric alone normalizes author output. Instantiation consumes the
        # host-accepted precheck and does not load a second schema validator.
        if self._parameter_rules is None:
            self._parameter_rules = _prepare_geometry_binding_parameters(self._bindings)
        return self._parameter_rules

    @staticmethod
    def local_result_key(binding, evaluation):
        from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import _geometry_source_request_key
        # Aliases are Family names, not author-observable Geometry inputs.
        # Keep complete parameters here: only Host reuse can supply the rigid
        # source-to-fragment transform required for SYSTEM pose projection.
        return _geometry_source_request_key(binding, evaluation, exact=True)

    def local_result(self, key):
        result = self._local_results.get(key)
        if result is not None:
            self._local_results.move_to_end(key)
        return result

    def remember_local_result(self, key, payload, construction):
        construction_bytes = (construction.model_dump_json().encode("utf-8")
                              if construction is not None else None)
        size = len(key.encode("utf-8")) + len(payload) + len(construction_bytes or b"")
        if size > _LOCAL_RESULT_CACHE_BYTES or _LOCAL_RESULT_CACHE_ITEMS <= 0:
            return
        while self._local_results and (len(self._local_results) >= _LOCAL_RESULT_CACHE_ITEMS
                                      or self._local_result_bytes + size > _LOCAL_RESULT_CACHE_BYTES):
            _, (_, _, old_size) = self._local_results.popitem(last=False)
            self._local_result_bytes -= old_size
        # Only immutable bytes survive the invocation; no author handles/shapes.
        self._local_results[key] = (payload, construction_bytes, size)
        self._local_result_bytes += size

    def interfaces_program(self, binding):
        if binding.binding_key not in self.interface_programs:
            from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import compile_geometry_interfaces
            self.interface_programs[binding.binding_key] = compile_geometry_interfaces(binding.source_bundle,
                f"cad2bim_interfaces_{len(self.interface_programs):03d}")
        return self.interface_programs[binding.binding_key]

    def prepare(self, selected_binding_keys: set[str]) -> _PreparedGeometryBindings:
        if self._prepared is None:
            self._prepared = _PreparedGeometryBindings(
                (), parameter_rules=self._parameter_rules if self._parameter_rules is not None else {},
            )
        missing = selected_binding_keys - self._loaded_keys
        if missing:
            indexed_bindings = tuple(
                (index, binding)
                for index, binding in enumerate(self._bindings)
                if binding.binding_key in missing
            )
            self._prepared.load(indexed_bindings)
            self._loaded_keys.update(missing)
        return self._prepared


class _ProductRealizationContext:
    __slots__ = (
        "_request",
        "_bindings",
        "_receipts",
        "_function_programs",
        "_function_geometries",
        "_parameter_rules",
        "_mapped_shapes",
        "_mapped_summaries",
        "_instance_shapes",
        "_prepared",
        "_include_summary",
        "_defer_validation_to_codec",
        "_instance_payloads",
        "_instance_payload_bytes",
        "_local_source_fragments",
        "_instance_constructions",
        "_capture_construction",
        "_reused_fragments",
        "_solved_fragment_shapes",
    )

    def __init__(self, request: ProductRealizationRequest, *, include_instances: bool = False,
                 prepared: _GeometryBindingLifecycle | None = None, parameter_rules=None,
                 capture_construction: bool = False,
                 include_summary: bool = True,
                 defer_validation_to_codec: bool = False,
                 reused_fragments=None) -> None:
        self._request = request
        active_bindings = {
            option.binding_key
            for fragment in product_realization_structure(request).geometry_fragments
            for option in fragment.binding_options
        }
        self._bindings = {item.binding_key: item for item in request.geometry_bindings if item.binding_key in active_bindings}
        self._receipts: list[ProductGeometryInstanceReceipt] = []
        if isinstance(prepared, _PreparedGeometryBindings):
            lifecycle = _GeometryBindingLifecycle(tuple(self._bindings.values()))
            lifecycle._prepared = prepared
            lifecycle._parameter_rules = prepared.parameter_rules
            lifecycle._loaded_keys = (
                set(prepared.function_programs) | set(prepared.mapped_shapes)
            )
            self._prepared = lifecycle
        else:
            self._prepared = prepared or _GeometryBindingLifecycle(tuple(self._bindings.values()))
        self._function_programs = {}
        self._function_geometries = {}
        self._parameter_rules = parameter_rules
        self._mapped_shapes = {}
        self._mapped_summaries = {}
        self._instance_shapes: dict[str, Any] = {}
        self._include_summary = include_summary
        self._defer_validation_to_codec = defer_validation_to_codec
        self._instance_payloads: dict[str, bytes] | None = {} if include_instances else None
        self._instance_payload_bytes = 0
        # Index bytes already owned by this invocation; no additional native
        # shapes or cross-invocation lifetime. Unlike the small batch LRU,
        # every current-row source remains reachable until its output is sent.
        self._local_source_fragments: dict[str, str] = {}
        self._instance_constructions: dict[str, Any] = {}
        self._capture_construction = capture_construction
        self._reused_fragments = dict(reused_fragments or {})
        if self._reused_fragments and not (capture_construction and include_instances
                                          and not include_summary and defer_validation_to_codec):
            raise ValueError("reused sources require local-fragment instantiation")
        self._solved_fragment_shapes: tuple[Any, ...] | None = None

    @property
    def receipts(self) -> tuple[ProductGeometryInstanceReceipt, ...]:
        return tuple(self._receipts)

    @property
    def parameter_rules(self):
        return (self._parameter_rules if self._parameter_rules is not None
                else self._prepared.parameter_rules)

    def contract(self) -> dict[str, Any]:
        return self._request.family_contract.model_dump(mode="json", by_alias=True)

    def param(self, name: str) -> Any:
        from copy import deepcopy

        try:
            return deepcopy(self._request.invocation.public_parameters[name])
        except KeyError as exc:
            raise KeyError(f"parameter {name!r} was not declared") from exc

    def parameters(self) -> dict[str, Any]:
        from copy import deepcopy

        return deepcopy(self._request.invocation.public_parameters)

    def product_origin(self) -> tuple[float, float, float]:
        return tuple(self._request.invocation.product_origin)

    def external_constraints(self) -> tuple[dict[str, Any], ...]:
        """Return caller requirements; each call is detached from authoritative inputs."""
        return tuple(item.model_dump(mode="python") for item in self._request.invocation.external_constraints)

    def policy(self) -> dict[str, Any]:
        return self._request.policy.model_dump(mode="python")

    def evaluate_interfaces(self, proposal):
        from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_interfaces
        evaluations = []
        for fragment in proposal.fragments:
            binding = self._bindings[fragment.binding_key]
            if isinstance(binding, ResolvedMappedGeometryBinding):
                interfaces = binding.interfaces
            else:
                program = self._prepared.interfaces_program(binding)
                try:
                    interfaces = execute_geometry_interfaces(program, fragment.geometry_parameters)
                except BaseException as exc:
                    diagnostic = runtime_code_diagnostic(program.sources["interfaces.py"], "evaluate", exc,
                        virtual_sources=program.sources, virtual_filename_prefix="<cad2bim-bundle:")
                    diagnostic.update(binding_key=binding.binding_key, source_bundle_sha256=binding.source_bundle_sha256)
                    raise _ChildError("REALIZATION_EXECUTION_FAILED", str(exc), "interface_evaluation", diagnostic=diagnostic) from exc
            evaluations.append(ProductGeometryInterfaceEvaluation(
                fragment_key=fragment.fragment_key, binding_key=fragment.binding_key,
                normalized_parameters=fragment.geometry_parameters, uniform_scale=fragment.uniform_scale,
                interfaces=scale_geometry_interfaces(interfaces, fragment.uniform_scale)))
        return tuple(evaluations)

    def build_fragments(self, proposal: SolvedProductRealizationProposal, evaluations=()) -> None:
        if self._receipts or self._instance_shapes:
            raise ValueError("product fragments were already built")
        prepared = self._prepared.prepare({fragment.binding_key for fragment in proposal.fragments
                                           if fragment.fragment_key not in self._reused_fragments})
        self._function_programs = prepared.function_programs
        self._function_geometries = prepared.function_geometries
        self._mapped_shapes = prepared.mapped_shapes
        self._mapped_summaries = prepared.mapped_summaries
        for fragment in proposal.fragments:
            self._build_fragment(fragment, next(item for item in evaluations if item.fragment_key == fragment.fragment_key))

    def _build_fragment(self, fragment, evaluation) -> None:
        binding_key = fragment.binding_key
        binding = self._bindings[binding_key]
        scale = fragment.uniform_scale
        normalized = dict(fragment.geometry_parameters)
        reused = self._reused_fragments.get(fragment.fragment_key)
        if reused is not None:
            with timed_phase("geometry_reused_source_receipt", binding_key=binding_key):
                self._receipts.append(ProductGeometryInstanceReceipt.from_evaluation(
                    evaluation, geometry_sha256=reused.sha256, geometry_summary=reused.summary,
                ))
            return
        construction = None
        cache_key, cached, local_fragment = None, None, None
        # This exact option combination is the LOCAL_FRAGMENTS instantiation
        # path. Preview, summaries and local validation keep their current path.
        if (not isinstance(binding, ResolvedMappedGeometryBinding)
                and self._capture_construction and not self._include_summary
                and self._defer_validation_to_codec and self._instance_payloads is not None):
            cache_key = self._prepared.local_result_key(binding, evaluation)
            local_fragment = self._local_source_fragments.get(cache_key)
            cache_hit = local_fragment is not None or cache_key in self._prepared._local_results
            with timed_phase("geometry_local_result_cache_hit" if cache_hit else "geometry_local_result_cache_miss",
                             binding_key=binding_key, cache_hit=cache_hit):
                if local_fragment is None:
                    cached = self._prepared.local_result(cache_key)
        source_hit = local_fragment is not None or cached is not None
        if isinstance(binding, ResolvedMappedGeometryBinding):
            if normalized:
                raise ValueError("mapped geometry does not accept build parameters")
            shape = self._mapped_shapes[binding_key].copy(mesh=False)
            summary = self._mapped_summaries[binding_key] if self._include_summary else None
            payload = binding.geometry.payload
        elif local_fragment is not None:
            payload = self._instance_payloads[local_fragment]
            original_construction = self._instance_constructions.get(local_fragment)
            construction = (original_construction.model_copy(deep=True)
                            if original_construction is not None else None)
            shape, summary = None, None
        elif cached is not None:
            from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction
            payload, construction_bytes, _ = cached
            with timed_phase("construction_cache_restore"):
                construction = (GeometryConstruction.model_validate_json(construction_bytes)
                                if construction_bytes is not None else None)
            shape, summary = None, None
        else:
            summary = None
            program = self._function_programs[binding_key]
            capture = self._capture_construction and binding.ifc_representation_route.value == "parametric"
            try:
                from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
                built = execute_geometry_build(program, normalized, self._function_geometries[binding_key],
                                               allow_interfaces=False,
                                               **({"capture_construction": True} if capture else {}))
                authored, _ = built[:2]
                construction = built[2] if capture else None
            except ModelingCapabilityError:
                raise
            except BaseException as exc:
                diagnostic = runtime_code_diagnostic(
                    program.sources["main.py"], "build", exc,
                    virtual_sources=program.sources,
                    virtual_filename_prefix="<cad2bim-bundle:",
                )
                diagnostic.update(
                    binding_key=binding_key,
                    source_bundle_sha256=binding.source_bundle_sha256,
                )
                raise _ChildError(
                    "REALIZATION_EXECUTION_FAILED", str(exc), "execution", diagnostic=diagnostic,
                ) from exc
            shape = authored
        if isinstance(binding, ResolvedMappedGeometryBinding) and binding.ifc_representation_route.value == "parametric":
            raise ValueError("mapped geometry has no parametric construction")
        if not source_hit and scale != 1.0:
            shape = _scale_shape(shape, scale)
            summary = None
            if not isinstance(binding, ResolvedMappedGeometryBinding) and construction is not None:
                construction = _append_observed_scale(construction, shape, scale)
        if not source_hit and (not isinstance(binding, ResolvedMappedGeometryBinding) or scale != 1.0):
            if self._defer_validation_to_codec:
                with timed_phase("local_brep_serialize", binding_key=binding_key):
                    payload = serialize_user_shape(shape)
                    if self._include_summary:
                        shape = reopen_user_shape(payload)
            else:
                # Local realization has no downstream Codec; retain actual-byte proof.
                with timed_phase("local_brep_save_validate", binding_key=binding_key):
                    shape, payload = save_user_shape(shape)
        if self._include_summary and summary is None:
            with timed_phase("local_brep_summary", binding_key=binding_key, requested_by="preview"):
                summary = summarize_shape(shape)
        fragment_key = fragment.fragment_key
        if not isinstance(binding, ResolvedMappedGeometryBinding) and construction is not None:
            self._instance_constructions[fragment_key] = construction
        with timed_phase("receipt_identity_dto", binding_key=binding_key):
            self._receipts.append(
                ProductGeometryInstanceReceipt.from_evaluation(
                    evaluation, geometry_sha256=hashlib.sha256(payload).hexdigest(),
                    geometry_summary=summary,
                )
            )
        if shape is not None:
            self._instance_shapes[fragment_key] = shape
        if self._instance_payloads is not None:
            self._instance_payload_bytes += len(payload)
            if self._instance_payload_bytes > MAX_PRODUCT_REALIZATION_INSTANCE_GEOMETRY_BYTES:
                raise ValueError("realization instance geometry exceeds its aggregate byte budget")
            self._instance_payloads[fragment_key] = payload
        if cache_key is not None:
            self._local_source_fragments.setdefault(cache_key, fragment_key)
            if not source_hit:
                with timed_phase("local_result_cache_store"):
                    self._prepared.remember_local_result(cache_key, payload, construction)

    def place_finalized_geometry(
        self,
        result: ProductRealizationResult,
        *, materialize_world: bool = True,
    ) -> None:
        """Place exact instance shapes using the independently validated current transform chain."""

        proposal = result.proposal
        assert isinstance(proposal, SolvedProductRealizationProposal)
        contract = product_realization_structure(self._request)
        component_world = {
            item.component_key: item.project_world_transform
            for item in result.component_project_world_transforms
        }

        receipt_by_fragment = {item.fragment_key: item for item in self._receipts}
        solved_fragment_shapes: list[Any] = []
        contract_fragments = {item.fragment_key: item for item in contract.geometry_fragments}
        for fragment in proposal.fragments:
            definition = contract_fragments[fragment.fragment_key]
            receipt = receipt_by_fragment.get(fragment.fragment_key)
            has_local_payload = (not materialize_world and self._instance_payloads is not None
                                 and fragment.fragment_key in self._instance_payloads)
            has_reused_source = not materialize_world and fragment.fragment_key in self._reused_fragments
            if receipt is None or (fragment.fragment_key not in self._instance_shapes
                                   and not has_local_payload and not has_reused_source):
                raise ValueError("realization fragment has no actual geometry result")
            if receipt.binding_key != fragment.binding_key:
                raise ValueError("realization fragment references the wrong geometry binding")
            world = compose_rigid_transform(
                component_world[definition.owner_component_key],
                fragment.local_transform,
            )
            if materialize_world:
                transformed = _transform_shape(
                    self._instance_shapes[fragment.fragment_key], world,
                )
                solved_fragment_shapes.append(transformed)

        self._solved_fragment_shapes = tuple(solved_fragment_shapes)

    def compose_preview_geometry(self) -> tuple[bytes, None]:
        """Serialize one world compound; Codec validates bytes and measures on demand."""

        if not self._solved_fragment_shapes:
            raise ValueError("product realization preview has no validated geometry")
        import cadquery as cq

        with timed_phase("world_compose"):
            compound = cq.Compound.makeCompound(list(self._solved_fragment_shapes))
        with timed_phase("world_brep_serialize"):
            payload = serialize_user_shape(compound)
        return payload, None


def _transform_shape(shape: Any, transform: RigidTransform3D) -> Any:
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import transform_occt_shape

    return transform_occt_shape(shape, transform)


def _scale_shape(shape: Any, uniform_scale: float) -> Any:
    import cadquery as cq
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf

    native = gp_Trsf()
    native.SetScale(gp_Pnt(0.0, 0.0, 0.0), uniform_scale)
    operation = BRepBuilderAPI_Transform(shape.wrapped, native, True)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("realization geometry uniform scale failed")
    return cq.Shape(operation.Shape())


def _append_observed_scale(construction, shape, scale: float):
    """Include the actual Backend scale performed after author construction."""
    import base64
    from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction, GeometryConstructionNode

    node_id = max(node.id for node in construction.nodes) + 1
    node = GeometryConstructionNode(
        id=node_id, op="scale", receiver=construction.root, args=(scale,),
        kwargs={}, children=(construction.root,),
        shape_brep=base64.b64encode(serialize_user_shape(shape)).decode("ascii"),
    )
    return GeometryConstruction(root=node_id, nodes=(*construction.nodes, node))


def _compile_family(content: bytes, package_name: str, entry_name: str, *, required_entry_names=None) -> CompiledPythonBundle:
    return compile_python_bundle(
        content,
        package_name=package_name,
        entry_name=entry_name,
        required_entry_names=required_entry_names,
        allowed_import_roots=_ALLOWED_IMPORTS,
        profile_label="Product realization",
        maximum_members=MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
        maximum_member_bytes=MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
        maximum_total_bytes=MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES,
        reject_private_attributes=True,
        controlled_modeling=True,
        controlled_import_roots={"math"},
    )


def _compile_geometry(content: bytes, package_name: str, entry_name: str) -> CompiledPythonBundle:
    return compile_python_bundle(
        content,
        package_name=package_name,
        entry_name=entry_name,
        allowed_import_roots={"math", "cadquery"},
        profile_label="Product Geometry",
        maximum_members=MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
        maximum_member_bytes=MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
        maximum_total_bytes=MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES,
        reject_private_attributes=True,
        controlled_modeling=True,
    )


class _ChildError(RuntimeError):
    def __init__(self, code: str, message: str, phase: str, *, diagnostic: dict[str, Any] | None = None) -> None:
        # Reuse the source diagnostic's existing byte budget and truncation
        # markers; transport the bounded object independently of human text.
        self.diagnostic = None if diagnostic is None else json.loads(diagnostic_json(diagnostic))
        if self.diagnostic is not None and isinstance(self.diagnostic.get("message"), str):
            message = self.diagnostic["message"]
        super().__init__(message)
        self.code = code
        self.phase = phase


def _failure_payload(failure: _ChildError) -> dict[str, Any]:
    return {
        "status": "failed",
        "code": failure.code,
        "phase": failure.phase,
        "message": _bounded(str(failure), 2_000),
        **({"diagnostic": failure.diagnostic} if failure.diagnostic is not None else {}),
    }


def _write_failure(stream: BinaryIO, failure: _ChildError, *, started: bool) -> None:
    _write_json(
        stream,
        {
            **_failure_payload(failure),
            "execution_started": started,
        },
    )


def _write_json(stream: BinaryIO, value: dict[str, Any]) -> None:
    try:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        stream.flush()
    except OSError:
        pass


def _descriptor(name: str) -> int:
    try:
        value = int(os.environ.pop(name))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("product realization worker descriptor contract is invalid") from exc
    if value < 3:
        raise RuntimeError("product realization worker descriptor contract is invalid")
    return value




def _bounded(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    return encoded[:limit].decode("utf-8", errors="ignore")


def _output_validation_message(exc: Exception) -> str:
    """Keep authoring diagnostics actionable without echoing values or Pydantic internals."""

    if not isinstance(exc, ValidationError):
        return _bounded(str(exc), 2_000)
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    rendered: list[str] = []
    for error in errors[:12]:
        location = ".".join(_validation_location_part(item) for item in error.get("loc", ()))
        error_type = _validation_location_part(error.get("type", "validation_error"))
        rendered.append(f"{location or '<root>'}: {error_type}")
    suffix = "; <truncated>" if len(errors) > len(rendered) else ""
    return _bounded(
        f"{len(errors)} product realization output validation error(s); "
        + "; ".join(rendered)
        + suffix,
        2_000,
    )


def _validation_location_part(value: object) -> str:
    if isinstance(value, int):
        return str(value) if 0 <= value <= 999_999_999 else "<item>"
    if not isinstance(value, str) or not value or len(value) > 64:
        return "<field>"
    if not all(
        character == "_" or character.isascii() and character.isalnum() for character in value
    ):
        return "<field>"
    return value


if __name__ == "__main__":
    raise SystemExit(main())
