"""Short-lived SQL and Python/Arrow transform workers.

Imports stay lazy so the dedicated child interpreter can load its small trusted
child module without also requiring parent-only supervisor dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .duckdb_worker import SubprocessDuckDBDataTransformWorker
    from .python_arrow_worker import SubprocessPythonArrowDataTransformWorker

__all__ = [
    "SubprocessDuckDBDataTransformWorker",
    "SubprocessPythonArrowDataTransformWorker",
]


def __getattr__(name: str) -> Any:
    if name == "SubprocessDuckDBDataTransformWorker":
        from .duckdb_worker import SubprocessDuckDBDataTransformWorker

        return SubprocessDuckDBDataTransformWorker
    if name == "SubprocessPythonArrowDataTransformWorker":
        from .python_arrow_worker import SubprocessPythonArrowDataTransformWorker

        return SubprocessPythonArrowDataTransformWorker
    raise AttributeError(name)
