from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts.release import check_compose
from scripts.release import run_cad_data_gates as subject


def test_gate_has_an_exact_focused_allowlist() -> None:
    gates = subject.cad_data_gates()

    assert tuple(gate.name for gate in gates) == (
        "collection_policy",
        "focused_tests",
    )
    assert gates[0].command[-1] == "scripts/release/check_test_collection.py"
    command = gates[1].command
    assert command[:6] == (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-m",
        subject._CURRENT_MARKER_EXPRESSION,
    )
    assert command[6:] == subject.FOCUSED_TEST_NODES

    required_current_nodes = {
        "tests/unit/backend/test_data_query_cursor.py",
        "tests/unit/backend/test_query_snapshot_cache.py",
        "tests/unit/backend/test_data_query_sql.py",
        "tests/unit/backend/test_data_query_python.py",
        "tests/unit/backend/test_ifc_official_docs.py",
        "tests/unit/backend/test_m3_geometry_kernel.py",
        "tests/unit/backend/test_m3_geometry_oracle.py",
        "tests/unit/backend/test_m3_worker_supervisor.py",
        "tests/unit/backend/test_data_runtime_queries.py",
        "tests/unit/mcp/test_direct_profile.py",
        "tests/integration/backend/test_phase2_real_gas_mcp_capability.py",
        "tests/integration/backend/test_direct_data_runtime_postgres.py",
        "tests/unit/backend/test_package_synopsis.py",
        "tests/unit/backend/test_data_transform_application.py",
        "tests/unit/backend/test_data_transform_python_worker.py",
        "tests/unit/backend/test_data_transform_worker.py",
        "tests/unit/backend/test_data_json_runtime.py",
        "tests/unit/backend/test_postgres_schema.py",
        "tests/integration/backend/test_m2a_direct_transform_capability.py",
        "tests/integration/backend/test_m2b_real_gas_capability.py",
        "tests/integration/backend/test_m2c_direct_json_capability.py",
        "tests/integration/backend/test_m2c_postgres_json_recovery.py",
        "tests/integration/backend/test_m2c_real_gas_ifc_inputs.py",
        "tests/integration/backend/test_q0_real_gas_python_query.py",
        "tests/integration/backend/test_q0_ifc_official_docs_scenario.py",
        "tests/integration/backend/test_m3_service_lifecycle.py",
        "tests/integration/backend/test_m3_multi_cad_service.py",
        "tests/integration/backend/test_m3_real_gas_golden.py",
        "tests/integration/backend/test_m3_downstream_golden_transform.py",
        "tests/unit/mcp/test_direct_m2c_provider_schema.py",
        "tests/unit/mcp/test_direct_q0_provider_schema.py",
        "tests/unit/mcp/test_direct_m3_provider_schema.py",
        "tests/unit/scripts/setup/test_setup_data_python_worker_env.py",
        "tests/integration/process/test_m2_direct_stdio_m2b.py",
    }
    assert required_current_nodes.issubset(command[6:])
    assert tuple(node for node in command[6:] if node.startswith("tests/integration/process/")) == (
        "tests/integration/process/test_m2_direct_stdio_m2b.py",
    )

    forbidden = (
        "compose_api_smoke",
        "process_model",
        "ifc_product_list_components",
        "workspace-exec",
        "component_agent",
        "run_offline_release_gates",
        "release_manifest",
    )
    rendered = " ".join(command)
    assert all(token not in rendered for token in forbidden)


def test_gate_does_not_inherit_pytest_collection_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "tests/unit/agent")
    monkeypatch.setenv("PYTEST_PLUGINS", "legacy_collection_plugin")

    environment = subject._safe_environment()

    assert "PYTEST_ADDOPTS" not in environment
    assert "PYTEST_PLUGINS" not in environment


def test_gate_never_builds_or_starts_docker() -> None:
    rendered = " ".join(part for gate in subject.cad_data_gates() for part in gate.command)

    assert "docker" not in rendered.lower()
    assert "check_compose.py" not in rendered
    assert "--compose" not in rendered


def test_compose_check_only_renders_static_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check_compose.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(check_compose.subprocess, "run", fake_run)

    result = check_compose.check_compose_config()

    assert len(commands) == 1
    command = commands[0]
    assert command.count("-f") == 1
    assert command[command.index("-f") + 1] == str(check_compose.COMPOSE_FILE)
    assert command[-2:] == ("config", "--quiet")
    assert not {"build", "up", "run", "down"}.intersection(command)
    assert result == {
        "status": "passed",
        "deployment_deferred": True,
        "images_built": False,
        "containers_started": False,
    }


def test_runner_waits_for_each_gate_and_returns_bounded_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv(
        subject._REQUIRED_POSTGRES_ENVIRONMENT,
        "postgresql+psycopg://local-test/cad2bim",
    )

    def fake_run_gate(gate: subject.Gate) -> float:
        calls.append(gate.name)
        return 0.125

    monkeypatch.setattr(subject, "_run_gate", fake_run_gate)

    result = subject.run_cad_data_gates()

    assert calls == ["collection_policy", "focused_tests"]
    assert result["status"] == "passed"
    assert result["focused_test_nodes"] == len(subject.FOCUSED_TEST_NODES)
    assert result["gate_seconds"] == {
        "collection_policy": 0.125,
        "focused_tests": 0.125,
    }
    assert set(result) == {
        "status",
        "focused_test_nodes",
        "gate_seconds",
        "duration_seconds",
    }


def test_runner_refuses_to_pass_when_postgres_chain_would_be_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(subject._REQUIRED_POSTGRES_ENVIRONMENT, raising=False)

    with pytest.raises(RuntimeError, match="PostgreSQL/RLS integration chain"):
        subject.run_cad_data_gates()


def test_runner_does_not_require_a_dedicated_python_worker_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        subject._REQUIRED_POSTGRES_ENVIRONMENT,
        "postgresql+psycopg://local-test/cad2bim",
    )
    monkeypatch.delenv("CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE", raising=False)
    calls: list[str] = []

    def fake_run_gate(gate: subject.Gate) -> float:
        calls.append(gate.name)
        return 0.125

    monkeypatch.setattr(subject, "_run_gate", fake_run_gate)

    result = subject.run_cad_data_gates()

    assert calls == ["collection_policy", "focused_tests"]
    assert result["status"] == "passed"


def test_failed_command_prints_only_a_bounded_tail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = "\n".join(f"line-{index}" for index in range(100))

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 2, stdout=output)

    monkeypatch.setattr(subject.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="failed with exit code 2"):
        subject._run_gate(subject.cad_data_gates()[1])

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == subject._FAILURE_TAIL_LINES
    assert lines[0] == "line-20"
    assert lines[-1] == "line-99"


def test_main_emits_one_json_line_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["run_cad_data_gates.py"])

    def fail() -> dict[str, object]:
        raise RuntimeError("focused_tests failed")

    monkeypatch.setattr(subject, "run_cad_data_gates", fail)

    assert subject.main() == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "status": "failed",
        "error": "focused_tests failed",
    }
