from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
BACKEND_ROOT = SOURCE_ROOT / "CAD2BIM_backend"
MCP_ROOT = SOURCE_ROOT / "CAD2BIM_mcp"
DIRECT_ROOT = MCP_ROOT / "direct"
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
DOCS_ROOT = REPOSITORY_ROOT / "docs"


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    )


def _contains_python_source(path: Path) -> bool:
    if path.is_file():
        return path.suffix == ".py"
    return any(_python_files(path))


def _absolute_from_import(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = list(path.relative_to(SOURCE_ROOT).with_suffix("").parts[:-1])
    package = package[: len(package) - node.level + 1]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _imports(path: Path) -> tuple[tuple[str, tuple[str, ...], int], ...]:
    imports: list[tuple[str, tuple[str, ...], int]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, (), node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                (
                    _absolute_from_import(path, node),
                    tuple(alias.name for alias in node.names),
                    node.lineno,
                )
            )
    return tuple(imports)


def _forbidden_imports(
    root: Path, forbidden_prefixes: tuple[str, ...]
) -> list[str]:
    violations: list[str] = []
    for path in _python_files(root):
        for module, _names, line in _imports(path):
            if any(
                module == prefix or module.startswith(f"{prefix}.")
                for prefix in forbidden_prefixes
            ):
                relative = path.relative_to(REPOSITORY_ROOT)
                violations.append(f"{relative}:{line}: {module}")
    return violations


def test_retired_product_paths_have_no_active_python_source() -> None:
    retired_paths = (
        SOURCE_ROOT / "CAD2BIM_agent",
        BACKEND_ROOT / "api",
        BACKEND_ROOT / "runner",
        BACKEND_ROOT / "operations",
        BACKEND_ROOT / "infrastructure" / "ifc_runtime",
        BACKEND_ROOT / "infrastructure" / "mcp",
        BACKEND_ROOT / "infrastructure" / "models",
        BACKEND_ROOT / "infrastructure" / "react_runtime",
        BACKEND_ROOT / "infrastructure" / "workflow_runtime",
        BACKEND_ROOT / "infrastructure" / "workspace_exec",
        BACKEND_ROOT / "context_resources",
        BACKEND_ROOT / "domain" / "ifc_plan",
    )

    remaining = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in retired_paths
        if path.exists() and _contains_python_source(path)
    ]
    assert remaining == []


def test_retired_product_only_backend_modules_are_absent() -> None:
    retired_modules = (
        "domain/component_authoring.py",
        "domain/component_candidate.py",
        "domain/component_catalog.py",
        "domain/component_family.py",
        "domain/component_source_authority.py",
        "domain/component_types.py",
        "domain/feedback_events.py",
        "domain/ifc_generation.py",
        "domain/operation_protocol.py",
        "domain/resource_contracts.py",
        "domain/session_savepoints.py",
        "domain/stage_handoff.py",
        "domain/task_tree.py",
        "domain/task_tree_compiler.py",
        "domain/task_tree_contracts.py",
        "domain/user_savepoints.py",
        "domain/writer_fence.py",
        "ports/repositories.py",
    )

    present = [name for name in retired_modules if (BACKEND_ROOT / name).exists()]
    assert present == []


def test_mcp_source_tree_has_one_direct_package_and_shared_tool_docs() -> None:
    active_root_entries = {
        path.name
        for path in MCP_ROOT.iterdir()
        if path.name != "__pycache__" and _contains_python_source(path)
    }

    assert active_root_entries == {"__init__.py", "direct", "tool_docs"}
    assert list(MCP_ROOT.glob("direct_*.py")) == []


def test_direct_package_has_all_stable_capability_families() -> None:
    expected_families = {
        "session",
        "source",
        "catalog",
        "operation",
        "query",
        "transform",
        "cad",
        "ifc",
    }

    assert {path.name for path in DIRECT_ROOT.iterdir() if path.is_dir()} >= (
        expected_families
    )
    for family in expected_families:
        assert (DIRECT_ROOT / family / "__init__.py").is_file()


def test_repository_lifecycle_roots_are_bounded() -> None:
    script_directories = {
        path.name for path in SCRIPTS_ROOT.iterdir() if path.is_dir()
    }
    script_files = {
        path.name for path in SCRIPTS_ROOT.iterdir() if path.is_file()
    }
    document_directories = {
        path.name for path in DOCS_ROOT.iterdir() if path.is_dir()
    }

    assert script_directories == {"release", "setup", "maintenance"}
    assert script_files == set()
    assert document_directories == {
        ".archive",
        "agent-rebuild",
        "assets",
        "reference",
        "work",
    }


def test_direct_capability_families_are_the_only_contract_authorities() -> None:
    assert not (DIRECT_ROOT / "contracts.py").exists()
    violations: list[str] = []
    for path in _python_files(DIRECT_ROOT):
        text = path.read_text(encoding="utf-8")
        if "CAD2BIM_mcp.direct.contracts" in text:
            violations.append(str(path.relative_to(REPOSITORY_ROOT)))
        if path.name == "tools.py" and "ruff: noqa" in text and "F401" in text:
            violations.append(str(path.relative_to(REPOSITORY_ROOT)))

    assert violations == []


def test_direct_capability_code_is_owned_by_exact_family_modules() -> None:
    expected_families = {
        "session",
        "source",
        "catalog",
        "operation",
        "query",
        "transform",
        "cad",
        "ifc",
    }
    flat_families = expected_families - {"ifc"}
    response_contract_families = flat_families - {"query"}

    assert not (DIRECT_ROOT / "contracts.py").exists()
    for family in flat_families:
        assert (DIRECT_ROOT / family / "application.py").is_file()
        assert (DIRECT_ROOT / family / "tools.py").is_file()
    for family in response_contract_families:
        assert (DIRECT_ROOT / family / "contracts.py").is_file()

    ifc_root = DIRECT_ROOT / "ifc"
    for retired_root_module in ("application.py", "contracts.py", "tools.py"):
        assert not (ifc_root / retired_root_module).exists()
    assert {path.name for path in ifc_root.iterdir() if path.is_dir()} >= {
        "model",
        "official_docs",
    }
    for module in ("application.py", "contracts.py", "tools.py"):
        assert (ifc_root / "model" / module).is_file()
    for module in ("application.py", "tools.py"):
        assert (ifc_root / "official_docs" / module).is_file()
    assert not (ifc_root / "official_docs" / "contracts.py").exists()


def test_backend_never_imports_the_mcp_adapter() -> None:
    assert _forbidden_imports(BACKEND_ROOT, ("CAD2BIM_mcp",)) == []


def test_production_source_never_imports_test_process_support() -> None:
    assert _forbidden_imports(SOURCE_ROOT, ("tests.process_support",)) == []


def test_backend_layer_import_direction() -> None:
    rules = {
        "domain": (
            "CAD2BIM_backend.application",
            "CAD2BIM_backend.infrastructure",
            "CAD2BIM_backend.api",
            "CAD2BIM_backend.runner",
            "CAD2BIM_mcp",
        ),
        "ports": (
            "CAD2BIM_backend.application",
            "CAD2BIM_backend.infrastructure",
            "CAD2BIM_backend.api",
            "CAD2BIM_mcp",
        ),
        "application": (
            "CAD2BIM_backend.api",
            "CAD2BIM_backend.runner",
            "CAD2BIM_mcp",
        ),
    }

    violations = {
        layer: _forbidden_imports(BACKEND_ROOT / layer, prefixes)
        for layer, prefixes in rules.items()
    }
    assert violations == {"domain": [], "ports": [], "application": []}


def test_application_to_infrastructure_dependencies_do_not_grow() -> None:
    allowed = {
        (
            "cad_measurement_service.py",
            "CAD2BIM_backend.infrastructure.cad_measurement.schemas",
        ): {"MEASUREMENT_LOGICAL_KEYS", "MEASUREMENT_PAYLOAD_SCHEMAS"},
        (
            "cad_measurement_service.py",
            "CAD2BIM_backend.infrastructure.cad_measurement.supervised_worker",
        ): {
            "CadMeasurementWorkerComputeResult",
            "CadMeasurementWorkerError",
            "SubprocessCadMeasurementWorker",
        },
        (
            "cad_measurement_service.py",
            "CAD2BIM_backend.infrastructure",
        ): {"cad_measurement"},
        (
            "data_json_service.py",
            "CAD2BIM_backend.infrastructure.data_query",
        ): {"CursorCodec", "CursorCodecError"},
        (
            "data_query_python_service.py",
            "CAD2BIM_backend.infrastructure.data_query",
        ): {"CursorCodec", "CursorCodecError"},
        (
            "data_query_python_service.py",
            "CAD2BIM_backend.infrastructure.data_query.snapshot_cache",
        ): {
            "QuerySnapshotCache",
            "QuerySnapshotCacheError",
            "QuerySnapshotLease",
            "QuerySnapshotRecord",
            "QuerySnapshotWrite",
        },
        (
            "data_query_service.py",
            "CAD2BIM_backend.infrastructure.data_query",
        ): {"CursorCodec", "CursorCodecError"},
        (
            "data_query_sql_service.py",
            "CAD2BIM_backend.infrastructure.data_query",
        ): {"CursorCodec", "CursorCodecError"},
        (
            "data_query_sql_service.py",
            "CAD2BIM_backend.infrastructure.data_query.snapshot_cache",
        ): {
            "QuerySnapshotCache",
            "QuerySnapshotCacheError",
            "QuerySnapshotLease",
            "QuerySnapshotRecord",
            "QuerySnapshotWrite",
        },
        (
            "data_runtime_service.py",
            "CAD2BIM_backend.infrastructure.cad_l0",
        ): {"CadL0ExtractionError", "CadL0StagingResult", "extract_ascii_dxf_l0"},
        (
            "data_runtime_service.py",
            "CAD2BIM_backend.infrastructure.cad_l0.extractor",
        ): {"DOCUMENT_KEY"},
        (
            "data_runtime_service.py",
            "CAD2BIM_backend.infrastructure.cad_l0.schemas",
        ): {"PAYLOAD_SCHEMAS"},
        (
            "data_transform_service.py",
            "CAD2BIM_backend.infrastructure.source_text.source_patch",
        ): {
            "SourcePatchFailure",
            "SourceEdit",
            "SourcePatchError",
            "apply_text_edits",
        },
        (
            "geometry_query_service.py",
            "CAD2BIM_backend.infrastructure.source_text.source_patch",
        ): {
            "SourceEdit",
            "SourcePatchError",
            "SourcePatchFailure",
            "apply_text_edits",
        },
        (
            "geometry_source_service.py",
            "CAD2BIM_backend.infrastructure.source_text.source_bundle",
        ): {
            "MAX_SOURCE_BUNDLE_MEMBER_BYTES",
            "SourceBundleError",
            "SourceBundleMemberNotFoundError",
            "SourceBundleMemberNotTextError",
            "inspect_source_bundle",
            "normalize_source_bundle_path",
        },
    }
    actual: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in _python_files(BACKEND_ROOT / "application"):
        for module, names, _line in _imports(path):
            if module == "CAD2BIM_backend.infrastructure" or module.startswith(
                "CAD2BIM_backend.infrastructure."
            ):
                relative = path.relative_to(BACKEND_ROOT / "application").as_posix()
                actual[(relative, module)].update(names or ("*",))

    unexpected = {
        key: names - allowed.get(key, set())
        for key, names in actual.items()
        if key not in allowed or not names <= allowed[key]
    }
    assert unexpected == {}


def test_active_business_filenames_do_not_use_delivery_phase_labels() -> None:
    capability_roots = (
        DIRECT_ROOT,
        BACKEND_ROOT / "domain",
        BACKEND_ROOT / "application",
        BACKEND_ROOT / "ports",
        BACKEND_ROOT / "infrastructure" / "cad_l0",
        BACKEND_ROOT / "infrastructure" / "cad_measurement",
        BACKEND_ROOT / "infrastructure" / "data_query",
        BACKEND_ROOT / "infrastructure" / "data_transform",
        BACKEND_ROOT / "infrastructure" / "ifc_official_docs",
        BACKEND_ROOT / "infrastructure" / "langgraph",
        BACKEND_ROOT / "infrastructure" / "postgres",
        BACKEND_ROOT / "infrastructure" / "storage",
    )
    phase_label = re.compile(r"(?:^|[_-])(m2a|m2b|m2c|m3|i0a|i0b0)(?:[_-]|$)")
    violations = [
        str(path.relative_to(REPOSITORY_ROOT))
        for root in capability_roots
        for path in _python_files(root)
        if "migrations" not in path.parts and phase_label.search(path.stem.lower())
    ]

    assert violations == []


def test_durable_ifc_migration_keeps_capability_slug_and_revision() -> None:
    versions = BACKEND_ROOT / "infrastructure" / "postgres" / "migrations" / "versions"
    migrations = tuple(versions.glob("20260830_0064_*.py"))

    assert [path.name for path in migrations] == [
        "20260830_0064_durable_ifc_model_kernel.py"
    ]
    tree = ast.parse(migrations[0].read_text(encoding="utf-8"))
    revision_values = {
        node.target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id in {"revision", "down_revision"}
    }
    assert revision_values == {
        "revision": "20260830_0064",
        "down_revision": "20260828_0063",
    }
