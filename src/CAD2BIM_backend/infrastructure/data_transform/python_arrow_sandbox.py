"""Python/Arrow compatibility wrapper for the shared Darwin Seatbelt launcher."""

from __future__ import annotations

import sys
from pathlib import Path

from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    DATA_PYTHON_CHILD_SOURCE_PATHS,
    DataPythonWorkerDiagnosticCategory,
    DataPythonWorkerEnvironmentError,
    data_python_worker_environment_error,
    execution_source_files,
)
from CAD2BIM_backend.infrastructure.worker_runtime.darwin_seatbelt import (
    DarwinSeatbeltSandbox,
)


DARWIN_SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")


def _trusted_source_files() -> tuple[Path, ...]:
    """Return only the trusted bootstrap sources needed by an editable test env."""

    return execution_source_files(DATA_PYTHON_CHILD_SOURCE_PATHS)


class DarwinSeatbeltPythonArrowSandbox(DarwinSeatbeltSandbox):
    """Preserve the Python/Arrow launcher API while sharing Seatbelt mechanics."""

    def __init__(self, python_executable: str) -> None:
        declared = Path(python_executable)
        try:
            trusted_sources = _trusted_source_files()
        except DataPythonWorkerEnvironmentError:
            raise
        except RuntimeError as exc:
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.SOURCE_MISMATCH,
                str(exc),
                python_executable=declared,
            ) from exc
        package_root = Path(__file__).resolve().parents[2]
        # Dependency environments must not freeze application code. Execute the
        # host's authorized child sources, using the selected venv's libraries.
        self.child_arguments = (
            "-I", "-B", "-c",
            "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
            "runpy.run_module(sys.argv[2], run_name='__main__')",
            str(package_root.parent),
            "CAD2BIM_backend.infrastructure.data_transform.python_arrow_child",
        )
        try:
            super().__init__(
                python_executable,
                trusted_source_files=trusted_sources,
                trusted_source_directories=(
                    Path(__file__).resolve().parents[3],
                    package_root,
                    package_root / "infrastructure",
                    Path(__file__).resolve().parent,
                    Path(__file__).resolve().parent.parent / "worker_runtime",
                ),
                trusted_source_metadata_root=Path(__file__).resolve().parents[3],
                sandbox_executable=DARWIN_SANDBOX_EXECUTABLE,
                platform=sys.platform,
            )
        except RuntimeError as exc:
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.SANDBOX_FAILURE,
                str(exc),
                python_executable=declared,
            ) from exc


__all__ = ["DarwinSeatbeltPythonArrowSandbox"]
