"""Deployment attestation for the dedicated Direct Python/Arrow environment."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping

from CAD2BIM_backend.infrastructure.data_transform.capabilities import (
    PYTHON_ARROW_APPROVED_LIBRARIES,
)


APPROVED_PACKAGES = tuple(sorted(PYTHON_ARROW_APPROVED_LIBRARIES))
ATTESTATION_FILENAME = ".cad2bim-data-python-worker.json"
MAX_ATTESTATION_BYTES = 64 * 1024
PYTHON_MINOR = (3, 11)
TRUSTED_PROJECT_DISTRIBUTION = "cad2bim"
_TRUSTED_PROJECT_PACKAGE = "CAD2BIM_backend"

# Only these local sources are readable by the sandboxed child.
# This is an access boundary, never an environment freshness fingerprint.
DATA_PYTHON_CHILD_SOURCE_PATHS = (
    "CAD2BIM_backend/infrastructure/__init__.py",
    "CAD2BIM_backend/infrastructure/data_transform/__init__.py",
    "CAD2BIM_backend/infrastructure/data_transform/capabilities.py",
    "CAD2BIM_backend/infrastructure/data_transform/python_arrow_child.py",
    "CAD2BIM_backend/infrastructure/data_transform/worker_protocol.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/__init__.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/protocol.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/python_user_code.py",
)


class DataPythonWorkerDiagnosticCategory(StrEnum):
    SOURCE_MISMATCH = "DATA_PYTHON_WORKER_SOURCE_MISMATCH"
    DEPENDENCY_MISMATCH = "DATA_PYTHON_WORKER_DEPENDENCY_MISMATCH"
    INTERPRETER_MISMATCH = "DATA_PYTHON_WORKER_INTERPRETER_MISMATCH"
    SANDBOX_FAILURE = "DATA_PYTHON_WORKER_SANDBOX_FAILURE"
    CHILD_READINESS_FAILED = "DATA_PYTHON_WORKER_CHILD_READINESS_FAILED"
    ENVIRONMENT_INVALID = "DATA_PYTHON_WORKER_ENVIRONMENT_INVALID"


class DataPythonWorkerEnvironmentError(RuntimeError):
    """Stable local-operator diagnostic shared by setup, check, and launch."""

    def __init__(
        self,
        category: DataPythonWorkerDiagnosticCategory,
        detail: str,
        *,
        recovery_commands: tuple[str, ...] = (),
    ) -> None:
        self.category = category
        self.detail = " ".join(detail.splitlines())
        self.recovery_commands = recovery_commands
        recovery = ""
        if recovery_commands:
            recovery = "; recovery: " + " ; then: ".join(recovery_commands)
        super().__init__(f"{category.value}: {self.detail}{recovery}")


def data_python_worker_recovery_commands(
    python_executable: str | Path,
    *,
    lock_path: Path | None = None,
) -> tuple[str, ...]:
    """Return exact rebuild and verification commands for one declared Worker."""

    worker_python = Path(python_executable)
    env_dir = _environment_prefix_from_executable(worker_python)
    source_project_root = Path(__file__).resolve().parents[4]
    effective_lock = Path(os.path.abspath(lock_path or source_project_root / "uv.lock"))
    project_root = effective_lock.parent
    setup_script = project_root / "scripts/setup/setup_data_python_worker_env.py"
    if not effective_lock.is_file() or not setup_script.is_file():
        return ()
    common = (
        os.path.abspath(sys.executable),
        str(setup_script),
        "--env-dir",
        str(env_dir),
        "--lock",
        str(effective_lock),
    )
    return (
        shlex.join((*common, "--python", "3.11")),
        shlex.join((*common, "--check")),
    )


def data_python_worker_environment_error(
    category: DataPythonWorkerDiagnosticCategory,
    detail: str,
    *,
    python_executable: str | Path | None,
    lock_path: Path | None = None,
) -> DataPythonWorkerEnvironmentError:
    try:
        commands = (
            ()
            if python_executable is None
            else data_python_worker_recovery_commands(
                python_executable,
                lock_path=lock_path,
            )
        )
    except RuntimeError:
        commands = ()
    if category is DataPythonWorkerDiagnosticCategory.SANDBOX_FAILURE:
        detail = (
            f"{detail}; recovery requires Darwin with executable "
            "/usr/bin/sandbox-exec before rerunning the environment check"
        )
        commands = commands[-1:]
    if python_executable is not None and not commands:
        detail = (
            f"{detail}; recovery requires uv.lock and "
            "scripts/setup/setup_data_python_worker_env.py from the trusted project checkout"
        )
    return DataPythonWorkerEnvironmentError(
        category,
        detail,
        recovery_commands=commands,
    )


def normalize_distribution_name(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


@dataclass(frozen=True, slots=True)
class PythonRuntimeIdentity:
    implementation: str
    version: tuple[int, int, int]
    cache_tag: str
    abi_flags: str
    executable: str
    prefix: str
    base_prefix: str


@dataclass(frozen=True, slots=True)
class PythonWorkerEnvironmentAttestation:
    approved_packages: tuple[str, ...]
    distributions: Mapping[str, str]
    runtime: PythonRuntimeIdentity


def _validate_source_manifest(source_paths: tuple[str, ...]) -> None:
    if not source_paths or source_paths != tuple(sorted(set(source_paths))):
        raise RuntimeError("Data Python Worker source manifest must be sorted and unique")
    for value in source_paths:
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or relative.as_posix() != value
            or not value.endswith(".py")
            or not relative.parts
            or relative.parts[0] != _TRUSTED_PROJECT_PACKAGE
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RuntimeError("Data Python Worker source manifest contains an invalid path")


def _trusted_project_roots() -> tuple[Path, ...]:
    spec = importlib.util.find_spec(_TRUSTED_PROJECT_PACKAGE)
    locations = None if spec is None else spec.submodule_search_locations
    if not locations:
        raise RuntimeError("trusted cad2bim package 'CAD2BIM_backend' is unavailable")
    return tuple(
        sorted(
            {Path(location).resolve(strict=True) for location in locations},
            key=lambda path: os.path.normcase(os.fspath(path)),
        )
    )


def _execution_source_files(
    source_paths: tuple[str, ...],
    *,
    package_roots: tuple[Path, ...] | None = None,
) -> tuple[tuple[str, Path], ...]:
    _validate_source_manifest(source_paths)
    roots = package_roots if package_roots is not None else _trusted_project_roots()
    if not roots:
        raise RuntimeError("trusted cad2bim package 'CAD2BIM_backend' is unavailable")
    normalized_roots = tuple(dict.fromkeys(root.resolve(strict=True) for root in roots))
    resolved_sources: list[tuple[str, Path]] = []
    for relative_value in source_paths:
        relative = PurePosixPath(relative_value)
        package_relative = Path(*relative.parts[1:])
        candidates: set[Path] = set()
        for root in normalized_roots:
            candidate = root / package_relative
            if not candidate.exists():
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise RuntimeError(
                    f"Data Python Worker execution source is not a regular file: {relative_value}"
                )
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise RuntimeError(
                    f"Data Python Worker execution source escapes its package: {relative_value}"
                )
            candidates.add(resolved)
        if not candidates:
            raise RuntimeError(f"Data Python Worker execution source is missing: {relative_value}")
        if len(candidates) != 1:
            raise RuntimeError(
                f"Data Python Worker execution source is ambiguous: {relative_value}"
            )
        resolved_sources.append((relative_value, candidates.pop()))
    return tuple(resolved_sources)


def execution_source_files(
    source_paths: tuple[str, ...] = DATA_PYTHON_CHILD_SOURCE_PATHS,
) -> tuple[Path, ...]:
    """Resolve one closed execution-source projection for sandbox and tests."""

    return tuple(path for _relative, path in _execution_source_files(source_paths))


def attestation_path(python: Path) -> Path:
    return _environment_prefix_from_executable(python) / ATTESTATION_FILENAME


def _environment_prefix_from_executable(python: Path) -> Path:
    absolute = Path(os.path.abspath(python))
    if absolute.name == "python.exe" and absolute.parent.name == "Scripts":
        return absolute.parent.parent
    if absolute.parent.name == "bin" and absolute.name.startswith("python"):
        return absolute.parent.parent
    raise RuntimeError("Data Python Worker executable is not a venv interpreter path")


def _path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(value))


def probe_environment(
    python: Path,
    *,
    recovery_lock_path: Path | None = None,
) -> tuple[PythonRuntimeIdentity, dict[str, str]]:
    probe = (
        "import importlib, importlib.metadata as m, json, os, sys; "
        f"[importlib.import_module(name) for name in {APPROVED_PACKAGES!r}]; "
        "print(json.dumps({"
        "'runtime': {'implementation': sys.implementation.name, "
        "'version': list(sys.version_info[:3]), "
        "'cache_tag': sys.implementation.cache_tag or '', "
        "'abi_flags': getattr(sys, 'abiflags', ''), "
        "'executable': os.path.abspath(sys.executable), "
        "'prefix': os.path.realpath(sys.prefix), "
        "'base_prefix': os.path.realpath(sys.base_prefix)}, "
        "'packages': [[d.metadata['Name'], d.version] for d in m.distributions()]}, "
        "sort_keys=True))"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", probe],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.ENVIRONMENT_INVALID,
            "the isolated environment probe could not run",
            python_executable=python,
            lock_path=recovery_lock_path,
        ) from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.ENVIRONMENT_INVALID,
            "the isolated environment probe did not return valid JSON",
            python_executable=python,
            lock_path=recovery_lock_path,
        ) from exc
    try:
        runtime = _parse_runtime(payload.get("runtime"))
    except RuntimeError as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            str(exc),
            python_executable=python,
            lock_path=recovery_lock_path,
        ) from exc
    packages = payload.get("packages")
    if not isinstance(packages, list):
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
            "the environment probe returned invalid distributions",
            python_executable=python,
            lock_path=recovery_lock_path,
        )
    distributions: dict[str, str] = {}
    for item in packages:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
                "the environment probe returned invalid distributions",
                python_executable=python,
                lock_path=recovery_lock_path,
            )
        name = normalize_distribution_name(item[0])
        if name in distributions:
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
                f"the environment has duplicate distribution {name!r}",
                python_executable=python,
                lock_path=recovery_lock_path,
            )
        distributions[name] = item[1]
    return runtime, distributions


def create_attestation(
    *,
    expected_distributions: Mapping[str, str],
    runtime: PythonRuntimeIdentity,
) -> PythonWorkerEnvironmentAttestation:
    return PythonWorkerEnvironmentAttestation(
        approved_packages=APPROVED_PACKAGES,
        distributions=dict(sorted(expected_distributions.items())),
        runtime=runtime,
    )


def write_attestation(
    python: Path,
    attestation: PythonWorkerEnvironmentAttestation,
) -> None:
    path = attestation_path(python)
    payload = _attestation_payload(attestation)
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_ATTESTATION_BYTES:
        raise RuntimeError("Data Python Worker attestation exceeds its size limit")
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_attestation(
    python: Path,
    *,
    recovery_lock_path: Path | None = None,
) -> PythonWorkerEnvironmentAttestation:
    path = attestation_path(python)
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.ENVIRONMENT_INVALID,
            f"the environment attestation is unavailable: {path}",
            python_executable=python,
            lock_path=recovery_lock_path,
        ) from exc
    if not encoded or len(encoded) > MAX_ATTESTATION_BYTES:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.ENVIRONMENT_INVALID,
            "the environment attestation has an invalid size",
            python_executable=python,
            lock_path=recovery_lock_path,
        )
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.ENVIRONMENT_INVALID,
            "the environment attestation is not valid JSON",
            python_executable=python,
            lock_path=recovery_lock_path,
        ) from exc
    try:
        attestation = _parse_attestation(payload)
    except RuntimeError as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.ENVIRONMENT_INVALID,
            str(exc),
            python_executable=python,
            lock_path=recovery_lock_path,
        ) from exc
    _validate_static_attestation(
        python,
        attestation,
        recovery_lock_path=recovery_lock_path,
    )
    return attestation


def verify_attested_environment(
    python: Path,
    attestation: PythonWorkerEnvironmentAttestation,
    *,
    recovery_lock_path: Path | None = None,
) -> None:
    runtime, installed = probe_environment(
        python,
        recovery_lock_path=recovery_lock_path,
    )
    if runtime != attestation.runtime:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            "the runtime differs from its environment attestation",
            python_executable=python,
            lock_path=recovery_lock_path,
        )
    expected = dependency_distributions(attestation.distributions)
    installed = dependency_distributions(installed)
    if installed != expected:
        missing = sorted(set(expected) - set(installed))
        extra = sorted(set(installed) - set(expected))
        mismatched = sorted(
            name
            for name in set(expected) & set(installed)
            if expected[name] != installed[name]
        )
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
            "the environment differs from its attested uv.lock closure: "
            f"missing={missing}, extra={extra}, version_mismatch={mismatched}",
            python_executable=python,
            lock_path=recovery_lock_path,
        )


def dependency_distributions(distributions: Mapping[str, str]) -> dict[str, str]:
    """Application source comes from the host; only third-party packages are pinned."""
    return {
        name: version
        for name, version in distributions.items()
        if name != TRUSTED_PROJECT_DISTRIBUTION
    }


def _validate_static_attestation(
    python: Path,
    attestation: PythonWorkerEnvironmentAttestation,
    *,
    recovery_lock_path: Path | None = None,
) -> None:
    if attestation.approved_packages != APPROVED_PACKAGES:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
            "the approved roots have drifted",
            python_executable=python,
            lock_path=recovery_lock_path,
        )
    if attestation.runtime.version[:2] != PYTHON_MINOR:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            "the environment attestation is not for Python 3.11",
            python_executable=python,
            lock_path=recovery_lock_path,
        )
    expected_prefix = _environment_prefix_from_executable(python)
    if _path_key(attestation.runtime.executable) != _path_key(python):
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            "the executable differs from its environment attestation",
            python_executable=python,
            lock_path=recovery_lock_path,
        )
    if _path_key(attestation.runtime.prefix) != _path_key(expected_prefix):
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            "the prefix differs from its environment attestation",
            python_executable=python,
            lock_path=recovery_lock_path,
        )
    if _path_key(attestation.runtime.prefix) == _path_key(attestation.runtime.base_prefix):
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            "the interpreter is not an independent environment",
            python_executable=python,
            lock_path=recovery_lock_path,
        )


def _parse_attestation(value: Any) -> PythonWorkerEnvironmentAttestation:
    if not isinstance(value, dict) or not {
        "approved_packages",
        "distributions",
        "python",
    }.issubset(value):
        raise RuntimeError("Data Python Worker attestation has invalid fields")
    approved = value["approved_packages"]
    if not isinstance(approved, list) or not all(isinstance(item, str) for item in approved):
        raise RuntimeError("Data Python Worker attestation has invalid approved roots")
    raw_distributions = value["distributions"]
    if not isinstance(raw_distributions, dict) or len(raw_distributions) > 256:
        raise RuntimeError("Data Python Worker attestation has invalid distributions")
    distributions: dict[str, str] = {}
    for name, version in raw_distributions.items():
        if (
            not isinstance(name, str)
            or normalize_distribution_name(name) != name
            or not isinstance(version, str)
            or not version
        ):
            raise RuntimeError("Data Python Worker attestation has invalid distributions")
        distributions[name] = version
    return PythonWorkerEnvironmentAttestation(
        approved_packages=tuple(approved),
        distributions=distributions,
        runtime=_parse_runtime(value["python"]),
    )


def _parse_runtime(value: Any) -> PythonRuntimeIdentity:
    if not isinstance(value, dict) or set(value) != {
        "abi_flags",
        "base_prefix",
        "cache_tag",
        "executable",
        "implementation",
        "prefix",
        "version",
    }:
        raise RuntimeError("Data Python Worker attestation has invalid Python identity")
    version = value["version"]
    string_fields = (
        "abi_flags",
        "base_prefix",
        "cache_tag",
        "executable",
        "implementation",
        "prefix",
    )
    if (
        not isinstance(version, list)
        or len(version) != 3
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in version)
        or not all(isinstance(value[field], str) for field in string_fields)
        or not value["cache_tag"]
        or not value["implementation"]
        or not Path(value["executable"]).is_absolute()
        or not Path(value["prefix"]).is_absolute()
        or not Path(value["base_prefix"]).is_absolute()
    ):
        raise RuntimeError("Data Python Worker attestation has invalid Python identity")
    return PythonRuntimeIdentity(
        implementation=value["implementation"],
        version=tuple(version),
        cache_tag=value["cache_tag"],
        abi_flags=value["abi_flags"],
        executable=value["executable"],
        prefix=value["prefix"],
        base_prefix=value["base_prefix"],
    )


def _attestation_payload(attestation: PythonWorkerEnvironmentAttestation) -> dict[str, Any]:
    return {
        "approved_packages": list(attestation.approved_packages),
        "distributions": dict(attestation.distributions),
        "python": {
            "abi_flags": attestation.runtime.abi_flags,
            "base_prefix": attestation.runtime.base_prefix,
            "cache_tag": attestation.runtime.cache_tag,
            "executable": attestation.runtime.executable,
            "implementation": attestation.runtime.implementation,
            "prefix": attestation.runtime.prefix,
            "version": list(attestation.runtime.version),
        },
    }


__all__ = [
    "APPROVED_PACKAGES",
    "ATTESTATION_FILENAME",
    "DATA_PYTHON_CHILD_SOURCE_PATHS",
    "DataPythonWorkerDiagnosticCategory",
    "DataPythonWorkerEnvironmentError",
    "MAX_ATTESTATION_BYTES",
    "PYTHON_MINOR",
    "PythonRuntimeIdentity",
    "PythonWorkerEnvironmentAttestation",
    "TRUSTED_PROJECT_DISTRIBUTION",
    "attestation_path",
    "create_attestation",
    "data_python_worker_environment_error",
    "data_python_worker_recovery_commands",
    "dependency_distributions",
    "execution_source_files",
    "load_attestation",
    "normalize_distribution_name",
    "probe_environment",
    "verify_attested_environment",
    "write_attestation",
]
