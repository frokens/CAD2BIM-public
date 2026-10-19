"""Immutable packaging of actual build bytes and existing realization facts.

The asset is evidence, never an installation request or an execution qualification.
No source program or caller-uploaded shape can enter this format through Provider
inputs.  Its creators consume the backend's already validated build execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, JsonValue, model_validator

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionContract,
    GeometryInspectionCoordinates,
    GeometryInspectionDiagnostic,
    GeometryInspectionSelection,
    GeometrySectionObservation,
)
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_catalog import GeometryPreviewReferenceSnapshot
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryPreview,
    GeometryPreviewProfile,
    GeometryRenderScene,
    GeometrySummary,
)
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyContract,
    ProductFamilyGeometryBindingRecord,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation,
    ProductRealizationResult,
    ProductRealizationStatus,
)
from CAD2BIM_backend.domain.preview_cases import PreviewCaseReport, PreviewSourceSnapshot

if TYPE_CHECKING:
    from CAD2BIM_backend.domain.geometry_runtime import GeometrySectionResult

GEOMETRY_INSPECTION_MEDIA_TYPE = "application/vnd.cad2bim.geometry-inspection+zip"
GEOMETRY_INSPECTION_MAX_BYTES = 128 * 1024 * 1024
GEOMETRY_INSPECTION_MANIFEST_MAX_BYTES = 2 * 1024 * 1024


class GeometryInspectionSampleSource(GeometryInspectionContract):
    source_kind: Literal["geometry_sample"] = "geometry_sample"
    resource_id: str = Field(min_length=1, max_length=80)
    resource_version_id: str = Field(min_length=1, max_length=80)
    resource_version_ref: str = Field(min_length=1, max_length=16_384)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters: dict[str, JsonValue]
    datum_transform: RigidTransform3D | None = None


class GeometryInspectionFamilySource(GeometryInspectionContract):
    source_kind: Literal["family_realization"] = "family_realization"
    family_id: str = Field(min_length=1, max_length=80)
    family_version_id: str = Field(min_length=1, max_length=80)
    family_version_ref: str = Field(min_length=1, max_length=16_384)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    invocation: ProductRealizationInvocation
    geometry_bindings: tuple[ProductFamilyGeometryBindingRecord, ...]


GeometryInspectionSource = Annotated[
    GeometryInspectionSampleSource | GeometryInspectionFamilySource,
    Field(discriminator="source_kind"),
]


class GeometryInspectionGeometryMember(GeometryInspectionContract):
    """One address of actual bytes; repeated bytes share the same ZIP member."""

    object_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    role: Literal["whole", "fragment", "reference", "display_context"]
    member: str = Field(pattern=r"^geometry/[0-9a-f]{64}\.bin$")
    format: CanonicalGeometryFormat
    content_type: str = Field(min_length=1, max_length=120)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=64 * 1024 * 1024)
    summary: GeometrySummary | None = None
    component_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    binding_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    result_from_local: RigidTransform3D = Field(default_factory=RigidTransform3D)
    uniform_scale: float = Field(default=1.0, gt=0, allow_inf_nan=False,
        description="Scale already applied to these exact instance bytes; never apply it again.")


class GeometryInspectionManifest(GeometryInspectionContract):
    owner_id: str
    tenant_id: str
    task_id: str
    origin_run_id: str
    operation_id: str
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source_snapshot: PreviewSourceSnapshot | None = None
    created_at: datetime
    title: str = Field(min_length=1, max_length=255)
    result_coordinate_space: Literal["geometry_local", "project_world"]
    source: GeometryInspectionSource
    geometries: tuple[GeometryInspectionGeometryMember, ...] = Field(min_length=1, max_length=138)
    scene: GeometryRenderScene
    geometry_interfaces: GeometryModelInterfaces = ()
    reference_geometries: tuple[GeometryPreviewReferenceSnapshot, ...] = Field(default=(), max_length=8)
    family_contract: ProductFamilyContract | None = None
    realization: ProductRealizationResult | None = None

    @model_validator(mode="after")
    def complete_result(self):
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("inspection creation time must be timezone-aware")
        wholes = [item for item in self.geometries if item.role == "whole"]
        if len(wholes) != 1 or wholes[0].sha256 != self.scene.geometry_sha256:
            raise ValueError("inspection scene must identify its one exact whole geometry")
        contexts = [item for item in self.geometries if item.role == "display_context"]
        if len(contexts) > 1 or bool(contexts) != (self.scene.display_context_sha256 is not None):
            raise ValueError("inspection scene must identify its optional one display context")
        if contexts and (contexts[0].sha256 != self.scene.display_context_sha256
                or contexts[0].format is not CanonicalGeometryFormat.OCCT_BREP_V1):
            raise ValueError("inspection display context differs from its exact scene bytes")
        identities = [(item.role, item.object_key) for item in self.geometries]
        if len(identities) != len(set(identities)):
            raise ValueError("inspection object identities must be unique within their role")
        fragments = {item.object_key: item for item in self.geometries if item.role == "fragment"}
        if isinstance(self.source, GeometryInspectionFamilySource):
            if self.family_contract is None or self.realization is None:
                raise ValueError("family inspection needs its original contract and realization")
            if self.realization.proposal.status is not ProductRealizationStatus.SOLVED:
                raise ValueError("only a solved realization can retain actual geometry")
            receipts = {item.fragment_key: item for item in self.realization.instance_receipts}
            if set(fragments) != set(receipts):
                raise ValueError("retained fragments must match every actual receipt")
            for key, receipt in receipts.items():
                item = fragments[key]
                if (item.sha256 != receipt.geometry_sha256
                        or item.binding_key != receipt.binding_key or item.uniform_scale != receipt.uniform_scale):
                    raise ValueError("retained fragment differs from its actual instance receipt")
        elif self.family_contract is not None or self.realization is not None:
            raise ValueError("a geometry sample cannot carry invented Family facts")
        return self


class GeometryInspectionModelObservation(GeometryInspectionContract):
    observation_kind: Literal["model"] = "model"
    render_profile: GeometryPreviewProfile | None = None


GeometryInspectionObservation = Annotated[
    GeometryInspectionModelObservation | GeometrySectionObservation,
    Field(discriminator="observation_kind"),
]


@dataclass(frozen=True, slots=True)
class GeometryInspectionPreviewOutcome:
    inspection_ref: str
    source_kind: Literal["geometry_sample", "family_realization"]
    source_version_ref: str
    result_coordinate_space: Literal["geometry_local", "project_world"]
    selection: GeometryInspectionSelection
    coordinates: GeometryInspectionCoordinates
    preview: GeometryPreview | None = None
    section: GeometrySectionResult | None = None
    operation_alias: str | None = None
    operation_ref: str | None = None
    case_key: str | None = None
    diagnostics: tuple[GeometryInspectionDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class GeometryInspectionOperationResult:
    inspection_ref: str | None
    source_kind: Literal["geometry_sample", "family_realization"]
    source_version_ref: str
    summary: str
    request_json: str | None = None
    result_kind: Literal["inspection", "case_report"] = "inspection"
    case_report: PreviewCaseReport | None = None
    case_count: int | None = None
    checks_passed: bool | None = None
    case_index: tuple[dict, ...] = ()
