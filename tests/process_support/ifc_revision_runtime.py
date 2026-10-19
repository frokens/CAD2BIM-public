"""Small in-process test double for the internal IFC revision runtime Port.

Production composition never imports this module.  It keeps Application tests
fast while exercising the same existing compiler implementations and typed
runtime results as the supervised adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from CAD2BIM_backend.application.ifc_model_service import IfcModelService
from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator

from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.geometry_basis import (
    IfcOpenShellGeometryBasisCompiler,
)
from CAD2BIM_backend.infrastructure.ifc.project import IfcOpenShellProjectCompiler
from CAD2BIM_backend.infrastructure.ifc.spatial_structure import (
    IfcOpenShellSpatialStructureCompiler,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    ApplyIfcSpatialStructureRevisionRequest,
    ConfigureIfcGeometryBasisRevisionRequest,
    GeneratedIfcRevision,
    IfcProjectRevisionEffect,
    IfcRevisionChunkSource,
    IfcRevisionRuntimeRequest,
    IfcRevisionRuntimeResult,
    IfcSpatialRevisionEffect,
    InitializeIfcRevisionRequest,
    InspectedIfcRevision,
    InspectExactIfcRevisionRequest,
    UpdateIfcProjectRevisionRequest,
)


class BytesIfcCandidate:
    def __init__(self, content: bytes, *, sha256: str) -> None:
        self.content = content
        self.size_bytes = len(content)
        self.sha256 = sha256
        self.closed = False

    async def iter_chunks(
        self,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        if self.closed:
            raise RuntimeError("candidate is closed")
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    async def aclose(self) -> None:
        self.closed = True


async def read_source(source: IfcRevisionChunkSource) -> bytes:
    return b"".join([chunk async for chunk in source.iter_chunks()])


class InProcessIfcRevisionRuntime:
    """Test-only implementation of the closed revision-runtime Port."""

    def __init__(self, codec: IfcOpenShellModelCodec | None = None) -> None:
        self.codec = codec or IfcOpenShellModelCodec()
        self.geometry = IfcOpenShellGeometryBasisCompiler(self.codec)
        self.project = IfcOpenShellProjectCompiler(self.codec)
        self.spatial = IfcOpenShellSpatialStructureCompiler(self.codec)
        self.execute_count = 0
        self.closed = False

    async def verify_ready(self) -> None:
        if self.closed:
            raise RuntimeError("runtime is closed")

    def invocation_scope(self, policy, *, deadline=None, on_guard_registered=None):
        from contextlib import nullcontext
        return nullcontext()

    async def execute(
        self,
        request: IfcRevisionRuntimeRequest,
        *,
        source: IfcRevisionChunkSource | None = None,
    ) -> IfcRevisionRuntimeResult:
        if self.closed:
            raise RuntimeError("runtime is closed")
        self.execute_count += 1
        if isinstance(request, InitializeIfcRevisionRequest):
            if source is not None:
                raise ValueError("initialize must not receive source")
            encoded = self.codec.create_revision1(
                project_global_id=request.project_global_id,
                project_name=request.command.project_name,
                header_timestamp=request.header_timestamp,
            )
            return self._generated(encoded)
        if source is None:
            raise ValueError("this request requires source")
        content = await read_source(source)
        if isinstance(request, InspectExactIfcRevisionRequest):
            from CAD2BIM_backend.infrastructure.ifc.product_realization_host import IfcOpenShellProductRealizationHostFactExtractor
            inspection = (
                self.codec.inspect(content)
                if request.expected_project_global_id is None
                else self.codec.inspect_revision1(
                    content,
                    expected_project_global_id=request.expected_project_global_id,
                    expected_project_name=request.expected_project_name or "",
                )
            )
            return InspectedIfcRevision(
                inspection=inspection,
                contexts=self.geometry.inspect_contexts(content) if request.projection == "contexts" else None,
                spatial_page=self.spatial.list_spatial_structure(content, request.spatial_query) if request.projection == "spatial" else None,
                host_facts=IfcOpenShellProductRealizationHostFactExtractor(self.codec).extract(content, request.host_request.selectors) if request.projection == "host_facts" else None,
            )
        if isinstance(request, ConfigureIfcGeometryBasisRevisionRequest):
            return self._generated(self.geometry.apply(content, request.command))
        if isinstance(request, UpdateIfcProjectRevisionRequest):
            encoded = self.project.apply(content, request.command)
            return self._generated(
                encoded,
                effect=IfcProjectRevisionEffect(
                    project=self.project.inspect_project(encoded.content)
                ),
            )
        if isinstance(request, ApplyIfcSpatialStructureRevisionRequest):
            compilation = self.spatial.apply(content, request.command)
            catalog = self.spatial.inspect_spatial_structure(
                compilation.candidate.content
            )
            affected = set(compilation.affected_global_ids)
            return self._generated(
                compilation.candidate,
                effect=IfcSpatialRevisionEffect(
                    project_global_id=catalog.project_global_id,
                    created=compilation.created,
                    affected_global_ids=compilation.affected_global_ids,
                    spatial_summary=tuple(
                        item for item in catalog.items if item.global_id in affected
                    ),
                ),
            )
        raise TypeError("unsupported runtime request")

    async def close(self) -> None:
        self.closed = True

    @staticmethod
    def _generated(encoded, *, effect=None) -> GeneratedIfcRevision:  # type: ignore[no-untyped-def]
        return GeneratedIfcRevision(
            candidate=BytesIfcCandidate(
                encoded.content,
                sha256=encoded.inspection.sha256,
            ),
            inspection=encoded.inspection,
            effect=effect,
        )


@dataclass(slots=True)
class TestIfcWriteStack:
    codec: IfcOpenShellModelCodec
    reader: IfcRevisionReader
    runtime: InProcessIfcRevisionRuntime
    coordinator: IfcWriteCoordinator
    writer: IfcRevisionWriter
    model_service: IfcModelService


def build_in_process_ifc_stack(
    repository,
    storage,
    *,
    codec: IfcOpenShellModelCodec | None = None,
) -> TestIfcWriteStack:  # type: ignore[no-untyped-def]
    selected_codec = codec or IfcOpenShellModelCodec()
    runtime = InProcessIfcRevisionRuntime(selected_codec)
    reader = IfcRevisionReader(repository, storage, runtime)
    from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
        bind_tenant_execution_context, current_tenant_execution_context,
    )
    coordinator = IfcWriteCoordinator(
        get_execution_context=current_tenant_execution_context,
        bind_execution_context=bind_tenant_execution_context,
    )
    writer = IfcRevisionWriter(repository, storage, reader, runtime)
    model_service = IfcModelService(
        repository,
        storage,
        selected_codec,
        writer,
        runtime,
        coordinator,
    )
    return TestIfcWriteStack(
        codec=selected_codec,
        reader=reader,
        runtime=runtime,
        coordinator=coordinator,
        writer=writer,
        model_service=model_service,
    )


__all__ = [
    "BytesIfcCandidate",
    "InProcessIfcRevisionRuntime",
    "TestIfcWriteStack",
    "build_in_process_ifc_stack",
    "read_source",
]
