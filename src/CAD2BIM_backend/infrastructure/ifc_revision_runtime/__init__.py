"""Supervised infrastructure adapter for exact IFC revision computation.

Imports stay lazy so the sandboxed child can load its protocol without also
loading parent-only supervision and filesystem adapter code.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


__all__ = ["IFC_REVISION_RUNTIME_BUDGET", "IfcOpenShellRevisionRuntime"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    return getattr(import_module(".adapter", __name__), name)
