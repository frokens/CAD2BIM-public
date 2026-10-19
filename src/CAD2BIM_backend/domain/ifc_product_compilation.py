"""Private projection from checked Family realizations to the IFC compiler.

Family/proposal semantics stay with their existing owners. These records add
only input-row identity, exact representation sources and compiler effects.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    IfcGeometryBasisSummary, IfcProductBodyContextFacts,
    IfcRepresentationContextNotFoundError, IfcRepresentationContextIncompatibleError,
)
from CAD2BIM_backend.domain.ifc_geometry_construction import (
    GeometryConstruction,
    IfcGeometryRepresentationRoute,
)
from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.domain.product_realization import ProductRealizationResult
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationComponentWorldTransform, SolvedProductRealizationProposal,
)
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D, compose_rigid_transform


# tesselate's linear deflection in metre-based canonical BREP coordinates.
IFC_PRODUCT_LINEAR_DEFLECTION_M = 0.001


class _CompilationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CompileIfcProductsCommand(_CompilationModel):
    family_contract: ProductFamilyContract


class IfcProductFragmentSource(_CompilationModel):
    fragment_key: str = Field(min_length=1, max_length=64)
    source_key: str = Field(min_length=1, max_length=512)
    local_transform: RigidTransform3D | None = None
    # None references a source already consumed in this same candidate.
    geometry: CanonicalGeometry | None = None
    # The producer supplies 1 when the canonical bytes already contain scale.
    mapping_scale: float = Field(default=1.0, gt=0.0)
    representation_route: IfcGeometryRepresentationRoute = IfcGeometryRepresentationRoute.TFS
    construction: GeometryConstruction | None = None


class IfcProductCompilationItem(_CompilationModel):
    product_key: str = Field(min_length=1, max_length=1024)
    input_row_index: int = Field(ge=0)
    component_project_world_transforms: tuple[ProductRealizationComponentWorldTransform, ...] = Field(min_length=1)
    fragment_sources: tuple[IfcProductFragmentSource, ...] = Field(min_length=1, max_length=256)

    @classmethod
    def from_realization(cls, *, product_key: str, input_row_index: int,
                         realization: ProductRealizationResult, fragment_sources,
                         source_to_fragment_transforms: dict[str, RigidTransform3D] | None = None):
        """Project accepted placement facts; the complete report stays with Apply."""
        if not isinstance(realization.proposal, SolvedProductRealizationProposal):
            raise ValueError("IFC compiler requires a checked solved proposal")
        if realization.frame_check_status == "FAIL" or any(
            not check.evaluation.satisfied for check in realization.external_condition_checks
        ):
            raise ValueError("IFC compiler cannot consume a failed realization")
        frames = {fragment.fragment_key: fragment.local_transform
                  for fragment in realization.proposal.fragments}
        sources = tuple(fragment_sources)
        if len(sources) != len(frames) or {source.fragment_key for source in sources} != set(frames):
            raise ValueError("representation sources do not exactly match realization fragments")
        source_frames = source_to_fragment_transforms or {}
        if not source_frames.keys() <= frames.keys():
            raise ValueError("source transforms must reference realization fragments")
        frames = {key: compose_rigid_transform(frame, source_frames[key])
                  if key in source_frames else frame for key, frame in frames.items()}
        return cls(product_key=product_key, input_row_index=input_row_index,
                   component_project_world_transforms=realization.component_project_world_transforms,
                   fragment_sources=tuple(source.model_copy(update={
                       "local_transform": frames[source.fragment_key]}) for source in sources))


class IfcProductCompilationBlock(_CompilationModel):
    items: tuple[IfcProductCompilationItem, ...] = Field(min_length=1, max_length=1024)


class IfcProductComponentBinding(_CompilationModel):
    product_key: str
    input_row_index: int
    component_key: str
    parent_component_key: str | None
    global_id: str = Field(pattern=r"^[0-9A-Za-z_$]{22}$")


class IfcProductBodyPreflight(_CompilationModel):
    context_identifier: str = "Body"
    project_length_unit_m: float = Field(gt=0.0)
    source_coordinate_unit: str = "METRE"
    linear_deflection_source_units: float = Field(gt=0.0)


def preflight_product_body_facts(
    contexts: tuple[IfcProductBodyContextFacts, ...],
    geometry_basis: IfcGeometryBasisSummary | None,
    project_length_unit_m: float | None,
) -> IfcProductBodyPreflight:
    if not contexts:
        raise IfcRepresentationContextNotFoundError(
            "Body; configure it with ifc_model_configure_geometry_basis")
    if len(contexts) != 1:
        raise IfcRepresentationContextIncompatibleError("Body", "must be unique")
    if geometry_basis is None or project_length_unit_m is None:
        raise IfcRepresentationContextIncompatibleError(
            "Body", "project geometry_basis is incomplete: requires project length and plane-angle units "
            "(LENGTHUNIT and PLANEANGLEUNIT) and exactly one 3D Model context; "
            "inspect and configure these with ifc_model_configure_geometry_basis")
    body = contexts[0]
    if (body.context_type != "Model" or body.target_view != "MODEL_VIEW"
            or body.parent_entity_type != "IfcGeometricRepresentationContext"
            or body.parent_context_type != "Model" or body.parent_coordinate_space_dimension != 3):
        raise IfcRepresentationContextIncompatibleError(
            "Body", "requires Model/3D parent and MODEL_VIEW; use ifc_model_configure_geometry_basis")
    return IfcProductBodyPreflight(
        project_length_unit_m=project_length_unit_m,
        linear_deflection_source_units=IFC_PRODUCT_LINEAR_DEFLECTION_M,
    )


class IfcProductRevisionEffect(_CompilationModel):
    bindings: tuple[IfcProductComponentBinding, ...]
    source_count: int = Field(ge=0)
    mapped_item_count: int = Field(ge=0)
    triangle_count: int = Field(ge=0)
    block_count: int = Field(ge=0)
    body: IfcProductBodyPreflight
    timings: dict[str, float]


class IfcProductCompilationError(RuntimeError):
    code = "IFC_PRODUCT_COMPILATION_FAILED"

    def __init__(self, message: str, *, product_key: str | None = None,
                 input_row_index: int | None = None, phase: str = "compile") -> None:
        self.product_key = product_key
        self.input_row_index = input_row_index
        self.phase = phase
        self.diagnostic = {"product_key": product_key, "input_row_index": input_row_index,
                           "stage": phase}
        super().__init__(message)
