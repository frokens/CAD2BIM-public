"""Independent controlled-local Product Realization Runtime."""

from __future__ import annotations

from typing import Any


__all__ = ["OcpCadQueryProductRealizationRuntime", "PRODUCT_REALIZATION_BUDGET"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter

    return getattr(adapter, name)
