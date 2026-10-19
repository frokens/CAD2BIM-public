from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import pyarrow as pa
import pytest

import CAD2BIM_backend.infrastructure.data_transform.python_arrow_worker as worker_module
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerError,
    DataTransformWorkerParameter,
    PythonArrowWorkerOutputDeclaration,
    PythonArrowWorkerPolicy,
    PythonArrowWorkerRequest,
)
from CAD2BIM_backend.infrastructure.data_transform.capabilities import (
    PYTHON_ARROW_APPROVED_LIBRARIES,
    PYTHON_ARROW_RUNTIME_CAPABILITIES,
)
from CAD2BIM_backend.infrastructure.data_transform.python_arrow_worker import (
    SubprocessPythonArrowDataTransformWorker,
)
from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    DataPythonWorkerDiagnosticCategory,
    DataPythonWorkerEnvironmentError,
)
from CAD2BIM_backend.ports.data_transform import ArrowTransformInput


def _policy(**overrides: object) -> PythonArrowWorkerPolicy:
    values = asdict(PythonArrowWorkerPolicy())
    values.update(overrides)
    return PythonArrowWorkerPolicy(**values)


def _worker(tmp_path: Path, **policy: object) -> SubprocessPythonArrowDataTransformWorker:
    return SubprocessPythonArrowDataTransformWorker(
        policy=_policy(**policy),
        python_executable=os.environ.get(
            "CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE",
            sys.executable,
        ),
        capacity_root=tmp_path / "capacity",
    )


def _input(
    name: str = "source",
    table: pa.Table | None = None,
    *,
    input_format: str = "parquet",
    json_kind: str | None = None,
) -> ArrowTransformInput:
    return ArrowTransformInput(
        name,
        (table or pa.table({"value": [1, 2]})).to_reader(),
        input_format=input_format,  # type: ignore[arg-type]
        json_kind=json_kind,  # type: ignore[arg-type]
    )


def _request(
    code: str,
    *outputs: PythonArrowWorkerOutputDeclaration,
    parameters: tuple[DataTransformWorkerParameter, ...] = (),
) -> PythonArrowWorkerRequest:
    return PythonArrowWorkerRequest(
        code=code,
        parameters=parameters,
        outputs=outputs or (PythonArrowWorkerOutputDeclaration("result", "parquet"),),
    )


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_projected_as_capacity_busy(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    await worker._capacity.acquire()
    task = asyncio.create_task(
        worker.execute(
            _request("def transform(ctx):\n    ctx.write_table('result', ctx.table('source'))\n"),
            (_input(),),
        )
    )
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        worker._capacity.release()
        await worker.close()


def test_capabilities_are_the_approved_scheme_a_library_source() -> None:
    assert PYTHON_ARROW_APPROVED_LIBRARIES == (
        "duckdb",
        "pyarrow",
        "pandas",
        "numpy",
        "scipy",
        "shapely",
        "networkx",
        "polars",
        "pyproj",
        "rapidfuzz",
        "pydantic",
        "jsonschema",
    )
    assert (
        PYTHON_ARROW_RUNTIME_CAPABILITIES.isolation_profile
        == "controlled_local_scheme_a_darwin_seatbelt"
    )
    assert PYTHON_ARROW_RUNTIME_CAPABILITIES.os_isolation_enforced is True
    assert PYTHON_ARROW_RUNTIME_CAPABILITIES.approved_local_platforms == ("darwin",)
    assert PYTHON_ARROW_RUNTIME_CAPABILITIES.arrow_only_write_table is True
    assert PYTHON_ARROW_RUNTIME_CAPABILITIES.json_enabled is True
    assert PYTHON_ARROW_RUNTIME_CAPABILITIES.pip_install_enabled is False
    assert PYTHON_ARROW_RUNTIME_CAPABILITIES.network_enabled is False


def test_python_policy_matches_doc18() -> None:
    policy = PythonArrowWorkerPolicy()
    assert policy.total_call_seconds == 730
    assert policy.capacity_wait_seconds == 600
    assert policy.execution_seconds == 110
    assert policy.wall_seconds == 90
    assert policy.cpu_seconds == 180
    assert policy.rss_bytes == 4 * 1024**3
    assert policy.scratch_bytes == 4 * 1024**3
    assert policy.threads == 2
    assert policy.max_concurrent_workers == 1
    assert policy.cleanup_seconds == 5
    assert policy.open_file_descriptors == 64


@pytest.mark.asyncio
async def test_all_approved_libraries_import_inside_os_sandbox(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    imports = "\n".join(f"import {name}" for name in PYTHON_ARROW_APPROVED_LIBRARIES)
    request = _request(
        f"{imports}\ndef transform(ctx):\n    ctx.write_table('result', ctx.table('source'))\n"
    )
    try:
        (output,) = await worker.execute(request, (_input(),))
    finally:
        await worker.close()
    assert output.receipt.row_count == 2


@pytest.mark.asyncio
async def test_six_core_context_methods_round_trip_arrow(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    request = _request(
        """
import pyarrow as pa
def transform(ctx):
    first = ctx.table("source")
    batches = ctx.batches("source").read_all()
    ctx.log("bounded diagnostic")
    ctx.write_table(
        "table_result",
        pa.table({"value": [sum(first.column("value").to_pylist()) + ctx.param("delta")]}),
    )
    ctx.write_table("batch_result", batches.to_batches()[0])
    ctx.write_table("reader_result", ctx.batches("source"))
""",
        PythonArrowWorkerOutputDeclaration("table_result", "parquet"),
        PythonArrowWorkerOutputDeclaration("batch_result", "parquet"),
        PythonArrowWorkerOutputDeclaration("reader_result", "parquet"),
        parameters=(DataTransformWorkerParameter("delta", 3),),
    )
    try:
        outputs = await worker.execute(request, (_input(),))
    finally:
        await worker.close()
    assert [item.payload_name for item in outputs] == [
        "table_result",
        "batch_result",
        "reader_result",
    ]
    assert pa.Table.from_batches(outputs[0].batches).to_pydict() == {"value": [6]}
    assert [item.receipt.row_count for item in outputs] == [1, 2, 2]


@pytest.mark.asyncio
async def test_numpy_requires_explicit_arrow_conversion(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    request = _request(
        """
import numpy as np
import pyarrow as pa
def transform(ctx):
    values = ctx.table("source").column("value").to_numpy()
    ctx.write_table("result", pa.table({"doubled": np.multiply(values, 2)}))
"""
    )
    try:
        (output,) = await worker.execute(request, (_input(),))
    finally:
        await worker.close()
    assert pa.Table.from_batches(output.batches).to_pydict() == {"doubled": [2, 4]}


@pytest.mark.asyncio
async def test_write_table_rejects_pandas_without_explicit_conversion(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    request = _request(
        """
import pandas as pd
def transform(ctx):
    ctx.write_table("result", pd.DataFrame({"value": [1]}))
"""
    )
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(request, (_input(),))
    await worker.close()
    assert rejected.value.code == "TRANSFORM_EXECUTION_FAILED"
    assert "accepts only pyarrow" in str(rejected.value)


@pytest.mark.asyncio
async def test_json_document_records_and_mixed_outputs(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    document = pa.table({"json_value": [json.dumps({"source": 7})]})
    records = pa.table(
        {
            "record_index": pa.array([0, 1], type=pa.int64()),
            "json_value": [json.dumps({"key": "a"}), json.dumps({"key": "b"})],
        }
    )
    request = _request(
        """
import pyarrow as pa
def transform(ctx):
    document = ctx.json("document")
    records = ctx.json("records")
    ctx.write_table("table_result", pa.table({"count": [len(records)]}))
    ctx.write_json("document_result", {"copy": document})
    ctx.write_json("records_result", records)
""",
        PythonArrowWorkerOutputDeclaration("table_result", "parquet"),
        PythonArrowWorkerOutputDeclaration("document_result", "json", "document"),
        PythonArrowWorkerOutputDeclaration("records_result", "json", "records"),
    )
    try:
        outputs = await worker.execute(
            request,
            (
                _input("document", document, input_format="json", json_kind="document"),
                _input("records", records, input_format="json", json_kind="records"),
            ),
        )
    finally:
        await worker.close()
    assert [item.schema.names for item in outputs] == [
        ["count"],
        ["json_value"],
        ["record_index", "json_value"],
    ]
    document_rows = pa.Table.from_batches(outputs[1].batches).to_pydict()
    assert json.loads(document_rows["json_value"][0]) == {"copy": {"source": 7}}
    records_rows = pa.Table.from_batches(outputs[2].batches).to_pydict()
    assert records_rows["record_index"] == [0, 1]


@pytest.mark.asyncio
async def test_json_input_cannot_be_read_as_table(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    request = _request(
        "def transform(ctx):\n    ctx.write_table('result', ctx.table('document'))\n"
    )
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(
            request,
            (
                _input(
                    "document",
                    pa.table({"json_value": ["null"]}),
                    input_format="json",
                    json_kind="document",
                ),
            ),
        )
    await worker.close()
    assert rejected.value.code == "TRANSFORM_EXECUTION_FAILED"
    assert "ctx.json" in str(rejected.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "def transform(ctx):\n    return 1\n",
        "import os\ndef transform(ctx):\n    pass\n",
        "import socket\ndef transform(ctx):\n    pass\n",
        "import subprocess\ndef transform(ctx):\n    pass\n",
        "def transform(ctx):\n    open('/etc/passwd')\n",
        "value = sum([1, 2])\ndef transform(ctx):\n    pass\n",
    ],
)
async def test_invalid_source_and_blocked_imports_fail_preflight(
    tmp_path: Path,
    code: str,
) -> None:
    worker = _worker(tmp_path)
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(_request(code), (_input(),))
    await worker.close()
    if "return 1" in code:
        assert rejected.value.code == "TRANSFORM_EXECUTION_FAILED"
        assert rejected.value.execution_started is True
    else:
        assert rejected.value.code == "TRANSFORM_PYTHON_INVALID"
        assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_syntax_error_returns_bounded_structured_code_diagnostic(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    code = (
        "import pyarrow as pa\n"
        "def transform(ctx):\n"
        "    values = [1, 2)\n"
        "    ctx.write_table('result', pa.table({'value': values}))\n"
    )
    try:
        with pytest.raises(DataTransformWorkerError) as rejected:
            await worker.execute(_request(code), (_input(),))
    finally:
        await worker.close()

    error = rejected.value
    assert error.code == "TRANSFORM_PYTHON_INVALID"
    assert error.phase == "code_validation"
    assert error.execution_started is False
    assert len(str(error).encode("utf-8")) <= 1_600
    diagnostic = json.loads(str(error))
    assert diagnostic == {
        "caret": "                  ^",
        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "column": 19,
        "end_column": 19,
        "end_line": 3,
        "exception_type": "SyntaxError",
        "line": 3,
        "message": "closing parenthesis ')' does not match opening parenthesis '['",
        "source_line": "    values = [1, 2)",
    }
    assert "traceback" not in str(error).lower()
    assert "<cad2bim-" not in str(error)
    assert str(tmp_path) not in str(error)


@pytest.mark.asyncio
async def test_invalid_entry_signature_returns_location_and_exact_source_line(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)
    code = "def transform(ctx, extra):\n\tctx.write_table('result', ctx.table('source'))\n"
    try:
        with pytest.raises(DataTransformWorkerError) as rejected:
            await worker.execute(_request(code), (_input(),))
    finally:
        await worker.close()

    error = rejected.value
    assert error.code == "TRANSFORM_PYTHON_INVALID"
    assert error.phase == "code_validation"
    assert error.execution_started is False
    diagnostic = json.loads(str(error))
    assert diagnostic["exception_type"] == "EntryPointError"
    assert diagnostic["message"] == "transform must have the exact signature transform(ctx)"
    assert diagnostic["line"] == 1
    assert diagnostic["column"] == 1
    assert diagnostic["source_line"] == "def transform(ctx, extra):"
    assert diagnostic["caret"].startswith("^")
    assert diagnostic["code_sha256"] == hashlib.sha256(code.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_runtime_error_returns_innermost_user_source_line_without_traceback(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)
    code = (
        "import pyarrow as pa\n"
        "def build_type():\n"
        "    pa = 'port-a'\n"
        "    value_type = pa.string()\n"
        "    return value_type\n"
        "def transform(ctx):\n"
        "    ctx.write_table('result', ctx.table('source').cast([build_type()]))\n"
    )
    try:
        with pytest.raises(DataTransformWorkerError) as rejected:
            await worker.execute(_request(code), (_input(),))
    finally:
        await worker.close()

    error = rejected.value
    assert error.code == "TRANSFORM_EXECUTION_FAILED"
    assert error.phase == "execution"
    assert error.execution_started is True
    diagnostic = json.loads(str(error))
    assert diagnostic == {
        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "exception_type": "AttributeError",
        "function": "build_type",
        "line": 4,
        "message": "'str' object has no attribute 'string'",
        "source_line": "    value_type = pa.string()",
    }
    assert "traceback" not in str(error).lower()
    assert "<cad2bim-" not in str(error)
    assert str(tmp_path) not in str(error)


@pytest.mark.asyncio
async def test_long_syntax_error_diagnostic_remains_valid_bounded_json(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    code = "def transform(ctx):\n    value = '" + ("\u71c3" * 2_000) + "\n"
    try:
        with pytest.raises(DataTransformWorkerError) as rejected:
            await worker.execute(_request(code), (_input(),))
    finally:
        await worker.close()

    rendered = str(rejected.value)
    assert len(rendered.encode("utf-8")) <= 1_600
    diagnostic = json.loads(rendered)
    assert diagnostic["exception_type"] == "SyntaxError"
    assert diagnostic["source_line_truncated"] is True
    assert diagnostic["code_sha256"] == hashlib.sha256(code.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_approved_library_cannot_read_an_ordinary_host_file(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    request = _request(
        """
import pandas as pd
def transform(ctx):
    frame = pd.read_csv("/etc/passwd")
    ctx.write_table("result", frame)
"""
    )
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(request, (_input(),))
    await worker.close()
    assert rejected.value.code == "TRANSFORM_PYTHON_INVALID"
    assert "path-based or external I/O" in str(rejected.value)


@pytest.mark.asyncio
async def test_pyarrow_native_path_api_is_rejected_before_it_can_open_host_file(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)
    request = _request(
        """
import pyarrow.parquet as pq
def transform(ctx):
    table = pq.read_table("/etc/passwd")
    ctx.write_table("result", table)
"""
    )
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(request, (_input(),))
    await worker.close()
    assert rejected.value.code == "TRANSFORM_PYTHON_INVALID"
    assert "path-based or external I/O" in str(rejected.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    (
        """
import pyarrow as pa
def transform(ctx):
    reader = pa.input_stream
    reader("/etc/hosts").read(1)
    ctx.write_table("result", ctx.table("source"))
""",
        """
import pyarrow as pa
def transform(ctx):
    readers = {"native": pa.input_stream}
    readers["native"]("/etc/hosts").read(1)
    ctx.write_table("result", ctx.table("source"))
""",
        """
import pyarrow.parquet as pq
def transform(ctx):
    reader = pq.ParquetFile
    reader("/etc/hosts")
    ctx.write_table("result", ctx.table("source"))
""",
    ),
)
async def test_os_sandbox_rejects_aliased_and_higher_order_pyarrow_host_reads(
    tmp_path: Path,
    code: str,
) -> None:
    worker = _worker(tmp_path)
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(_request(code), (_input(),))
    await worker.close()
    assert rejected.value.code == "TRANSFORM_EXECUTION_FAILED"
    assert rejected.value.execution_started is True
    assert "Operation not permitted" in str(rejected.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ("arrow", "parquet"))
async def test_os_sandbox_rejects_native_pyarrow_host_writes(
    tmp_path: Path,
    api: str,
) -> None:
    host_output = tmp_path / f"forbidden-{api}.bin"
    if api == "arrow":
        code = f"""
import pyarrow as pa
def transform(ctx):
    writer = pa.output_stream
    writer({str(host_output)!r})
    ctx.write_table("result", ctx.table("source"))
"""
    else:
        code = f"""
import pyarrow.parquet as pq
def transform(ctx):
    writer = pq.write_table
    writer(ctx.table("source"), {str(host_output)!r})
    ctx.write_table("result", ctx.table("source"))
"""
    worker = _worker(tmp_path / "worker")
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(_request(code), (_input(),))
    await worker.close()
    assert rejected.value.code == "TRANSFORM_EXECUTION_FAILED"
    assert rejected.value.execution_started is True
    assert "Operation not permitted" in str(rejected.value)
    assert not host_output.exists()


@pytest.mark.asyncio
async def test_duckdb_is_locked_to_in_memory_no_external_access(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    safe = _request(
        """
import duckdb
import pyarrow as pa
def transform(ctx):
    value = duckdb.sql("SELECT 40 + 2 AS answer").arrow().read_all()
    ctx.write_table("result", value)
"""
    )
    try:
        (output,) = await worker.execute(safe, (_input(),))
    finally:
        await worker.close()
    assert pa.Table.from_batches(output.batches).to_pydict() == {"answer": [42]}

    blocked_worker = _worker(tmp_path / "blocked")
    blocked = _request(
        """
import duckdb
def transform(ctx):
    value = duckdb.sql("SELECT * FROM read_csv('/etc/passwd')").arrow().read_all()
    ctx.write_table("result", value)
"""
    )
    with pytest.raises(DataTransformWorkerError) as rejected:
        await blocked_worker.execute(blocked, (_input(),))
    await blocked_worker.close()
    assert rejected.value.code == "TRANSFORM_EXECUTION_FAILED"
    assert "disabled by configuration" in str(rejected.value).lower()


@pytest.mark.asyncio
async def test_missing_duplicate_and_wrong_output_methods_are_rejected(tmp_path: Path) -> None:
    cases = (
        "def transform(ctx):\n    pass\n",
        "def transform(ctx):\n    ctx.write_table('result', ctx.table('source'))\n    ctx.write_table('result', ctx.table('source'))\n",
        "def transform(ctx):\n    ctx.write_json('result', {'value': 1})\n",
    )
    for code in cases:
        worker = _worker(tmp_path / str(len(code)))
        with pytest.raises(DataTransformWorkerError) as rejected:
            await worker.execute(_request(code), (_input(),))
        await worker.close()
        assert rejected.value.code == "TRANSFORM_EXECUTION_FAILED"


@pytest.mark.asyncio
async def test_wall_timeout_kills_process_group(tmp_path: Path) -> None:
    worker = _worker(tmp_path, wall_seconds=0.1, total_call_seconds=2.0,
                     execution_seconds=1.5, capacity_wait_seconds=0.2, cleanup_seconds=0.2, terminal_seconds=.05)
    request = _request("def transform(ctx):\n    while True:\n        pass\n")
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(request, (_input(),))
    await worker.close()
    assert rejected.value.code == "TRANSFORM_TIMEOUT"
    assert rejected.value.execution_started is True


@pytest.mark.asyncio
async def test_stdout_and_stderr_share_bounded_parent_capture(tmp_path: Path) -> None:
    worker = _worker(tmp_path, capture_bytes=1024)
    request = _request(
        "def transform(ctx):\n    print('x' * 2048)\n    ctx.write_table('result', ctx.table('source'))\n"
    )
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(request, (_input(),))
    await worker.close()
    assert rejected.value.code == "TRANSFORM_RESOURCE_LIMIT"


@pytest.mark.asyncio
async def test_input_and_output_arrow_budgets_are_aggregate(tmp_path: Path) -> None:
    input_worker = _worker(tmp_path / "input", input_row_limit=1)
    with pytest.raises(DataTransformWorkerError) as input_error:
        await input_worker.execute(
            _request("def transform(ctx):\n    ctx.write_table('result', ctx.table('source'))\n"),
            (_input(),),
        )
    await input_worker.close()
    assert input_error.value.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED"

    output_worker = _worker(tmp_path / "output", output_row_limit=1)
    with pytest.raises(DataTransformWorkerError) as output_error:
        await output_worker.execute(
            _request("def transform(ctx):\n    ctx.write_table('result', ctx.table('source'))\n"),
            (_input(),),
        )
    await output_worker.close()
    assert output_error.value.code == "TRANSFORM_OUTPUT_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_process_group_permission_error_falls_back_to_direct_child_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        start_new_session=True,
    )

    def reject_process_group_signal(_pid: int, _signal: int) -> None:
        raise PermissionError(1, "operation not permitted")

    monkeypatch.setattr(worker_module.os, "killpg", reject_process_group_signal)
    try:
        await worker._stop_process(process)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await worker.close()

    assert process.returncode is not None


@pytest.mark.asyncio
async def test_prestart_service_failure_is_retried_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    calls = 0

    async def invoke(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        raise DataTransformWorkerError(
            "TRANSFORM_SERVICE_UNAVAILABLE",
            "transient launch failure",
            phase="preflight",
            execution_started=False,
        )

    monkeypatch.setattr(worker, "_invoke_child", invoke)
    with pytest.raises(DataTransformWorkerError):
        await worker.execute(_request("def transform(ctx):\n    pass\n"), (_input(),))
    await worker.close()
    assert calls == 2


@pytest.mark.asyncio
async def test_verify_ready_returns_stable_child_diagnostic_without_raw_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)

    async def fail_readiness(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise DataTransformWorkerError(
            "TRANSFORM_SERVICE_UNAVAILABLE",
            "raw child detail",
            phase="preflight",
            execution_started=False,
        )

    monkeypatch.setattr(worker, "execute", fail_readiness)
    with pytest.raises(DataPythonWorkerEnvironmentError) as captured:
        await worker.verify_ready()
    assert captured.value.category is DataPythonWorkerDiagnosticCategory.CHILD_READINESS_FAILED
    assert "raw child detail" not in str(captured.value)
    assert len(captured.value.recovery_commands) == 2


@pytest.mark.asyncio
async def test_verify_ready_uses_a_real_isolated_child(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    try:
        await worker.verify_ready()
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_one_worker_slot_serializes_concurrent_calls(tmp_path: Path) -> None:
    worker = _worker(tmp_path, wall_seconds=2.0, total_call_seconds=6.0,
                     execution_seconds=3.0, capacity_wait_seconds=2.5, cleanup_seconds=0.2, terminal_seconds=.1)
    request = _request(
        """
def transform(ctx):
    total = 0
    for value in range(500000):
        total += value
    ctx.write_table("result", ctx.table("source"))
"""
    )
    first = asyncio.create_task(worker.execute(request, (_input(),)))
    second = asyncio.create_task(worker.execute(request, (_input(),)))
    first_result, second_result = await asyncio.gather(first, second)
    await worker.close()
    assert first_result[0].receipt.row_count == 2
    assert second_result[0].receipt.row_count == 2


@pytest.mark.asyncio
async def test_sixteen_large_output_pipes_all_have_concurrent_readers(tmp_path: Path) -> None:
    worker = _worker(tmp_path, wall_seconds=5.0, total_call_seconds=8.0,
                     execution_seconds=6.0, capacity_wait_seconds=1.0, cleanup_seconds=0.5, terminal_seconds=.1)
    declarations = tuple(
        PythonArrowWorkerOutputDeclaration(f"output_{index:02d}", "parquet") for index in range(16)
    )
    request = _request(
        """
import pyarrow as pa
def transform(ctx):
    blob = "x" * 131072
    for index in range(16):
        ctx.write_table(f"output_{index:02d}", pa.table({"blob": [blob]}))
""",
        *declarations,
    )
    try:
        outputs = await worker.execute(request, (_input(),))
    finally:
        await worker.close()
    assert len(outputs) == 16
    assert all(item.receipt.row_count == 1 for item in outputs)
    assert all(item.receipt.arrow_nbytes >= 131072 for item in outputs)
