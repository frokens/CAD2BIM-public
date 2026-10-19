"""Run the durable IFC model kernel before its Direct MCP entry gates."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.release.current_test_manifest import (  # noqa: E402
    IFC_DIRECT_ENTRY_TEST_NODES,
    IFC_MODEL_KERNEL_TEST_NODES,
)

_REQUIRED_POSTGRES_ENVIRONMENT = "CAD2BIM_TEST_DATABASE_URL"
_MARKERS = "not benchmark and not e2e and not live"
_TIMEOUT_SECONDS = 360
# The kernel includes real Geometry/Product/IFC worker and durable-write suites.
# Current-scope coverage includes durable reference, migration and recovery tests.
# A measured run reached 1,057 passing cases before the 20-minute process deadline,
# with durable Geometry/IFC tests still pending. Allow the complete suite 30 minutes.
# This suite deadline remains separate from every worker's production budget.
_KERNEL_TIMEOUT_SECONDS = 1800
_SKIPPED_SUMMARY_PATTERN = re.compile(r"\b[1-9][0-9]* skipped\b")
_REMOVED_ENVIRONMENT_PREFIXES = ("DEEPSEEK_", "KIMI_", "AUTHING_", "LANGSMITH_")
_REMOVED_ENVIRONMENT_NAMES = {
    "CAD2BIM_ENV_FILE",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_TRACING_V2",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
}


@dataclass(frozen=True)
class Gate:
    name: str
    test_nodes: tuple[str, ...]
    timeout_seconds: int = _TIMEOUT_SECONDS

    @property
    def command(self) -> tuple[str, ...]:
        return (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-x",
            "--tb=short",
            "--durations=0",
            "--durations-min=0",
            "-m",
            _MARKERS,
            *self.test_nodes,
        )


def _safe_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in _REMOVED_ENVIRONMENT_NAMES
        and not name.startswith(_REMOVED_ENVIRONMENT_PREFIXES)
    }
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(REPOSITORY_ROOT / "src"), str(REPOSITORY_ROOT))
    )
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def ifc_model_gates() -> tuple[Gate, ...]:
    return (
        Gate(
            name="kernel",
            test_nodes=IFC_MODEL_KERNEL_TEST_NODES,
            timeout_seconds=_KERNEL_TIMEOUT_SECONDS,
        ),
        Gate(name="direct_entry", test_nodes=IFC_DIRECT_ENTRY_TEST_NODES),
    )


def _run_gate(gate: Gate) -> float:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            gate.command,
            cwd=REPOSITORY_ROOT,
            env=_safe_environment(),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=gate.timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        output = (
            error.stdout.decode(errors="replace")
            if isinstance(error.stdout, bytes)
            else error.stdout
        )
        if output:
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
        raise RuntimeError(f"{gate.name} timed out after {gate.timeout_seconds} seconds") from error
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n", flush=True)
    if completed.returncode != 0:
        raise RuntimeError(f"{gate.name} failed with exit code {completed.returncode}")
    if _SKIPPED_SUMMARY_PATTERN.search(completed.stdout or ""):
        raise RuntimeError(f"{gate.name} skipped required tests")
    return max(0.0, time.monotonic() - started)


def run_ifc_model_gates() -> dict[str, object]:
    if not os.environ.get(_REQUIRED_POSTGRES_ENVIRONMENT, "").strip():
        raise RuntimeError(
            "CAD2BIM_TEST_DATABASE_URL is required; the IFC model kernel and "
            "Direct entry gates must run against an isolated PostgreSQL database"
        )
    started = time.monotonic()
    durations: dict[str, float] = {}
    for gate in ifc_model_gates():
        durations[gate.name] = round(_run_gate(gate), 3)
    return {
        "status": "passed",
        "kernel_test_nodes": len(IFC_MODEL_KERNEL_TEST_NODES),
        "direct_entry_test_nodes": len(IFC_DIRECT_ENTRY_TEST_NODES),
        "gate_seconds": durations,
        "duration_seconds": round(max(0.0, time.monotonic() - started), 3),
    }


def main() -> int:
    try:
        summary = run_ifc_model_gates()
    except RuntimeError as error:
        summary = {
            "status": "failed",
            "error": str(error),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
