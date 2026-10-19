from __future__ import annotations

import ast
import importlib.util
import json
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from CAD2BIM_backend.infrastructure.data_transform import python_worker_environment
from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    DATA_PYTHON_CHILD_SOURCE_PATHS,
    MAX_ATTESTATION_BYTES,
    DataPythonWorkerDiagnosticCategory,
    DataPythonWorkerEnvironmentError,
    PythonWorkerEnvironmentAttestation,
    attestation_path,
    data_python_worker_recovery_commands,
)
from scripts.setup import setup_data_python_worker_env as subject


PROJECT_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_APPROVED = {
    "duckdb",
    "jsonschema",
    "networkx",
    "numpy",
    "pandas",
    "polars",
    "pyarrow",
    "pydantic",
    "pyproj",
    "rapidfuzz",
    "scipy",
    "shapely",
}


def test_approved_computation_roots_are_exact_and_version_locked() -> None:
    lock_path = PROJECT_ROOT / "uv.lock"
    requirements = subject.locked_requirements(lock_path)
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    with lock_path.open("rb") as stream:
        lock = tomllib.load(stream)
    project_names = {
        re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0]
        for requirement in project["project"]["dependencies"]
    }
    conda_names = {
        re.split(r"[<>=!~\[]", line.removeprefix("  - "), maxsplit=1)[0]
        for line in (PROJECT_ROOT / "environment.conda.yml")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("  - ")
    }
    root_lock = next(package for package in lock["package"] if package["name"] == "cad2bim")
    root_lock_names = {dependency["name"] for dependency in root_lock["dependencies"]}

    assert set(subject.APPROVED_PACKAGES) == EXPECTED_APPROVED
    assert EXPECTED_APPROVED.issubset(project_names)
    assert EXPECTED_APPROVED.issubset(conda_names)
    assert EXPECTED_APPROVED.issubset(root_lock_names)
    assert EXPECTED_APPROVED.issubset(requirements)
    assert all(requirements[name] for name in EXPECTED_APPROVED)
    assert subject.expected_environment(lock_path) == {
        **requirements,
        "cad2bim": project["project"]["version"],
    }


def test_locked_closure_excludes_product_and_service_libraries() -> None:
    requirements = subject.locked_requirements(PROJECT_ROOT / "uv.lock")

    assert not {
        "alembic",
        "cadquery",
        "fastapi",
        "fastmcp",
        "httpx",
        "ifcopenshell",
        "psycopg",
        "requests",
        "sqlalchemy",
    }.intersection(requirements)


def test_existing_non_venv_directory_is_never_replaced(tmp_path: Path) -> None:
    target = tmp_path / "worker-env"
    target.mkdir()
    (target / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to replace non-venv"):
        subject._assert_safe_env_dir(target)

    assert (target / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_recovery_commands_exactly_target_the_declared_environment_and_lock(
    tmp_path: Path,
) -> None:
    env_dir = tmp_path / "worker env"
    python = env_dir / "bin" / "python"
    source_root = tmp_path / "source tree"
    lock = source_root / "uv.lock"
    setup_script = source_root / "scripts/setup/setup_data_python_worker_env.py"
    setup_script.parent.mkdir(parents=True)
    setup_script.write_text("# setup entry\n", encoding="utf-8")
    lock.write_text("revision = 1\n", encoding="utf-8")

    build, check = data_python_worker_recovery_commands(python, lock_path=lock)
    common = [
        str(Path(sys.executable).absolute()),
        str(setup_script),
        "--env-dir",
        str(env_dir),
        "--lock",
        str(lock),
    ]
    assert shlex.split(build) == [*common, "--python", "3.11"]
    assert shlex.split(check) == [*common, "--check"]


def test_recovery_command_preserves_the_host_venv_launcher_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_python = tmp_path / "host-venv" / "bin" / "python"
    base_python = tmp_path / "base" / "python3.11"
    base_python.parent.mkdir(parents=True)
    base_python.touch()
    host_python.parent.mkdir(parents=True)
    host_python.symlink_to(base_python)
    worker_python = tmp_path / "worker-venv" / "bin" / "python"
    monkeypatch.setattr(python_worker_environment.sys, "executable", str(host_python))

    build, _check = data_python_worker_recovery_commands(
        worker_python,
        lock_path=PROJECT_ROOT / "uv.lock",
    )

    assert shlex.split(build)[0] == str(host_python)
    assert shlex.split(build)[0] != str(base_python)


def test_installed_layout_does_not_invent_checkout_recovery_paths(tmp_path: Path) -> None:
    worker_python = tmp_path / "worker-venv" / "bin" / "python"
    unowned_lock = tmp_path / "installed" / "uv.lock"

    assert (
        data_python_worker_recovery_commands(
            worker_python,
            lock_path=unowned_lock,
        )
        == ()
    )


def test_builder_installs_current_project_without_product_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(subject, "locked_requirements", lambda _lock: {"numpy": "2.2.6"})
    monkeypatch.setattr(
        subject,
        "expected_environment",
        lambda _lock: {"numpy": "2.2.6", "cad2bim": "0.1.0"},
    )
    monkeypatch.setattr(subject, "_run", commands.append)
    project_sdist = tmp_path / "cad2bim-0.1.0.tar.gz"
    monkeypatch.setattr(
        subject,
        "_build_project_sdist",
        lambda **_kwargs: project_sdist,
    )
    runtime = subject.PythonRuntimeIdentity(
        implementation="cpython",
        version=(3, 11, 9),
        cache_tag="cpython-311",
        abi_flags="",
        executable=str(tmp_path / "worker-env" / "bin" / "python"),
        prefix=str(tmp_path / "worker-env"),
        base_prefix="/trusted/base-python",
    )
    monkeypatch.setattr(
        subject,
        "verify_environment",
        lambda _env, _expected, **_kwargs: runtime,
    )
    written: list[tuple[Path, PythonWorkerEnvironmentAttestation]] = []
    monkeypatch.setattr(
        subject,
        "write_attestation",
        lambda python, attestation: written.append((python, attestation)),
    )

    subject.build_environment(
        env_dir=tmp_path / "worker-env",
        lock_path=PROJECT_ROOT / "uv.lock",
    )

    install = next(command for command in commands if command[:3] == ["uv", "pip", "install"])
    assert "--no-deps" in install
    assert install[-1] == str(project_sdist)
    assert written[0][0] == tmp_path / "worker-env" / "bin" / "python"
    assert written[0][1].runtime == runtime
    assert written[0][1].distributions == {"numpy": "2.2.6", "cad2bim": "0.1.0"}


@pytest.mark.parametrize(
    ("failed_step", "expected_category"),
    (
        ("venv", DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH),
        ("sync", DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH),
        ("source", DataPythonWorkerDiagnosticCategory.SOURCE_MISMATCH),
    ),
)
def test_builder_returns_stable_diagnostic_for_each_environment_stage(
    failed_step: str,
    expected_category: DataPythonWorkerDiagnosticCategory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text("revision = 1\n", encoding="utf-8")
    setup_script = tmp_path / "scripts/setup/setup_data_python_worker_env.py"
    setup_script.parent.mkdir(parents=True)
    setup_script.write_text("# setup entry\n", encoding="utf-8")
    monkeypatch.setattr(subject, "locked_requirements", lambda _lock: {"numpy": "2.2.6"})
    monkeypatch.setattr(
        subject,
        "expected_environment",
        lambda _lock: {"numpy": "2.2.6", "cad2bim": "0.1.0"},
    )
    call_count = 0

    def run(command: list[str]) -> None:
        nonlocal call_count
        call_count += 1
        if (failed_step == "venv" and call_count == 1) or (
            failed_step == "sync" and call_count == 2
        ):
            raise subprocess.CalledProcessError(1, command)

    def fail_sdist(**_kwargs: object) -> Path:
        raise RuntimeError("sdist failed")

    monkeypatch.setattr(subject, "_run", run)
    if failed_step == "source":
        monkeypatch.setattr(subject, "_build_project_sdist", fail_sdist)

    with pytest.raises(DataPythonWorkerEnvironmentError) as captured:
        subject.build_environment(
            env_dir=tmp_path / "worker-env",
            lock_path=lock,
        )

    assert captured.value.category is expected_category
    assert len(captured.value.recovery_commands) == 2


def test_current_distribution_runs_real_isolated_child_readiness() -> None:
    subject._probe_child_readiness(Path(sys.executable))


def _write_execution_sources(root: Path) -> Path:
    package_root = root / "CAD2BIM_backend"
    for relative_value in DATA_PYTHON_CHILD_SOURCE_PATHS:
        relative = Path(relative_value)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {relative_value}\n", encoding="utf-8")
    return package_root


@pytest.mark.parametrize(
    "source_paths",
    (
        ("CAD2BIM_backend/../outside.py",),
        ("/CAD2BIM_backend/worker.py",),
        ("CAD2BIM_mcp/tools.py",),
        ("CAD2BIM_backend/worker.txt",),
        (
            "CAD2BIM_backend/b.py",
            "CAD2BIM_backend/a.py",
        ),
        (
            "CAD2BIM_backend/a.py",
            "CAD2BIM_backend/a.py",
        ),
    ),
)
def test_execution_source_manifest_rejects_invalid_paths(
    source_paths: tuple[str, ...],
) -> None:
    with pytest.raises(RuntimeError, match="source manifest"):
        python_worker_environment._execution_source_files(
            source_paths,
            package_roots=(PROJECT_ROOT / "src" / "CAD2BIM_backend",),
        )


def test_execution_source_manifest_rejects_ambiguous_package_roots(tmp_path: Path) -> None:
    first_package_root = _write_execution_sources(tmp_path / "first")
    second_package_root = _write_execution_sources(tmp_path / "second")

    with pytest.raises(RuntimeError, match="execution source is ambiguous"):
        python_worker_environment._execution_source_files(
            DATA_PYTHON_CHILD_SOURCE_PATHS,
            package_roots=(first_package_root, second_package_root),
        )


def test_execution_source_manifest_rejects_symlinked_source(tmp_path: Path) -> None:
    package_root = _write_execution_sources(tmp_path)
    declared = tmp_path / DATA_PYTHON_CHILD_SOURCE_PATHS[0]
    outside = tmp_path / "outside.py"
    outside.write_text("# outside the trusted package\n", encoding="utf-8")
    declared.unlink()
    declared.symlink_to(outside)

    with pytest.raises(RuntimeError, match="execution source is not a regular file"):
        python_worker_environment._execution_source_files(
            DATA_PYTHON_CHILD_SOURCE_PATHS,
            package_roots=(package_root,),
        )


def test_execution_source_manifest_allows_an_internal_directory_symlink(tmp_path: Path) -> None:
    package_root = tmp_path / "CAD2BIM_backend"
    actual_directory = package_root / "actual"
    actual_directory.mkdir(parents=True)
    source = actual_directory / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (package_root / "linked").symlink_to(actual_directory, target_is_directory=True)

    resolved = python_worker_environment._execution_source_files(
        ("CAD2BIM_backend/linked/source.py",),
        package_roots=(package_root,),
    )

    assert resolved == (("CAD2BIM_backend/linked/source.py", source),)


def test_execution_source_manifest_rejects_an_escaping_directory_symlink(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "CAD2BIM_backend"
    package_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package_root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="execution source escapes its package"):
        python_worker_environment._execution_source_files(
            ("CAD2BIM_backend/linked/source.py",),
            package_roots=(package_root,),
        )


def _fresh_loaded_execution_sources() -> dict[str, list[str]]:
    code = """
import importlib
import json
import sys
from pathlib import Path

targets = (
    'CAD2BIM_backend.infrastructure.data_transform.python_arrow_child',
)
groups = {}
for target in targets:
    for name in tuple(sys.modules):
        if name.startswith('CAD2BIM_backend'):
            del sys.modules[name]
    importlib.import_module(target)
    loaded = set()
    for module in sys.modules.values():
        raw = getattr(module, '__file__', None)
        if raw is None:
            continue
        path = Path(raw).resolve()
        if 'CAD2BIM_backend' not in path.parts:
            continue
        index = path.parts.index('CAD2BIM_backend')
        loaded.add(Path(*path.parts[index:]).as_posix())
    groups[target] = sorted(loaded)
print('EXECUTION_SOURCES=' + json.dumps(groups, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = next(
        line.removeprefix("EXECUTION_SOURCES=")
        for line in result.stdout.splitlines()
        if line.startswith("EXECUTION_SOURCES=")
    )
    value = json.loads(payload)
    assert isinstance(value, dict)
    return value


def test_sandbox_source_manifest_matches_child_imports() -> None:
    groups = _fresh_loaded_execution_sources()
    assert set(groups["CAD2BIM_backend.infrastructure.data_transform.python_arrow_child"]) == set(
        DATA_PYTHON_CHILD_SOURCE_PATHS
    )


def _module_name(relative_value: str) -> str:
    value = relative_value.removesuffix(".py").replace("/", ".")
    return value.removesuffix(".__init__")


def _runtime_imports(node: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
        return []
    if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
        if node.test.id == "TYPE_CHECKING":
            return [item for branch in node.orelse for item in _runtime_imports(branch)]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [node]
    return [
        imported for child in ast.iter_child_nodes(node) for imported in _runtime_imports(child)
    ]


def _required_local_source_paths(
    imported_module: str,
    *,
    source_root: Path,
) -> set[str]:
    if not imported_module.startswith("CAD2BIM_backend"):
        return set()
    parts = imported_module.split(".")
    required: set[str] = set()
    for boundary in range(1, len(parts) + 1):
        package_init = source_root.joinpath(*parts[:boundary], "__init__.py")
        if package_init.is_file():
            required.add(package_init.relative_to(source_root).as_posix())
    module_file = source_root.joinpath(*parts).with_suffix(".py")
    if module_file.is_file():
        required.add(module_file.relative_to(source_root).as_posix())
    return required


def _missing_local_source_paths(
    source_paths: tuple[str, ...],
    *,
    source_root: Path,
) -> tuple[str, ...]:
    declared = set(source_paths)
    required: set[str] = set()
    for relative_value in source_paths:
        source = source_root / relative_value
        module_name = _module_name(relative_value)
        package_name = (
            module_name
            if relative_value.endswith("/__init__.py")
            else module_name.rpartition(".")[0]
        )
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=relative_value)
        for imported in _runtime_imports(tree):
            if isinstance(imported, ast.Import):
                module_names = [alias.name for alias in imported.names]
            else:
                if imported.level:
                    relative_name = "." * imported.level + (imported.module or "")
                    imported_package = importlib.util.resolve_name(
                        relative_name,
                        package_name,
                    )
                else:
                    imported_package = imported.module or ""
                module_names = [imported_package]
                module_names.extend(
                    f"{imported_package}.{alias.name}"
                    for alias in imported.names
                    if alias.name != "*"
                )
            for imported_module in module_names:
                required.update(
                    _required_local_source_paths(imported_module, source_root=source_root)
                )
    return tuple(sorted(required - declared))


def test_execution_source_manifest_closes_static_local_imports() -> None:
    missing = _missing_local_source_paths(
        DATA_PYTHON_CHILD_SOURCE_PATHS,
        source_root=PROJECT_ROOT / "src",
    )

    assert missing == (), f"Data Python execution source manifest omits local imports: {missing}"


def test_static_closure_detects_unlisted_lazy_from_and_relative_imports(tmp_path: Path) -> None:
    package = tmp_path / "CAD2BIM_backend" / "example"
    package.mkdir(parents=True)
    declared = "CAD2BIM_backend/example/__init__.py"
    (tmp_path / declared).write_text(
        "def load_later():\n"
        "    from . import omitted_relative\n"
        "    from CAD2BIM_backend.example import omitted_absolute\n",
        encoding="utf-8",
    )
    (package / "omitted_relative.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "omitted_absolute.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert _missing_local_source_paths((declared,), source_root=tmp_path) == (
        "CAD2BIM_backend/example/omitted_absolute.py",
        "CAD2BIM_backend/example/omitted_relative.py",
    )


def test_verifier_rejects_extra_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_dir = tmp_path / "worker-env"
    python = env_dir / ("Scripts/python.exe" if subject.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(
        subject,
        "_installed_environment",
        lambda _python, **_kwargs: (
            subject.PythonRuntimeIdentity(
                implementation="cpython",
                version=(3, 11, 9),
                cache_tag="cpython-311",
                abi_flags="",
                executable=str(python),
                prefix=str(env_dir),
                base_prefix="/trusted/base-python",
            ),
            {"numpy": "2.2.6", "httpx": "0.28.1"},
        ),
    )

    with pytest.raises(
        DataPythonWorkerEnvironmentError,
        match=r"extra=\['httpx'\]",
    ) as captured:
        subject.verify_environment(env_dir, {"numpy": "2.2.6"})
    assert captured.value.category is DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH
    assert len(captured.value.recovery_commands) == 2


def test_attestation_is_bounded_and_binds_runtime_roots_and_dependencies(
    tmp_path: Path,
) -> None:
    env_dir = tmp_path / "worker-env"
    python = env_dir / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    lock = tmp_path / "uv.lock"
    lock.write_text("revision = 3\n", encoding="utf-8")
    runtime = subject.PythonRuntimeIdentity(
        implementation="cpython",
        version=(3, 11, 12),
        cache_tag="cpython-311",
        abi_flags="",
        executable=str(python),
        prefix=str(env_dir),
        base_prefix=str(tmp_path / "base-python"),
    )
    attestation = subject.create_attestation(
        expected_distributions={"cad2bim": "0.1.0", "numpy": "2.2.6"},
        runtime=runtime,
    )

    subject.write_attestation(python, attestation)
    payload = json.loads(attestation_path(python).read_text())
    payload.update(cad2bim_sha256="retired source fingerprint", lock_sha256="retired lock fingerprint")
    attestation_path(python).write_text(json.dumps(payload))
    loaded = subject.load_attestation(
        python,
    )

    assert loaded == attestation
    assert loaded.approved_packages == subject.APPROVED_PACKAGES
    assert attestation_path(python).stat().st_size <= MAX_ATTESTATION_BYTES

    subject.write_attestation(
        python,
        replace(attestation, approved_packages=(*subject.APPROVED_PACKAGES, "httpx")),
    )
    with pytest.raises(RuntimeError, match="approved roots have drifted"):
        subject.load_attestation(
            python,
        )


def test_check_mode_reuses_environment_after_unrelated_lock_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_dir = tmp_path / "worker-env"
    python = env_dir / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    lock = tmp_path / "uv.lock"
    lock.write_text("revision = 3\n", encoding="utf-8")
    setup_script = tmp_path / "scripts/setup/setup_data_python_worker_env.py"
    setup_script.parent.mkdir(parents=True)
    setup_script.write_text("# setup entry\n", encoding="utf-8")
    expected = {"cad2bim": "0.1.0"}
    subject.write_attestation(
        python,
        subject.create_attestation(
            expected_distributions=expected,
            runtime=subject.PythonRuntimeIdentity(
                implementation="cpython",
                version=(3, 11, 12),
                cache_tag="cpython-311",
                abi_flags="",
                executable=str(python),
                prefix=str(env_dir),
                base_prefix=str(tmp_path / "base-python"),
            ),
        ),
    )
    lock.write_text("revision = 4\n", encoding="utf-8")
    arguments = SimpleNamespace(
        env_dir=env_dir,
        lock=lock,
        check=True,
        uv="uv",
        python="3.11",
    )
    monkeypatch.setattr(
        subject,
        "_parser",
        lambda: SimpleNamespace(parse_args=lambda: arguments),
    )
    monkeypatch.setattr(subject, "expected_environment", lambda _lock: {"cad2bim": "0.2.0"})

    verified: list[Path] = []
    monkeypatch.setattr(subject, "verify_attested_environment", lambda python, *_args, **_kwargs: verified.append(python))
    monkeypatch.setattr(subject, "_check_child_readiness", lambda *_args, **_kwargs: None)

    assert subject.main() == 0
    assert verified == [python]
