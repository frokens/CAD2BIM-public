"""Application-facing boundary for one isolated ProductFamily realization."""

from __future__ import annotations

from typing import Protocol, Callable, Awaitable
from pathlib import Path
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager

from CAD2BIM_backend.domain.runtime_execution import RuntimeExecutionPolicy
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry
from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction, IfcGeometryRepresentationRoute

from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationGeometryExecution,
    ProductRealizationRequest,
    ProductRealizationResult,
    ProductRealizationOutputNeed,
)


class ProductRealizationRuntimeError(RuntimeError):
    """Base class for stable Product Realization runtime failures."""


class ProductRealizationRuntimeValidationError(ProductRealizationRuntimeError, ValueError):
    """Program source, execution, or typed output is attributable and invalid."""

    def __init__(
        self, code: str, message: str, *, phase: str,
        item_index: int | None = None, diagnostic: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.phase = phase
        self.item_index = item_index
        self.diagnostic = diagnostic


class ProductRealizationRuntimeUnavailableError(ProductRealizationRuntimeError):
    """The isolated runtime or host capacity is temporarily unavailable."""

    def __init__(
        self, message: str, *, diagnostic: dict[str, object] | None = None,
        retryable: bool = True, item_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic
        self.retryable = retryable
        self.item_index = item_index


class ProductRealizationRuntimeResourceLimitError(ProductRealizationRuntimeUnavailableError):
    """A started realization or codec execution exceeded its fixed resource limit."""


class ProductGeometrySourceCache(Protocol):
    """One Apply's bounded spool of independently checked immutable sources."""

    @property
    def spool_directory(self) -> Path: ...

    @property
    def spool_size_bytes(self) -> int: ...

    def reserve_spool_file(self, path: Path, size_bytes: int) -> bool: ...

    def get(self, key: str) -> tuple[CanonicalGeometry, GeometryConstruction | None, dict] | None: ...

    def put(self, key: str, geometry: CanonicalGeometry, construction: GeometryConstruction | None,
            route: IfcGeometryRepresentationRoute, parameters: dict) -> None: ...


class ProductRealizationRuntimePort(Protocol):
    def invocation_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None,
        on_guard_registered: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> AbstractAsyncContextManager[object]: ...

    def execution_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None) -> AbstractAsyncContextManager[object]: ...

    def release_execution(self) -> None: ...

    async def verify_ready(self) -> None: ...

    async def realize(self, request: ProductRealizationRequest) -> ProductRealizationResult: ...

    def precheck_batch(self, requests: Sequence[ProductRealizationRequest], *, window_size: int | None = None) -> AsyncIterator[ProductRealizationResult]: ...

    def instantiate_batch(self, requests: Sequence[ProductRealizationRequest], prechecks: Sequence[ProductRealizationResult], *,
        output_need: ProductRealizationOutputNeed = ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
        source_cache: ProductGeometrySourceCache | None = None,
        window_size: int | None = None,
    ) -> AsyncIterator[ProductRealizationGeometryExecution]: ...

    def realize_batch(
        self, requests: Sequence[ProductRealizationRequest], *,
        output_need: ProductRealizationOutputNeed | Sequence[ProductRealizationOutputNeed],
    ) -> AsyncIterator[ProductRealizationGeometryExecution]:
        """Numeric-only page; geometry uses precheck_batch then instantiate_batch."""
        ...

    async def realize_geometry(
        self,
        request: ProductRealizationRequest,
        *,
        include_instances: bool = False,
    ) -> ProductRealizationGeometryExecution: ...

    async def close(self) -> None: ...


__all__ = [
    "ProductGeometrySourceCache",
    "ProductRealizationRuntimeError",
    "ProductRealizationRuntimePort",
    "ProductRealizationRuntimeResourceLimitError",
    "ProductRealizationRuntimeUnavailableError",
    "ProductRealizationRuntimeValidationError",
]
