from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"


def test_active_source_tree_has_only_the_current_product_roots() -> None:
    required_paths = (
        "src/CAD2BIM_backend/domain",
        "src/CAD2BIM_backend/application",
        "src/CAD2BIM_backend/ports",
        "src/CAD2BIM_backend/infrastructure/postgres",
        "src/CAD2BIM_backend/infrastructure/storage",
        "src/CAD2BIM_backend/infrastructure/langgraph",
        "src/CAD2BIM_mcp/direct/entry.py",
        "src/CAD2BIM_mcp/direct/bootstrap.py",
        "src/CAD2BIM_mcp/direct/server.py",
        "src/CAD2BIM_mcp/direct/cad",
        "src/CAD2BIM_mcp/direct/catalog",
        "src/CAD2BIM_mcp/direct/ifc",
        "src/CAD2BIM_mcp/direct/operation",
        "src/CAD2BIM_mcp/direct/query",
        "src/CAD2BIM_mcp/direct/session",
        "src/CAD2BIM_mcp/direct/source",
        "src/CAD2BIM_mcp/direct/transform",
    )

    missing = [path for path in required_paths if not (REPOSITORY_ROOT / path).exists()]
    assert not missing


def test_retired_product_source_roots_are_physically_absent() -> None:
    retired_paths = (
        "src/CAD2BIM_agent",
        "src/CAD2BIM_backend/api",
        "src/CAD2BIM_backend/runner",
        "src/CAD2BIM_backend/infrastructure/mcp",
        "src/CAD2BIM_backend/infrastructure/workspace_exec",
        "src/CAD2BIM_backend/context_resources",
        "src/CAD2BIM_backend/domain/ifc_plan",
        "src/CAD2BIM_backend/domain/ifc_generation.py",
        "src/CAD2BIM_backend/domain/operation_protocol.py",
        "src/CAD2BIM_backend/domain/task_tree.py",
        "src/CAD2BIM_backend/domain/task_tree_compiler.py",
        "src/CAD2BIM_backend/domain/task_tree_contracts.py",
        "src/CAD2BIM_backend/ports/repositories.py",
        "src/CAD2BIM_mcp/cad_tools",
        "src/CAD2BIM_mcp/doc_tools",
        "src/CAD2BIM_mcp/ifc_tools",
        "src/CAD2BIM_mcp/descriptions",
        "src/CAD2BIM_mcp/http_entry.py",
        "src/CAD2BIM_mcp/internal_session_api.py",
        "src/CAD2BIM_mcp/main.py",
        "src/CAD2BIM_mcp/workspace_paths.py",
        "src/CAD2BIM_mcp/workspace_service.py",
        "src/CAD2BIM_mcp/write_receipts.py",
    )

    present = [path for path in retired_paths if (REPOSITORY_ROOT / path).exists()]
    assert not present


def test_active_source_imports_do_not_reach_retired_product_namespaces() -> None:
    retired_imports = (
        "CAD2BIM_agent",
        "CAD2BIM_backend.api",
        "CAD2BIM_backend.runner",
        "CAD2BIM_backend.infrastructure.mcp",
        "CAD2BIM_backend.infrastructure.workspace_exec",
        "CAD2BIM_mcp.cad_tools",
        "CAD2BIM_mcp.doc_tools",
        "CAD2BIM_mcp.ifc_tools",
        "CAD2BIM_mcp.descriptions",
        "CAD2BIM_mcp.http_entry",
        "CAD2BIM_mcp.internal_session_api",
        "CAD2BIM_mcp.main",
        "CAD2BIM_mcp.workspace_paths",
        "CAD2BIM_mcp.workspace_service",
        "CAD2BIM_mcp.write_receipts",
    )
    violations: list[str] = []

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        for imported in imports:
            if any(
                imported == retired or imported.startswith(f"{retired}.")
                for retired in retired_imports
            ):
                relative = path.relative_to(REPOSITORY_ROOT).as_posix()
                violations.append(f"{relative}: {imported}")

    assert not violations


def test_retired_test_and_gate_entrypoints_are_physically_absent() -> None:
    retired_paths = (
        "tests/agent",
        "tests/unit/agent",
        "tests/mcp",
        "tests/fakes",
        "scripts/release/run_cad_data_phase1_gates.py",
        "scripts/release/run_i0a_gates.py",
        "scripts/release/run_i0b0_gates.py",
        "scripts/release/run_offline_release_gates.py",
        "scripts/release/run_postgres_integration.py",
    )

    present = [path for path in retired_paths if (REPOSITORY_ROOT / path).exists()]
    assert not present
