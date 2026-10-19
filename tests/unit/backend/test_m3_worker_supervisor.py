from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from time import monotonic

import pyarrow as pa
import pytest

from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_PAYLOAD_NAMES,
    CadAnalyzeToleranceCandidatesCommand,
    CadComputeMeasurementsCommand,
    CadToleranceSpec,
)
from CAD2BIM_backend.domain.data_transform import OutputPackageDeclaration
from CAD2BIM_backend.infrastructure.cad_measurement import supervised_worker as worker_module
from CAD2BIM_backend.infrastructure.cad_measurement.supervised_worker import (
    CadMeasurementWorkerError,
    CadMeasurementWorkerObservation,
    SubprocessCadMeasurementWorker,
)


def _command() -> CadAnalyzeToleranceCandidatesCommand:
    return CadAnalyzeToleranceCandidatesCommand(
        package_alias="data_001",
        source_unit_to_m=1.0,
        path_layers=("pipe",),
        point_groups=(),
        trials=(),
    )


def _tables() -> dict[str, pa.Table]:
    return {
        "entities": pa.table({"value": [1]}),
        "geometry_elements": pa.table({"value": [2]}),
        "relationships": pa.table({"value": [3]}),
    }


def _real_tables() -> dict[str, pa.Table]:
    entities = pa.Table.from_pylist(
        [
            {
                "entity_key": "line_1",
                "entity_type": "LINE",
                "layer": "paths",
                "extrusion_x": 0.0,
                "extrusion_y": 0.0,
                "extrusion_z": 1.0,
                "typed_geometry_supported": True,
                "unsupported_reason": None,
            }
        ]
    )
    geometry = pa.Table.from_pylist(
        [
            {
                "geometry_element_key": "g1",
                "entity_key": "line_1",
                "geometry_component_kind": "line_start",
                "ordinal": 0,
                "x": 0.0,
                "y": 0.0,
                "z": 0.0,
                "bulge": None,
                "closed": None,
                "flags": None,
            },
            {
                "geometry_element_key": "g2",
                "entity_key": "line_1",
                "geometry_component_kind": "line_end",
                "ordinal": 1,
                "x": 1.0,
                "y": 0.0,
                "z": 0.0,
                "bulge": None,
                "closed": None,
                "flags": None,
            },
        ]
    )
    relationships = pa.Table.from_pylist(
        [], schema=pa.schema([pa.field("relation_kind", pa.string())])
    )
    return {"entities": entities, "geometry_elements": geometry, "relationships": relationships}


def _compute_command() -> CadComputeMeasurementsCommand:
    return CadComputeMeasurementsCommand(
        package_alias="data_001",
        source_unit_to_m=1.0,
        path_layers=("paths",),
        point_scopes=(),
        tolerance_spec=CadToleranceSpec(
            endpoint_quantization_m=0.0001,
            endpoint_snap_candidate_m=0.01,
            endpoint_on_path_candidate_m=0.01,
            host_endpoint_guard_m=0.01,
            point_target_uniqueness_margin_m=0.005,
            straight_turn_tolerance_deg=0.5,
        ),
        output_package=OutputPackageDeclaration(
            name="m3_worker_smoke",
            description_markdown="# m3_worker_smoke\n\n## Summary\nSmoke.\n\n## Intended use\nTest.\n\n## Contents\nTen payloads.\n\n## Method\nM3.\n\n## Scope and completeness\nOne line.\n\n## Known limitations\nTest only.",
        ),
    )


def _child(tmp_path, body: str):
    path = tmp_path / "child.py"
    path.write_text(
        "import json, pathlib, sys, time\n"
        "request=json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "root=pathlib.Path(request['root'])\n"
        "print(json.dumps({'event':'ready'}), flush=True)\n"
        "assert json.loads(sys.stdin.readline()) == {'event':'start'}\n"
        "print(json.dumps({'event':'started','started_at':'2026-08-24T00:00:00+00:00'}), flush=True)\n"
        + body,
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_analyzer_uses_ready_start_started_and_returns_bounded_json(tmp_path) -> None:
    child = _child(
        tmp_path,
        "(root/'analysis.json').write_text('{}')\n"
        "print(json.dumps({'event':'terminal','outcome':'succeeded'}), flush=True)\n",
    )
    worker = SubprocessCadMeasurementWorker(
        python_executable=sys.executable,
        child_script=child,
        capacity_root=tmp_path / "capacity",
    )

    result = await worker.analyze(_tables(), _command(), deadline=monotonic() + 490)

    assert result.payload == {}
    assert result.started_at.isoformat() == "2026-08-24T00:00:00+00:00"


@pytest.mark.asyncio
async def test_started_child_failure_preserves_operation_lifecycle_fact(tmp_path) -> None:
    child = _child(
        tmp_path,
        "print(json.dumps({'event':'terminal','outcome':'failed','code':'CAD_ZERO_LENGTH_EDGE','phase':'execution','message':'zero edge'}), flush=True)\n"
        "raise SystemExit(1)\n",
    )
    worker = SubprocessCadMeasurementWorker(
        python_executable=sys.executable,
        child_script=child,
        capacity_root=tmp_path / "capacity",
    )

    with pytest.raises(CadMeasurementWorkerError) as caught:
        await worker.analyze(_tables(), _command(), deadline=monotonic() + 490)

    assert caught.value.code == "CAD_ZERO_LENGTH_EDGE"
    assert caught.value.execution_started is True
    assert caught.value.started_at is not None
    assert caught.value.phase == "execution"


@pytest.mark.asyncio
async def test_worker_wall_timeout_terminates_child_and_reports_started(
    tmp_path, monkeypatch
) -> None:
    child = _child(tmp_path, "time.sleep(30)\n")
    monkeypatch.setattr(
        worker_module,
        "CAD_TOLERANCE_ANALYZER_BUDGET",
        replace(worker_module.CAD_TOLERANCE_ANALYZER_BUDGET, worker_wall_seconds=1),
    )
    worker = SubprocessCadMeasurementWorker(
        python_executable=sys.executable,
        child_script=child,
        capacity_root=tmp_path / "capacity",
    )

    with pytest.raises(CadMeasurementWorkerError) as caught:
        await worker.analyze(_tables(), _command(), deadline=monotonic() + 490)

    assert caught.value.code == "TIMEOUT"
    assert caught.value.execution_started is True
    assert caught.value.started_at is not None


@pytest.mark.asyncio
async def test_two_instances_share_one_host_capacity_slot(tmp_path, monkeypatch) -> None:
    child = _child(
        tmp_path,
        "time.sleep(2)\n"
        "(root/'analysis.json').write_text('{}')\n"
        "print(json.dumps({'event':'terminal','outcome':'succeeded'}), flush=True)\n",
    )
    monkeypatch.setattr(
        worker_module,
        "CAD_TOLERANCE_ANALYZER_BUDGET",
        replace(worker_module.CAD_TOLERANCE_ANALYZER_BUDGET, capacity_wait_seconds=1),
    )
    first = SubprocessCadMeasurementWorker(
        python_executable=sys.executable, child_script=child, capacity_root=tmp_path / "capacity"
    )
    second = SubprocessCadMeasurementWorker(
        python_executable=sys.executable, child_script=child, capacity_root=tmp_path / "capacity"
    )
    running = asyncio.create_task(first.analyze(_tables(), _command(), deadline=monotonic() + 490))
    await asyncio.sleep(0.2)

    with pytest.raises(CadMeasurementWorkerError) as caught:
        await second.analyze(_tables(), _command(), deadline=monotonic() + 490)

    assert caught.value.code == "CAPACITY_BUSY"
    assert caught.value.retryable is True
    assert caught.value.execution_started is False
    await running


@pytest.mark.asyncio
async def test_cancellation_preserves_started_fact_and_kills_process_group(tmp_path) -> None:
    child = _child(tmp_path, "time.sleep(30)\n")
    worker = SubprocessCadMeasurementWorker(
        python_executable=sys.executable,
        child_script=child,
        capacity_root=tmp_path / "capacity",
    )
    running = asyncio.create_task(worker.analyze(_tables(), _command(), deadline=monotonic() + 490))
    await asyncio.sleep(0.2)
    running.cancel()

    with pytest.raises(asyncio.CancelledError) as caught:
        await running

    failure = getattr(caught.value, "worker_failure")
    assert failure.code == "TIMEOUT"
    assert failure.execution_started is True
    assert failure.started_at is not None


@pytest.mark.asyncio
async def test_staging_timeout_waits_for_writer_before_cleanup(tmp_path, monkeypatch) -> None:
    from CAD2BIM_backend.domain import runtime_execution

    monkeypatch.setattr(
        runtime_execution, "CAD_ANALYSIS_EXECUTION",
        runtime_execution.RuntimeExecutionPolicy(0.01, 0.015, 0.001, 0.001),
    )
    worker_root = tmp_path / "staging-root"
    worker_root.mkdir()
    counter = 0

    def make_root(*, prefix: str):
        nonlocal counter
        counter += 1
        path = worker_root / f"{prefix}{counter}"
        path.mkdir()
        return str(path)

    original = SubprocessCadMeasurementWorker._stage_inputs

    def slow_stage(root, tables):
        time.sleep(0.2)
        original(root, tables)

    monkeypatch.setattr(worker_module.tempfile, "mkdtemp", make_root)
    monkeypatch.setattr(SubprocessCadMeasurementWorker, "_stage_inputs", staticmethod(slow_stage))
    worker = SubprocessCadMeasurementWorker(
        python_executable=sys.executable,
        child_script=_child(tmp_path, "raise AssertionError('must not start')\n"),
        capacity_root=tmp_path / "capacity",
    )

    with pytest.raises(CadMeasurementWorkerError) as caught:
        await worker.analyze(_tables(), _command(), deadline=monotonic() + 0.03)

    assert caught.value.code == "TIMEOUT"
    assert caught.value.execution_started is False
    await worker.close()
    assert list(worker_root.iterdir()) == []


@pytest.mark.asyncio
async def test_real_module_child_runs_analyzer_and_exact_ten_compute_payloads(tmp_path) -> None:
    observations: list[CadMeasurementWorkerObservation] = []
    worker = SubprocessCadMeasurementWorker(
        python_executable=sys.executable,
        capacity_root=tmp_path / "capacity",
        observation_sink=observations.append,
    )
    analyze_command = CadAnalyzeToleranceCandidatesCommand(
        package_alias="data_001",
        source_unit_to_m=1.0,
        path_layers=("paths",),
        point_groups=(),
        trials=(),
    )

    analysis = await worker.analyze(_real_tables(), analyze_command, deadline=monotonic() + 490)
    computed = await worker.compute(_real_tables(), _compute_command(), deadline=monotonic() + 550)

    assert analysis.payload["coverage"]["atomic_edge_count"] == 1
    assert tuple(computed.tables) == CAD_MEASUREMENT_PAYLOAD_NAMES
    assert computed.tables["atomic_edges"].num_rows == 1
    assert computed.tables["endpoints"].num_rows == 2
    assert tuple(computed.parquet_paths) == CAD_MEASUREMENT_PAYLOAD_NAMES
    assert all(path.is_file() for path in computed.parquet_paths.values())
    assert [item.mode for item in observations] == ["analyze", "compute"]
    analyzer_observation, compute_observation = observations
    assert analyzer_observation.outcome == "succeeded"
    assert analyzer_observation.code is None
    assert analyzer_observation.input_rows == 3
    assert analyzer_observation.input_arrow_bytes > 0
    assert analyzer_observation.staged_input_bytes > 0
    assert analyzer_observation.peak_rss_bytes > 0
    assert analyzer_observation.peak_scratch_bytes > 0
    assert analyzer_observation.worker_wall_seconds > 0
    assert analyzer_observation.worker_cpu_seconds >= 0
    assert analyzer_observation.analysis_response_bytes is not None
    assert analyzer_observation.output_rows is None
    assert compute_observation.output_rows is not None
    assert compute_observation.output_arrow_bytes is not None
    assert compute_observation.output_parquet_bytes is not None
    assert compute_observation.analysis_response_bytes is None
    await computed.cleanup()
    assert all(not path.exists() for path in computed.parquet_paths.values())


@pytest.mark.asyncio
async def test_os_cpu_signal_is_reported_as_resource_limit(tmp_path) -> None:
    child = _child(
        tmp_path,
        "import os, signal\nos.kill(os.getpid(), signal.SIGXCPU)\n",
    )
    worker = SubprocessCadMeasurementWorker(
        python_executable=sys.executable,
        child_script=child,
        capacity_root=tmp_path / "capacity",
    )

    with pytest.raises(CadMeasurementWorkerError) as caught:
        await worker.analyze(_tables(), _command(), deadline=monotonic() + 490)

    assert caught.value.code == "RESOURCE_LIMIT"
    assert caught.value.execution_started is True


@pytest.mark.asyncio
async def test_observation_sink_failure_never_changes_successful_result(tmp_path) -> None:
    child = _child(
        tmp_path,
        "(root/'analysis.json').write_text('{}')\n"
        "print(json.dumps({'event':'terminal','outcome':'succeeded'}), flush=True)\n",
    )

    def broken_sink(_observation) -> None:
        raise RuntimeError("metrics backend unavailable")

    worker = SubprocessCadMeasurementWorker(
        python_executable=sys.executable,
        child_script=child,
        capacity_root=tmp_path / "capacity",
        observation_sink=broken_sink,
    )

    result = await worker.analyze(_tables(), _command(), deadline=monotonic() + 490)

    assert result.payload == {}
