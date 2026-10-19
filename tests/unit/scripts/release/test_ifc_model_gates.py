from __future__ import annotations

import subprocess

import pytest

from scripts.release import run_ifc_model_gates as subject
from scripts.release.current_test_manifest import (
    IFC_DIRECT_ENTRY_TEST_NODES,
    IFC_MODEL_KERNEL_TEST_NODES,
)


def test_runner_requires_isolated_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(subject._REQUIRED_POSTGRES_ENVIRONMENT, raising=False)

    with pytest.raises(RuntimeError, match="isolated PostgreSQL"):
        subject.run_ifc_model_gates()


def test_gate_rejects_required_test_skips(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = "..s\n2 passed, 1 skipped in 0.25s\n"

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, stdout=output)

    monkeypatch.setattr(subject.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="skipped required tests"):
        subject._run_gate(subject.ifc_model_gates()[0])

    assert "1 skipped" in capsys.readouterr().out


def test_gate_command_uses_only_the_explicit_manifest() -> None:
    gates = subject.ifc_model_gates()

    assert tuple(gate.name for gate in gates) == ("kernel", "direct_entry")
    assert all("-x" in gate.command and "--tb=short" in gate.command for gate in gates)
    assert all("tests/integration/process/" not in " ".join(gate.command) for gate in gates)
    assert tuple(gates[0].command[-len(IFC_MODEL_KERNEL_TEST_NODES) :]) == (
        IFC_MODEL_KERNEL_TEST_NODES
    )
    assert tuple(gates[1].command[-len(IFC_DIRECT_ENTRY_TEST_NODES) :]) == (
        IFC_DIRECT_ENTRY_TEST_NODES
    )
    assert all("geometry_resource" not in node for node in IFC_DIRECT_ENTRY_TEST_NODES)


def test_manifest_contains_the_ifc_query_architecture_and_capability_tests() -> None:
    assert {
        "tests/contract/architecture/test_ifc_query_boundaries.py",
        "tests/unit/backend/test_ifc_query_contracts.py",
        "tests/unit/backend/test_ifc_query_postgres_schema.py",
        "tests/unit/backend/test_ifc_query_service.py",
        "tests/unit/backend/test_ifc_query_worker.py",
        "tests/unit/backend/test_ifc_revision_reader.py",
        "tests/integration/backend/test_ifc_query_postgres.py",
    } <= set(IFC_MODEL_KERNEL_TEST_NODES)
    assert "tests/unit/mcp/test_direct_ifc_query_provider.py" in IFC_DIRECT_ENTRY_TEST_NODES


def test_manifest_contains_product_family_kernel_and_provider_tests() -> None:
    assert {
        "tests/unit/backend/test_product_family_contracts.py",
        "tests/unit/backend/test_product_family_asset_service.py",
        "tests/unit/backend/test_product_family_authoring_contracts.py",
        "tests/unit/backend/test_product_family_authoring_persistence.py",
        "tests/unit/backend/test_product_family_authoring_service.py",
        "tests/unit/backend/test_product_family_catalog_service.py",
        "tests/unit/backend/test_product_family_operation_projection.py",
        "tests/unit/backend/test_product_family_postgres_schema.py",
        "tests/unit/backend/test_product_realization_runtime.py",
        "tests/unit/backend/test_product_realization_frames.py",
        "tests/unit/backend/test_product_realization_legacy_snapshot.py",
        "tests/contract/architecture/test_product_family_boundaries.py",
        "tests/integration/backend/test_product_family_migrations.py",
        "tests/integration/backend/test_authoring_draft_preview_publication_postgres.py",
        "tests/integration/backend/test_product_family_postgres.py",
    } <= set(IFC_MODEL_KERNEL_TEST_NODES)
    assert (
        "tests/unit/mcp/test_direct_ifc_product_family_provider.py" in IFC_DIRECT_ENTRY_TEST_NODES
    )
