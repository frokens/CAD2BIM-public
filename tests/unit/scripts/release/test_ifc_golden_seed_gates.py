from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts.release import run_ifc_golden_seed_gates as subject
from scripts.release.current_test_manifest import (
    CAD_DATA_TEST_NODES,
    IFC_DIRECT_ENTRY_TEST_NODES,
    IFC_GOLDEN_SEED_POSTGRES_TEST_NODES,
    IFC_GOLDEN_SEED_SYNTHETIC_TEST_NODES,
    IFC_GOLDEN_SEED_TEST_NODES,
    IFC_MODEL_KERNEL_TEST_NODES,
    REPOSITORY_HYGIENE_TEST_NODES,
)


def test_gate_owns_exact_non_overlapping_capability_allowlists() -> None:
    gates = subject.ifc_golden_seed_gates()

    assert tuple(gate.name for gate in gates) == ("synthetic_seed", "postgres_seed")
    for gate in gates:
        assert gate.command[:6] == (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            subject._MARKERS,
        )
    assert gates[0].command[6:] == IFC_GOLDEN_SEED_SYNTHETIC_TEST_NODES
    assert gates[1].command[6:] == IFC_GOLDEN_SEED_POSTGRES_TEST_NODES
    assert IFC_GOLDEN_SEED_TEST_NODES == (
        *IFC_GOLDEN_SEED_SYNTHETIC_TEST_NODES,
        *IFC_GOLDEN_SEED_POSTGRES_TEST_NODES,
    )
    assert set(IFC_GOLDEN_SEED_SYNTHETIC_TEST_NODES).isdisjoint(
        IFC_GOLDEN_SEED_POSTGRES_TEST_NODES
    )
    assert set(IFC_GOLDEN_SEED_TEST_NODES).isdisjoint(
        {
            *CAD_DATA_TEST_NODES,
            *IFC_MODEL_KERNEL_TEST_NODES,
            *IFC_DIRECT_ENTRY_TEST_NODES,
            *REPOSITORY_HYGIENE_TEST_NODES,
        }
    )

    rendered = " ".join(part for gate in gates for part in gate.command).lower()
    for forbidden in (
        "tests/integration/process/",
        "native_mcp",
        "goal_only",
        "diagnostic_guided",
        "harness",
        "comparator",
        "evaluator",
        "real_m4",
    ):
        assert forbidden not in rendered


def test_gate_does_not_inherit_real_fixture_or_pytest_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = {
        "CAD2BIM_I0_SEED_ROOT": "/real/seed",
        "CAD2BIM_I0_FIXTURE_ROOT": "/real/fixture",
        "CAD2BIM_I0_SOURCE_DATABASE_URL": "postgresql://secret/source",
        "CAD2BIM_I0_SOURCE_STORAGE_ROOT": "/real/source",
        "CAD2BIM_I0_TARGET_DATABASE_URL": "postgresql://secret/target",
        "CAD2BIM_I0_TARGET_STORAGE_ROOT": "/real/target",
        "PYTEST_ADDOPTS": "tests/integration",
        "PYTEST_PLUGINS": "legacy_plugin",
    }
    for name, value in removed.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(
        subject._REQUIRED_POSTGRES_ENVIRONMENT,
        "postgresql://isolated/tests",
    )

    environment = subject._safe_environment()

    assert set(removed).isdisjoint(environment)
    assert environment[subject._REQUIRED_POSTGRES_ENVIRONMENT] == (
        "postgresql://isolated/tests"
    )


def test_runner_requires_the_isolated_postgres_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(subject._REQUIRED_POSTGRES_ENVIRONMENT, raising=False)

    with pytest.raises(RuntimeError, match="round-trip, RLS, and copy isolation"):
        subject.run_ifc_golden_seed_gates()


def test_runner_returns_a_bounded_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv(
        subject._REQUIRED_POSTGRES_ENVIRONMENT,
        "postgresql://isolated/tests",
    )

    def fake_run_gate(gate: subject.Gate) -> float:
        calls.append(gate.name)
        return 0.125

    monkeypatch.setattr(subject, "_run_gate", fake_run_gate)

    result = subject.run_ifc_golden_seed_gates()

    assert calls == ["synthetic_seed", "postgres_seed"]
    assert result == {
        "status": "passed",
        "test_nodes": len(IFC_GOLDEN_SEED_TEST_NODES),
        "gate_seconds": {"synthetic_seed": 0.125, "postgres_seed": 0.125},
        "duration_seconds": result["duration_seconds"],
    }


def test_failed_command_prints_only_a_bounded_tail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = "\n".join(f"line-{index}" for index in range(100))

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 2, stdout=output)

    monkeypatch.setattr(subject.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="failed with exit code 2"):
        subject._run_gate(subject.ifc_golden_seed_gates()[0])

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == subject._FAILURE_TAIL_LINES
    assert lines[0] == "line-20"
    assert lines[-1] == "line-99"


def test_skipped_postgres_tests_fail_the_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = "..s\n2 passed, 1 skipped in 0.25s\n"

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, stdout=output)

    monkeypatch.setattr(subject.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="skipped required tests"):
        subject._run_gate(subject.ifc_golden_seed_gates()[1])

    assert "1 skipped" in capsys.readouterr().err


def test_main_emits_one_json_line_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> dict[str, object]:
        raise RuntimeError("postgres_seed failed")

    monkeypatch.setattr(subject, "run_ifc_golden_seed_gates", fail)

    assert subject.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error": "postgres_seed failed",
    }
