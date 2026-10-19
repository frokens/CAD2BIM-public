"""Check the supported controlled-modeling compiler and Python runtime."""
from __future__ import annotations

import importlib.metadata
import os
import sys


class ModelingEnvironmentError(RuntimeError):
    """The supported controlled-modeling environment is unavailable."""


def require_modeling_environment() -> None:
    try:
        compiler_version = importlib.metadata.version("RestrictedPython")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ModelingEnvironmentError("controlled modeling requires RestrictedPython 8.5") from exc
    if compiler_version != "8.5" or sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11):
        raise ModelingEnvironmentError("controlled modeling requires RestrictedPython 8.5 and CPython 3.11")


def load_modeling_dependencies() -> None:
    """Initialize the native pool before CadQuery and before author execution.

    Small repeated Boolean operations lose time scheduling the machine-wide
    default pool. Bound native work within this one isolated Modeling Worker;
    this does not change the Supervisor's thread/resource limits.
    """
    from OCP.OSD import OSD_ThreadPool
    from CAD2BIM_backend.infrastructure.worker_runtime.timing import timed_phase

    threads = min(4, os.cpu_count() or 1)
    with timed_phase("geometry_native_pool", threads=threads):
        pool = OSD_ThreadPool.DefaultPool_s(threads)
        pool.SetNbDefaultThreadsToLaunch(threads)
    import cadquery  # noqa: F401


__all__ = ["ModelingEnvironmentError", "require_modeling_environment", "load_modeling_dependencies"]
