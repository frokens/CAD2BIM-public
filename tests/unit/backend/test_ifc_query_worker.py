from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import ifcopenshell
import pyarrow as pa
import pytest

from CAD2BIM_backend.infrastructure.ifc_query import IfcOpenShellQueryWorker
from CAD2BIM_backend.infrastructure.ifc_query.adapter import IFC_QUERY_BUDGET, IFC_TRANSFORM_BUDGET
from CAD2BIM_backend.infrastructure.worker_runtime import (
    WorkerLaunchSpec,
    WorkerProcessBudget,
    WorkerSupervisorError,
)
from CAD2BIM_backend.ports.data_transform import ArrowTransformInput
from CAD2BIM_backend.ports.ifc_query_worker import (
    IfcQueryWorkerError,
    IfcQueryWorkerRequest,
)
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerError, PythonArrowWorkerPolicy, PythonArrowWorkerRequest, PythonArrowWorkerOutputDeclaration,
)


def _ifc_bytes() -> bytes:
    model = ifcopenshell.file(schema="IFC4X3")
    model.create_entity(
        "IfcProject",
        GlobalId="0fA5xY2rjB9eNH2dFsKQv8",
        Name="Exact source project",
    )
    return model.to_string().encode("utf-8")


def test_serialized_digest_preserves_utf8_across_unicode_chunk_boundaries():
    from types import SimpleNamespace
    from CAD2BIM_backend.infrastructure.ifc_query.readonly_model import _serialized_digest

    text = "a" * (1024 * 1024 - 1) + "中😀é" + "b" * (1024 * 1024 - 2) + "𐐷尾"
    model = SimpleNamespace(to_string=lambda: text)
    assert _serialized_digest(model) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert _serialized_digest(SimpleNamespace(to_string=lambda: "")) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("value,path,kind,reason", (
    ({"units": [{"Units": (1, 2)}]}, '$["units"][0]["Units"]', "tuple", "unsupported_type"),
    ({"bad": object()}, '$["bad"]', "object", "unsupported_type"),
    ({"number": float("nan")}, '$["number"]', "float", "non_finite_number"),
    ({"number": float("inf")}, '$["number"]', "float", "non_finite_number"),
    ({"nested": {3: "private-value"}}, '$["nested"]', "int", "non_string_object_key"),
))
def test_result_validation_reports_path_type_reason_without_values(value, path, kind, reason):
    from CAD2BIM_backend.infrastructure.ifc_query.child import _validate_json_value
    with pytest.raises(ValueError) as caught:
        _validate_json_value(value)
    assert caught.value.diagnostic == {"json_path": path, "value_type": kind, "reason": reason}
    assert "private-value" not in str(caught.value.diagnostic)


def test_result_validation_diagnostic_is_bounded_at_depth_limit():
    from CAD2BIM_backend.infrastructure.ifc_query.child import _validate_json_value
    value = None
    for _ in range(66):
        value = {"中" * 1000: value}
    with pytest.raises(ValueError) as caught:
        _validate_json_value(value)
    assert caught.value.diagnostic["reason"] == "maximum_depth_exceeded"
    assert len(caught.value.diagnostic["json_path"].encode("utf-8")) <= 384


def _worker(tmp_path: Path) -> IfcOpenShellQueryWorker:
    return IfcOpenShellQueryWorker(
        python_executable=os.environ.get(
            "CAD2BIM_TEST_IFC_QUERY_PYTHON_EXECUTABLE",
            sys.executable,
        ),
        capacity_root=tmp_path / "capacity",
    )


def _request(
    code: str,
    *,
    ifc_bytes: bytes | None = None,
    max_result_bytes: int = 256 * 1024,
) -> IfcQueryWorkerRequest:
    payload = ifc_bytes or _ifc_bytes()
    return IfcQueryWorkerRequest(
        code=code,
        ifc_bytes=payload,
        expected_ifc_sha256=hashlib.sha256(payload).hexdigest(),
        max_result_bytes=max_result_bytes,
    )


def _arrow_input(name: str = "items") -> ArrowTransformInput:
    return ArrowTransformInput(name, pa.table({"value": [2, 3]}).to_reader())


@pytest.mark.asyncio
async def test_ifc_derive_reuses_data_context_for_ifc_only_and_two_inputs(tmp_path):
    payload = _ifc_bytes()
    worker = _worker(tmp_path)
    try:
        for inputs in ((), (_arrow_input(), _json_input())):
            code = '''import pyarrow as pa
import numpy as np
def transform(ctx):
    names = set()
    names.add(ctx.model().by_type("IfcProject")[0].Name)
    values = [1, 2]
    values.remove(1)
    ctx.write_table("projects", pa.table({"name": sorted(names), "n": [int(np.sum(values))]}))
    ctx.write_json("summary", {"projects": len(names)})
'''
            if inputs:
                code = code.replace('len(names)', 'len(names) + sum(ctx.table("items").column("value").to_pylist()) + ctx.json("settings")["factor"]')
            outputs = await worker.execute_transform(PythonArrowWorkerRequest(code=code, outputs=(
                PythonArrowWorkerOutputDeclaration("projects", "parquet"),
                PythonArrowWorkerOutputDeclaration("summary", "json", "document"),
            )), inputs, ifc_bytes=payload, expected_ifc_sha256=hashlib.sha256(payload).hexdigest())
            assert pa.Table.from_batches(outputs[0].batches, schema=outputs[0].schema).to_pylist() == [{"name": "Exact source project", "n": 2}]
            document = pa.Table.from_batches(outputs[1].batches, schema=outputs[1].schema).column("json_value")[0].as_py()
            assert json.loads(document) == {"projects": 10 if inputs else 1}
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("derive", (False, True))
@pytest.mark.parametrize("attack", ("create", "restore_and_clear", "delete", "transaction", "native_save", "ordinary_save"))
async def test_shared_ifc_guard_discards_all_outputs_on_dynamic_attack(tmp_path, derive, attack):
    statements = {
        "create": 'ifcopenshell.api.run("root.create_entity", ctx.model(), ifc_class="IfcBuilding")',
        "restore_and_clear": 'p = ctx.model().by_type("IfcProject")[0]\n    p[2] = "changed"\n    p[2] = "Exact source project"\n    operator.attrgetter("transaction.operations")(ctx.model()).clear()',
        "delete": 'operator.attrgetter("remove")(ctx.model())(ctx.model().by_type("IfcProject")[0])',
        "transaction": 'operator.attrgetter("discard_transaction")(ctx.model())()',
        "native_save": 'operator.attrgetter("OSFile")(pa)("native-save.arrow", "wb")',
        "ordinary_save": 'operator.attrgetter("write")(ctx.model())("ordinary-save.ifc")',
    }
    entry = "transform" if derive else "query"
    source = f'import operator\nimport pyarrow as pa\nimport ifcopenshell.api\ndef {entry}(ctx):\n'
    if derive:
        source += '    ctx.write_json("result", {"candidate": True})\n'
    source += f'    {statements[attack]}\n    return {{}}\n'
    worker = _worker(tmp_path)
    try:
        with pytest.raises(DataTransformWorkerError if derive else IfcQueryWorkerError) as caught:
            if derive:
                payload = _ifc_bytes()
                await worker.execute_transform(PythonArrowWorkerRequest(code=source, outputs=(
                    PythonArrowWorkerOutputDeclaration("result", "json", "document"),
                )), ifc_bytes=payload, expected_ifc_sha256=hashlib.sha256(payload).hexdigest())
            else:
                await worker.execute_query(_request(source))
        if attack == "native_save":
            assert caught.value.code == "IFC_QUERY_EXECUTION_FAILED"
            assert caught.value.diagnostic["exception_type"] == "PermissionError"
        else:
            assert caught.value.code == "IFC_QUERY_MUTATION_DETECTED"
        assert caught.value.execution_started
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_query_local_add_remove_remain_normal_computation(tmp_path):
    worker = _worker(tmp_path)
    try:
        result = await worker.execute_query(_request('def query(ctx):\n    s = set()\n    s.add(ctx.model().by_type("IfcProject")[0].Name)\n    values = [1, 2]\n    values.remove(1)\n    return {"names": sorted(s), "values": values}\n'))
        assert result.result == {"names": ["Exact source project"], "values": [2]}
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_ifc_derive_approved_library_imports_use_project_environment(tmp_path):
    from CAD2BIM_backend.infrastructure.data_transform.capabilities import PYTHON_ARROW_APPROVED_LIBRARIES
    imports = "\n".join(f"import {name}" for name in PYTHON_ARROW_APPROVED_LIBRARIES)
    source = imports + '\nimport ifcopenshell\ndef transform(ctx):\n    ctx.write_json("result", {"projects": len(ctx.model().by_type("IfcProject"))})\n'
    payload = _ifc_bytes()
    worker = _worker(tmp_path)
    try:
        outputs = await worker.execute_transform(PythonArrowWorkerRequest(code=source, outputs=(
            PythonArrowWorkerOutputDeclaration("result", "json", "document"),
        )), ifc_bytes=payload, expected_ifc_sha256=hashlib.sha256(payload).hexdigest())
        assert outputs[0].receipt.row_count == 1
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_ifc_derive_applies_shared_data_output_budget(tmp_path):
    from dataclasses import replace
    payload = _ifc_bytes()
    worker = _worker(tmp_path)
    worker._transform_policy = replace(worker._transform_policy, output_row_limit=1)
    try:
        with pytest.raises(DataTransformWorkerError) as caught:
            await worker.execute_transform(PythonArrowWorkerRequest(
                code='import pyarrow as pa\ndef transform(ctx):\n    ctx.write_table("result", pa.table({"n": [1,2]}))\n',
                outputs=(PythonArrowWorkerOutputDeclaration("result", "parquet"),),
            ), ifc_bytes=payload, expected_ifc_sha256=hashlib.sha256(payload).hexdigest())
        assert caught.value.code == "TRANSFORM_OUTPUT_BUDGET_EXCEEDED"
        assert caught.value.phase == "output_validation"
        assert caught.value.execution_started
    finally:
        await worker.close()


def test_transform_worker_error_copies_explicit_diagnostic_without_changing_failure():
    diagnostic = {"exception_type": "RuntimeError", "line": 3, "message": "missingGUID"}
    error = DataTransformWorkerError(
        "IFC_QUERY_EXECUTION_FAILED", "IFC query execution failed", phase="execution",
        execution_started=True, retryable=False, diagnostic=diagnostic,
    )
    diagnostic["line"] = 99
    assert error.diagnostic == {"exception_type": "RuntimeError", "line": 3, "message": "missingGUID"}
    assert (error.code, error.phase, error.execution_started, error.retryable) == (
        "IFC_QUERY_EXECUTION_FAILED", "execution", True, False,
    )
    assert str(error) == "IFC query execution failed"


@pytest.mark.asyncio
async def test_ifc_derive_preserves_real_python_failure_location_and_message(tmp_path):
    payload = _ifc_bytes()
    worker = _worker(tmp_path)
    source = 'def transform(ctx):\n    ctx.write_json("result", {"candidate": True})\n    raise RuntimeError("missingGUID")\n'
    try:
        with pytest.raises(DataTransformWorkerError) as caught:
            await worker.execute_transform(PythonArrowWorkerRequest(code=source, outputs=(
                PythonArrowWorkerOutputDeclaration("result", "json", "document"),
            )), ifc_bytes=payload, expected_ifc_sha256=hashlib.sha256(payload).hexdigest())
        error = caught.value
        assert (error.code, error.phase, error.execution_started, error.retryable) == (
            "IFC_QUERY_EXECUTION_FAILED", "execution", True, False,
        )
        assert error.diagnostic["exception_type"] == "RuntimeError"
        assert error.diagnostic["message"] == "missingGUID"
        assert error.diagnostic["function"] == "transform"
        assert error.diagnostic["line"] == 3
        assert error.diagnostic["source_line"].strip() == 'raise RuntimeError("missingGUID")'
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_ifc_derive_author_compilation_waits_for_host_baseline(tmp_path):
    from CAD2BIM_backend.infrastructure.worker_runtime import (
        MAX_CONTROL_BYTES, WorkerProcessSupervisor, read_json_frame,
    )

    class Supervisor(WorkerProcessSupervisor):
        accepted = None

        async def run(self, spec, *, initialize, **kwargs):
            assert kwargs["budget"].rss_bytes == 6 * 1024**3
            with (spec.scratch / "control.bin").open("rb") as control_stream:
                payload = read_json_frame(control_stream, limit=MAX_CONTROL_BYTES)
            assert payload["policy"]["rss_bytes"] == kwargs["budget"].rss_bytes

            async def record(control):
                await initialize(control)
                self.accepted = control.thread_baseline
                with pytest.raises(RuntimeError):
                    control.accept_thread_baseline()
            return await super().run(spec, initialize=record, **kwargs)

    supervisor = Supervisor()
    worker = IfcOpenShellQueryWorker(python_executable=sys.executable, supervisor=supervisor,
                                   capacity_root=tmp_path / "capacity")
    payload = _ifc_bytes()
    try:
        with pytest.raises(DataTransformWorkerError) as caught:
            await worker.execute_transform(PythonArrowWorkerRequest(code="def transform(:\n", outputs=(
                PythonArrowWorkerOutputDeclaration("result", "json", "document"),
            )), ifc_bytes=payload, expected_ifc_sha256=hashlib.sha256(payload).hexdigest())
        assert caught.value.phase == "code_validation"
        assert not caught.value.execution_started
        assert supervisor.accepted is not None
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("top_level", (False, True))
async def test_ifc_derive_author_native_threads_cannot_become_baseline(tmp_path, top_level):
    payload = _ifc_bytes()
    worker = _worker(tmp_path)
    assignment = "connections = [duckdb.connect() for i in range(8)]"
    source = (f"import duckdb\n{assignment}\ndef transform(ctx):\n    pass\n" if top_level else
              f"import duckdb\ndef transform(ctx):\n    {assignment}\n    while True:\n        pass\n")
    try:
        with pytest.raises(DataTransformWorkerError) as caught:
            await worker.execute_transform(PythonArrowWorkerRequest(code=source, outputs=(
                PythonArrowWorkerOutputDeclaration("result", "json", "document"),
            )), ifc_bytes=payload, expected_ifc_sha256=hashlib.sha256(payload).hexdigest())
        if top_level:
            assert caught.value.code == "IFC_QUERY_SOURCE_INVALID"
            assert caught.value.phase == "code_validation"
            assert not caught.value.execution_started
        else:
            assert caught.value.code == "IFC_QUERY_RESOURCE_LIMIT"
            assert caught.value.diagnostic["resource_kind"] == "threads"
            assert caught.value.execution_started
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_host_sections_real_worker_returns_reusable_exact_references_without_mutation(tmp_path):
    from tests.unit.backend.test_product_realization_host_resolver import _sectioned_model, _extract, PIPE_GLOBAL_ID
    from CAD2BIM_backend.domain.product_realization_host import CurrentIfcProductBoundarySelector

    payload = _sectioned_model([(0.2, 0.8), (1.2, 1.8)], millimetres=True, reverse=True).to_string().encode()
    worker = _worker(tmp_path)
    try:
        result = await worker.execute_query(_request(
            f'def query(ctx):\n    return ctx.host_sections("{PIPE_GLOBAL_ID}")\n', ifc_bytes=payload,
        ))
    finally:
        await worker.close()
    body = result.result
    assert body["ifc_sha256"] == hashlib.sha256(payload).hexdigest()
    assert [row["station_interval_m"] for row in body["sections"]] == [[0.2, 0.8], [1.2, 1.8]]
    reference = body["sections"][1]["body_section_ref"]
    fact, = _extract(payload, CurrentIfcProductBoundarySelector(
        product_global_id=PIPE_GLOBAL_ID, boundary="axis_start", attachment_key="pipe", interface_key="outlet",
        body_section_ref=reference,
    ))
    assert fact.attachment.project_world_frame.translation_m == (10.0, 21.2, 30.0)
    assert not fact.body_reference_remapped


@pytest.mark.asyncio
async def test_host_sections_reports_stable_geometry_diagnostic_in_real_worker(tmp_path):
    from tests.unit.backend.test_product_realization_host_resolver import _model, PIPE_GLOBAL_ID

    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as caught:
            await worker.execute_query(_request(
                f'def query(ctx):\n    return ctx.host_sections("{PIPE_GLOBAL_ID}")\n', ifc_bytes=_model(include_body=False),
            ))
    finally:
        await worker.close()
    assert caught.value.execution_started
    assert "PRODUCT_REALIZATION_HOST_PROFILE_MISSING" in json.dumps(caught.value.diagnostic)


def _json_input(name: str = "settings") -> ArrowTransformInput:
    document = {"factor": 4, "labels": ["a", "b"]}
    return ArrowTransformInput(
        name,
        pa.table({"json_value": [json.dumps(document)]}).to_reader(),
        input_format="json",
        json_kind="document",
        row_count=1,
    )


class _UnavailableSupervisor:
    def __init__(self) -> None:
        self.staged_paths_absent = False

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
    ) -> None:
        del budget, deadline
        scratch = spec.scratch
        self.staged_paths_absent = (
            scratch is not None
            and not (scratch / "source.ifc").exists()
            and not tuple(scratch.glob("input-*.arrow"))
        )
        raise WorkerSupervisorError(
            "WORKER_SERVICE_UNAVAILABLE",
            "readiness supervisor unavailable",
            execution_started=False,
        )


def test_request_rejects_non_exact_digest_and_non_utf8_budget() -> None:
    payload = _ifc_bytes()
    with pytest.raises(ValueError, match="does not match"):
        IfcQueryWorkerRequest(
            code="def query(ctx):\n    return {}\n",
            ifc_bytes=payload,
            expected_ifc_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="65536 UTF-8 bytes"):
        IfcQueryWorkerRequest(
            code="界" * 21_846,
            ifc_bytes=payload,
            expected_ifc_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_worker_budget_retains_representative_large_ifc_characterization_margin() -> None:
    assert IFC_QUERY_BUDGET.wall_seconds == 45.0
    assert IFC_QUERY_BUDGET.cpu_seconds == 45.0
    assert IFC_QUERY_BUDGET.rss_bytes == 5 * 1024**3
    assert IFC_QUERY_BUDGET.scratch_bytes == 128 * 1024**2
    assert IFC_TRANSFORM_BUDGET.rss_bytes == 6 * 1024**3
    assert PythonArrowWorkerPolicy().rss_bytes == 4 * 1024**3


@pytest.mark.asyncio
async def test_worker_rejects_more_than_sixteen_exact_inputs(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    inputs = tuple(_arrow_input(f"item_{index}") for index in range(17))
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(_request("def query(ctx):\n    return {}\n"), inputs)
    finally:
        await worker.close()

    assert rejected.value.code == "IFC_QUERY_INPUT_BUDGET_EXCEEDED"
    assert rejected.value.phase == "input_validation"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_verify_ready_exercises_real_child_and_closed_worker_fails_closed(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)
    await worker.verify_ready()
    await worker.close()

    with pytest.raises(IfcQueryWorkerError) as closed:
        await worker.verify_ready()
    assert closed.value.code == "IFC_QUERY_SERVICE_UNAVAILABLE"
    assert closed.value.phase == "runtime"
    assert closed.value.execution_started is False
    assert closed.value.retryable is True


@pytest.mark.asyncio
async def test_verify_ready_maps_failed_child_launch_to_stable_unavailable(
    tmp_path: Path,
) -> None:
    supervisor = _UnavailableSupervisor()
    worker = IfcOpenShellQueryWorker(
        python_executable=os.environ.get(
            "CAD2BIM_TEST_IFC_QUERY_PYTHON_EXECUTABLE",
            sys.executable,
        ),
        capacity_root=tmp_path / "capacity",
        supervisor=supervisor,  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(IfcQueryWorkerError) as failed:
            await worker.verify_ready()
    finally:
        await worker.close()

    assert failed.value.code == "IFC_QUERY_SERVICE_UNAVAILABLE"
    assert failed.value.phase == "runtime"
    assert failed.value.execution_started is False
    assert failed.value.retryable is True
    assert supervisor.staged_paths_absent is True


@pytest.mark.asyncio
async def test_query_exposes_only_read_context_and_returns_canonical_json(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    request = _request("def query(ctx):\n    return {}\n")
    source = """import pyarrow as pa

def query(ctx):
    model = ctx.model()
    table = ctx.table("items")
    batches = ctx.batches("items").read_all()
    settings = ctx.json("settings")
    ctx.log("inspected exact private inputs")
    return {
        "project_names": [item.Name for item in model.by_type("IfcProject")],
        "sum": sum(table.column("value").to_pylist()),
        "batch_rows": batches.num_rows,
        "factor": settings["factor"],
        "arrow_type": str(pa.int64()),
    }
"""
    try:
        result = await worker.execute_query(
            IfcQueryWorkerRequest(
                code=source,
                ifc_bytes=request.ifc_bytes,
                expected_ifc_sha256=request.expected_ifc_sha256,
            ),
            (_arrow_input(), _json_input()),
        )
    finally:
        await worker.close()

    assert result.result == {
        "arrow_type": "int64",
        "batch_rows": 2,
        "factor": 4,
        "project_names": ["Exact source project"],
        "sum": 5,
    }
    assert result.result_json == (
        '{"arrow_type":"int64","batch_rows":2,"factor":4,'
        '"project_names":["Exact source project"],"sum":5}'
    )
    assert result.result_bytes == len(result.result_json.encode("utf-8"))
    assert result.logs == ("inspected exact private inputs",)
    assert result.ifc_sha256 == request.expected_ifc_sha256


@pytest.mark.asyncio
async def test_ifcopenshell_transaction_log_detects_and_rolls_back_dynamic_mutation(
    tmp_path: Path,
) -> None:
    payload = _ifc_bytes()
    worker = _worker(tmp_path)
    source = """import ifcopenshell.api

def query(ctx):
    ifcopenshell.api.run(
        "root.create_entity",
        ctx.model(),
        ifc_class="IfcBuilding",
        name="must be rolled back",
    )
    return {"buildings": len(ctx.model().by_type("IfcBuilding"))}
"""
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(_request(source, ifc_bytes=payload))
    finally:
        await worker.close()

    assert rejected.value.code == "IFC_QUERY_MUTATION_DETECTED"
    assert rejected.value.phase == "execution"
    assert rejected.value.execution_started is True
    assert "rolled back" in str(rejected.value)
    assert (
        hashlib.sha256(payload).hexdigest()
        == _request(
            "def query(ctx):\n    return {}\n",
            ifc_bytes=payload,
        ).expected_ifc_sha256
    )
    reopened = ifcopenshell.file.from_string(payload.decode("utf-8"))
    assert reopened.by_type("IfcBuilding") == []


@pytest.mark.asyncio
async def test_transaction_log_rejects_mutation_even_when_final_digest_is_restored(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)
    source = """import ifcopenshell.api

def query(ctx):
    project = ctx.model().by_type("IfcProject")[0]
    ifcopenshell.api.run("attribute.edit_attributes", ctx.model(), product=project, attributes={"Name": "temporary"})
    ifcopenshell.api.run("attribute.edit_attributes", ctx.model(), product=project, attributes={"Name": "Exact source project"})
    return {"project_name": project.Name}
"""
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(_request(source))
    finally:
        await worker.close()

    assert rejected.value.code == "IFC_QUERY_MUTATION_DETECTED"
    assert rejected.value.phase == "execution"
    assert rejected.value.execution_started is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    (
        'ctx.model().by_type("IfcProject")[0].Name = "changed"',
        'del ctx.model().by_type("IfcProject")[0].Name',
    ),
)
async def test_ast_rejects_attribute_store_or_delete(
    tmp_path: Path,
    statement: str,
) -> None:
    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(
                _request(f"def query(ctx):\n    {statement}\n    return {{}}\n")
            )
    finally:
        await worker.close()

    assert rejected.value.code == "IFC_QUERY_MUTATION_FORBIDDEN"
    assert rejected.value.phase == "code_validation"
    assert rejected.value.execution_started is False
    assert rejected.value.diagnostic is not None
    assert rejected.value.diagnostic["line"] == 2
    assert rejected.value.diagnostic["column"] == (9 if statement.startswith("del ") else 5)
    assert rejected.value.diagnostic["end_line"] == 2
    assert rejected.value.diagnostic["end_column"] > rejected.value.diagnostic["column"]
    assert rejected.value.diagnostic["ast_node"] == "Attribute"
    assert rejected.value.diagnostic["operation"] == (
        "delete" if statement.startswith("del ") else "assign"
    )


@pytest.mark.asyncio
async def test_local_dict_and_list_index_edits_leave_exact_ifc_unchanged(tmp_path: Path) -> None:
    payload = _ifc_bytes()
    worker = _worker(tmp_path)
    source = """def query(ctx):
    counts = {}
    names = ["discard"]
    for entity in ctx.model().by_type("IfcProject"):
        kind = entity.is_a()
        counts[kind] = counts.get(kind, 0)
        counts[kind] += 1
        names.append(entity.Name)
    counts["discard"] = 0
    del counts["discard"]
    names[0] = "changed"
    del names[0]
    names[:] = sorted(names)
    return {"counts": counts, "names": names}
"""
    try:
        result = await worker.execute_query(_request(source, ifc_bytes=payload))
    finally:
        await worker.close()
    assert result.result == {"counts": {"IfcProject": 1}, "names": ["Exact source project"]}
    assert result.ifc_sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", (False, True))
@pytest.mark.parametrize("assignment", ("index", "operator"))
async def test_ifc_index_mutation_is_rejected_even_after_restore(
    tmp_path: Path, restore: bool, assignment: str
) -> None:
    worker = _worker(tmp_path)
    source = """import operator

def query(ctx):
    project = ctx.model().by_type("IfcProject")[0]
    original = project[2]
"""
    source += (
        '    project[2] = "changed"\n'
        if assignment == "index"
        else '    operator.setitem(project, 2, "changed")\n'
    )
    if restore:
        source += (
            "    project[2] = original\n"
            if assignment == "index"
            else "    operator.setitem(project, 2, original)\n"
        )
    source += "    return {}\n"
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(_request(source))
    finally:
        await worker.close()
    assert rejected.value.code == "IFC_QUERY_MUTATION_DETECTED"
    assert rejected.value.phase == "execution"
    assert rejected.value.execution_started is True


@pytest.mark.asyncio
async def test_unsupported_ifc_index_deletion_fails_during_execution(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(
                _request(
                    'def query(ctx):\n    del ctx.model().by_type("IfcProject")[0][2]\n    return {}\n'
                )
            )
    finally:
        await worker.close()
    assert rejected.value.code == "IFC_QUERY_EXECUTION_FAILED"
    assert rejected.value.phase == "execution"
    assert rejected.value.execution_started is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attribute",
    (
        "create_entity",
        "remove",
        "write",
        "undo",
        "redo",
        "wrapped_data",
        "transaction",
        "file",
        "history_size",
    ),
)
async def test_ast_rejects_ifc_mutator_and_writer_attributes(
    tmp_path: Path,
    attribute: str,
) -> None:
    worker = _worker(tmp_path)
    source = f"def query(ctx):\n    return ctx.model().{attribute}()\n"
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(_request(source))
    finally:
        await worker.close()

    assert rejected.value.code == "IFC_QUERY_MUTATION_FORBIDDEN"
    assert rejected.value.phase == "code_validation"


@pytest.mark.asyncio
async def test_ast_rejects_private_context_and_ifcopenshell_wrapper_bypass(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as private_context:
            await worker.execute_query(
                _request("def query(ctx):\n    return str(ctx._ifc_model)\n")
            )
        assert private_context.value.code == "IFC_QUERY_SOURCE_INVALID"

        with pytest.raises(IfcQueryWorkerError) as low_level:
            await worker.execute_query(
                _request(
                    "from ifcopenshell import ifcopenshell_wrapper\n\n"
                    "def query(ctx):\n    return {}\n"
                )
            )
        assert low_level.value.code == "IFC_QUERY_MUTATION_FORBIDDEN"
        assert low_level.value.phase == "code_validation"
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name",
    (
        "socket",
        "subprocess",
        "OCP",
        "CAD2BIM_backend.infrastructure.geometry_runtime",
    ),
)
async def test_query_cannot_import_external_effects_or_geometry_runtime(
    tmp_path: Path,
    module_name: str,
) -> None:
    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(
                _request(f"import {module_name}\n\ndef query(ctx):\n    return {{}}\n")
            )
    finally:
        await worker.close()

    assert rejected.value.code == "IFC_QUERY_SOURCE_INVALID"
    assert rejected.value.phase == "code_validation"
    assert rejected.value.execution_started is False
    assert rejected.value.diagnostic is not None
    assert rejected.value.diagnostic["exception_type"] == "ImportPolicyError"
    assert rejected.value.diagnostic["line"] == 1


@pytest.mark.asyncio
async def test_context_has_no_legacy_ifc_alias(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(_request("def query(ctx):\n    return str(ctx.ifc())\n"))
    finally:
        await worker.close()

    assert rejected.value.code == "IFC_QUERY_EXECUTION_FAILED"
    assert rejected.value.phase == "execution"
    assert rejected.value.diagnostic is not None
    assert rejected.value.diagnostic["exception_type"] == "AttributeError"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    (
        (
            "import ifcopenshell\n\n"
            "def query(ctx):\n"
            "    return {'projects': len(ifcopenshell.open('source.ifc').by_type('IfcProject'))}\n"
        ),
        (
            "from ifcopenshell import open as reopen\n\n"
            "def query(ctx):\n"
            "    return {'projects': len(reopen('source.ifc').by_type('IfcProject'))}\n"
        ),
    ),
)
async def test_ast_rejects_reopening_the_staged_ifc_path(
    tmp_path: Path,
    source: str,
) -> None:
    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as rejected:
            await worker.execute_query(_request(source))
    finally:
        await worker.close()

    assert rejected.value.code == "IFC_QUERY_SOURCE_INVALID"
    assert rejected.value.phase == "code_validation"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_result_and_diagnostics_are_bounded_and_typed(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as invalid_source:
            await worker.execute_query(_request("def query(ctx)\n    return {}\n"))
        assert invalid_source.value.code == "IFC_QUERY_PYTHON_INVALID"
        assert invalid_source.value.phase == "code_validation"
        assert invalid_source.value.diagnostic is not None
        assert invalid_source.value.diagnostic["exception_type"] == "SyntaxError"
        assert "/Users/" not in str(invalid_source.value.diagnostic)

        with pytest.raises(IfcQueryWorkerError) as invalid_result:
            await worker.execute_query(_request('def query(ctx):\n    return {"bad": object()}\n'))
        assert invalid_result.value.code == "IFC_QUERY_RESULT_INVALID"
        assert invalid_result.value.phase == "result_validation"
        assert invalid_result.value.diagnostic == {
            "json_path": '$["bad"]', "value_type": "object", "reason": "unsupported_type",
        }
        with pytest.raises(IfcQueryWorkerError) as invalid_tuple:
            await worker.execute_query(_request('def query(ctx):\n    return {"geometry": {"coordinates": (0., 1., 2.)}}\n'))
        assert invalid_tuple.value.diagnostic == {
            "json_path": '$["geometry"]["coordinates"]', "value_type": "tuple", "reason": "unsupported_type",
        }

        with pytest.raises(IfcQueryWorkerError) as oversized:
            await worker.execute_query(
                _request(
                    'def query(ctx):\n    return {"value": "x" * 512}\n',
                    max_result_bytes=128,
                )
            )
        assert oversized.value.code == "IFC_QUERY_RESULT_BUDGET_EXCEEDED"
        assert oversized.value.phase == "result_validation"
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_path_and_unlisted_inputs_fail_without_exposing_host_paths(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    try:
        with pytest.raises(IfcQueryWorkerError) as path_access:
            await worker.execute_query(
                _request("def query(ctx):\n    return open('/private/etc/passwd').read()\n")
            )
        assert path_access.value.code == "IFC_QUERY_SOURCE_INVALID"
        assert path_access.value.phase == "code_validation"

        with pytest.raises(IfcQueryWorkerError) as undeclared:
            await worker.execute_query(
                _request("def query(ctx):\n    return ctx.json('missing')\n")
            )
        assert undeclared.value.code == "IFC_QUERY_EXECUTION_FAILED"
        assert undeclared.value.phase == "execution"
        assert "/Users/" not in str(undeclared.value)
    finally:
        await worker.close()
