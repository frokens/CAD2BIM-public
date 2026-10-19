"""Server-only contracts for deriving realization attachments from CURRENT IFC."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_interface import CircularInterfaceProfile, InterfaceFrameRelation
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D, Vector3
from CAD2BIM_backend.domain.geometry_interface import GeometryInterfaceProfile


_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_IFC_GLOBAL_ID_PATTERN = r"^[0-9A-Za-z_$]{22}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_CURRENT_IFC_HOST_SELECTORS = 64
MAX_CURRENT_IFC_BODY_SECTIONS = 64


class _HostFactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CircularConnectionProfile(_HostFactModel):
    profile_kind: Literal["circular"] = "circular"
    outer_diameter_m: float = Field(gt=0.0, le=1000.0)

    @field_validator("outer_diameter_m")
    @classmethod
    def require_finite_diameter(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("connection diameter must be finite")
        return value


class ProductRealizationAttachmentFact(_HostFactModel):
    """Exact fixed circular interface fact selected from IFC."""

    attachment_key: str = Field(pattern=_KEY_PATTERN)
    source_kind: Literal["explicit", "current_ifc"] = "explicit"
    project_world_frame: RigidTransform3D
    profile: CircularConnectionProfile


class _StraightHostFact(_HostFactModel):
    attachment_key: str = Field(pattern=_KEY_PATTERN)
    source_kind: Literal["explicit"] = "explicit"
    centerline_start_m: Vector3
    centerline_end_m: Vector3
    profile_reference_direction: Vector3
    profile: GeometryInterfaceProfile

    @model_validator(mode="after")
    def require_straight_host_geometry(self) -> Self:
        values = (
            *self.centerline_start_m,
            *self.centerline_end_m,
            *self.profile_reference_direction,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("editable straight Host facts must be finite")
        delta = tuple(
            end - start
            for start, end in zip(
                self.centerline_start_m,
                self.centerline_end_m,
                strict=True,
            )
        )
        length = math.hypot(*delta)
        if not math.isfinite(length) or length <= 0.0:
            raise ValueError("editable straight Host centerline must have finite positive length")
        reference_length = math.hypot(*self.profile_reference_direction)
        if not math.isfinite(reference_length) or abs(reference_length - 1.0) > 1e-9:
            raise ValueError("editable straight Host profile reference must be a unit vector")
        tangent = tuple(value / length for value in delta)
        if (
            abs(
                sum(
                    axis * reference
                    for axis, reference in zip(
                        tangent,
                        self.profile_reference_direction,
                        strict=True,
                    )
                )
            )
            > 1e-9
        ):
            raise ValueError(
                "editable straight Host profile reference must be perpendicular to its centerline"
            )
        return self


class ProductRealizationStraightHostBoundary(_HostFactModel):
    """Named authorized connection on a complete Host, independent of product port names."""

    boundary_key: str = Field(pattern=_KEY_PATTERN)
    retained_side: Literal["before", "after"]
    connection_station_interval_m: tuple[float, float]


class ProductRealizationSectionedStraightHostFact(_StraightHostFact):
    """Complete Host with explicit removable intervals and named retained-side boundaries."""

    host_kind: Literal["bounded_sectioned_straight"] = "bounded_sectioned_straight"
    source_kind: Literal["explicit", "current_ifc"] = "explicit"
    body_station_intervals_m: tuple[tuple[float, float], ...] | None = Field(default=None, min_length=1, max_length=64)
    editable_station_intervals_m: tuple[tuple[float, float], ...] = Field(max_length=64)
    required_retained_station_intervals_m: tuple[tuple[float, float], ...] = Field(min_length=1, max_length=64)
    boundaries: tuple[ProductRealizationStraightHostBoundary, ...] = Field(min_length=1, max_length=16)

    @model_serializer(mode="wrap")
    def omit_unspecified_body(self, handler):
        result = handler(self)
        if self.body_station_intervals_m is None:
            result.pop("body_station_intervals_m", None)
        return result

    @model_validator(mode="after")
    def require_section_permissions(self) -> Self:
        length = math.dist(self.centerline_start_m, self.centerline_end_m)
        keys = [item.boundary_key for item in self.boundaries]
        if len(keys) != len(set(keys)):
            raise ValueError("sectioned Host boundary keys must be unique")
        body = self.body_station_intervals_m or ((0.0, length),)
        for interval in (*body, *self.editable_station_intervals_m,
                         *self.required_retained_station_intervals_m,
                         *(item.connection_station_interval_m for item in self.boundaries)):
            if (not all(math.isfinite(value) for value in interval)
                or not 0 <= interval[0] <= interval[1] <= length):
                raise ValueError("sectioned Host intervals must be finite and within its centerline")
        for intervals in (body, self.editable_station_intervals_m, self.required_retained_station_intervals_m):
            if any(a >= b for a, b in intervals) or any(
                left[1] > right[0] for left, right in zip(intervals, intervals[1:])
            ):
                raise ValueError("sectioned Host intervals must be positive, ordered and disjoint")
        if any(not any(a <= low and high <= b for a, b in body)
               for low, high in self.required_retained_station_intervals_m):
            raise ValueError("required retained intervals must exist in the current Host Body")
        return self


class ProductRealizationInterfaceBinding(_HostFactModel):
    interface_key: str = Field(pattern=_KEY_PATTERN)
    attachment_key: str = Field(pattern=_KEY_PATTERN)
    frame_relation: InterfaceFrameRelation | None = None
    host_boundary_key: str | None = Field(default=None, pattern=_KEY_PATTERN)

    @model_serializer(mode="wrap")
    def serialize_optional_relation(self, handler):
        result = handler(self)
        if self.frame_relation is None:
            result.pop("frame_relation", None)
        if self.host_boundary_key is None:
            result.pop("host_boundary_key", None)
        return result


ProductRealizationAttachment = ProductRealizationAttachmentFact | ProductRealizationSectionedStraightHostFact


class CurrentIfcAxisBoundary(StrEnum):
    AXIS_START = "axis_start"
    AXIS_END = "axis_end"


class CurrentIfcBodySectionReference(_HostFactModel):
    """Geometry selection bound to an exact read; not an access capability."""

    ifc_sha256: str = Field(pattern=_SHA256_PATTERN)
    project_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    product_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    geometry_signature: str = Field(pattern=_SHA256_PATTERN)
    body_digest: str = Field(pattern=_SHA256_PATTERN)
    section_key: str = Field(pattern=r"^bs_[0-9a-f]{32}$")
    station_interval_m: tuple[float, float]

    @model_validator(mode="after")
    def require_section_identity(self) -> Self:
        low, high = self.station_interval_m
        if not 0 <= low < high:
            raise ValueError("Body section reference requires a finite positive station interval")
        if self.section_key != current_ifc_body_section_key(self.geometry_signature, self.station_interval_m):
            raise ValueError("Body section reference key does not match its geometry facts")
        return self


def current_ifc_body_section_key(signature: str, interval: tuple[float, float]) -> str:
    return "bs_" + sha256_digest({"geometry_signature": signature, "station_interval_m": interval})[:32]


class CurrentIfcStraightBodySection(_HostFactModel):
    section_key: str = Field(pattern=r"^bs_[0-9a-f]{32}$")
    station_interval_m: tuple[float, float]
    centerline_start_m: tuple[float, float, float]
    centerline_end_m: tuple[float, float, float]


class CurrentIfcStraightBody(_HostFactModel):
    """Current IFC geometry projection; the IFC remains its sole source of truth."""

    ifc_sha256: str = Field(pattern=_SHA256_PATTERN)
    project_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    product_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    geometry_signature: str = Field(pattern=_SHA256_PATTERN)
    body_digest: str = Field(pattern=_SHA256_PATTERN)
    centerline_start_m: tuple[float, float, float]
    centerline_end_m: tuple[float, float, float]
    profile_reference_direction: tuple[float, float, float]
    profile: CircularInterfaceProfile
    inner_diameter_m: float | None = Field(default=None, gt=0)
    sections: tuple[CurrentIfcStraightBodySection, ...] = Field(min_length=1, max_length=MAX_CURRENT_IFC_BODY_SECTIONS)

    def section_reference(self, section: CurrentIfcStraightBodySection) -> CurrentIfcBodySectionReference:
        if section not in self.sections:
            raise ValueError("Body section does not belong to this current geometry")
        return CurrentIfcBodySectionReference(
            **{key: getattr(self, key) for key in (
                "ifc_sha256", "project_global_id", "product_global_id", "geometry_signature", "body_digest",
            )},
            section_key=section.section_key,
            station_interval_m=section.station_interval_m,
        )


class CurrentIfcBodyEndpointEditPermission(_HostFactModel):
    """Caller-authorized station limits, checked against the selected actual Body."""

    connection_station_interval_m: tuple[float, float]
    required_retained_station_intervals_m: tuple[tuple[float, float], ...] = Field(min_length=1, max_length=MAX_CURRENT_IFC_BODY_SECTIONS)

    @model_validator(mode="after")
    def require_ordered_permissions(self) -> Self:
        low, high = self.connection_station_interval_m
        if not 0 <= low <= high:
            raise ValueError("CURRENT Body connection interval must be nonnegative and ordered")
        intervals = self.required_retained_station_intervals_m
        if any(not 0 <= a < b for a, b in intervals) or any(
            left[1] > right[0] for left, right in zip(intervals, intervals[1:])
        ):
            raise ValueError("CURRENT Body retained intervals must be positive, ordered and disjoint")
        return self


class CurrentIfcProductBoundarySelector(_HostFactModel):
    """One exact host occurrence boundary mapped into the family ABI."""

    product_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    boundary: CurrentIfcAxisBoundary
    attachment_key: str = Field(pattern=_KEY_PATTERN)
    interface_key: str = Field(pattern=_KEY_PATTERN)
    frame_relation: InterfaceFrameRelation | None = None
    body_section_ref: CurrentIfcBodySectionReference | None = None
    edit_permission: CurrentIfcBodyEndpointEditPermission | None = None

    @model_validator(mode="after")
    def require_matching_product(self) -> Self:
        if self.body_section_ref is not None and self.body_section_ref.product_global_id != self.product_global_id:
            raise ValueError("Body section reference belongs to a different product")
        return self


class CurrentIfcHostFactRequest(_HostFactModel):
    selectors: tuple[CurrentIfcProductBoundarySelector, ...] = Field(
        min_length=1,
        max_length=MAX_CURRENT_IFC_HOST_SELECTORS,
    )

    @model_validator(mode="after")
    def require_closed_mapping(self) -> Self:
        attachment_keys = tuple(item.attachment_key for item in self.selectors)
        interface_keys = tuple(item.interface_key for item in self.selectors)
        boundaries = tuple((item.product_global_id, item.body_section_ref.section_key if item.body_section_ref else None, item.boundary) for item in self.selectors)
        for key in set(attachment_keys):
            group = [item for item in self.selectors if item.attachment_key == key]
            if len(group) > 1 and (any(item.edit_permission is None for item in group) or len({item.product_global_id for item in group}) != 1):
                raise ValueError("CURRENT IFC host attachment keys must be unique except editable boundaries of one product")
        for product in {item.product_global_id for item in self.selectors}:
            group = [item for item in self.selectors if item.product_global_id == product]
            if any(item.edit_permission is not None for item in group) and (
                any(item.edit_permission is None for item in group) or len({item.attachment_key for item in group}) != 1
            ):
                raise ValueError("editable CURRENT product boundaries must share one attachment key and explicit permissions")
        if len(interface_keys) != len(set(interface_keys)):
            raise ValueError("CURRENT IFC host interface keys must be unique")
        if len(boundaries) != len(set(boundaries)):
            raise ValueError("CURRENT IFC host product boundaries must be unique")
        return self


class ProductRealizationHostRevisionBinding(_HostFactModel):
    """Server-only exact revision identity; no public revision number is exposed."""

    model_id: str = Field(min_length=1, max_length=80)
    revision_id: str = Field(min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=80)
    artifact_version_id: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class ResolvedCurrentIfcHostFact(_HostFactModel):
    """One normalized realization attachment and its exact IFC source boundary."""

    product_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    product_ifc_class: Literal["IfcPipeSegment"] = "IfcPipeSegment"
    boundary: CurrentIfcAxisBoundary
    attachment: ProductRealizationAttachment
    interface_binding: ProductRealizationInterfaceBinding
    body: CurrentIfcStraightBody | None = None
    body_section_ref: CurrentIfcBodySectionReference | None = None
    body_reference_remapped: bool = False

    @model_validator(mode="after")
    def require_current_ifc_mapping(self) -> Self:
        if self.attachment.source_kind != "current_ifc":
            raise ValueError("resolved CURRENT IFC attachment must retain its source kind")
        if self.interface_binding.attachment_key != self.attachment.attachment_key:
            raise ValueError("resolved CURRENT IFC interface must map to its attachment")
        return self


class ResolvedCurrentIfcHostFacts(_HostFactModel):
    revision: ProductRealizationHostRevisionBinding
    facts: tuple[ResolvedCurrentIfcHostFact, ...] = Field(
        min_length=1,
        max_length=MAX_CURRENT_IFC_HOST_SELECTORS,
    )

    @model_validator(mode="after")
    def require_unique_mapping(self) -> Self:
        attachment_keys = tuple(item.attachment.attachment_key for item in self.facts)
        interface_keys = tuple(item.interface_binding.interface_key for item in self.facts)
        for key in set(attachment_keys):
            group = [item for item in self.facts if item.attachment.attachment_key == key]
            if any(item.attachment != group[0].attachment for item in group):
                raise ValueError("resolved CURRENT IFC shared attachment facts must be identical")
        if len(interface_keys) != len(set(interface_keys)):
            raise ValueError("resolved CURRENT IFC interface keys must be unique")
        return self

    @property
    def attachments(self) -> tuple[ProductRealizationAttachment, ...]:
        return tuple({item.attachment.attachment_key: item.attachment for item in self.facts}.values())

    @property
    def interface_bindings(self) -> tuple[ProductRealizationInterfaceBinding, ...]:
        return tuple(item.interface_binding for item in self.facts)


class ProductRealizationHostErrorCode(StrEnum):
    SCOPE_INVALID = "PRODUCT_REALIZATION_HOST_SCOPE_INVALID"
    CURRENT_IFC_UNAVAILABLE = "PRODUCT_REALIZATION_CURRENT_IFC_UNAVAILABLE"
    CURRENT_IFC_INTEGRITY_FAILED = "PRODUCT_REALIZATION_CURRENT_IFC_INTEGRITY_FAILED"
    IFC_CONTENT_INVALID = "PRODUCT_REALIZATION_HOST_IFC_CONTENT_INVALID"
    PRODUCT_NOT_FOUND = "PRODUCT_REALIZATION_HOST_PRODUCT_NOT_FOUND"
    PRODUCT_AMBIGUOUS = "PRODUCT_REALIZATION_HOST_PRODUCT_AMBIGUOUS"
    PRODUCT_CLASS_UNSUPPORTED = "PRODUCT_REALIZATION_HOST_PRODUCT_CLASS_UNSUPPORTED"
    PLACEMENT_INVALID = "PRODUCT_REALIZATION_HOST_PLACEMENT_INVALID"
    AXIS_MISSING = "PRODUCT_REALIZATION_HOST_AXIS_MISSING"
    AXIS_AMBIGUOUS = "PRODUCT_REALIZATION_HOST_AXIS_AMBIGUOUS"
    AXIS_UNSUPPORTED = "PRODUCT_REALIZATION_HOST_AXIS_UNSUPPORTED"
    AXIS_INVALID = "PRODUCT_REALIZATION_HOST_AXIS_INVALID"
    PROFILE_MISSING = "PRODUCT_REALIZATION_HOST_PROFILE_MISSING"
    PROFILE_AMBIGUOUS = "PRODUCT_REALIZATION_HOST_PROFILE_AMBIGUOUS"
    PROFILE_INVALID = "PRODUCT_REALIZATION_HOST_PROFILE_INVALID"
    BODY_UNSUPPORTED = "PRODUCT_REALIZATION_HOST_BODY_UNSUPPORTED"
    BODY_INVALID = "PRODUCT_REALIZATION_HOST_BODY_INVALID"
    BODY_SECTION_AMBIGUOUS = "PRODUCT_REALIZATION_HOST_BODY_SECTION_AMBIGUOUS"
    BODY_REFERENCE_STALE = "PRODUCT_REALIZATION_HOST_BODY_REFERENCE_STALE"
    EDIT_PERMISSION_INVALID = "PRODUCT_REALIZATION_HOST_EDIT_PERMISSION_INVALID"


class ProductRealizationHostError(Exception):
    """Expected bounded failure while resolving one exact CURRENT IFC host fact."""

    def __init__(self, code: ProductRealizationHostErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


__all__ = [
    "CurrentIfcAxisBoundary",
    "CurrentIfcBodySectionReference",
    "CurrentIfcBodyEndpointEditPermission",
    "CurrentIfcStraightBody",
    "CurrentIfcStraightBodySection",
    "current_ifc_body_section_key",
    "MAX_CURRENT_IFC_BODY_SECTIONS",
    "CurrentIfcHostFactRequest",
    "CurrentIfcProductBoundarySelector",
    "MAX_CURRENT_IFC_HOST_SELECTORS",
    "ProductRealizationHostError",
    "ProductRealizationHostErrorCode",
    "ProductRealizationHostRevisionBinding",
    "ResolvedCurrentIfcHostFact",
    "ResolvedCurrentIfcHostFacts",
]
