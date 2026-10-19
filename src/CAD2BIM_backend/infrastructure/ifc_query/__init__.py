"""Isolated, read-only IfcOpenShell query adapter."""

from __future__ import annotations

from typing import Any

__all__ = ["IfcOpenShellQueryWorker"]


def __getattr__(name: str) -> Any:
    # The sandboxed child loads this package before its module.  Keep the
    # parent-only supervisor adapter (and its broader imports) out of the child.
    if name == "IfcOpenShellQueryWorker":
        from .adapter import IfcOpenShellQueryWorker

        return IfcOpenShellQueryWorker
    raise AttributeError(name)
