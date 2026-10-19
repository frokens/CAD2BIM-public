from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
BACKEND_ROOT = SOURCE_ROOT / "CAD2BIM_backend"
DIRECT_BOOTSTRAP = SOURCE_ROOT / "CAD2BIM_mcp" / "direct" / "bootstrap.py"
BACKEND_BOOTSTRAP = BACKEND_ROOT / "bootstrap.py"
IFC_COMPOSITION = BACKEND_ROOT / "ifc_application_resources.py"
RUNTIME_ROOT = BACKEND_ROOT / "infrastructure" / "ifc_revision_runtime"
RUNTIME_PORT = BACKEND_ROOT / "ports" / "ifc_revision_runtime.py"
WRITE_SERVICES = (
    BACKEND_ROOT / "application" / "ifc_model_service.py",
    BACKEND_ROOT / "application" / "ifc_geometry_basis_service.py",
    BACKEND_ROOT / "application" / "ifc_project_service.py",
    BACKEND_ROOT / "application" / "ifc_spatial_structure_service.py",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _constructor_count(path: Path, constructor: str) -> int:
    return sum(
        1
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == constructor
    )


def test_ifc_revision_runtime_has_no_durable_or_provider_dependencies() -> None:
    forbidden = (
        "CAD2BIM_backend.application",
        "CAD2BIM_backend.infrastructure.postgres",
        "CAD2BIM_backend.infrastructure.storage",
        "CAD2BIM_backend.ports.ifc_model",
        "CAD2BIM_backend.ports.storage",
        "CAD2BIM_mcp",
    )
    violations: list[str] = []
    for path in (*sorted(RUNTIME_ROOT.glob("*.py")), RUNTIME_PORT):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules = (node.module or "",)
            else:
                continue
            for module in modules:
                if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden):
                    violations.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}:{module}")
    assert violations == []


def test_ifc_write_services_do_not_invoke_compiler_mutation_in_backend_process() -> None:
    violations = [
        f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}"
        for path in WRITE_SERVICES
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"apply", "create_revision1"}
    ]
    assert violations == []


def test_shared_composition_owns_one_revision_runtime_and_one_write_coordinator() -> None:
    assert _constructor_count(IFC_COMPOSITION, "IfcOpenShellRevisionRuntime") == 1
    assert _constructor_count(IFC_COMPOSITION, "IfcWriteCoordinator") == 1
    assert _constructor_count(DIRECT_BOOTSTRAP, "create_ifc_application_resources") == 1
    assert _constructor_count(BACKEND_BOOTSTRAP, "create_ifc_application_resources") == 1

    production = tuple(
        path
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    )
    assert sum(_constructor_count(path, "IfcOpenShellRevisionRuntime") for path in production) == 1
    assert sum(_constructor_count(path, "IfcWriteCoordinator") for path in production) == 1


def test_revision_runtime_protocol_contains_no_durable_identity_vocabulary() -> None:
    protocol_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (*sorted(RUNTIME_ROOT.glob("*.py")), RUNTIME_PORT)
    ).lower()
    forbidden_fields = (
        "artifact_version_id",
        "operation_id",
        "provider_alias",
        "revision_id",
        "run_id",
        "storage_key",
        "tenant_id",
        "writer_generation",
    )
    assert [field for field in forbidden_fields if field in protocol_text] == []
