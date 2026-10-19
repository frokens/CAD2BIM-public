"""Entry-neutral contracts for the reusable geometry runtime.

The runtime consumes exact geometry bytes.  Catalog identities, storage paths,
database records, Provider aliases, and authoring Operations are deliberately
outside this boundary.
"""

from __future__ import annotations

import hashlib
import math
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

if TYPE_CHECKING:
    from jsonschema import Draft202012Validator

from CAD2BIM_backend.domain.validation_diagnostics import json_schema_validation_message

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
    model_serializer,
)

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D, Vector3
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionCoordinates,
    GeometryInspectionSelection,
    GeometrySectionDisplay,
    GeometrySectionObservation,
    require_inspection_coordinate_selection,
)
from CAD2BIM_backend.domain.geometry_interface import (
    GeometryInterfaceProfile,
    GeometryModelInterfaces,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS = 16
MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES = 64 * 1024
MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES = 256 * 1024
# 256 KiB exact UTF-8 source plus deterministic ZIP headers for at most 16
# normalized paths.  This is an envelope guard, not extra source capacity.
_MAX_AUTHORING_BUNDLE_BYTES = MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES + 16 * 1024
_MAX_PARAMETER_DOCUMENT_BYTES = 16 * 1024
MAX_GEOMETRY_PARAMETER_SCHEMA_DEPTH = 4
MAX_GEOMETRY_PARAMETER_SCHEMA_NODES = 256
MAX_GEOMETRY_PARAMETER_ARRAY_ITEMS = 128
MAX_GEOMETRY_PREVIEW_PANELS = 4
MAX_GEOMETRY_BUILD_INPUTS = 8
MAX_GEOMETRY_BUILD_INPUT_BYTES = 32 * 1024 * 1024
GEOMETRY_INPUT_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS = 128
MAX_GEOMETRY_RENDER_SOURCE_PATHS = 16
MAX_GEOMETRY_RENDER_SOURCE_POINTS_PER_PATH = 256
MAX_GEOMETRY_RENDER_SOURCE_POINTS_TOTAL = 1024
_PARAMETER_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
_PREVIEW_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_OVERLAY_ID_PATTERN = r"^(?:(?:model|family|host|temporary):[a-z][a-z0-9_]{0,63}|candidate:[a-z][a-z0-9_]{0,63}(?::(?:section_[0-9]{1,2}|boundary_[a-z][a-z0-9_]{0,63}))?)$"
_STRAIGHT_PROFILE_RECIPE_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}(?::[a-z][a-z0-9_]{0,63})?(?::section_[0-9]{1,2})?$"
_STRAIGHT_PROFILE_DISPLAY_RECIPE_KEY_PATTERN = r"^(?:host|candidate|context):[a-z][a-z0-9_]{0,63}(?::section_[0-9]{1,2})?$"
Coordinate3 = tuple[StrictFloat, StrictFloat, StrictFloat]
Triangle3 = tuple[StrictInt, StrictInt, StrictInt]


class GeometryRuntimeError(Exception):
    """Base class for stable, expected geometry-runtime failures."""


class GeometryRuntimeValidationError(GeometryRuntimeError, ValueError):
    """Geometry bytes or a query result violate the fixed runtime ABI."""


class GeometryPreviewRequestError(GeometryRuntimeError, ValueError):
    """A requested view cannot frame the selected exact scene interfaces."""


class GeometryRuntimeUnavailableError(GeometryRuntimeError, RuntimeError):
    """The configured runtime or its required query supervisor is unavailable."""

    def __init__(
        self, message: str, *, diagnostic: dict[str, object] | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic
        self.retryable = retryable


class GeometryRuntimeResourceLimitError(GeometryRuntimeUnavailableError):
    """Started native work exhausted a supervised limit and must not auto-retry."""

    def __init__(
        self, message: str, *, diagnostic: GeometryRuntimeResourceDiagnostic | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class GeometryRuntimeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GeometryRuntimeResourceDiagnostic(GeometryRuntimeContract):
    """Safe observation projection, never another execution-budget authority."""

    operation: Literal["normalize", "normalize_batch", "reopen", "render", "section", "build_display_context", "export_stl"] | None = None
    resource_kind: Literal[
        "wall", "cpu", "rss", "threads", "open_file_descriptors", "scratch",
        "capture", "subprocess", "cancelled", "unknown",
    ]
    execution_started: Literal[True] = True
    configured_limits: dict[Literal[
        "wall_seconds", "cpu_seconds", "rss_bytes", "scratch_bytes", "threads",
        "open_file_descriptors", "stdout_bytes", "stderr_bytes", "cleanup_seconds",
    ], StrictFloat | StrictInt]
    sampled_peaks: dict[Literal[
        "peak_rss_bytes", "peak_cpu_seconds", "peak_threads",
        "peak_open_file_descriptors", "peak_scratch_bytes",
    ], StrictFloat | StrictInt] | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    effective_wall_seconds: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)

    def limit_summary(self) -> str:
        """Describe measured resource facts without exposing worker identities."""
        fields = {
            "wall": ("wall_seconds", None, "wall-clock seconds"),
            "cpu": ("cpu_seconds", "peak_cpu_seconds", "CPU seconds"),
            "rss": ("rss_bytes", "peak_rss_bytes", "RSS bytes"),
            "scratch": ("scratch_bytes", "peak_scratch_bytes", "scratch bytes"),
            "threads": ("threads", "peak_threads", "threads"),
            "open_file_descriptors": ("open_file_descriptors", "peak_open_file_descriptors", "open descriptors"),
        }
        stage = f" Stage: {self.operation}." if self.operation else ""
        if self.resource_kind not in fields:
            return f"{stage} Resource: {self.resource_kind}."
        limit_key, peak_key, unit = fields[self.resource_kind]
        limit = self.configured_limits.get(limit_key)
        observed = (self.sampled_peaks or {}).get(peak_key)
        if self.resource_kind == "wall":
            limit = self.effective_wall_seconds if self.effective_wall_seconds is not None else limit
            observed = self.elapsed_seconds
        values = []
        if observed is not None:
            values.append(f"{observed:.3f} observed")
        if limit is not None:
            values.append(f"{limit:.3f} limit")
        return f"{stage} {unit}: {', '.join(values) if values else 'measurement unavailable'}."

    @field_validator("configured_limits", "sampled_peaks")
    @classmethod
    def require_safe_observed_numbers(cls, values):
        if values is not None and any(not math.isfinite(v) or v < 0 for v in values.values()):
            raise ValueError("resource observations must be finite and nonnegative")
        return values


class CanonicalGeometryFormat(StrEnum):
    OCCT_BREP_V1 = "occt_brep_v1"
    INDEXED_TRIANGLE_MESH_V1 = "indexed_triangle_mesh_v1"

    @property
    def content_type(self) -> str:
        """Return the one canonical media type for this immutable payload format."""

        if self is CanonicalGeometryFormat.OCCT_BREP_V1:
            return "application/vnd.opencascade.brep"
        return "application/vnd.cad2bim.indexed-triangle-mesh+json;version=1"


class GeometrySourceFormat(StrEnum):
    OCCT_BREP = "occt_brep"
    INDEXED_TRIANGLE_MESH = "indexed_triangle_mesh"
    IFC = "ifc"


class GeometryPreviewView(StrEnum):
    ISOMETRIC = "isometric"
    FRONT = "front"
    BACK = "back"
    TOP = "top"
    BOTTOM = "bottom"
    RIGHT = "right"
    LEFT = "left"


class GeometryPreviewInterfaceFocusOrientation(StrEnum):
    SIDE_X = "side_x"
    SIDE_Y = "side_y"
    AXIAL = "axial"
    OBLIQUE = "oblique"


class GeometryRenderCoordinateSpace(StrEnum):
    GEOMETRY_COORDINATES = "geometry_coordinates"
    PROJECT_WORLD = "project_world"


class GeometryRenderInterfaceMarkerKind(StrEnum):
    MODEL = "model"
    FAMILY_INTERNAL = "family_internal"
    FAMILY_EXTERNAL = "family_external"
    HOST_ATTACHMENT = "host_attachment"
    HOST_CANDIDATE = "host_candidate"
    TEMPORARY = "temporary"


class GeometryPreviewPresetFitCamera(GeometryRuntimeContract):
    camera_kind: Literal["preset_fit"] = "preset_fit"
    view: GeometryPreviewView


class GeometryPreviewPresetRegionCamera(GeometryRuntimeContract):
    camera_kind: Literal["preset_region"] = "preset_region"
    view: GeometryPreviewView
    target_m: Vector3
    vertical_extent_m: float = Field(gt=0.0, allow_inf_nan=False)

    @field_validator("target_m")
    @classmethod
    def require_finite_target(cls, value: Vector3) -> Vector3:
        return _require_finite_vector(value, label="preview camera target")


class GeometryPreviewCustomCamera(GeometryRuntimeContract):
    camera_kind: Literal["custom"] = "custom"
    target_m: Vector3
    direction: Vector3
    up: Vector3
    vertical_extent_m: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_orthogonal_camera_axes(self) -> Self:
        _require_finite_vector(self.target_m, label="preview camera target")
        _require_unit_vector(self.direction, label="preview camera direction")
        _require_unit_vector(self.up, label="preview camera up")
        if abs(_dot(self.direction, self.up)) > 1e-9:
            raise ValueError("preview camera direction and up must be perpendicular")
        return self


class GeometryPreviewInterfaceFocusCamera(GeometryRuntimeContract):
    camera_kind: Literal["interface_focus"] = "interface_focus"
    overlay_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    orientation: GeometryPreviewInterfaceFocusOrientation
    vertical_extent_m: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)

    @field_validator("overlay_ids")
    @classmethod
    def require_unique_overlay_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_overlay_ids(value)
        if len(value) != len(set(value)):
            raise ValueError("interface-focus camera overlay ids must be unique")
        return value


GeometryPreviewCamera = Annotated[
    GeometryPreviewPresetFitCamera
    | GeometryPreviewPresetRegionCamera
    | GeometryPreviewCustomCamera
    | GeometryPreviewInterfaceFocusCamera,
    Field(discriminator="camera_kind"),
]


class GeometryPreviewPanel(GeometryRuntimeContract):
    panel_key: str = Field(pattern=_PREVIEW_KEY_PATTERN)
    camera: GeometryPreviewCamera


class GeometryPreviewAllInterfacesFocus(GeometryRuntimeContract):
    focus_kind: Literal["all"] = "all"


class GeometryPreviewInterfacePairFocus(GeometryRuntimeContract):
    focus_kind: Literal["interface_pair"] = "interface_pair"
    overlay_ids: tuple[str, str]

    @field_validator("overlay_ids")
    @classmethod
    def require_unique_pair(cls, value: tuple[str, str]) -> tuple[str, str]:
        _require_overlay_ids(value)
        if value[0] == value[1]:
            raise ValueError("preview interface pair overlay ids must be unique")
        return value


GeometryPreviewInterfaceFocus = Annotated[
    GeometryPreviewAllInterfacesFocus | GeometryPreviewInterfacePairFocus,
    Field(discriminator="focus_kind"),
]


class GeometryPreviewCleanOverlayRequest(GeometryRuntimeContract):
    overlay_kind: Literal["clean"] = "clean"


class GeometryPreviewInterfacesOverlayRequest(GeometryRuntimeContract):
    overlay_kind: Literal["interfaces"] = "interfaces"
    focus: GeometryPreviewInterfaceFocus = Field(default_factory=GeometryPreviewAllInterfacesFocus)
    temporary_interfaces: GeometryModelInterfaces | None = None

    @model_serializer(mode="wrap")
    def preserve_inherited_temporary_interfaces(self, handler):
        result = handler(self)
        if self.temporary_interfaces is None:
            result.pop("temporary_interfaces", None)
        return result


GeometryPreviewOverlayRequest = Annotated[
    GeometryPreviewCleanOverlayRequest | GeometryPreviewInterfacesOverlayRequest,
    Field(discriminator="overlay_kind"),
]


class GeometryPreviewPresentation(GeometryRuntimeContract):
    panels: tuple[GeometryPreviewPanel, ...] = Field(
        min_length=1,
        max_length=MAX_GEOMETRY_PREVIEW_PANELS,
    )
    overlay: GeometryPreviewOverlayRequest = Field(
        default_factory=GeometryPreviewCleanOverlayRequest
    )

    @model_validator(mode="after")
    def require_unique_panel_keys(self) -> Self:
        keys = tuple(panel.panel_key for panel in self.panels)
        if len(keys) != len(set(keys)):
            raise ValueError("preview panel keys must be unique")
        return self


class GeometryPreviewPipeSegment(GeometryRuntimeContract):
    start_m: Vector3
    end_m: Vector3
    outer_diameter_m: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_finite_nonzero_segment(self) -> Self:
        _require_finite_vector(self.start_m, label="display pipe start")
        _require_finite_vector(self.end_m, label="display pipe end")
        length = math.dist(self.start_m, self.end_m)
        if not math.isfinite(length) or length <= 0:
            raise ValueError("display pipe endpoints must define a finite nonzero segment")
        return self


class GeometryPreviewNoConnectionContext(GeometryRuntimeContract):
    mode: Literal["none"] = "none"


class GeometryPreviewAutoConnectionContext(GeometryRuntimeContract):
    mode: Literal["auto"] = "auto"


class GeometryPreviewPipesConnectionContext(GeometryRuntimeContract):
    mode: Literal["pipes"] = "pipes"
    items: tuple[GeometryPreviewPipeSegment, ...] = Field(min_length=1, max_length=64)


GeometryPreviewConnectionContext = Annotated[
    GeometryPreviewNoConnectionContext | GeometryPreviewAutoConnectionContext
    | GeometryPreviewPipesConnectionContext, Field(discriminator="mode"),
]


class GeometryPreviewProfile(GeometryRuntimeContract):
    view: GeometryPreviewView = GeometryPreviewView.ISOMETRIC
    width_px: int = Field(default=640, ge=64, le=2048)
    height_px: int = Field(default=480, ge=64, le=2048)
    presentation: GeometryPreviewPresentation | None = None
    connection_context: GeometryPreviewConnectionContext | None = None

    @model_validator(mode="after")
    def require_unambiguous_presentation(self) -> Self:
        if self.presentation is None:
            return self
        if self.view is not GeometryPreviewView.ISOMETRIC:
            raise ValueError("modern preview presentation cannot also select a legacy view")
        panel_count = len(self.presentation.panels)
        columns, rows = (1, 1) if panel_count == 1 else (2, 1) if panel_count == 2 else (2, 2)
        if self.width_px // columns < 64 or self.height_px // rows < 64:
            raise ValueError("preview canvas is too small for the requested panels")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_profile(self, handler):
        result = handler(self)
        if self.presentation is None:
            result.pop("presentation", None)
        else:
            result.pop("view", None)
        if self.connection_context is None:
            result.pop("connection_context", None)
        return result


class GeometryStraightProfileRecipe(GeometryRuntimeContract):
    recipe_key: str = Field(pattern=_STRAIGHT_PROFILE_RECIPE_KEY_PATTERN, max_length=140)
    frame: RigidTransform3D
    profile: GeometryInterfaceProfile
    length_m: float = Field(gt=0.0, allow_inf_nan=False)


class GeometryRenderInterfaceOverlay(GeometryRuntimeContract):
    overlay_id: str = Field(pattern=_OVERLAY_ID_PATTERN, max_length=160)
    label: str = Field(min_length=1, max_length=255)
    marker_kind: GeometryRenderInterfaceMarkerKind
    frame: RigidTransform3D
    profile: GeometryInterfaceProfile | None = None

    @model_validator(mode="after")
    def require_matching_source_and_marker_kind(self) -> Self:
        source_kind, _separator, _source_key = self.overlay_id.partition(":")
        allowed_markers = {
            "model": {GeometryRenderInterfaceMarkerKind.MODEL},
            "family": {
                GeometryRenderInterfaceMarkerKind.FAMILY_INTERNAL,
                GeometryRenderInterfaceMarkerKind.FAMILY_EXTERNAL,
            },
            "host": {GeometryRenderInterfaceMarkerKind.HOST_ATTACHMENT},
            "candidate": {GeometryRenderInterfaceMarkerKind.HOST_CANDIDATE},
            "temporary": {GeometryRenderInterfaceMarkerKind.TEMPORARY},
        }
        if self.marker_kind not in allowed_markers[source_kind]:
            raise ValueError("render interface overlay source and marker kind do not match")
        return self


class GeometryRenderHostOverlay(GeometryRuntimeContract):
    overlay_id: str = Field(pattern=r"^host:[a-z][a-z0-9_]{0,63}$", max_length=69)
    construction_kind: Literal["none", "illustrative", "retained_straight", "unsupported"]


class GeometryRenderStraightProfileOverlay(GeometryStraightProfileRecipe):
    recipe_key: str = Field(pattern=_STRAIGHT_PROFILE_DISPLAY_RECIPE_KEY_PATTERN, max_length=86)
    display_kind: Literal["original", "illustrative", "candidate"]

    @model_validator(mode="after")
    def require_matching_recipe_role(self) -> Self:
        prefix, _separator, _key = self.recipe_key.partition(":")
        if self.display_kind == "candidate" and prefix != "candidate":
            raise ValueError("candidate straight-profile overlay requires a candidate recipe key")
        if self.display_kind == "original" and prefix != "host":
            raise ValueError("straight-profile recipe key does not match its display kind")
        if self.display_kind == "illustrative" and prefix not in {"host", "context"}:
            raise ValueError("straight-profile recipe key does not match its display kind")
        return self


class GeometryRenderSourcePathOverlay(GeometryRuntimeContract):
    """Display-only ordered source polyline in the scene coordinate space."""

    overlay_id: str = Field(pattern=r"^source:[a-z][a-z0-9_]{0,63}$", max_length=71)
    source_label: str = Field(min_length=1, max_length=160)
    points_m: tuple[Vector3, ...] = Field(
        min_length=2, max_length=MAX_GEOMETRY_RENDER_SOURCE_POINTS_PER_PATH,
    )

    @field_validator("source_label")
    @classmethod
    def require_visible_label(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("source path label must be visible plain text")
        return value

    @field_validator("points_m")
    @classmethod
    def require_finite_points(cls, value: tuple[Vector3, ...]) -> tuple[Vector3, ...]:
        for point in value:
            _require_finite_vector(point, label="source path point")
        return value


class GeometryRenderPlacement(GeometryRuntimeContract):
    """Exact local canonical source and the rigid transform used for its world BREP."""

    datum_transform: RigidTransform3D
    local_geometry_sha256: str = Field(pattern=_SHA256_PATTERN)


class GeometryRenderReference(GeometryRuntimeContract):
    """One display-only canonical source placed in the scene coordinate space."""

    reference_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    geometry_format: CanonicalGeometryFormat
    datum_transform: RigidTransform3D


MAX_GEOMETRY_RENDER_REFERENCE_BYTES = 32 * 1024 * 1024


class GeometryRenderScene(GeometryRuntimeContract):
    reference_geometries: tuple[GeometryRenderReference, ...] = Field(default=(), max_length=8)
    coordinate_space: GeometryRenderCoordinateSpace
    geometry_placement: GeometryRenderPlacement | None = None
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    display_context_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    connection_context: GeometryPreviewPipesConnectionContext | None = None
    connection_context_diagnostics: tuple[str, ...] = ()
    interface_overlays: tuple[GeometryRenderInterfaceOverlay, ...] = Field(
        default=(),
        max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    )
    host_overlays: tuple[GeometryRenderHostOverlay, ...] = Field(
        default=(),
        max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    )
    straight_profile_overlays: tuple[GeometryRenderStraightProfileOverlay, ...] = Field(
        default=(),
        max_length=MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS,
    )
    source_path_overlays: tuple[GeometryRenderSourcePathOverlay, ...] = Field(
        default=(), max_length=MAX_GEOMETRY_RENDER_SOURCE_PATHS,
    )

    @model_validator(mode="after")
    def require_closed_overlays(self) -> Self:
        if len({item.reference_key for item in self.reference_geometries}) != len(self.reference_geometries):
            raise ValueError("render reference keys must be unique")
        if self.geometry_placement is not None and self.coordinate_space is not GeometryRenderCoordinateSpace.PROJECT_WORLD:
            raise ValueError("placed geometry scene requires project-world coordinates")
        overlay_ids = tuple(item.overlay_id for item in self.interface_overlays)
        if len(overlay_ids) != len(set(overlay_ids)):
            raise ValueError("render scene overlay ids must be unique")
        host_ids = tuple(item.overlay_id for item in self.host_overlays)
        if len(host_ids) != len(set(host_ids)):
            raise ValueError("render scene Host overlay ids must be unique")
        recipe_keys = tuple(item.recipe_key for item in self.straight_profile_overlays)
        if len(recipe_keys) != len(set(recipe_keys)):
            raise ValueError("render scene straight-profile recipe keys must be unique")
        source_ids = tuple(item.overlay_id for item in self.source_path_overlays)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("render scene source path overlay ids must be unique")
        if sum(len(item.points_m) for item in self.source_path_overlays) > MAX_GEOMETRY_RENDER_SOURCE_POINTS_TOTAL:
            raise ValueError("render scene source path point budget exceeded")
        if (
            len(self.interface_overlays)
            + len(self.host_overlays)
            + len(self.straight_profile_overlays)
            + len(self.source_path_overlays)
            > MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS
        ):
            raise ValueError("render scene exceeds its combined overlay budget")
        has_surface_context = any(
            item.display_kind != "original" for item in self.straight_profile_overlays
        )
        if (self.display_context_sha256 is None) == has_surface_context:
            raise ValueError(
                "render scene display context and straight-profile overlays must appear together"
            )
        context_recipes = tuple(item for item in self.straight_profile_overlays
                                if item.recipe_key.startswith("context:"))
        if bool(context_recipes) != (self.connection_context is not None):
            raise ValueError("connection context facts and display recipes must appear together")
        if self.connection_context is not None:
            if len(context_recipes) != len(self.connection_context.items):
                raise ValueError("connection context facts must identify every display segment")
            for recipe, pipe in zip(context_recipes, self.connection_context.items, strict=True):
                end = tuple(p + recipe.length_m * row[2] for p, row in zip(recipe.frame.translation_m, recipe.frame.rotation))
                if (math.dist(recipe.frame.translation_m, pipe.start_m) > 1e-9
                        or math.dist(end, pipe.end_m) > 1e-9
                        or recipe.profile.kind != "circular"
                        or recipe.profile.diameter_m != pipe.outer_diameter_m):
                    raise ValueError("connection context facts differ from their display segments")
        interface_markers = {item.overlay_id: item.marker_kind for item in self.interface_overlays}
        host_dispositions = {item.overlay_id: item.construction_kind for item in self.host_overlays}
        straight_by_key = {
            item.recipe_key: item.display_kind for item in self.straight_profile_overlays
        }
        if any(
            interface_markers.get(host_id) is not GeometryRenderInterfaceMarkerKind.HOST_ATTACHMENT
            for host_id in host_dispositions
        ):
            raise ValueError("render Host disposition lacks its Host interface marker")
        for recipe_key, display_kind in straight_by_key.items():
            prefix, _separator, attachment_suffix = recipe_key.partition(":")
            if prefix == "context":
                continue
            attachment_key = attachment_suffix.partition(":")[0]
            host_id = f"host:{attachment_key}"
            if host_id not in host_dispositions:
                raise ValueError("straight-profile overlay lacks its Host disposition")
            marker_id = recipe_key if prefix == "candidate" else host_id
            expected_marker = (
                GeometryRenderInterfaceMarkerKind.HOST_CANDIDATE
                if prefix == "candidate"
                else GeometryRenderInterfaceMarkerKind.HOST_ATTACHMENT
            )
            if interface_markers.get(marker_id) is not expected_marker:
                raise ValueError("straight-profile overlay lacks its matching interface marker")
            if display_kind == "candidate" and host_dispositions[host_id] != "retained_straight":
                raise ValueError("candidate straight profile requires retained-straight status")
        for host_id, construction_kind in host_dispositions.items():
            attachment_key = host_id.removeprefix("host:")
            original_kind = straight_by_key.get(host_id)
            candidate_kind = straight_by_key.get(f"candidate:{attachment_key}")
            expected = {
                "none": (None, None),
                "unsupported": (None, None),
                "illustrative": ("illustrative", None),
                "retained_straight": ("original", "candidate"),
            }[construction_kind]
            if (original_kind, candidate_kind) != expected:
                raise ValueError("Host construction status changed its display primitives")
        if any(
            marker_kind is GeometryRenderInterfaceMarkerKind.HOST_CANDIDATE
            and (
                host_dispositions.get(f"host:{overlay_id.split(':')[1]}")
                != "retained_straight"
                or straight_by_key.get(
                    ':'.join(overlay_id.split(':')[:2])
                    if ':boundary_' in overlay_id else overlay_id
                ) != "candidate"
            )
            for overlay_id, marker_kind in interface_markers.items()
        ):
            raise ValueError("Host candidate marker lacks its retained display context")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_render_scene(self, handler):
        result = handler(self)
        if not self.reference_geometries:
            result.pop("reference_geometries", None)
        if self.geometry_placement is None:
            result.pop("geometry_placement", None)
        if self.display_context_sha256 is None:
            result.pop("display_context_sha256", None)
        if not self.host_overlays:
            result.pop("host_overlays", None)
        if not self.straight_profile_overlays:
            result.pop("straight_profile_overlays", None)
        if not self.source_path_overlays:
            result.pop("source_path_overlays", None)
        if self.connection_context is None:
            result.pop("connection_context", None)
        if not self.connection_context_diagnostics:
            result.pop("connection_context_diagnostics", None)
        return result


class GeometryResolvedOrthographicCamera(GeometryRuntimeContract):
    target_m: Vector3
    direction: Vector3
    up: Vector3
    right: Vector3
    horizontal_extent_m: float = Field(gt=0.0, allow_inf_nan=False)
    vertical_extent_m: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_resolved_camera_basis(self) -> Self:
        _require_finite_vector(self.target_m, label="resolved preview camera target")
        for label, value in (
            ("direction", self.direction),
            ("up", self.up),
            ("right", self.right),
        ):
            _require_unit_vector(value, label=f"resolved preview camera {label}")
        if any(
            abs(_dot(left, right)) > 1e-9
            for left, right in (
                (self.direction, self.up),
                (self.direction, self.right),
                (self.up, self.right),
            )
        ):
            raise ValueError("resolved preview camera axes must be perpendicular")
        expected_right = _cross(self.direction, self.up)
        if any(
            abs(actual - expected) > 1e-9 for actual, expected in zip(self.right, expected_right)
        ):
            raise ValueError("resolved preview camera right must equal direction cross up")
        return self


class GeometryResolvedPreviewPanel(GeometryRuntimeContract):
    panel_key: str = Field(pattern=_PREVIEW_KEY_PATTERN)
    x_px: int = Field(ge=0, le=2048)
    y_px: int = Field(ge=0, le=2048)
    width_px: int = Field(ge=64, le=2048)
    height_px: int = Field(ge=64, le=2048)
    camera: GeometryResolvedOrthographicCamera
    geometry_clipped: bool = False
    visible_overlay_ids: tuple[str, ...] = ()
    hidden_overlay_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_disjoint_overlay_disclosure(self) -> Self:
        _require_overlay_ids(self.visible_overlay_ids)
        _require_overlay_ids(self.hidden_overlay_ids)
        visible = set(self.visible_overlay_ids)
        hidden = set(self.hidden_overlay_ids)
        if len(visible) != len(self.visible_overlay_ids) or len(hidden) != len(
            self.hidden_overlay_ids
        ):
            raise ValueError("resolved preview overlay ids must be unique")
        if visible & hidden:
            raise ValueError("resolved preview overlay ids cannot be both visible and hidden")
        return self


class GeometryDisplayQualityWarning(GeometryRuntimeContract):
    """Display-only heuristic evidence, never canonical or business acceptance."""

    code: Literal["DISPLAY_MESH_AREA_WARNING"] = "DISPLAY_MESH_AREA_WARNING"
    message: str = "Display mesh required a bounded retry; image is diagnostic only. Area agreement does not certify geometric accuracy or business correctness."
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    face_index: int = Field(ge=0, description="Zero-based face index in this canonical geometry.")
    native_area_m2: float = Field(ge=0, allow_inf_nan=False)
    initial_mesh_area_m2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    retry_mesh_area_m2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    initial_relative_difference: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    retry_relative_difference: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    selected_attempt: Literal["initial", "retry"]
    retry_count: Literal[1] = 1
    retry_error: str | None = None


class GeometryRenderedPreviewMetadata(GeometryRuntimeContract):
    metadata_kind: Literal["rendered"] = "rendered"
    scene_sha256: str = Field(pattern=_SHA256_PATTERN)
    coordinate_space: GeometryRenderCoordinateSpace
    display_quality_warnings: tuple[GeometryDisplayQualityWarning, ...] = ()
    connection_context: GeometryPreviewPipesConnectionContext | None = None
    connection_context_diagnostics: tuple[str, ...] = ()
    resolved_panels: tuple[GeometryResolvedPreviewPanel, ...] = Field(
        min_length=1,
        max_length=MAX_GEOMETRY_PREVIEW_PANELS,
    )

    @model_validator(mode="after")
    def require_unique_resolved_panels(self) -> Self:
        keys = tuple(panel.panel_key for panel in self.resolved_panels)
        if len(keys) != len(set(keys)):
            raise ValueError("resolved preview panel keys must be unique")
        return self

    @model_serializer(mode="wrap")
    def preserve_existing_metadata(self, handler):
        result = handler(self)
        if not self.display_quality_warnings:
            result.pop("display_quality_warnings", None)
        if self.connection_context is None:
            result.pop("connection_context", None)
        if not self.connection_context_diagnostics:
            result.pop("connection_context_diagnostics", None)
        return result


class GeometryStoredPreviewMetadata(GeometryRuntimeContract):
    metadata_kind: Literal["stored_preview"] = "stored_preview"


GeometryPreviewRenderMetadata = Annotated[
    GeometryRenderedPreviewMetadata | GeometryStoredPreviewMetadata,
    Field(discriminator="metadata_kind"),
]


def _require_finite_vector(value: Vector3, *, label: str) -> Vector3:
    if len(value) != 3 or any(not math.isfinite(item) for item in value):
        raise ValueError(f"{label} must contain exactly three finite values")
    return value


def _dot(left: Vector3, right: Vector3) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: Vector3, right: Vector3) -> Vector3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _require_unit_vector(value: Vector3, *, label: str) -> Vector3:
    _require_finite_vector(value, label=label)
    try:
        length = math.sqrt(sum(item * item for item in value))
    except OverflowError as exc:
        raise ValueError(f"{label} exceeds finite arithmetic") from exc
    if not math.isfinite(length) or abs(length - 1.0) > 1e-9:
        raise ValueError(f"{label} must be a unit vector")
    return value


def _require_overlay_ids(values: tuple[str, ...]) -> None:
    if any(
        not isinstance(value, str)
        or len(value) > 160
        or re.fullmatch(_OVERLAY_ID_PATTERN, value) is None
        for value in values
    ):
        raise ValueError("preview overlay ids must be source-qualified safe identifiers")


def _profile_requires_scene(profile: GeometryPreviewProfile) -> bool:
    presentation = profile.presentation
    if presentation is None:
        return False
    return isinstance(presentation.overlay, GeometryPreviewInterfacesOverlayRequest) or any(
        isinstance(panel.camera, GeometryPreviewInterfaceFocusCamera)
        for panel in presentation.panels
    )


def _require_profile_scene_closure(
    profile: GeometryPreviewProfile,
    scene: GeometryRenderScene,
) -> None:
    presentation = profile.presentation
    if presentation is None:
        return
    available = {item.overlay_id for item in scene.interface_overlays}
    if isinstance(presentation.overlay, GeometryPreviewCleanOverlayRequest):
        if any(
            isinstance(panel.camera, GeometryPreviewInterfaceFocusCamera)
            for panel in presentation.panels
        ):
            raise ValueError("interface-focus camera requires interface overlays")
        return
    focus = presentation.overlay.focus
    if isinstance(focus, GeometryPreviewInterfacePairFocus) and not set(focus.overlay_ids).issubset(
        available
    ):
        raise ValueError("preview interface pair references an absent scene overlay")
    for panel in presentation.panels:
        camera = panel.camera
        if isinstance(camera, GeometryPreviewInterfaceFocusCamera) and not set(
            camera.overlay_ids
        ).issubset(available):
            raise ValueError("interface-focus camera references an absent scene overlay")


def validate_geometry_preview_scene(
    profile: GeometryPreviewProfile,
    scene: GeometryRenderScene,
) -> None:
    """Validate that one resolved scene closes every presentation reference."""

    _require_profile_scene_closure(profile, scene)


class GeometryBounds(GeometryRuntimeContract):
    minimum_m: Coordinate3
    maximum_m: Coordinate3

    @model_validator(mode="after")
    def require_ordered_bounds(self) -> "GeometryBounds":
        if any(low > high for low, high in zip(self.minimum_m, self.maximum_m, strict=True)):
            raise ValueError("geometry bounds minimum must not exceed maximum")
        return self


class GeometryTopologySummary(GeometryRuntimeContract):
    vertex_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    wire_count: int = Field(ge=0)
    face_count: int = Field(ge=0)
    shell_count: int = Field(ge=0)
    solid_count: int = Field(ge=0)
    triangle_count: int | None = Field(ge=0, description="Intrinsic mesh triangle count; null for BREP. Historical BREP counts are retained as display metadata, never an integrity gate.")
    boundary_edge_count: int = Field(ge=0)


class GeometrySummary(GeometryRuntimeContract):
    format: CanonicalGeometryFormat
    unit: str = Field(default="METRE", pattern=r"^METRE$")
    bounds: GeometryBounds
    topology: GeometryTopologySummary
    surface_area_m2: float = Field(ge=0.0)
    volume_m3: float | None = Field(default=None, ge=0.0)


class IfcGeometrySourceItem(GeometryRuntimeContract):
    """An original representation item, not a backend resource identity."""

    entity_id: int = Field(ge=1)
    ifc_class: str = Field(min_length=1, max_length=80)


class IfcGeometrySourceProvenance(GeometryRuntimeContract):
    """Bounded extraction facts produced by the IFC codec before user execution."""

    source_format: Literal["ifc"] = "ifc"
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    product_entity_id: int = Field(ge=1)
    product_ifc_class: str = Field(min_length=1, max_length=80)
    length_unit_scale_to_metre: float = Field(gt=0.0, allow_inf_nan=False)
    product_placement_m: RigidTransform3D
    canonical_from_product_local: RigidTransform3D = Field(default_factory=RigidTransform3D)
    representation_items: tuple[IfcGeometrySourceItem, ...] = Field(min_length=1, max_length=1024)


class CanonicalGeometry(GeometryRuntimeContract):
    format: CanonicalGeometryFormat
    content_type: str = Field(min_length=1, max_length=120)
    payload: bytes = Field(min_length=1, max_length=64 * 1024 * 1024)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    summary: GeometrySummary | None = None
    source_provenance: IfcGeometrySourceProvenance | None = None

    @model_serializer(mode="wrap")
    def preserve_existing_canonical_metadata(self, handler):
        value = handler(self)
        if self.source_provenance is None:
            value.pop("source_provenance", None)
        return value

    @model_validator(mode="after")
    def require_matching_format(self) -> "CanonicalGeometry":
        if self.content_type != self.format.content_type:
            raise ValueError("canonical geometry content type does not match its format")
        if self.summary is not None and self.summary.format is not self.format:
            raise ValueError("geometry payload and summary formats must match")
        return self


class GeometryInspectionFragmentInput(GeometryRuntimeContract):
    """Backend-owned exact local bytes and the existing result placement facts."""

    fragment_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    component_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    binding_key: str = Field(min_length=1, max_length=128)
    geometry: CanonicalGeometry
    result_from_local: RigidTransform3D
    uniform_scale: float = Field(
        default=1.0, gt=0.0, allow_inf_nan=False,
        description="Receipt scale already applied to these same-build local instance bytes; never apply it again.",
    )


def require_inspection_inputs(selection, coordinates, fragment_inputs) -> None:
    if (selection is None) != (coordinates is None):
        raise ValueError("inspection selection and coordinates must appear together")
    keys = [item.fragment_key for item in fragment_inputs]
    if len(keys) != len(set(keys)):
        raise ValueError("inspection fragment keys must be unique")
    distinct_payloads = {(item.geometry.format, item.geometry.sha256): len(item.geometry.payload) for item in fragment_inputs}
    if sum(distinct_payloads.values()) > 64 * 1024 * 1024:
        raise ValueError("inspection fragment bytes exceed the 64 MiB transport budget")
    if selection is None:
        if fragment_inputs:
            raise ValueError("inspection fragments require explicit selection and coordinates")
        return
    require_inspection_coordinate_selection(selection, coordinates)
    if selection.selection_kind == "fragment" and selection.fragment_key not in keys:
        raise ValueError("inspection fragment selection is not in the exact fragment inputs")
    if selection.selection_kind == "component" and not any(
        item.component_key == selection.component_key for item in fragment_inputs
    ):
        raise ValueError("inspection component selection has no exact fragment inputs")


class GeometrySectionDiagnostic(GeometryRuntimeContract):
    code: Literal[
        "EMPTY_SECTION", "TANGENT_OR_DEGENERATE", "COPLANAR_BOUNDARY",
        "NO_SOLID_MATERIAL", "OPEN_BOUNDARY", "CURVE_SAMPLE_APPROXIMATION",
        "UNION_AREA_NOT_COMPUTED", "SECTION_OPERATION_FAILED",
        "UNSUPPORTED_GEOMETRY_FORMAT", "BOUNDARY_DRAWING_FAILED",
        "DISPLAY_CLIPPED", "SECTION_RENDER_FAILED",
    ]
    message: str = Field(min_length=1, max_length=2000)


class GeometrySectionCurve(GeometryRuntimeContract):
    curve_type: str = Field(min_length=1, max_length=32)
    length_m: float = Field(ge=0.0, allow_inf_nan=False)
    radius_m: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    major_radius_m: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    minor_radius_m: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    center_uv_m: tuple[FiniteFloat, FiniteFloat] | None = None
    drawing_points_uv_m: tuple[tuple[FiniteFloat, FiniteFloat], ...] = Field(default=(), max_length=128)


class GeometrySectionBoundary(GeometryRuntimeContract):
    boundary_index: int = Field(ge=0)
    role: Literal["outer", "hole", "unknown"]
    closed: bool
    length_m: float = Field(ge=0.0, allow_inf_nan=False)
    curves: tuple[GeometrySectionCurve, ...]


class GeometrySectionRegion(GeometryRuntimeContract):
    region_index: int = Field(ge=0)
    area_m2: float = Field(ge=0.0, allow_inf_nan=False)
    boundaries: tuple[GeometrySectionBoundary, ...]


class GeometrySectionObjectMeasurement(GeometryRuntimeContract):
    object_index: int = Field(ge=0)
    fragment_key: str | None = None
    component_key: str | None = None
    binding_key: str | None = None
    source_shape_kind: str = Field(min_length=1, max_length=32)
    status: Literal["computed", "empty", "degenerate", "failed", "unsupported"]
    material_regions: tuple[GeometrySectionRegion, ...] = ()
    material_area_m2: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    intersection_edge_count: int = Field(default=0, ge=0)
    intersection_vertex_count: int = Field(default=0, ge=0)
    intersection_curves: tuple[GeometrySectionCurve, ...] = ()
    intersection_points_uv_m: tuple[tuple[FiniteFloat, FiniteFloat], ...] = ()
    diagnostics: tuple[GeometrySectionDiagnostic, ...] = ()


class GeometrySectionResolvedPlane(GeometryRuntimeContract):
    origin_m: Vector3
    unit_normal: Vector3
    u_axis: Vector3
    v_axis: Vector3

    @model_validator(mode="after")
    def require_finite_orthonormal_basis(self) -> Self:
        RigidTransform3D(translation_m=self.origin_m, rotation=(self.u_axis, self.v_axis, self.unit_normal))
        return self


class GeometrySectionMeasurement(GeometryRuntimeContract):
    section_index: int = Field(ge=0, le=3)
    offset_m: float = Field(allow_inf_nan=False)
    plane: GeometrySectionResolvedPlane
    status: Literal["computed", "empty", "degenerate", "failed", "unsupported"]
    objects: tuple[GeometrySectionObjectMeasurement, ...]
    material_area_sum_m2: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    area_semantics: Literal["sum_of_object_material_regions_not_union"] = "sum_of_object_material_regions_not_union"
    projected_bounds_uv_m: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat] | None = Field(
        default=None, description="u_min,u_max,v_min,v_max of all measured boundaries, before display cropping."
    )
    diagnostics: tuple[GeometrySectionDiagnostic, ...] = ()

    @field_validator("projected_bounds_uv_m")
    @classmethod
    def require_ordered_projected_bounds(cls, value):
        if value is not None and (value[0] > value[1] or value[2] > value[3]):
            raise ValueError("section projected bounds must be ordered")
        return value


class GeometrySectionRequest(GeometryRuntimeContract):
    geometry: CanonicalGeometry
    selection: GeometryInspectionSelection
    coordinates: GeometryInspectionCoordinates
    observation: GeometrySectionObservation
    fragment_inputs: tuple[GeometryInspectionFragmentInput, ...] = Field(default=(), max_length=128)
    width_px: int = Field(default=640, ge=64, le=2048)
    height_px: int = Field(default=480, ge=64, le=2048)

    @model_validator(mode="after")
    def require_exact_selection(self) -> Self:
        require_inspection_inputs(self.selection, self.coordinates, self.fragment_inputs)
        return self


class GeometryBuildDisplayContextRequest(GeometryRuntimeContract):
    recipes: tuple[GeometryStraightProfileRecipe, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_unique_bounded_recipes(self) -> Self:
        keys = tuple(recipe.recipe_key for recipe in self.recipes)
        if len(keys) != len(set(keys)):
            raise ValueError("display-context recipe keys must be unique")
        if len(canonical_json(self.model_dump(mode="json")).encode("utf-8")) > 64 * 1024:
            raise ValueError("display-context recipe request exceeds its byte budget")
        return self


class GeometryBuildDisplayContextResult(GeometryRuntimeContract):
    geometry: CanonicalGeometry
    recipe_digest: str = Field(pattern=_SHA256_PATTERN)
    recipe_keys: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_closed_display_context(self) -> Self:
        if self.geometry.format is not CanonicalGeometryFormat.OCCT_BREP_V1:
            raise ValueError("display context must use canonical OCCT BREP geometry")
        if len(self.recipe_keys) != len(set(self.recipe_keys)) or any(
            re.fullmatch(_STRAIGHT_PROFILE_RECIPE_KEY_PATTERN, key) is None
            for key in self.recipe_keys
        ):
            raise ValueError("display-context result recipe keys are invalid")
        return self


class GeometryNormalizeRequest(GeometryRuntimeContract):
    format: CanonicalGeometryFormat
    source_format: GeometrySourceFormat | None = None
    payload: bytes = Field(min_length=1, max_length=64 * 1024 * 1024)
    unit: str = Field(default="METRE", pattern=r"^METRE$")
    transform: RigidTransform3D | None = None
    include_summary: bool = Field(default=True, strict=True, description="Compute the full geometry measurements; false leaves summary null while still validating actual geometry.")

    @model_validator(mode="after")
    def require_supported_conversion(self) -> "GeometryNormalizeRequest":
        if self.transform is not None and self.format is not CanonicalGeometryFormat.OCCT_BREP_V1:
            raise ValueError("rigid canonical placement currently requires OCCT BREP geometry")
        source = self.effective_source_format
        if (
            source is GeometrySourceFormat.OCCT_BREP
            and self.format is not CanonicalGeometryFormat.OCCT_BREP_V1
        ) or (
            source is GeometrySourceFormat.INDEXED_TRIANGLE_MESH
            and self.format is not CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1
        ):
            raise ValueError("canonical geometry source and target formats must match")
        return self

    @property
    def effective_source_format(self) -> GeometrySourceFormat:
        if self.source_format is not None:
            return self.source_format
        if self.format is CanonicalGeometryFormat.OCCT_BREP_V1:
            return GeometrySourceFormat.OCCT_BREP
        return GeometrySourceFormat.INDEXED_TRIANGLE_MESH


class GeometryRenderRequest(GeometryPreviewProfile):
    reference_geometries: tuple[CanonicalGeometry, ...] = Field(default=(), max_length=8)
    geometry: CanonicalGeometry
    scene: GeometryRenderScene | None = None
    display_context: CanonicalGeometry | None = None
    inspection_selection: GeometryInspectionSelection | None = None
    inspection_coordinates: GeometryInspectionCoordinates | None = None
    fragment_inputs: tuple[GeometryInspectionFragmentInput, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def require_matching_render_scene(self) -> Self:
        require_inspection_inputs(self.inspection_selection, self.inspection_coordinates, self.fragment_inputs)
        if sum(len(item.payload) for item in self.reference_geometries) > MAX_GEOMETRY_RENDER_REFERENCE_BYTES:
            raise ValueError("render references exceed the combined 32 MiB payload budget")
        declarations = self.scene.reference_geometries if self.scene is not None else ()
        if tuple((item.geometry_sha256, item.geometry_format) for item in declarations) != tuple(
            (item.sha256, item.format) for item in self.reference_geometries
        ):
            raise ValueError("render reference payloads disagree with the exact scene")
        if self.scene is not None:
            if self.scene.geometry_sha256 != self.geometry.sha256:
                raise ValueError("render scene geometry digest does not match its geometry")
            if (self.display_context is None) != (self.scene.display_context_sha256 is None):
                raise ValueError("render scene and request display context must appear together")
            if self.display_context is not None:
                if self.display_context.format is not CanonicalGeometryFormat.OCCT_BREP_V1:
                    raise ValueError("render display context must use canonical OCCT BREP geometry")
                if self.scene.display_context_sha256 != self.display_context.sha256:
                    raise ValueError("render scene display-context digest does not match")
            validate_geometry_preview_scene(self, self.scene)
        elif _profile_requires_scene(self) or self.display_context is not None:
            raise ValueError("modern interface preview requires a render scene")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_render_request(self, handler):
        result = handler(self)
        if self.connection_context is None:
            result.pop("connection_context", None)
        if not self.reference_geometries:
            result.pop("reference_geometries", None)
        if self.presentation is None:
            result.pop("presentation", None)
        else:
            result.pop("view", None)
        if self.scene is None:
            result.pop("scene", None)
        if self.display_context is None:
            result.pop("display_context", None)
        if self.inspection_selection is None:
            result.pop("inspection_selection", None)
            result.pop("inspection_coordinates", None)
        if not self.fragment_inputs:
            result.pop("fragment_inputs", None)
        return result


class GeometryDisplayMesh(GeometryRuntimeContract):
    """Display-only STL projected from one exact saved canonical geometry."""

    content_type: Literal["model/stl"] = "model/stl"
    unit: Literal["METRE"] = "METRE"
    payload: bytes = Field(min_length=84, max_length=64 * 1024 * 1024)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    display_quality_warnings: tuple[GeometryDisplayQualityWarning, ...] = ()


class GeometryPreview(GeometryRuntimeContract):
    view: GeometryPreviewView | None = None
    width_px: int = Field(ge=64, le=2048)
    height_px: int = Field(ge=64, le=2048)
    content_type: str = Field(default="image/png", pattern=r"^image/png$")
    payload: bytes = Field(min_length=1, max_length=16 * 1024 * 1024)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    display_quality_warnings: tuple[GeometryDisplayQualityWarning, ...] = ()
    connection_context: GeometryPreviewPipesConnectionContext | None = None
    connection_context_diagnostics: tuple[str, ...] = ()
    resolved_panels: tuple[GeometryResolvedPreviewPanel, ...] = Field(
        default=(),
        max_length=MAX_GEOMETRY_PREVIEW_PANELS,
    )

    @model_validator(mode="after")
    def require_resolved_panels_within_canvas(self) -> Self:
        keys = tuple(panel.panel_key for panel in self.resolved_panels)
        if len(keys) != len(set(keys)):
            raise ValueError("geometry preview resolved panel keys must be unique")
        if any(
            panel.x_px + panel.width_px > self.width_px
            or panel.y_px + panel.height_px > self.height_px
            for panel in self.resolved_panels
        ):
            raise ValueError("geometry preview resolved panel exceeds its canvas")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_preview(self, handler):
        result = handler(self)
        if self.view is None:
            result.pop("view", None)
        if not self.resolved_panels:
            result.pop("resolved_panels", None)
        if not self.display_quality_warnings:
            result.pop("display_quality_warnings", None)
        if self.connection_context is None:
            result.pop("connection_context", None)
        if not self.connection_context_diagnostics:
            result.pop("connection_context_diagnostics", None)
        return result


class GeometrySectionImage(GeometryRuntimeContract):
    section_index: int = Field(ge=0, le=3)
    preview: GeometryPreview
    resolved_display: GeometrySectionDisplay
    display_clipped: bool
    diagnostics: tuple[GeometrySectionDiagnostic, ...] = ()


class GeometrySectionRenderFailure(GeometryRuntimeContract):
    section_index: int = Field(ge=0, le=3)
    phase: Literal["render"] = "render"
    code: Literal["SECTION_RENDER_FAILED"] = "SECTION_RENDER_FAILED"
    message: str = Field(min_length=1, max_length=2000)


class GeometrySectionResult(GeometryRuntimeContract):
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection: GeometryInspectionSelection
    coordinates: GeometryInspectionCoordinates
    measurements: tuple[GeometrySectionMeasurement, ...] = Field(min_length=1, max_length=4)
    images: tuple[GeometrySectionImage, ...] = Field(default=(), max_length=4)
    render_errors: tuple[GeometrySectionRenderFailure, ...] = Field(default=(), max_length=4)


class GeometryBuildInput(GeometryRuntimeContract):
    """One resolved BREP payload, without catalog identities or physical paths."""

    input_key: str = Field(pattern=GEOMETRY_INPUT_KEY_PATTERN)
    payload: bytes = Field(min_length=1, max_length=MAX_GEOMETRY_BUILD_INPUT_BYTES)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def require_exact_payload(self) -> Self:
        if hashlib.sha256(self.payload).hexdigest() != self.sha256:
            raise ValueError("geometry build input payload digest mismatch")
        return self


def _validate_build_inputs(values: tuple[GeometryBuildInput, ...]) -> tuple[GeometryBuildInput, ...]:
    if len({item.input_key for item in values}) != len(values):
        raise ValueError("geometry build input keys must be unique")
    if sum(len(item.payload) for item in values) > MAX_GEOMETRY_BUILD_INPUT_BYTES:
        raise ValueError("geometry build inputs exceed the aggregate 32 MiB limit")
    return tuple(sorted(values, key=lambda item: item.input_key))


GeometryBuildInputs = Annotated[
    tuple[GeometryBuildInput, ...], Field(max_length=MAX_GEOMETRY_BUILD_INPUTS),
    AfterValidator(_validate_build_inputs),
]


class GeometryBuildFixedRequest(GeometryRuntimeContract):
    """Build one fixed shape from the exact ``main.py:build(ctx)`` bundle."""

    source_bundle: bytes = Field(min_length=1, max_length=_MAX_AUTHORING_BUNDLE_BYTES)
    build_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    geometry_inputs: GeometryBuildInputs = ()

    @field_validator("build_parameters")
    @classmethod
    def require_bounded_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _require_parameter_document(value, label="fixed build parameters")
        return value


class GeometryFixedBuildResult(GeometryRuntimeContract):
    geometry: CanonicalGeometry
    interfaces: GeometryModelInterfaces = ()
    source_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    build_parameters_sha256: str = Field(pattern=_SHA256_PATTERN)


class GeometryValidateFunctionRequest(GeometryRuntimeContract):
    """Validate one immutable modeling function against explicit samples."""

    source_bundle: bytes = Field(min_length=1, max_length=_MAX_AUTHORING_BUNDLE_BYTES)
    parameter_schema: dict[str, JsonValue]
    geometry_inputs: GeometryBuildInputs = ()
    retain_sample_geometries: bool = Field(default=False, strict=True)
    sample_parameters: tuple[dict[str, JsonValue], ...] = Field(
        min_length=1,
        max_length=8,
    )

    @model_validator(mode="after")
    def require_safe_schema_and_valid_samples(self) -> "GeometryValidateFunctionRequest":
        prepared = _PreparedGeometryFunctionParameters(self.parameter_schema)
        object.__setattr__(
            self,
            "sample_parameters",
            tuple(
                prepared.normalize(sample)
                for sample in self.sample_parameters
            ),
        )
        return self


class GeometryFunctionInterfaceEvaluation(GeometryRuntimeContract):
    """Invocation-local numeric facts associated with the exact in-memory source."""
    source_bundle: bytes = Field(min_length=1, max_length=_MAX_AUTHORING_BUNDLE_BYTES)
    sample_parameters: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=8)
    sample_interfaces: tuple[GeometryModelInterfaces, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def require_sample_associations(self):
        if len(self.sample_parameters) != len(self.sample_interfaces):
            raise ValueError("numeric interface results must cover the exact sample parameters")
        return self


class GeometryFunctionSampleExecution(GeometryRuntimeContract):
    output_sha256: str = Field(pattern=_SHA256_PATTERN)
    geometry_summary: GeometrySummary
    interfaces: GeometryModelInterfaces = ()

    @model_serializer(mode="wrap")
    def preserve_empty_interface_receipt(self, handler):
        result = handler(self)
        if not self.interfaces:
            result.pop("interfaces", None)
        return result


class GeometryFunctionValidationResult(GeometryRuntimeContract):
    sample_geometry: CanonicalGeometry
    sample_geometries: tuple[CanonicalGeometry, ...] = Field(default=(), max_length=8)
    sample_executions: tuple[GeometryFunctionSampleExecution, ...] = Field(
        min_length=1,
        max_length=8,
    )

    @model_validator(mode="after")
    def require_exact_retained_samples(self):
        if self.sample_geometries:
            if len(self.sample_geometries) != len(self.sample_executions):
                raise ValueError("retained sample geometries must cover the same batch")
            if self.sample_geometries[0] != self.sample_geometry:
                raise ValueError("representative geometry must be the first retained sample")
            for geometry, execution in zip(self.sample_geometries, self.sample_executions, strict=True):
                if geometry.sha256 != execution.output_sha256 or geometry.summary != execution.geometry_summary:
                    raise ValueError("retained sample differs from its actual execution receipt")
        return self



class GeometryQueryRequest(GeometryRuntimeContract):
    geometry: CanonicalGeometry
    source_code: str = Field(min_length=1, max_length=65_536)
    max_result_bytes: int = Field(default=64 * 1024, ge=2, le=1024 * 1024)
    selection: GeometryInspectionSelection | None = None
    coordinates: GeometryInspectionCoordinates | None = None
    inspection_context: dict[str, JsonValue] | None = None
    fragment_inputs: tuple[GeometryInspectionFragmentInput, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def require_exact_inspection(self) -> Self:
        require_inspection_inputs(self.selection, self.coordinates, self.fragment_inputs)
        if self.inspection_context is not None and self.selection is None:
            raise ValueError("inspection context requires explicit selection and coordinates")
        return self

    @field_validator("source_code")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("geometry query source must not contain NUL")
        if len(value.encode("utf-8")) > 65_536:
            raise ValueError("geometry query source exceeds 65536 UTF-8 bytes")
        return value


class IsolatedGeometryQueryRequest(GeometryRuntimeContract):
    """Private request handed to the shared process-supervision seam."""

    format: CanonicalGeometryFormat
    content_type: str = Field(min_length=1, max_length=120)
    geometry_payload: bytes = Field(min_length=1, max_length=64 * 1024 * 1024)
    expected_geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_code: str = Field(min_length=1, max_length=65_536)
    max_result_bytes: int = Field(ge=2, le=1024 * 1024)

    @field_validator("source_code")
    @classmethod
    def require_source_byte_budget(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > 65_536:
            raise ValueError("isolated geometry query source is invalid")
        return value


class IsolatedGeometryQueryExecution(GeometryRuntimeContract):
    """Bounded receipt produced by an OS-isolated read-only query execution."""

    result: JsonValue
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)


class GeometryQueryResult(GeometryRuntimeContract):
    result: JsonValue
    result_bytes: int = Field(ge=0, le=1024 * 1024)
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)


IndexedVertex = Annotated[Coordinate3, Field()]
IndexedTriangle = Annotated[Triangle3, Field()]


def _tagged_object_discriminator(branches: list[dict[str, Any]]) -> str:
    candidates = None
    for branch in branches:
        properties = branch.get("properties", {})
        required = branch.get("required", [])
        tags = {key for key, child in properties.items()
                if key in required and child.get("type") == "string"
                and isinstance(child.get("enum"), list) and len(child["enum"]) == 1
                and isinstance(child["enum"][0], str) and "default" not in child}
        candidates = tags if candidates is None else candidates & tags
    unique = [key for key in candidates or ()
              if len({branch["properties"][key]["enum"][0] for branch in branches}) == len(branches)]
    if len(unique) != 1:
        raise ValueError("modeling-function anyOf requires one shared required unique string discriminator")
    return unique[0]


def _safe_parameter_schema_validator(
    schema: dict[str, JsonValue],
) -> Draft202012Validator:
    from jsonschema import Draft202012Validator

    from CAD2BIM_backend.domain.canonical import canonical_json

    if len(canonical_json(schema).encode("utf-8")) > _MAX_PARAMETER_DOCUMENT_BYTES:
        raise ValueError("modeling-function parameter schema exceeds 16 KiB")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError("modeling-function parameter schema must be a closed object")
    allowed_scalar_keys = {
        "type",
        "title",
        "description",
        "default",
        "enum",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
    }
    nodes: list[dict[str, JsonValue]] = []

    def visit(definition: Any, depth: int) -> None:
        if depth > MAX_GEOMETRY_PARAMETER_SCHEMA_DEPTH:
            raise ValueError("modeling-function parameter schema exceeds depth 4")
        if not isinstance(definition, dict):
            raise ValueError("modeling-function parameter schema nodes must be objects")
        nodes.append(definition)
        if len(nodes) > MAX_GEOMETRY_PARAMETER_SCHEMA_NODES:
            raise ValueError("modeling-function parameter schema exceeds 256 nodes")
        if "anyOf" in definition:
            if not frozenset(definition).issubset({"anyOf", "title", "description", "default"}):
                raise ValueError("modeling-function tagged object uses unsupported keywords")
            branches = definition["anyOf"]
            if not isinstance(branches, list) or not 2 <= len(branches) <= 32:
                raise ValueError("modeling-function tagged object requires 2 to 32 branches")
            for branch in branches:
                if not isinstance(branch, dict) or branch.get("type") != "object" or "default" in branch:
                    raise ValueError("modeling-function tagged object branches must be complete objects without defaults")
                visit(branch, depth)
            _tagged_object_discriminator(branches)
            return
        kind = definition.get("type")
        if not isinstance(kind, str):
            raise ValueError("modeling-function parameter type must be one supported string")
        allowed = {"type", "title", "description", "default"}
        if kind == "object":
            allowed |= {"properties", "required", "additionalProperties"}
            if depth == 0:
                allowed.add("$schema")
            if definition.get("additionalProperties") is not False:
                raise ValueError("modeling-function parameter objects must be closed")
            properties = definition.get("properties")
            required = definition.get("required")
            if not isinstance(properties, dict) or len(properties) > 64:
                raise ValueError("modeling-function parameter schema properties are invalid")
            if not isinstance(required, list) or any(not isinstance(k, str) for k in required):
                raise ValueError("modeling-function parameter schema required list is invalid")
            if len(required) != len(set(required)) or any(k not in properties for k in required):
                raise ValueError("modeling-function required parameters must exist uniquely in properties")
            for name, child in properties.items():
                if not isinstance(name, str) or re.fullmatch(_PARAMETER_NAME_PATTERN, name) is None:
                    raise ValueError("modeling-function parameter names must be safe identifiers")
                visit(child, depth + 1)
        elif kind == "array":
            allowed |= {"items", "minItems", "maxItems"}
            lower, upper = definition.get("minItems"), definition.get("maxItems")
            if (type(lower) is not int or type(upper) is not int
                or not 0 <= lower <= upper <= MAX_GEOMETRY_PARAMETER_ARRAY_ITEMS):
                raise ValueError("modeling-function arrays require 0 <= minItems <= maxItems <= 128")
            visit(definition.get("items"), depth + 1)
        elif kind in {"number", "integer", "string", "boolean"}:
            allowed = allowed_scalar_keys
        else:
            raise ValueError("modeling-function parameter type is unsupported")
        if not frozenset(definition).issubset(allowed):
            raise ValueError("modeling-function property schema uses unsupported keywords")

    visit(schema, 0)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError("modeling-function parameter schema is invalid") from exc
    for node in nodes:
        if "default" in node and not Draft202012Validator(node).is_valid(node["default"]):
            raise ValueError("modeling-function parameter default violates its schema")
    return Draft202012Validator(schema)


def normalize_geometry_function_parameters(
    parameter_schema: dict[str, JsonValue],
    parameters: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Apply schema defaults once, before builds, receipts and Preview digests."""

    return _PreparedGeometryFunctionParameters(parameter_schema).normalize(parameters)


class _PreparedGeometryFunctionParameters:
    """Private, lifecycle-local rules; never retains an invocation or author values."""

    __slots__ = ("__schema", "__schema_identity", "__validator")

    def __init__(self, parameter_schema: dict[str, JsonValue]) -> None:
        from copy import deepcopy

        self.__schema = deepcopy(parameter_schema)
        self.__validator = _safe_parameter_schema_validator(self.__schema)
        self.__schema_identity = canonical_json(self.__schema)

    def normalize_for(self, parameter_schema, parameters):
        # A stale preparation must not silently validate a changed exact binding.
        if canonical_json(parameter_schema) != self.__schema_identity:
            raise ValueError("prepared geometry parameter schema does not match the exact binding")
        return self.normalize(parameters)

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _normalize_prepared_geometry_parameters(self.__schema, self.__validator, parameters)

    def request_from_normalized_samples(self, source, samples, *, retain_sample_geometries=False):
        """The same preparation owner passes its normalized values to execution."""
        from copy import deepcopy
        if not isinstance(source, (GeometryBuildFixedRequest, GeometryValidateFunctionRequest)):
            raise TypeError("prepared samples require a validated exact build input")
        if not 1 <= len(samples) <= 8 or type(retain_sample_geometries) is not bool:
            raise ValueError("prepared sample request exceeds its existing case contract")
        return GeometryValidateFunctionRequest.model_construct(
            source_bundle=source.source_bundle, geometry_inputs=source.geometry_inputs,
            parameter_schema=deepcopy(self.__schema), sample_parameters=deepcopy(tuple(samples)),
            retain_sample_geometries=retain_sample_geometries)


def _normalize_prepared_geometry_parameters(parameter_schema, validator, parameters):
    from copy import deepcopy

    _require_parameter_document(parameters, label="modeling-function parameters")

    def defaults(node: dict[str, JsonValue], value: JsonValue) -> JsonValue:
        if "anyOf" in node:
            if isinstance(value, dict):
                key = _tagged_object_discriminator(node["anyOf"])
                for branch in node["anyOf"]:
                    if value.get(key) == branch["properties"][key]["enum"][0]:
                        return defaults(branch, value)
            return deepcopy(value)
        if node["type"] == "object" and isinstance(value, dict):
            result = deepcopy(value)
            for key, child in node["properties"].items():
                if key not in result and "default" in child:
                    result[key] = deepcopy(child["default"])
                if key in result:
                    result[key] = defaults(child, result[key])
            return result
        if node["type"] == "array" and isinstance(value, list):
            return [defaults(node["items"], item) for item in value]
        return deepcopy(value)

    normalized = defaults(parameter_schema, parameters)
    _require_parameter_document(normalized, label="modeling-function parameters")
    error = next(validator.iter_errors(normalized), None)
    if error is not None:
        raise ValueError(json_schema_validation_message(
            "modeling function requires schema-valid samples", error,
        ))
    assert isinstance(normalized, dict)
    return normalized


def validate_geometry_function_parameter_schema(parameter_schema: dict[str, JsonValue]) -> None:
    """Validate the formal parameter contract through the same normalization owner."""
    _PreparedGeometryFunctionParameters(parameter_schema)


def validate_geometry_function_inputs(
    parameter_schema: dict[str, JsonValue],
    samples: tuple[dict[str, JsonValue], ...],
) -> None:
    """Validate the shared closed schema and bounded authoring samples."""

    if not 1 <= len(samples) <= 8:
        raise ValueError("modeling function requires 1..8 validation samples")
    prepared = _PreparedGeometryFunctionParameters(parameter_schema)
    for sample in samples:
        prepared.normalize(sample)


def _require_parameter_document(value: Any, *, label: str) -> None:
    from CAD2BIM_backend.domain.canonical import canonical_json

    try:
        content = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc
    if len(content) > _MAX_PARAMETER_DOCUMENT_BYTES:
        raise ValueError(f"{label} exceeds 16 KiB")


__all__ = [
    "MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS",
    "MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES",
    "MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES",
    "CanonicalGeometry",
    "CanonicalGeometryFormat",
    "Coordinate3",
    "MAX_GEOMETRY_PREVIEW_PANELS",
    "MAX_GEOMETRY_RENDER_INTERFACE_OVERLAYS",
    "MAX_GEOMETRY_RENDER_SOURCE_PATHS",
    "MAX_GEOMETRY_RENDER_SOURCE_POINTS_PER_PATH",
    "MAX_GEOMETRY_RENDER_SOURCE_POINTS_TOTAL",
    "GeometryBounds",
    "GeometryBuildDisplayContextRequest",
    "GeometryBuildDisplayContextResult",
    "GeometryBuildFixedRequest",
    "GeometryFixedBuildResult",
    "GeometryFunctionSampleExecution",
    "GeometryFunctionValidationResult",
    "GeometryNormalizeRequest",
    "GeometryDisplayMesh",
    "GeometryPreview",
    "GeometryPreviewAllInterfacesFocus",
    "GeometryPreviewCamera",
    "GeometryPreviewCleanOverlayRequest",
    "GeometryPreviewConnectionContext",
    "GeometryPreviewNoConnectionContext",
    "GeometryPreviewAutoConnectionContext",
    "GeometryPreviewPipesConnectionContext",
    "GeometryPreviewPipeSegment",
    "GeometryPreviewCustomCamera",
    "GeometryPreviewInterfaceFocus",
    "GeometryPreviewInterfaceFocusCamera",
    "GeometryPreviewInterfaceFocusOrientation",
    "GeometryPreviewInterfacePairFocus",
    "GeometryPreviewInterfacesOverlayRequest",
    "GeometryPreviewOverlayRequest",
    "GeometryPreviewPanel",
    "GeometryPreviewPresentation",
    "GeometryPreviewPresetFitCamera",
    "GeometryPreviewPresetRegionCamera",
    "GeometryPreviewProfile",
    "GeometryPreviewRenderMetadata",
    "GeometryPreviewView",
    "GeometryQueryRequest",
    "GeometryQueryResult",
    "GeometryRenderRequest",
    "GeometryRenderCoordinateSpace",
    "GeometryRenderHostOverlay",
    "GeometryRenderedPreviewMetadata",
    "GeometryRenderInterfaceMarkerKind",
    "GeometryRenderInterfaceOverlay",
    "GeometryRenderScene",
    "GeometryRenderReference",
    "MAX_GEOMETRY_RENDER_REFERENCE_BYTES",
    "GeometryRenderPlacement",
    "GeometryRenderStraightProfileOverlay",
    "GeometryRenderSourcePathOverlay",
    "GeometryRuntimeError",
    "GeometryRuntimeUnavailableError",
    "GeometryRuntimeResourceLimitError",
    "GeometryRuntimeResourceDiagnostic",
    "GeometryRuntimeValidationError",
    "GeometryPreviewRequestError",
    "GeometrySourceFormat",
    "GeometryStoredPreviewMetadata",
    "GeometryStraightProfileRecipe",
    "GeometryResolvedOrthographicCamera",
    "GeometryResolvedPreviewPanel",
    "GeometrySummary",
    "GeometryTopologySummary",
    "GeometryValidateFunctionRequest",
    "IndexedTriangle",
    "IndexedVertex",
    "IsolatedGeometryQueryExecution",
    "IsolatedGeometryQueryRequest",
    "Triangle3",
    "validate_geometry_function_inputs",
    "validate_geometry_function_parameter_schema",
    "validate_geometry_preview_scene",
]
