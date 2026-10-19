from __future__ import annotations

import ast
import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
BACKEND_ROOT = SOURCE_ROOT / "CAD2BIM_backend"
DIRECT_BOOTSTRAP = SOURCE_ROOT / "CAD2BIM_mcp" / "direct" / "bootstrap.py"
BACKEND_BOOTSTRAP = BACKEND_ROOT / "bootstrap.py"
IFC_COMPOSITION = BACKEND_ROOT / "ifc_application_resources.py"
IFC_QUERY_APPLICATION = BACKEND_ROOT / "application" / "ifc_query_service.py"
IFC_QUERY_WORKER_ROOT = BACKEND_ROOT / "infrastructure" / "ifc_query"
IFC_QUERY_WORKER_PORT = BACKEND_ROOT / "ports" / "ifc_query_worker.py"
IFC_QUERY_REPOSITORY_PORT = BACKEND_ROOT / "ports" / "ifc_queries.py"
POSTGRES_ROOT = BACKEND_ROOT / "infrastructure" / "postgres"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _absolute_from_import(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = list(path.relative_to(SOURCE_ROOT).with_suffix("").parts[:-1])
    package = package[: len(package) - node.level + 1]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _imports(path: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    imports: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                (
                    _absolute_from_import(path, node),
                    tuple(alias.name for alias in node.names),
                )
            )
    return tuple(imports)


def _constructor_calls(path: Path, constructor: str) -> tuple[ast.Call, ...]:
    return tuple(
        node
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == constructor
    )


def _production_constructor_locations(constructor: str) -> tuple[str, ...]:
    locations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for call in _constructor_calls(path, constructor):
            locations.append(f"{path.relative_to(REPOSITORY_ROOT)}:{call.lineno}")
    return tuple(locations)


def _mixin_private_methods(path: Path) -> set[str]:
    mixin = next(
        node
        for node in _tree(path).body
        if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin")
    )
    return {
        node.name
        for node in mixin.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_")
        and not node.name.startswith("__")
    }


def test_ifc_query_application_does_not_reach_into_infrastructure_or_transform_helpers() -> None:
    violations: list[str] = []
    for module, names in _imports(IFC_QUERY_APPLICATION):
        if module == "CAD2BIM_backend.infrastructure" or module.startswith(
            "CAD2BIM_backend.infrastructure."
        ):
            violations.append(module)
        if module == "CAD2BIM_backend.application.data_transform_service":
            violations.append(module)
        if "data_transform" in module:
            violations.extend(f"{module}:{name}" for name in names if name.startswith("_"))

    assert violations == []


def test_ifc_query_worker_receives_only_process_private_query_inputs() -> None:
    forbidden_import_prefixes = (
        "CAD2BIM_backend.application.artifact_service",
        "CAD2BIM_backend.application.ifc_revision_reader",
        "CAD2BIM_backend.application.ifc_revision_writer",
        "CAD2BIM_backend.application.data_runtime_service",
        "CAD2BIM_backend.domain.data_runtime",
        "CAD2BIM_backend.infrastructure.postgres",
        "CAD2BIM_backend.infrastructure.storage",
        "CAD2BIM_backend.ports.data_runtime",
        "CAD2BIM_backend.ports.ifc_model",
        "CAD2BIM_backend.ports.storage",
    )
    violations = [
        f"{path.relative_to(REPOSITORY_ROOT)}: {module}"
        for path in sorted(IFC_QUERY_WORKER_ROOT.glob("*.py"))
        for module, _names in _imports(path)
        if any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in forbidden_import_prefixes
        )
    ]
    assert violations == []

    worker_class = next(
        node
        for node in _tree(IFC_QUERY_WORKER_ROOT / "adapter.py").body
        if isinstance(node, ast.ClassDef) and node.name == "IfcOpenShellQueryWorker"
    )
    initializer = next(
        node
        for node in worker_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__"
    )
    injected_names = {
        argument.arg
        for argument in (
            *initializer.args.posonlyargs,
            *initializer.args.args,
            *initializer.args.kwonlyargs,
        )
        if argument.arg != "self"
    }
    assert injected_names == {
        "python_executable",
        "supervisor",
        "sandbox",
        "capacity",
        "capacity_root",
    }

    request_class = next(
        node
        for node in _tree(IFC_QUERY_WORKER_PORT).body
        if isinstance(node, ast.ClassDef) and node.name == "IfcQueryWorkerRequest"
    )
    request_fields = {
        node.target.id
        for node in request_class.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert request_fields == {
        "code",
        "ifc_bytes",
        "expected_ifc_sha256",
        "max_result_bytes",
    }

    assert all(module != "CAD2BIM_backend.ports.data_transform"
               for module, _names in _imports(IFC_QUERY_WORKER_PORT))
    # The same private IFC worker now submits declared Data outputs directly;
    # the Query request and attempt lifecycle remain entry-specific.
    assert any(module == "CAD2BIM_backend.ports.data_transform" and
               names == ("ArrowTransformOutput",)
               for module, names in _imports(IFC_QUERY_WORKER_ROOT / "adapter.py"))


def test_ifc_query_repository_port_contains_only_consumed_attempt_lifecycle() -> None:
    repository = next(
        node
        for node in _tree(IFC_QUERY_REPOSITORY_PORT).body
        if isinstance(node, ast.ClassDef) and node.name == "IfcQueryRepository"
    )
    methods = {
        node.name
        for node in repository.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert methods == {
        "prepare_ifc_query_attempt",
        "get_ifc_query_attempt",
        "list_terminal_ifc_query_attempts",
        "commit_ifc_query_attempt_terminal",
    }


def test_direct_bootstrap_owns_one_shared_ifc_revision_reader() -> None:
    """Entry composition delegates to the shared IFC owner without another reader."""
    assert len(_constructor_calls(IFC_COMPOSITION, "IfcRevisionReader")) == 1
    assert len(_production_constructor_locations("IfcRevisionReader")) == 1

    writer_call = _constructor_calls(IFC_COMPOSITION, "IfcRevisionWriter")
    query_call = _constructor_calls(IFC_COMPOSITION, "IfcQueryService")
    assert len(writer_call) == 1
    assert len(query_call) == 1
    for call in (*writer_call, *query_call):
        assert any(
            isinstance(argument, ast.Name) and argument.id == "revision_reader"
            for argument in call.args
        )
    resources_call, = _constructor_calls(IFC_COMPOSITION, "IfcApplicationResources")
    for field in ("revision_reader", "revision_writer", "revision_runtime"):
        assert any(keyword.arg == field and isinstance(keyword.value, ast.Name)
                   and keyword.value.id == field for keyword in resources_call.keywords)
    read_call, = _constructor_calls(IFC_COMPOSITION, "IfcProductsReadService")
    for field, member in (("writer", "revision_writer"), ("runtime", "revision_runtime")):
        assert any(keyword.arg == field and isinstance(keyword.value, ast.Attribute)
                   and isinstance(keyword.value.value, ast.Name)
                   and keyword.value.value.id == "ifc" and keyword.value.attr == member
                   for keyword in read_call.keywords)


def test_direct_bootstrap_is_the_only_ifc_query_composition_root() -> None:
    """Both entry roots delegate query ownership to the one shared IFC factory."""
    for entry in (DIRECT_BOOTSTRAP, BACKEND_BOOTSTRAP):
        assert len(_constructor_calls(entry, "create_ifc_application_resources")) == 1
        assert len(_constructor_calls(entry, "connect_product_read_service")) == 1
        factory_call, = _constructor_calls(entry, "create_ifc_application_resources")
        assert any(keyword.arg == "source_patches" and isinstance(keyword.value, ast.Name)
                   for keyword in factory_call.keywords)
    for constructor in ("IfcOpenShellQueryWorker", "IfcQueryService"):
        assert len(_constructor_calls(IFC_COMPOSITION, constructor)) == 1
        assert len(_production_constructor_locations(constructor)) == 1
    query_call, = _constructor_calls(IFC_COMPOSITION, "IfcQueryService")
    assert any(isinstance(argument, ast.Name) and argument.id == "source_patches"
               for argument in query_call.args)


def test_ifc_query_repository_helpers_cannot_resolve_through_another_mixin() -> None:
    target = POSTGRES_ROOT / "repository_ifc_query.py"
    target_methods = _mixin_private_methods(target)
    collisions = {
        path.name: sorted(target_methods & _mixin_private_methods(path))
        for path in sorted(POSTGRES_ROOT.glob("repository_*.py"))
        if path not in {target, POSTGRES_ROOT / "repository_base.py"}
        and any(
            isinstance(node, ast.ClassDef) and node.name.endswith("Mixin")
            for node in _tree(path).body
        )
    }

    assert {name: methods for name, methods in collisions.items() if methods} == {}


def test_ifc_query_production_filenames_do_not_use_delivery_phase_labels() -> None:
    phase_label = re.compile(
        r"(?:^|[_-])(?:i0[_-]?b3(?:[_-]?0?4)?|b3[_-]?0?4|phase[_-]b3)(?:[_-]|$)"
    )
    violations = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "migrations" not in path.parts and phase_label.search(path.stem.lower())
    ]

    assert violations == []
