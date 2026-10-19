"""Run only the approved current CAD/Data gates.

This entry point intentionally uses an explicit test allowlist. It does not
discover the repository test suite and does not import superseded Agent/IFC
product-flow gates. Docker deployment validation remains deferred.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.release.current_test_manifest import (  # noqa: E402
    CAD_DATA_TEST_NODES as FOCUSED_TEST_NODES,
)


# Includes real CAD/Data worker and stdio integration paths. This bounds the
# whole explicit suite; individual production worker budgets remain separate.
_FOCUSED_TEST_TIMEOUT_SECONDS = 900
_REQUIRED_POSTGRES_ENVIRONMENT = "CAD2BIM_TEST_DATABASE_URL"
_CURRENT_MARKER_EXPRESSION = "not benchmark and not e2e and not live"
_REMOVED_ENVIRONMENT_PREFIXES = ("DEEPSEEK_", "KIMI_", "AUTHING_", "LANGSMITH_")
_REMOVED_ENVIRONMENT_NAMES = {
    "CAD2BIM_ENV_FILE",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_TRACING_V2",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
}
_FAILURE_TAIL_LINES = 80


@dataclass(frozen=True)
class Gate:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int


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


def cad_data_gates() -> tuple[Gate, ...]:
    """Return the exact local gate sequence for current CAD/Data work."""

    return (
        Gate(
            name="collection_policy",
            command=(sys.executable, "scripts/release/check_test_collection.py"),
            timeout_seconds=120,
        ),
        Gate(
            name="focused_tests",
            command=(
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-m",
                _CURRENT_MARKER_EXPRESSION,
                *FOCUSED_TEST_NODES,
            ),
            timeout_seconds=_FOCUSED_TEST_TIMEOUT_SECONDS,
        ),
    )


def _failure_tail(output: str) -> str:
    return "\n".join(output.splitlines()[-_FAILURE_TAIL_LINES:])


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
            print(_failure_tail(output), file=sys.stderr)
        raise RuntimeError(f"{gate.name} timed out after {gate.timeout_seconds} seconds") from error
    if completed.returncode != 0:
        if completed.stdout:
            print(_failure_tail(completed.stdout), file=sys.stderr)
        raise RuntimeError(f"{gate.name} failed with exit code {completed.returncode}")
    return max(0.0, time.monotonic() - started)


def run_cad_data_gates() -> dict[str, object]:
    """Wait for each bounded gate and return a small non-persistent summary."""

    if not os.environ.get(_REQUIRED_POSTGRES_ENVIRONMENT, "").strip():
        raise RuntimeError(
            "CAD2BIM_TEST_DATABASE_URL is required; the current CAD/Data M3 gate must not skip "
            "the PostgreSQL/RLS integration chain"
        )
    started = time.monotonic()
    durations: dict[str, float] = {}
    for gate in cad_data_gates():
        durations[gate.name] = round(_run_gate(gate), 3)
    return {
        "status": "passed",
        "focused_test_nodes": len(FOCUSED_TEST_NODES),
        "gate_seconds": durations,
        "duration_seconds": round(max(0.0, time.monotonic() - started), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        summary = run_cad_data_gates()
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
