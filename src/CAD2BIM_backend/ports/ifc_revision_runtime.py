"""Internal boundary for supervised IFC revision computation.

Durable identities deliberately do not appear here.  The caller owns all
Scope, Operation, Artifact, revision, and CURRENT decisions; this Port only
computes or inspects one exact process-private IFC payload.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias
from CAD2BIM_backend.domain.runtime_execution import RuntimeExecutionPolicy
from CAD2BIM_backend.domain.ifc_product_compilation import (
    CompileIfcProductsCommand, IfcProductCompilationBlock, IfcProductBodyPreflight,
    IfcProductRevisionEffect,
)
from CAD2BIM_backend.domain.product_realization_host import (
    CurrentIfcHostFactRequest, ResolvedCurrentIfcHostFact,
)

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcGeometryBasisBusinessError,
    IfcRepresentationContextCatalog,
)
from CAD2BIM_backend.domain.ifc_model import InitializeIfcModelCommand, IfcRevisionInspection
from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadRuntimeRequest, IfcProductsReadRuntimeResult
from CAD2BIM_backend.domain.product_edit import (
    CompileIfcProductsEditCommand, IfcProductsEditPreflightRequest,
    IfcProductsEditPreflightResult, IfcProductsEditRevisionEffect,
)
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectBusinessError,
    IfcProjectSummary,
    UpdateIfcProjectCommand,
)
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    IfcSpatialCreatedBinding,
    IfcSpatialNodeSummary,
    IfcSpatialStructureBusinessError,
    IfcSpatialStructureListQuery,
    IfcSpatialStructurePage,
)


_GLOBAL_ID = re.compile(r"^[0-9A-Za-z_$]{22}$")


class IfcRevisionChunkSource(Protocol):
    """Repeatable exact-byte source; every iterator starts at byte zero."""

    @property
    def size_bytes(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    def iter_chunks(self, *, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]: ...


class IfcRevisionCandidateHandle(IfcRevisionChunkSource, Protocol):
    """Private short-lived candidate capability owned by its caller."""

    @property
    def closed(self) -> bool: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class InitializeIfcRevisionRequest:
    command: InitializeIfcModelCommand
    project_global_id: str
    header_timestamp: datetime
    kind: Literal["initialize"] = field(default="initialize", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.command, InitializeIfcModelCommand):
            raise TypeError("command must be InitializeIfcModelCommand")
        if _GLOBAL_ID.fullmatch(self.project_global_id) is None:
            raise ValueError("project_global_id must be a valid IFC GlobalId")
        if self.header_timestamp.tzinfo is None or self.header_timestamp.utcoffset() is None:
            raise ValueError("header_timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ConfigureIfcGeometryBasisRevisionRequest:
    command: ConfigureIfcGeometryBasisCommand
    kind: Literal["configure_geometry_basis"] = field(
        default="configure_geometry_basis", init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.command, ConfigureIfcGeometryBasisCommand):
            raise TypeError("command must be ConfigureIfcGeometryBasisCommand")


@dataclass(frozen=True, slots=True)
class UpdateIfcProjectRevisionRequest:
    command: UpdateIfcProjectCommand
    kind: Literal["update_project"] = field(default="update_project", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.command, UpdateIfcProjectCommand):
            raise TypeError("command must be UpdateIfcProjectCommand")


@dataclass(frozen=True, slots=True)
class ApplyIfcSpatialStructureRevisionRequest:
    command: ApplyIfcSpatialStructureCommand
    kind: Literal["apply_spatial_structure"] = field(
        default="apply_spatial_structure", init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.command, ApplyIfcSpatialStructureCommand):
            raise TypeError("command must be ApplyIfcSpatialStructureCommand")


@dataclass(frozen=True, slots=True)
class CompileIfcProductsRevisionRequest:
    command: CompileIfcProductsCommand
    blocks: AsyncIterator[IfcProductCompilationBlock]
    kind: Literal["compile_products"] = field(default="compile_products", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.command, CompileIfcProductsCommand):
            raise TypeError("command must be CompileIfcProductsCommand")
        if not hasattr(self.blocks, "__aiter__"):
            raise TypeError("blocks must be an async iterator")


@dataclass(frozen=True, slots=True)
class EditIfcProductsRevisionRequest:
    command: CompileIfcProductsEditCommand
    blocks: AsyncIterator[IfcProductCompilationBlock]
    kind: Literal["edit_products"] = field(default="edit_products", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.command, CompileIfcProductsEditCommand):
            raise TypeError("command must be CompileIfcProductsEditCommand")
        if not hasattr(self.blocks, "__aiter__"):
            raise TypeError("blocks must be an async iterator")




@dataclass(frozen=True, slots=True)
class InspectExactIfcRevisionRequest:
    expected_project_global_id: str | None = None
    expected_project_name: str | None = None
    projection: Literal["model", "contexts", "spatial", "host_facts", "body", "products", "edit_targets"] = "model"
    spatial_query: IfcSpatialStructureListQuery | None = None
    host_request: CurrentIfcHostFactRequest | None = None
    products_request: IfcProductsReadRuntimeRequest | None = None
    edit_targets_request: IfcProductsEditPreflightRequest | None = None
    kind: Literal["inspect_exact"] = field(default="inspect_exact", init=False)

    def __post_init__(self) -> None:
        if self.projection not in {"model", "contexts", "spatial", "host_facts", "body", "products", "edit_targets"}:
            raise ValueError("unknown IFC inspection projection")
        if (self.projection == "spatial") != isinstance(self.spatial_query, IfcSpatialStructureListQuery):
            raise ValueError("only spatial inspection requires its typed list query")
        if (self.projection == "host_facts") != isinstance(self.host_request, CurrentIfcHostFactRequest):
            raise ValueError("only host inspection requires its typed selectors")
        if (self.projection == "products") != isinstance(self.products_request, IfcProductsReadRuntimeRequest):
            raise ValueError("only products inspection requires its typed selectors")
        if (self.projection == "edit_targets") != isinstance(self.edit_targets_request, IfcProductsEditPreflightRequest):
            raise ValueError("only edit_targets inspection requires its typed target request")
        if (self.expected_project_global_id is None) != (self.expected_project_name is None):
            raise ValueError(
                "expected_project_global_id and expected_project_name must be provided together"
            )
        if (
            self.expected_project_global_id is not None
            and _GLOBAL_ID.fullmatch(self.expected_project_global_id) is None
        ):
            raise ValueError("expected_project_global_id must be a valid IFC GlobalId")
        if self.expected_project_name is not None:
            InitializeIfcModelCommand(project_name=self.expected_project_name)


IfcRevisionRuntimeRequest: TypeAlias = (
    InitializeIfcRevisionRequest
    | ConfigureIfcGeometryBasisRevisionRequest
    | UpdateIfcProjectRevisionRequest
    | ApplyIfcSpatialStructureRevisionRequest
    | CompileIfcProductsRevisionRequest
    | EditIfcProductsRevisionRequest
    | InspectExactIfcRevisionRequest
)


@dataclass(frozen=True, slots=True)
class IfcProjectRevisionEffect:
    project: IfcProjectSummary


@dataclass(frozen=True, slots=True)
class IfcSpatialRevisionEffect:
    project_global_id: str
    created: tuple[IfcSpatialCreatedBinding, ...]
    affected_global_ids: tuple[str, ...]
    spatial_summary: tuple[IfcSpatialNodeSummary, ...]

    def __post_init__(self) -> None:
        if _GLOBAL_ID.fullmatch(self.project_global_id) is None:
            raise ValueError("project_global_id must be a valid IFC GlobalId")
        if self.affected_global_ids != tuple(sorted(set(self.affected_global_ids))):
            raise ValueError("affected_global_ids must be unique and sorted")
        if any(_GLOBAL_ID.fullmatch(value) is None for value in self.affected_global_ids):
            raise ValueError("affected_global_ids must contain valid IFC GlobalIds")
        summary_ids = {item.global_id for item in self.spatial_summary}
        if summary_ids != set(self.affected_global_ids):
            raise ValueError("spatial_summary must match affected_global_ids exactly")
        if not {item.global_id for item in self.created}.issubset(summary_ids):
            raise ValueError("created spatial objects must be present in spatial_summary")




IfcRevisionEffect: TypeAlias = IfcProjectRevisionEffect | IfcSpatialRevisionEffect | IfcProductRevisionEffect | IfcProductsEditRevisionEffect


@dataclass(frozen=True, slots=True)
class GeneratedIfcRevision:
    candidate: IfcRevisionCandidateHandle
    inspection: IfcRevisionInspection
    effect: IfcRevisionEffect | None = None

    def __post_init__(self) -> None:
        if self.candidate.size_bytes != self.inspection.size_bytes:
            raise ValueError("candidate size does not match its inspection")
        if self.candidate.sha256 != self.inspection.sha256:
            raise ValueError("candidate digest does not match its inspection")


@dataclass(frozen=True, slots=True)
class InspectedIfcRevision:
    inspection: IfcRevisionInspection
    contexts: IfcRepresentationContextCatalog | None = None
    spatial_page: IfcSpatialStructurePage | None = None
    host_facts: tuple[ResolvedCurrentIfcHostFact, ...] | None = None
    body: IfcProductBodyPreflight | None = None
    products: IfcProductsReadRuntimeResult | None = None
    edit_targets: IfcProductsEditPreflightResult | None = None


IfcRevisionRuntimeResult: TypeAlias = GeneratedIfcRevision | InspectedIfcRevision


class IfcRevisionRuntimeError(RuntimeError):
    """Stable failure returned across the supervised runtime boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        execution_started: bool,
        diagnostic: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.phase = phase
        self.execution_started = execution_started
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None
        self.retryable = retryable
        super().__init__(message)


class IfcGeometryBasisRuntimeBusinessError(
    IfcRevisionRuntimeError,
    IfcGeometryBasisBusinessError,
):
    """Geometry-basis business rejection transported from the child."""


class IfcProjectRuntimeBusinessError(
    IfcRevisionRuntimeError,
    IfcProjectBusinessError,
):
    """Project business rejection transported from the child."""


class IfcSpatialRuntimeBusinessError(
    IfcRevisionRuntimeError,
    IfcSpatialStructureBusinessError,
):
    """Spatial business rejection transported from the child."""


class IfcRevisionRuntimePort(Protocol):
    def invocation_scope(
        self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None,
        on_guard_registered: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> AbstractAsyncContextManager[object]: ...

    async def verify_ready(self) -> None: ...

    async def execute(
        self,
        request: IfcRevisionRuntimeRequest,
        *,
        source: IfcRevisionChunkSource | None = None,
    ) -> IfcRevisionRuntimeResult: ...

    async def close(self) -> None: ...


__all__ = [
    "CompileIfcProductsRevisionRequest",
    "ApplyIfcSpatialStructureRevisionRequest",
    "ConfigureIfcGeometryBasisRevisionRequest",
    "GeneratedIfcRevision",
    "IfcProjectRevisionEffect",
    "IfcProjectRuntimeBusinessError",
    "IfcGeometryBasisRuntimeBusinessError",
    "IfcRevisionCandidateHandle",
    "IfcRevisionChunkSource",
    "IfcRevisionEffect",
    "IfcRevisionRuntimeError",
    "IfcRevisionRuntimePort",
    "IfcRevisionRuntimeRequest",
    "IfcRevisionRuntimeResult",
    "IfcSpatialRevisionEffect",
    "IfcSpatialRuntimeBusinessError",
    "InitializeIfcRevisionRequest",
    "InspectedIfcRevision",
    "InspectExactIfcRevisionRequest",
    "UpdateIfcProjectRevisionRequest",
]
