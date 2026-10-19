"""Private child entry point for the fixed M3 geometry kernel."""

from __future__ import annotations

import json
import os
import resource
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def main() -> int:
    started = False
    try:
        request = _request(Path(sys.argv[1]))
        _limits(dict(request["budget"]))
        _emit({"event": "ready"})
        control = sys.stdin.buffer.readline(32 * 1024 + 1)
        if len(control) > 32 * 1024 or json.loads(control) != {"event": "start"}:
            raise _ChildError("WORKER_FAILED", "invalid start command", "preflight")
        started = True
        _emit({"event": "started", "started_at": datetime.now(timezone.utc).isoformat()})
        _execute(request)
        _emit({"event": "terminal", "outcome": "succeeded"})
        return 0
    except BaseException as exc:
        _emit(
            {
                "event": "terminal",
                "outcome": "failed",
                "code": str(getattr(exc, "code", "WORKER_FAILED")),
                "phase": str(getattr(exc, "phase", "execution" if started else "preflight")),
                "message": _bounded(str(exc), 2_000),
            }
        )
        return 1


def _execute(request: dict[str, Any]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from CAD2BIM_backend.infrastructure.cad_measurement.analysis import (
        analyze_tolerance_candidates,
    )
    from CAD2BIM_backend.infrastructure.cad_measurement.kernel import (
        MeasurementTolerances,
        NamedToleranceTrial,
        PointGroup,
        PointScope,
        PointSearchRadius,
        compute_measurements,
    )
    from CAD2BIM_backend.infrastructure.cad_measurement.schemas import MEASUREMENT_PAYLOAD_SCHEMAS

    root = Path(request["root"])
    inputs: dict[str, pa.Table] = {}
    for name in ("entities", "geometry_elements", "relationships"):
        with pa.memory_map(str(root / "inputs" / f"{name}.arrow"), "r") as source:
            inputs[name] = pa.ipc.open_file(source).read_all()
    command = dict(request["command"])
    budget = dict(request["budget"])

    if request["mode"] == "analyze":
        groups = tuple(PointGroup(**item) for item in command.get("point_groups", ()))
        trials = tuple(
            NamedToleranceTrial(
                trial_key=item["trial_key"],
                tolerances=MeasurementTolerances(**item["tolerance_spec"]),
                point_search_radii=tuple(
                    PointSearchRadius(**value) for value in item.get("point_search_radii", ())
                ),
            )
            for item in command.get("trials", ())
        )
        result = analyze_tolerance_candidates(
            entities=inputs["entities"],
            geometry_elements=inputs["geometry_elements"],
            relationships=inputs["relationships"],
            path_layers=command["path_layers"],
            point_groups=groups,
            source_unit_to_m=float(command["source_unit_to_m"]),
            trials=trials,
            candidate_budget=int(budget["candidate_count"]),
        )
        content = json.dumps(
            result.to_domain_payload(), ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(content) > int(budget["response_bytes"]):
            raise _ChildError(
                "OUTPUT_BUDGET_EXCEEDED",
                "analysis response exceeds byte budget",
                "output_validation",
            )
        temporary = root / "analysis.json.partial"
        temporary.write_bytes(content)
        os.replace(temporary, root / "analysis.json")
        return

    scopes = tuple(PointScope(**item) for item in command.get("point_scopes", ()))
    bundle = compute_measurements(
        entities=inputs["entities"],
        geometry_elements=inputs["geometry_elements"],
        relationships=inputs["relationships"],
        path_layers=command["path_layers"],
        point_scopes=scopes,
        source_unit_to_m=float(command["source_unit_to_m"]),
        tolerances=MeasurementTolerances(**command["tolerance_spec"]),
        candidate_budget=int(budget["candidate_count"]),
        fail_on_blockers=True,
    )
    outputs = bundle.to_arrow_tables()
    if set(outputs) != set(MEASUREMENT_PAYLOAD_SCHEMAS):
        raise _ChildError(
            "OUTPUT_INVALID", "kernel did not return exact ten Payloads", "output_validation"
        )
    if sum(table.num_rows for table in outputs.values()) > int(budget["output_rows"]):
        raise _ChildError(
            "OUTPUT_BUDGET_EXCEEDED", "output row budget exceeded", "output_validation"
        )
    if sum(table.nbytes for table in outputs.values()) > int(budget["output_arrow_bytes"]):
        raise _ChildError(
            "OUTPUT_BUDGET_EXCEEDED", "Arrow output budget exceeded", "output_validation"
        )
    partial = root / "outputs.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(mode=0o700)
    parquet_bytes = 0
    for name in sorted(outputs):
        table = outputs[name]
        if table.schema != MEASUREMENT_PAYLOAD_SCHEMAS[name]:
            raise _ChildError("OUTPUT_INVALID", f"{name} schema mismatch", "output_validation")
        with pa.OSFile(str(partial / f"{name}.arrow"), "wb") as sink:
            with pa.ipc.new_file(sink, table.schema) as writer:
                writer.write_table(table)
        pq.write_table(table, partial / f"{name}.parquet", compression="zstd")
        payload_bytes = (partial / f"{name}.parquet").stat().st_size
        if payload_bytes > int(budget["output_payload_parquet_bytes"]):
            raise _ChildError(
                "OUTPUT_BUDGET_EXCEEDED", f"{name} Parquet budget exceeded", "output_validation"
            )
        parquet_bytes += payload_bytes
    if parquet_bytes > int(budget["output_parquet_bytes"]):
        raise _ChildError(
            "OUTPUT_BUDGET_EXCEEDED", "aggregate Parquet budget exceeded", "output_validation"
        )
    os.replace(partial, root / "outputs")


def _request(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 128 * 1024:
        raise _ChildError("INVALID_ARGUMENT", "request exceeds 128 KiB", "preflight")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("mode") not in {"analyze", "compute"}:
        raise _ChildError("INVALID_ARGUMENT", "invalid worker request", "preflight")
    return value


def _limits(budget: dict[str, Any]) -> None:
    cpu = int(budget["worker_cpu_seconds"])
    scratch = int(budget["worker_scratch_bytes"])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (scratch, scratch))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    # Darwin's RLIMIT_AS is not a usable RSS boundary; the parent measures RSS.
    if sys.platform.startswith("linux"):
        memory = int(budget["worker_rss_bytes"])
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


class _ChildError(RuntimeError):
    def __init__(self, code: str, message: str, phase: str) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase


def _emit(value: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _bounded(value: str, maximum: int) -> str:
    content = value.encode("utf-8")
    return value if len(content) <= maximum else content[:maximum].decode("utf-8", "ignore")


if __name__ == "__main__":
    raise SystemExit(main())
