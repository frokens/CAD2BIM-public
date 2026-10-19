#!/usr/bin/env python3
"""Build or verify the isolated, computation-only Data Python 3.11 Worker."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import struct
import subprocess
import tempfile
import tomllib
from collections import deque
from pathlib import Path
from typing import Any

from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    APPROVED_PACKAGES,
    PYTHON_MINOR,
    TRUSTED_PROJECT_DISTRIBUTION,
    DataPythonWorkerDiagnosticCategory,
    DataPythonWorkerEnvironmentError,
    PythonRuntimeIdentity,
    create_attestation,
    data_python_worker_environment_error,
    dependency_distributions,
    load_attestation,
    normalize_distribution_name,
    probe_environment,
    verify_attested_environment,
    write_attestation,
)
from CAD2BIM_backend.infrastructure.data_transform.python_arrow_sandbox import (
    DarwinSeatbeltPythonArrowSandbox,
)

_FRAME_HEADER = struct.Struct("!I")


def _normalize(name: str) -> str:
    return normalize_distribution_name(name)


def locked_requirements(lock_path: Path) -> dict[str, str]:
    """Return the exact registry-package closure for the approved roots."""
    with lock_path.open("rb") as stream:
        lock = tomllib.load(stream)

    index: dict[str, dict[str, Any]] = {}
    for package in lock.get("package", []):
        name = _normalize(package["name"])
        if name in index:
            raise RuntimeError(f"uv.lock has more than one resolution for {name!r}")
        index[name] = package

    requirements: dict[str, str] = {}
    pending = deque(_normalize(name) for name in APPROVED_PACKAGES)
    while pending:
        name = pending.popleft()
        if name in requirements:
            continue
        package = index.get(name)
        if package is None:
            raise RuntimeError(f"approved package {name!r} is missing from uv.lock")
        source = package.get("source", {})
        version = package.get("version")
        if "registry" not in source or not version:
            raise RuntimeError(
                f"Data Python Worker package {name!r} is not registry-version locked"
            )
        requirements[name] = str(version)
        pending.extend(_normalize(item["name"]) for item in package.get("dependencies", []))
    return requirements


def expected_environment(lock_path: Path) -> dict[str, str]:
    """Add the trusted project wheel to the locked computation closure."""
    requirements = locked_requirements(lock_path)
    with lock_path.open("rb") as stream:
        lock = tomllib.load(stream)
    projects = [
        package
        for package in lock.get("package", [])
        if _normalize(package["name"]) == TRUSTED_PROJECT_DISTRIBUTION
    ]
    if len(projects) != 1:
        raise RuntimeError("uv.lock must contain exactly one trusted cad2bim distribution")
    project = projects[0]
    if project.get("source", {}).get("editable") != "." or not project.get("version"):
        raise RuntimeError("uv.lock cad2bim distribution is not the trusted current project")
    requirements[TRUSTED_PROJECT_DISTRIBUTION] = str(project["version"])
    return requirements


def _interpreter(env_dir: Path) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return env_dir / relative


def _assert_safe_env_dir(env_dir: Path) -> None:
    if not env_dir.exists():
        return
    if not (env_dir / "pyvenv.cfg").is_file():
        raise RuntimeError(f"refusing to replace non-venv directory: {env_dir}")


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _build_project_sdist(*, project_root: Path, temporary: Path, uv: str) -> Path:
    """Build from the current source manifest, excluding stale local wheel output."""

    output = temporary / "project-dist"
    output.mkdir()
    _run([uv, "build", "--sdist", "--out-dir", str(output), str(project_root)])
    archives = tuple(output.glob("cad2bim-*.tar.gz"))
    if len(archives) != 1:
        raise RuntimeError("Data Python Worker source build did not produce exactly one sdist")
    return archives[0]


def _installed_environment(
    python: Path,
    *,
    recovery_lock_path: Path | None = None,
) -> tuple[PythonRuntimeIdentity, dict[str, str]]:
    return probe_environment(python, recovery_lock_path=recovery_lock_path)


def _write_frame(stream: Any, value: dict[str, Any]) -> None:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    stream.write(_FRAME_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = stream.read(size)
        if not chunk:
            raise RuntimeError("Data Python Worker child readiness frame ended early")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: Any, *, timeout: float = 15.0) -> dict[str, Any]:
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise RuntimeError("Data Python Worker child readiness probe timed out")
    size = _FRAME_HEADER.unpack(_read_exact(stream, _FRAME_HEADER.size))[0]
    value = json.loads(_read_exact(stream, size))
    if not isinstance(value, dict):
        raise RuntimeError("Data Python Worker child readiness frame is not an object")
    return value


def _probe_child_readiness(python: Path) -> None:
    """Execute the installed child under ``-I -m`` through its real protocol."""
    sandbox = DarwinSeatbeltPythonArrowSandbox(str(python))
    control_read, control_write = os.pipe()
    status_read, status_write = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="cad2bim-data-python-worker-probe-"
        ) as scratch_value:
            scratch = Path(scratch_value)
            environment = {
                "CAD2BIM_WORKER_CONTROL_FD": str(control_read),
                "CAD2BIM_WORKER_STATUS_FD": str(status_write),
                "HOME": str(scratch),
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
                "POLARS_MAX_THREADS": "2",
                "PROJ_NETWORK": "OFF",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "TMPDIR": str(scratch),
            }
            try:
                process = subprocess.Popen(
                    sandbox.command(
                        scratch=scratch,
                        child_args=sandbox.child_arguments,
                    ),
                    cwd=scratch,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                    pass_fds=(control_read, status_write),
                )
            except OSError as exc:
                raise data_python_worker_environment_error(
                    DataPythonWorkerDiagnosticCategory.SANDBOX_FAILURE,
                    "the Darwin Seatbelt process could not start",
                    python_executable=python,
                ) from exc
            os.close(control_read)
            control_read = -1
            os.close(status_write)
            status_write = -1
            with (
                os.fdopen(control_write, "wb", buffering=0) as control,
                os.fdopen(status_read, "rb", buffering=0) as status,
            ):
                control_write = status_read = -1
                _write_frame(
                    control,
                    {
                        "code": "def transform(ctx):\n    pass\n",
                        "inputs": [],
                        "outputs": [],
                        "parameters": [],
                        "policy": {
                            "capture_bytes": 4096,
                            "cpu_seconds": 15,
                            "open_file_descriptors": 64,
                            "scratch_bytes": 16 * 1024 * 1024,
                            "threads": 2,
                        },
                    },
                )
                events = [_read_frame(status)]
                _write_frame(control, {"command": "start"})
                events.extend((_read_frame(status), _read_frame(status)))
            stdout, stderr = process.communicate(timeout=15.0)
            if process.returncode != 0 or [item.get("event") for item in events] != [
                "ready",
                "started",
                "done",
            ]:
                raise RuntimeError(
                    "Data Python Worker child readiness probe failed: "
                    f"returncode={process.returncode}, events={events}, "
                    f"stdout={stdout[-2000:]!r}, stderr={stderr[-2000:]!r}"
                )
    finally:
        for descriptor in (control_read, control_write, status_read, status_write):
            if descriptor >= 0:
                os.close(descriptor)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def _check_child_readiness(python: Path, *, lock_path: Path) -> None:
    try:
        _probe_child_readiness(python)
    except DataPythonWorkerEnvironmentError:
        raise
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.CHILD_READINESS_FAILED,
            str(exc),
            python_executable=python,
            lock_path=lock_path,
        ) from exc


def verify_environment(
    env_dir: Path,
    expected: dict[str, str],
    *,
    lock_path: Path | None = None,
) -> PythonRuntimeIdentity:
    python = _interpreter(env_dir)
    effective_lock = lock_path or Path(__file__).resolve().parents[2] / "uv.lock"
    if not python.is_file():
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            f"the interpreter is missing: {python}",
            python_executable=python,
            lock_path=effective_lock,
        )
    runtime, installed = _installed_environment(
        python,
        recovery_lock_path=effective_lock,
    )
    if runtime.version[:2] != PYTHON_MINOR:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            "the environment requires Python 3.11, found "
            f"{runtime.version[0]}.{runtime.version[1]}",
            python_executable=python,
            lock_path=effective_lock,
        )
    installed = dependency_distributions(installed)
    expected = dependency_distributions(expected)
    if installed != expected:
        missing = sorted(set(expected) - set(installed))
        extra = sorted(set(installed) - set(expected))
        mismatched = sorted(
            name for name in set(expected) & set(installed) if expected[name] != installed[name]
        )
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
            "the environment differs from the uv.lock closure: "
            f"missing={missing}, extra={extra}, version_mismatch={mismatched}",
            python_executable=python,
            lock_path=effective_lock,
        )
    _check_child_readiness(python, lock_path=effective_lock)
    return runtime


def build_environment(
    *, env_dir: Path, lock_path: Path, uv: str = "uv", python_request: str = "3.11"
) -> None:
    locked = locked_requirements(lock_path)
    expected = expected_environment(lock_path)
    _assert_safe_env_dir(env_dir)
    python = _interpreter(env_dir)
    try:
        _run([uv, "venv", "--python", python_request, "--clear", str(env_dir)])
    except (OSError, subprocess.CalledProcessError) as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
            "the isolated Python 3.11 environment could not be created",
            python_executable=python,
            lock_path=lock_path,
        ) from exc
    requirements = "".join(f"{name}=={version}\n" for name, version in sorted(locked.items()))
    with tempfile.TemporaryDirectory(prefix="cad2bim-data-python-worker-") as temporary:
        temporary_path = Path(temporary)
        requirements_path = temporary_path / "requirements.lock"
        requirements_path.write_text(requirements, encoding="utf-8")
        try:
            _run(
                [
                    uv,
                    "pip",
                    "sync",
                    "--strict",
                    "--python",
                    str(python),
                    str(requirements_path),
                ]
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
                "the uv.lock dependency closure could not be installed",
                python_executable=python,
                lock_path=lock_path,
            ) from exc
        try:
            project_sdist = _build_project_sdist(
                project_root=lock_path.resolve().parent,
                temporary=temporary_path,
                uv=uv,
            )
            _run(
                [
                    uv,
                    "pip",
                    "install",
                    "--no-deps",
                    "--python",
                    str(python),
                    str(project_sdist),
                ]
            )
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.SOURCE_MISMATCH,
                "the current cad2bim source distribution could not be installed",
                python_executable=python,
                lock_path=lock_path,
            ) from exc
    runtime = verify_environment(
        env_dir,
        expected,
        lock_path=lock_path,
    )
    write_attestation(
        _interpreter(env_dir),
        create_attestation(
            expected_distributions=expected,
            runtime=runtime,
        ),
    )


def _parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-dir",
        type=Path,
        default=project_root / ".venv" / "data-python-worker",
    )
    parser.add_argument("--lock", type=Path, default=project_root / "uv.lock")
    parser.add_argument("--check", action="store_true", help="verify without changing the venv")
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--python", default="3.11", help="Python 3.11 interpreter request for uv")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    env_dir = arguments.env_dir.resolve()
    lock_path = arguments.lock.resolve()
    python = _interpreter(env_dir)
    try:
        expected = expected_environment(lock_path)
    except DataPythonWorkerEnvironmentError:
        raise
    except (OSError, RuntimeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise data_python_worker_environment_error(
            DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
            str(exc),
            python_executable=python,
            lock_path=lock_path,
        ) from exc
    if arguments.check:
        attestation = load_attestation(
            python,
            recovery_lock_path=lock_path,
        )
        if dependency_distributions(attestation.distributions) != dependency_distributions(expected):
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.DEPENDENCY_MISMATCH,
                "the environment attestation has a stale uv.lock closure",
                python_executable=python,
                lock_path=lock_path,
            )
        verify_attested_environment(
            python,
            attestation,
            recovery_lock_path=lock_path,
        )
        _check_child_readiness(python, lock_path=lock_path)
    else:
        build_environment(
            env_dir=env_dir,
            lock_path=lock_path,
            uv=arguments.uv,
            python_request=arguments.python,
        )
    print(
        json.dumps(
            {
                "status": "verified",
                "python": "3.11",
                "environment": str(env_dir),
                "approved_packages": list(APPROVED_PACKAGES),
                "locked_distribution_count": len(expected),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
