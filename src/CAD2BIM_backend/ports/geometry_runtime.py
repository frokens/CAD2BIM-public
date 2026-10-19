"""The single application-facing boundary for canonical geometry execution."""

from __future__ import annotations

from typing import Protocol
from contextlib import AbstractAsyncContextManager

from CAD2BIM_backend.domain.runtime_execution import RuntimeExecutionPolicy

from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    GeometryBuildDisplayContextRequest,
    GeometryBuildDisplayContextResult,
    GeometryBuildFixedRequest,
    GeometryFixedBuildResult,
    GeometryFunctionValidationResult,
    GeometryFunctionInterfaceEvaluation,
    GeometryNormalizeRequest,
    GeometryDisplayMesh,
    GeometryPreview,
    GeometryQueryRequest,
    GeometryQueryResult,
    GeometrySectionRequest,
    GeometrySectionResult,
    GeometryRenderRequest,
    GeometrySummary,
    GeometryValidateFunctionRequest,
)


class GeometryRuntimePort(Protocol):
    def invocation_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None) -> AbstractAsyncContextManager[object]: ...

    def execution_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None) -> AbstractAsyncContextManager[object]: ...

    def release_execution(self) -> None: ...

    async def verify_ready(self) -> None: ...

    async def normalize(self, request: GeometryNormalizeRequest) -> CanonicalGeometry: ...

    async def build_display_context(
        self, request: GeometryBuildDisplayContextRequest
    ) -> GeometryBuildDisplayContextResult: ...

    async def reopen(self, geometry: CanonicalGeometry) -> GeometrySummary: ...

    async def build_fixed(self, request: GeometryBuildFixedRequest) -> GeometryFixedBuildResult: ...

    async def validate_function(
        self, request: GeometryValidateFunctionRequest
    ) -> GeometryFunctionValidationResult: ...

    async def evaluate_function_interfaces(self, request: GeometryValidateFunctionRequest) -> GeometryFunctionInterfaceEvaluation: ...

    async def instantiate_function(self, request: GeometryValidateFunctionRequest,
        evaluation: GeometryFunctionInterfaceEvaluation,
    ) -> GeometryFunctionValidationResult: ...

    async def query(self, request: GeometryQueryRequest) -> GeometryQueryResult: ...

    async def render(self, request: GeometryRenderRequest) -> GeometryPreview: ...

    async def export_stl(self, geometry: CanonicalGeometry) -> GeometryDisplayMesh: ...

    async def section(self, request: GeometrySectionRequest) -> GeometrySectionResult: ...

    async def close(self) -> None: ...


__all__ = ["GeometryRuntimePort"]
