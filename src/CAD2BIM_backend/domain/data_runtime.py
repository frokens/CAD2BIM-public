"""Persisted identities for the CAD/Data runtime.

These contracts describe durable resources, not provider-facing aliases or
large payload bodies.  Parquet, JSON, Markdown, and source bytes remain exact
Artifact versions referenced by package members.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Mapping

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_PAYLOAD_NAMES,
    CadPointScope,
    CadToleranceSpec,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ConflictError
from CAD2BIM_backend.domain.product_apply import (
    ApplyIfcProductsCommand, ProductApplyInputBinding, ProductApplySummary,
)
from CAD2BIM_backend.domain.product_edit import EditIfcProductsCommand, ProductsEditSummary
from CAD2BIM_backend.domain.data_transform import InlineTable
from CAD2BIM_backend.domain.ifc_geometry_basis import ConfigureIfcGeometryBasisCommand
from CAD2BIM_backend.domain.ifc_project import IfcProjectSummary, UpdateIfcProjectCommand
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand, CreateIfcSpatialOperation,
    IfcSpatialBlockerSummary, IfcSpatialStructureApplyResult,
)
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.product_family_authoring_contracts import (
    ProductFamilyAuthoringVersionDraft,
)
from CAD2BIM_backend.domain.system_family_publication import SystemFamilyCopyPlan
from CAD2BIM_backend.domain.version_usage import validate_version_usage_instructions


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_OPERATION_ALIAS_PATTERN = r"^op_[0-9]{3,}$"
_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
SOURCE_L0_PAYLOAD_ORDER = (
    "records",
    "raw_tags",
    "entities",
    "geometry_elements",
    "xdata_tags",
    "relationships",
)
SOURCE_L0_PAYLOAD_NAMES = frozenset(SOURCE_L0_PAYLOAD_ORDER)
SOURCE_L0_LOGICAL_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "records": ("record_key",),
        "raw_tags": ("raw_tag_key",),
        "entities": ("entity_key",),
        "geometry_elements": ("geometry_element_key",),
        "xdata_tags": ("xdata_tag_key",),
        "relationships": ("relationship_key",),
    }
)
SOURCE_L0_SOURCE_MEMBER_NAME = "source/original.dxf"
MAX_ARROW_TYPE_UTF8_BYTES = 65_536
MAX_INLINE_TABLES_UTF8_BYTES = 1_048_576
IFC_MODEL_INITIALIZE_INTENT_SUMMARY = "Initialize a durable IFC4X3_ADD2 model with one IfcProject."
IFC_PRODUCTS_APPLY_INTENT_SUMMARY = "Add all products from one exact Family and Parquet input atomically."
IFC_PRODUCTS_EDIT_INTENT_SUMMARY = "Edit existing physical IFC products atomically from one exact Parquet input."
IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY = (
    "Configure the durable IFC model geometry basis and representation contexts."
)
IFC_PROJECT_UPDATE_INTENT_SUMMARY = "Update the durable IFC Project business metadata."
IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY = "Load a trusted IFC baseline for controlled test preparation."
IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY = (
    "Apply one atomic batch of IFC spatial-structure changes."
)


def validate_arrow_type_string(value: str) -> str:
    """Keep persisted and returned Arrow type descriptions reasonably bounded."""

    if len(value.encode("utf-8")) > MAX_ARROW_TYPE_UTF8_BYTES:
        raise ValueError(f"Arrow type description exceeds {MAX_ARROW_TYPE_UTF8_BYTES} UTF-8 bytes")
    return value


ArrowTypeString = Annotated[
    str,
    Field(min_length=1, max_length=MAX_ARROW_TYPE_UTF8_BYTES),
    AfterValidator(validate_arrow_type_string),
]


def source_l0_payload_member_name(payload_name: str) -> str:
    """Return the package member name for one canonical L0 payload."""

    if payload_name not in SOURCE_L0_PAYLOAD_NAMES:
        raise ValueError(f"unknown source_l0 payload: {payload_name}")
    return f"payloads/{payload_name}/part-00000.parquet"


class DataRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def normalize_direct_session_name(value: str) -> str:
    """Return the stable lookup key for a human-supplied Direct session name."""

    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("Direct session name contains control characters")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        raise ValueError("Direct session name must not be empty")
    if len(normalized) > 100:
        raise ValueError("Direct session name exceeds 100 characters")
    if "/" in normalized or "\\" in normalized:
        raise ValueError("Direct session name cannot contain path separators")
    return normalized


def _require_source_filename(value: str | None) -> str | None:
    if value is None:
        return None
    if "/" in value or "\\" in value:
        raise ValueError("source_filename must be a filename, not a path")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("source_filename contains control characters")
    return value


class DirectSessionBinding(DataRuntimeModel):
    session_id: str = Field(min_length=1, max_length=80)
    direct_session_alias: str = Field(pattern=_DIRECT_ALIAS_PATTERN, max_length=32)
    ordinal: int = Field(ge=1)
    configured_subject: str = Field(min_length=1, max_length=255)
    session_name: str = Field(min_length=1, max_length=100)
    normalized_session_name: str = Field(min_length=1, max_length=100)
    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=80)
    principal_id: str = Field(min_length=1, max_length=80)
    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    created_at: datetime

    @model_validator(mode="after")
    def validate_normalized_name(self) -> "DirectSessionBinding":
        if normalize_direct_session_name(self.session_name) != self.normalized_session_name:
            raise ValueError("normalized Direct session name does not match session_name")
        return self


class OpenDirectSessionRequest(DataRuntimeModel):
    """Resolved configured identity plus the user-visible Direct session name."""

    configured_subject: str = Field(min_length=1, max_length=255)
    session_name: str = Field(min_length=1, max_length=100)
    normalized_session_name: str = Field(min_length=1, max_length=100)
    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=80)
    principal_id: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_normalized_name(self) -> "OpenDirectSessionRequest":
        if normalize_direct_session_name(self.session_name) != self.normalized_session_name:
            raise ValueError("normalized Direct session name does not match session_name")
        return self


class OpenedDirectSession(DataRuntimeModel):
    binding: DirectSessionBinding
    resumed: bool


class ArtifactVersionRef(DataRuntimeModel):
    artifact_id: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class ValidatedSource(DataRuntimeModel):
    """Entry-neutral exact source accepted by shared ingestion."""

    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    content: bytes | None = None
    artifact_version: ArtifactVersionRef | None = None
    source_identity: str = Field(min_length=1, max_length=4_096)
    adapter_metadata: dict[str, str] = Field(default_factory=dict)

    _validate_filename = field_validator("filename")(_require_source_filename)

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> "ValidatedSource":
        if (self.content is None) == (self.artifact_version is None):
            raise ValueError("source requires exact bytes XOR exact ArtifactVersion")
        if self.content is not None and not self.content:
            raise ValueError("source content must not be empty")
        return self


class DataPackageKind(StrEnum):
    SOURCE_L0 = "source_l0"
    DERIVED = "derived"


class DataPackageRecord(DataRuntimeModel):
    """Immutable Task-owned collection of exact Artifact members."""

    package_id: str = Field(min_length=1, max_length=80)
    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=80)
    task_id: str = Field(min_length=1, max_length=80)
    created_by_run_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=255)
    package_kind: DataPackageKind
    source_filename: str | None = Field(default=None, min_length=1, max_length=255)
    description_title: str | None = Field(default=None, min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1_000)
    section_titles: tuple[str, ...] = Field(default=(), max_length=16)
    idempotency_key: str = Field(min_length=1, max_length=255)
    request_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    created_at: datetime

    _validate_source_filename = field_validator("source_filename")(_require_source_filename)

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "DataPackageRecord":
        if self.package_kind is DataPackageKind.SOURCE_L0:
            if self.source_filename is None:
                raise ValueError("source_l0 package requires source_filename")
        elif self.source_filename is not None:
            raise ValueError("derived package cannot contain source_filename")
        if len(set(self.section_titles)) != len(self.section_titles):
            raise ValueError("Package section titles must be unique")
        return self


class PackageMemberKind(StrEnum):
    SOURCE = "source"
    MANIFEST = "manifest"
    PAYLOAD = "payload"
    DESCRIPTION = "description"
    CODE = "code"
    SCHEMA = "schema"


class JsonPayloadKind(StrEnum):
    DOCUMENT = "document"
    RECORDS = "records"


class ArrowColumnRecord(DataRuntimeModel):
    name: str = Field(min_length=1, max_length=255)
    type: ArrowTypeString
    nullable: bool


class DataPackageMemberRecord(DataRuntimeModel):
    member_id: str = Field(min_length=1, max_length=80)
    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=80)
    task_id: str = Field(min_length=1, max_length=80)
    package_id: str = Field(min_length=1, max_length=80)
    member_name: str = Field(min_length=1, max_length=255)
    member_kind: PackageMemberKind
    artifact: ArtifactVersionRef
    media_type: str = Field(min_length=1, max_length=255)
    row_count: int | None = Field(default=None, ge=0)
    logical_payload_name: str | None = Field(default=None, min_length=1, max_length=120)
    payload_description: str | None = Field(default=None, min_length=1, max_length=500)
    logical_key_fields: tuple[str, ...] = Field(default=(), max_length=16)
    arrow_schema: tuple[ArrowColumnRecord, ...] = Field(default=())
    logical_byte_count: int | None = Field(default=None, ge=0)
    json_kind: JsonPayloadKind | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_row_count_scope(self) -> "DataPackageMemberRecord":
        payload_metadata_present = (
            self.logical_payload_name is not None
            or self.payload_description is not None
            or bool(self.logical_key_fields)
            or bool(self.arrow_schema)
            or self.logical_byte_count is not None
            or self.json_kind is not None
        )
        if self.member_kind is not PackageMemberKind.PAYLOAD:
            if self.row_count is not None or payload_metadata_present:
                raise ValueError("payload metadata is only valid for payload members")
            if self.member_kind is PackageMemberKind.SCHEMA:
                if (
                    re.fullmatch(r"schemas/[a-z][a-z0-9_]{0,119}[.]schema[.]json", self.member_name)
                    is None
                    or self.media_type != "application/schema+json"
                ):
                    raise ValueError("JSON Schema members require the fixed name and media type")
            return self
        if self.media_type == "application/json":
            if self.logical_payload_name is None:
                raise ValueError("JSON payload requires logical_payload_name")
            if self.member_name != f"payloads/{self.logical_payload_name}.json":
                raise ValueError("JSON payload requires the fixed member name")
            if self.json_kind is None:
                raise ValueError("JSON payload requires json_kind")
            if self.row_count is not None or self.logical_key_fields or self.arrow_schema:
                raise ValueError("JSON payload cannot contain Parquet row or schema metadata")
        elif self.json_kind is not None:
            raise ValueError("json_kind is only valid for JSON payload members")
        if len(set(self.logical_key_fields)) != len(self.logical_key_fields):
            raise ValueError("logical_key_fields must be unique")
        if len({column.name for column in self.arrow_schema}) != len(self.arrow_schema):
            raise ValueError("Arrow column names must be unique")
        return self


class CriticalOperationKind(StrEnum):
    SOURCE_INGESTION = "source_ingestion"
    DATA_TRANSFORM = "data_transform"
    IFC_MODEL_WRITE = "ifc_model_write"
    GEOMETRY_RESOURCE_WRITE = "geometry_resource_write"
    PRODUCT_FAMILY_WRITE = "product_family_write"
    GEOMETRY_INSPECTION = "geometry_inspection"
    AUTHORING_PUBLISH = "authoring_publish"


class CriticalOperationInitiator(StrEnum):
    DIRECT_CLIENT = "direct_client"
    RUNNER = "runner"


class CriticalOperationErrorPhase(StrEnum):
    SOURCE_EXTRACTION = "source_extraction"
    CODE_VALIDATION = "code_validation"
    EXECUTION = "execution"
    OUTPUT_VALIDATION = "output_validation"
    IFC_VALIDATION = "ifc_validation"
    STORAGE_VERIFICATION = "storage_verification"
    COMMIT_FENCE = "commit_fence"
    RECONCILE = "reconcile"


class DataTransformEngine(StrEnum):
    DUCKDB_SQL = "duckdb_sql"
    PYTHON_ARROW = "python_arrow"
    CAD_MEASUREMENT = "cad_measurement"


class TransformParameterKind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_LIST = "string_list"
    INTEGER_LIST = "integer_list"
    NUMBER_LIST = "number_list"
    BOOLEAN_LIST = "boolean_list"


TransformParameterValue = (
    str
    | int
    | float
    | bool
    | tuple[str, ...]
    | tuple[int, ...]
    | tuple[float, ...]
    | tuple[bool, ...]
)


class TransformInputBindingRecord(DataRuntimeModel):
    ordinal: int = Field(ge=1, le=16)
    input_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    package_id: str = Field(min_length=1, max_length=80)
    member_id: str = Field(min_length=1, max_length=80)
    payload_name: str = Field(min_length=1, max_length=120)


class TransformParameterRecord(DataRuntimeModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    kind: TransformParameterKind
    value: TransformParameterValue

    @model_validator(mode="after")
    def validate_value_kind(self) -> "TransformParameterRecord":
        value = self.value
        scalar_kinds = {
            TransformParameterKind.STRING: str,
            TransformParameterKind.INTEGER: int,
            TransformParameterKind.NUMBER: (int, float),
            TransformParameterKind.BOOLEAN: bool,
        }
        expected = scalar_kinds.get(self.kind)
        if expected is not None:
            if isinstance(value, tuple) or not isinstance(value, expected):
                raise ValueError("transform parameter value does not match kind")
            if self.kind in {
                TransformParameterKind.INTEGER,
                TransformParameterKind.NUMBER,
            } and isinstance(value, bool):
                raise ValueError("boolean is not a numeric transform parameter")
            return self
        if not isinstance(value, tuple) or not value or len(value) > 100:
            raise ValueError("transform list parameter requires 1..100 values")
        item_kind = {
            TransformParameterKind.STRING_LIST: str,
            TransformParameterKind.INTEGER_LIST: int,
            TransformParameterKind.NUMBER_LIST: (int, float),
            TransformParameterKind.BOOLEAN_LIST: bool,
        }[self.kind]
        if any(not isinstance(item, item_kind) for item in value):
            raise ValueError("transform list parameter value does not match kind")
        if self.kind in {
            TransformParameterKind.INTEGER_LIST,
            TransformParameterKind.NUMBER_LIST,
        } and any(isinstance(item, bool) for item in value):
            raise ValueError("boolean is not a numeric transform parameter")
        return self


class TransformOutputRecord(DataRuntimeModel):
    payload_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    format: Literal["parquet", "json"] = "parquet"
    description: str = Field(min_length=1, max_length=500)
    logical_key_fields: tuple[str, ...] = Field(default=(), max_length=16)
    json_kind: JsonPayloadKind | None = None
    schema_json: str | None = Field(default=None, max_length=65_536)

    @model_validator(mode="after")
    def validate_format_metadata(self) -> "TransformOutputRecord":
        if len(set(self.logical_key_fields)) != len(self.logical_key_fields):
            raise ValueError("transform output logical keys must be unique")
        if self.format == "parquet":
            if not self.logical_key_fields:
                raise ValueError("Parquet transform output requires logical_key_fields")
            if self.json_kind is not None:
                raise ValueError("Parquet transform output cannot contain json_kind")
            if self.schema_json is not None:
                raise ValueError("Parquet transform output cannot contain schema_json")
        elif self.logical_key_fields or self.json_kind is None:
            raise ValueError("JSON transform output requires only json_kind and schema_json")
        elif self.schema_json is not None:
            if not self.schema_json.strip() or "\x00" in self.schema_json:
                raise ValueError("JSON transform output schema_json is invalid")
            if len(self.schema_json.encode("utf-8")) > 65_536:
                raise ValueError("schema_json exceeds 65,536 UTF-8 bytes")
        return self


class SourceIngestionOperationDetail(DataRuntimeModel):
    detail_kind: Literal["source_ingestion"] = "source_ingestion"
    filename: str = Field(min_length=1, max_length=255)
    media_type: Literal["application/dxf"] = "application/dxf"
    size_bytes: int | None = Field(default=None, ge=1)

    _validate_filename = field_validator("filename")(_require_source_filename)


class IfcReadSourceRef(DataRuntimeModel):
    """Exact server-resolved IFC read source; revision numbers are explanatory."""

    model_id: str = Field(min_length=1, max_length=80)
    revision_id: str = Field(min_length=1, max_length=80)
    revision_no: int = Field(ge=1)


class IfcSourceChangedError(ConflictError):
    code = "IFC_SOURCE_CHANGED"

    def __init__(self, expected: IfcReadSourceRef, actual: IfcReadSourceRef | None):
        super().__init__("The IFC source has changed; no Package was published. Create a new derivation from CURRENT.")
        self.diagnostic = {"expected_revision_no": expected.revision_no,
                           "actual_revision_no": actual.revision_no if actual else None}


class DataTransformOperationDetail(DataRuntimeModel):
    detail_kind: Literal["data_transform"] = "data_transform"
    engine: Literal["duckdb_sql", "python_arrow"] = "duckdb_sql"
    inputs: tuple[TransformInputBindingRecord, ...] = Field(default=(), max_length=16)
    ifc_source: IfcReadSourceRef | None = None
    parameters: tuple[TransformParameterRecord, ...] = Field(default=(), max_length=32)
    inline_tables: tuple[InlineTable, ...] = Field(default=(), max_length=8)
    outputs: tuple[TransformOutputRecord, ...] = Field(min_length=1, max_length=16)
    output_package_name: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_unique_names(self) -> "DataTransformOperationDetail":
        if not self.inputs and self.ifc_source is None:
            raise ValueError("data derivation requires a Package input or an exact IFC source")
        if self.ifc_source is not None and self.engine != "python_arrow":
            raise ValueError("an IFC source requires Python execution")
        if [item.ordinal for item in self.inputs] != list(range(1, len(self.inputs) + 1)):
            raise ValueError("transform input ordinals must be contiguous from one")
        if len({item.input_name for item in self.inputs}) != len(self.inputs):
            raise ValueError("transform input names must be unique")
        if len({(item.package_id, item.member_id) for item in self.inputs}) != len(self.inputs):
            raise ValueError("transform input members must be unique")
        if len({item.name for item in self.parameters}) != len(self.parameters):
            raise ValueError("transform parameter names must be unique")
        inline_names = [item.input_name for item in self.inline_tables]
        if len(set(inline_names)) != len(inline_names):
            raise ValueError("transform inline table names must be unique")
        input_names = {item.input_name for item in self.inputs}
        if input_names & set(inline_names):
            raise ValueError("transform inline table names cannot collide with input names")
        inline_bytes = len(
            canonical_json([item.model_dump(mode="json") for item in self.inline_tables]).encode(
                "utf-8"
            )
        )
        if inline_bytes > MAX_INLINE_TABLES_UTF8_BYTES:
            raise ValueError("transform inline tables exceed 1 MiB canonical JSON")
        output_names = [item.payload_name for item in self.outputs]
        if len(set(output_names)) != len(output_names):
            raise ValueError("transform output payload names must be unique")
        if set(output_names) & (input_names | set(inline_names)):
            raise ValueError("transform output payload names cannot collide with input names")
        return self


class CadMeasurementOperationDetail(DataRuntimeModel):
    """Exact server-owned M3 measurement inputs published as a transform Operation."""

    detail_kind: Literal["cad_measurement"] = "cad_measurement"
    engine: Literal["cad_measurement"] = "cad_measurement"
    inputs: tuple[TransformInputBindingRecord, ...] = Field(min_length=3, max_length=3)
    source_unit_to_m: float = Field(gt=0, allow_inf_nan=False)
    path_layers: tuple[str, ...] = Field(min_length=1, max_length=100)
    point_scopes: tuple[CadPointScope, ...] = Field(default=(), max_length=16)
    tolerance_spec: CadToleranceSpec
    outputs: tuple[TransformOutputRecord, ...] = Field(min_length=10, max_length=10)
    output_package_name: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_measurement_contract(self) -> "CadMeasurementOperationDetail":
        expected_inputs = ("entities", "geometry_elements", "relationships")
        if tuple(item.input_name for item in self.inputs) != expected_inputs:
            raise ValueError("CAD measurement inputs must use the exact three L0 payload names")
        if [item.ordinal for item in self.inputs] != [1, 2, 3]:
            raise ValueError("CAD measurement input ordinals must be one through three")
        if len({item.package_id for item in self.inputs}) != 1:
            raise ValueError("CAD measurement inputs must come from one source_l0 package")
        if len({item.member_id for item in self.inputs}) != 3:
            raise ValueError("CAD measurement input members must be exact and unique")
        if tuple(item.payload_name for item in self.outputs) != CAD_MEASUREMENT_PAYLOAD_NAMES:
            raise ValueError("CAD measurement outputs must be the fixed ten Parquet payloads")
        if any(item.format != "parquet" for item in self.outputs):
            raise ValueError("CAD measurement outputs must all be Parquet")
        return self






class ProductFamilyWriteOperationDetail(DataRuntimeModel):
    """Bounded immutable request projection for one ProductFamily authoring write."""

    detail_kind: Literal["product_family_write"] = "product_family_write"
    mode: Literal["system_copy"]
    version_draft: ProductFamilyAuthoringVersionDraft
    system_copy: SystemFamilyCopyPlan | None = None

    @model_validator(mode="after")
    def require_copy_plan(self) -> "ProductFamilyWriteOperationDetail":
        if (self.mode == "system_copy") != (self.system_copy is not None):
            raise ValueError("only SYSTEM copy mode carries a copy plan")
        if self.system_copy is not None:
            draft = self.version_draft
            if (draft.parent_family_version_id is not None
                or draft.family_id == self.system_copy.source_family_id
                or draft.family_version_id == self.system_copy.source_family_version_id):
                raise ValueError("SYSTEM copy requires a new Family root without a parent")
            if any(binding.geometry_owner_scope != self.system_copy.target_owner
                   for binding in draft.geometry_bindings):
                raise ValueError("SYSTEM Family cannot bind private Geometry")
            mapped = {item.target_version.version.resource_version_id: item.target_version
                      for item in self.system_copy.geometry_copies}
            for binding in draft.geometry_bindings:
                target = mapped.get(binding.geometry_resource_version_id)
                if (target is None or target.resource.resource_id != binding.geometry_resource_id
                    or target.version.content_digest != binding.geometry_content_digest
                    or target.resource.resource_kind != binding.expected_resource_kind):
                    raise ValueError("Family bindings must consume the saved Geometry mapping")
            if any(asset.family_version_id != draft.family_version_id
                   for asset in self.system_copy.family_assets):
                raise ValueError("copy assets must belong to the saved target Family version")
        return self



class AuthoringDraftWriteOperationDetail(DataRuntimeModel):
    """A saved working copy; its generation is a CAS, not a resource version."""

    detail_kind: Literal["authoring_draft_write"] = "authoring_draft_write"
    root_kind: Literal["geometry", "family"]
    root_id: str = Field(min_length=1, max_length=80)
    expected_generation: int | None = Field(default=None, ge=1)
    result_generation: int = Field(ge=1)

    @model_validator(mode="after")
    def require_next_generation(self):
        if self.result_generation != (self.expected_generation or 0) + 1:
            raise ValueError("draft write must advance the expected generation once")
        return self


class AuthoringDraftSelection(DataRuntimeModel):
    root_kind: Literal["geometry", "family"]
    root_id: str = Field(min_length=1, max_length=80)
    generation: int = Field(ge=1)
    snapshot: ArtifactVersionRef


class AuthoringPreviewSelection(DataRuntimeModel):
    operation_alias: str = Field(min_length=1, max_length=16_384)
    case_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_cases(self):
        if any(not key for key in self.case_keys) or len(set(self.case_keys)) != len(self.case_keys):
            raise ValueError("preview case keys must be nonempty and unique")
        return self


class AuthoringPreviewExampleReference(DataRuntimeModel):
    origin_run_id: str = Field(min_length=1, max_length=80)
    operation_id: str = Field(min_length=1, max_length=80)
    case_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_cases(self):
        if any(not key for key in self.case_keys) or len(set(self.case_keys)) != len(self.case_keys):
            raise ValueError("preview case keys must be nonempty and unique")
        return self


class AuthoringPublishedGeometryReference(DataRuntimeModel):
    resource_id: str = Field(min_length=1, max_length=80)
    resource_version_id: str = Field(min_length=1, max_length=80)


class AuthoringPublishOperationDetail(DataRuntimeModel):
    """Exact publication inputs, optional observation links, and committed outputs."""

    detail_kind: Literal["authoring_publish"] = "authoring_publish"
    root_kind: Literal["geometry", "family"]
    root_id: str = Field(min_length=1, max_length=80)
    expected_drafts: tuple[AuthoringDraftSelection, ...] = Field(min_length=1)
    preview_examples: tuple[AuthoringPreviewExampleReference, ...] = ()
    geometry_versions: tuple[AuthoringPublishedGeometryReference, ...] = ()
    family_version: ProductFamilyAuthoringVersionDraft | None = None

    @model_validator(mode="after")
    def require_exact_closure(self):
        keys = tuple((item.root_kind, item.root_id) for item in self.expected_drafts)
        if len(keys) != len(set(keys)) or (self.root_kind, self.root_id) not in keys:
            raise ValueError("publication requires a unique draft closure containing its target")
        geometry_ids = tuple(item.resource_id for item in self.geometry_versions)
        if len(geometry_ids) != len(set(geometry_ids)):
            raise ValueError("publication cannot create two versions of the same Geometry")
        if any(("geometry", root_id) not in keys for root_id in geometry_ids):
            raise ValueError("published Geometry must belong to the selected draft closure")
        if self.family_version is not None and (
            self.root_kind != "family" or self.family_version.family_id != self.root_id
        ):
            raise ValueError("Family publication result must match its exact target")
        return self


class GeometryInspectionOperationDetail(DataRuntimeModel):
    """Exact source and request retained by one same-build Preview publication."""

    detail_kind: Literal["geometry_inspection"] = "geometry_inspection"
    result_kind: Literal["inspection", "case_report"] = "inspection"
    source_type: Literal["published", "draft"] = "published"
    source_draft_artifact: ArtifactVersionRef | None = None
    source_kind: Literal["geometry_sample", "family_realization"]
    source_id: str = Field(min_length=1, max_length=80)
    source_version_id: str = Field(min_length=1, max_length=80)
    source_content_digest: str = Field(pattern=_SHA256_PATTERN)
    source_version_ref: str = Field(min_length=1, max_length=16_384)
    request_artifact: ArtifactVersionRef

    @model_validator(mode="after")
    def require_exact_source(self):
        if (self.source_type == "draft") != (self.source_draft_artifact is not None):
            raise ValueError("only a draft inspection source carries a draft snapshot")
        if self.source_draft_artifact is not None and self.source_version_id != self.source_draft_artifact.version_id:
            raise ValueError("draft inspection source must name its exact snapshot")
        return self


class IfcModelInitializeOperationDetail(DataRuntimeModel):
    """Bounded business projection for durable IFC model initialization."""

    detail_kind: Literal["ifc_model_initialize"] = "ifc_model_initialize"
    ifc_schema: Literal["IFC4X3_ADD2"] = "IFC4X3_ADD2"
    project_name: str = Field(min_length=1, max_length=255)
    project_global_id: str = Field(pattern=r"^[0-9A-Za-z_$]{22}$")
    result: Literal["model_initialized"] | None = None

    @field_validator("project_name", mode="before")
    @classmethod
    def normalize_project_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ValueError("project_name contains control characters")
        normalized = value.strip()
        if not normalized:
            raise ValueError("project_name must not be empty")
        if len(normalized) > 255:
            raise ValueError("project_name exceeds 255 characters")
        return normalized


class IfcGeometryBasisConfigureOperationDetail(DataRuntimeModel):
    """Provider-safe input and terminal result for one geometry-basis write."""

    detail_kind: Literal["ifc_model_configure_geometry_basis"] = (
        "ifc_model_configure_geometry_basis"
    )
    request: ConfigureIfcGeometryBasisCommand
    result: Literal["geometry_basis_configured"] | None = None

    def persisted_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "detail_kind": self.detail_kind,
            "request": self.request.operation_request_payload(),
        }
        if self.result is not None:
            payload["result"] = self.result
        return payload


class IfcTrustedBaselineLoadOperationDetail(DataRuntimeModel):
    """Exact source and honest result for internal trusted test preparation."""

    detail_kind: Literal["ifc_trusted_baseline_load"] = "ifc_trusted_baseline_load"
    source_asset: ArtifactVersionRef
    result: Literal["baseline_loaded"] | None = None

    def persisted_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)


class IfcProjectUpdateOperationDetail(DataRuntimeModel):
    """Provider-safe request and terminal result for one Project update."""

    detail_kind: Literal["ifc_project_update"] = "ifc_project_update"
    request: UpdateIfcProjectCommand
    result: IfcProjectSummary | None = None

    def persisted_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "detail_kind": self.detail_kind,
            "request": self.request.operation_request_payload(),
        }
        if self.result is not None:
            payload["result"] = self.result.model_dump(mode="json")
        return payload


class IfcSpatialStructureApplyOperationDetail(DataRuntimeModel):
    """Provider-safe request and original result for one spatial batch."""

    detail_kind: Literal["ifc_spatial_structure_apply"] = "ifc_spatial_structure_apply"
    request: ApplyIfcSpatialStructureCommand
    result: IfcSpatialStructureApplyResult | None = None
    failure_blockers: IfcSpatialBlockerSummary | None = None

    @model_validator(mode="after")
    def validate_result_count(self) -> "IfcSpatialStructureApplyOperationDetail":
        if self.result is not None and self.result.applied_count != len(self.request.operations):
            raise ValueError("applied_count must equal the persisted operation count")
        if self.result is not None:
            for requested, applied in zip(
                self.request.operations, self.result.applied, strict=True,
            ):
                if requested.action != applied.action:
                    raise ValueError("spatial applied action differs from the request")
                if isinstance(requested, CreateIfcSpatialOperation):
                    if requested.client_ref != applied.client_ref:
                        raise ValueError("spatial create binding differs from the request")
                elif requested.target_global_id != applied.target_global_id:
                    raise ValueError("spatial target binding differs from the request")
        if self.result is not None and self.failure_blockers is not None:
            raise ValueError("successful spatial result cannot contain failure blockers")
        return self

    def persisted_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "detail_kind": self.detail_kind,
            "request": self.request.operation_request_payload(),
        }
        if self.result is not None:
            payload["result"] = self.result.model_dump(mode="json")
        if self.failure_blockers is not None:
            payload["failure_blockers"] = self.failure_blockers.model_dump(mode="json")
        return payload






class CriticalOperationStatus(StrEnum):
    PREPARED = "prepared"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class IfcProductsApplyOperationDetail(DataRuntimeModel):
    detail_kind: Literal["ifc_products_apply"] = "ifc_products_apply"
    request: ApplyIfcProductsCommand
    input_binding: ProductApplyInputBinding
    result: ProductApplySummary | None = None

    @model_validator(mode="after")
    def exact_input(self):
        if (self.request.package_id != self.input_binding.package_id
                or self.request.payload_name != self.input_binding.payload_name):
            raise ValueError("Apply input binding differs from its request")
        return self

    def persisted_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class IfcProductsEditOperationDetail(DataRuntimeModel):
    detail_kind: Literal["ifc_products_edit"] = "ifc_products_edit"
    request: EditIfcProductsCommand
    input_binding: ProductApplyInputBinding
    result: ProductsEditSummary | None = None

    @model_validator(mode="after")
    def exact_input(self):
        if (self.request.package_id != self.input_binding.package_id
                or self.request.payload_name != self.input_binding.payload_name):
            raise ValueError("Edit input binding differs from its request")
        return self

    def persisted_payload(self):
        return self.model_dump(mode="json", exclude_none=True)


class _ProductReportPublication(DataRuntimeModel):
    """Complete private report bytes, registered only in the IFC owner's transaction."""

    package: DataPackageRecord
    members: tuple[DataPackageMemberRecord, ...]
    artifacts: tuple[ArtifactRecord, ...]

    @model_validator(mode="after")
    def complete_report(self):
        by_version = {artifact.version_id: artifact for artifact in self.artifacts}
        if len(by_version) != len(self.artifacts) or len(self.members) != len(self.artifacts):
            raise ValueError("report assets must be unique and match its complete member set")
        for member in self.members:
            artifact = by_version.get(member.artifact.version_id)
            if (artifact is None or artifact.artifact_id != member.artifact.artifact_id
                    or artifact.sha256 != member.artifact.sha256
                    or artifact.owner_id != self.package.owner_id
                    or artifact.task_id != self.package.task_id
                    or artifact.run_id != self.package.created_by_run_id
                    or member.package_id != self.package.package_id
                    or member.owner_id != self.package.owner_id
                    or member.tenant_id != self.package.tenant_id
                    or member.task_id != self.package.task_id):
                raise ValueError("report member has an unrelated exact asset or Scope")
            if member.member_kind is PackageMemberKind.PAYLOAD and (
                    member.media_type != "application/vnd.apache.parquet"
                    or member.row_count is None or not member.arrow_schema):
                raise ValueError("Apply report payload must be a typed Parquet table")
        if {member.member_name for member in self.members
                if member.member_kind is PackageMemberKind.MANIFEST} != {"manifest.json"}:
            raise ValueError("Apply report requires its exact manifest")
        return self


class ProductApplyReportPublication(_ProductReportPublication):
    summary: ProductApplySummary

    @model_validator(mode="after")
    def four_payloads(self):
        if {item.logical_payload_name for item in self.members if item.member_kind is PackageMemberKind.PAYLOAD} != {
                "products", "components", "interfaces", "checks"}:
            raise ValueError("Apply report requires exactly its four Parquet payloads")
        return self


class ProductEditReportPublication(_ProductReportPublication):
    summary: ProductsEditSummary
    committed: bool

    @model_validator(mode="after")
    def edit_payloads_and_counts(self):
        payloads = {item.logical_payload_name for item in self.members if item.member_kind is PackageMemberKind.PAYLOAD}
        if payloads not in ({"products", "changes", "checks"}, {"products", "changes", "checks", "interfaces"}):
            raise ValueError("Edit report requires three core payloads and only actual optional interfaces")
        if self.committed:
            if self.summary.committed_count != self.summary.input_count:
                raise ValueError("committed report requires all input rows")
        elif any((self.summary.committed_count, self.summary.created_product_count, self.summary.deleted_product_count)):
            raise ValueError("failed edit report cannot contain actual changes")
        if not self.committed and next(item.row_count for item in self.members
                if item.logical_payload_name == "changes") != 0:
            raise ValueError("failed edit changes must be empty")
        return self


ProductReportPublication = ProductApplyReportPublication | ProductEditReportPublication


def product_report_committed(report: ProductReportPublication) -> bool:
    return report.committed if isinstance(report, ProductEditReportPublication) else report.summary.outcome == "committed"


class CriticalOperationRecord(DataRuntimeModel):
    """Durable record for one important side-effecting tool call."""

    operation_id: str = Field(min_length=1, max_length=80)
    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=80)
    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    operation_kind: CriticalOperationKind = CriticalOperationKind.SOURCE_INGESTION
    initiator: CriticalOperationInitiator = CriticalOperationInitiator.DIRECT_CLIENT
    tool_name: str = Field(min_length=1, max_length=120)
    intent_summary: str | None = Field(default=None, min_length=1, max_length=1_303)
    source_filename: str | None = Field(default=None, min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    request_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    status: CriticalOperationStatus
    result_package_id: str | None = Field(default=None, min_length=1, max_length=80)
    error_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{0,119}$",
    )
    error_message: str | None = Field(default=None, min_length=1, max_length=2_000)
    error_phase: CriticalOperationErrorPhase | None = None
    code_artifact: ArtifactVersionRef | None = None
    description_artifact: ArtifactVersionRef | None = None
    repair_parent_operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Exact parent failed authoring or repair Operation.",
    )
    detail: (
        SourceIngestionOperationDetail
        | DataTransformOperationDetail
        | CadMeasurementOperationDetail
        | IfcModelInitializeOperationDetail
        | IfcGeometryBasisConfigureOperationDetail
        | IfcTrustedBaselineLoadOperationDetail
        | IfcProjectUpdateOperationDetail
        | IfcSpatialStructureApplyOperationDetail
        | IfcProductsApplyOperationDetail
        | IfcProductsEditOperationDetail
        | ProductFamilyWriteOperationDetail
        | AuthoringDraftWriteOperationDetail
        | AuthoringPublishOperationDetail
        | GeometryInspectionOperationDetail
        | None
    ) = None
    base_ifc_revision_id: str | None = Field(default=None, min_length=1, max_length=80)
    expected_ifc_write_epoch: int | None = Field(default=None, ge=1)
    result_ifc_revision_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_geometry_resource_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Server-issued stable geometry resource chain identity.",
    )
    result_geometry_resource_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
    )
    target_product_family_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Server-issued stable ProductFamily root identity.",
    )
    base_product_family_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Exact immutable ProductFamilyVersion used as a revision base.",
    )
    result_product_family_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Exact immutable ProductFamilyVersion published by this Operation.",
    )
    result_inspection_artifact: ArtifactVersionRef | None = None
    started_at: datetime
    finished_at: datetime | None = None

    _validate_source_filename = field_validator("source_filename")(_require_source_filename)

    @field_validator("error_message")
    @classmethod
    def validate_error_message_bytes(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 2_000:
            raise ValueError("error_message exceeds 2,000 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def validate_lifecycle_result(self) -> "CriticalOperationRecord":
        if (self.base_ifc_revision_id is None) != (self.expected_ifc_write_epoch is None):
            raise ValueError("base IFC revision and expected write epoch must be provided together")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("operation finished_at precedes started_at")
        if self.status is CriticalOperationStatus.PREPARED:
            if any(
                value is not None
                for value in (
                    self.finished_at,
                    self.result_package_id,
                    self.result_ifc_revision_id,
                    self.result_geometry_resource_version_id,
                    self.result_product_family_version_id,
                    self.result_inspection_artifact,
                    self.error_code,
                    self.error_message,
                    self.error_phase,
                )
            ):
                raise ValueError("prepared operation cannot contain a terminal result")
        elif self.status is CriticalOperationStatus.SUCCEEDED:
            if self.finished_at is None:
                raise ValueError("successful operation requires finished_at")
            if any(
                value is not None
                for value in (self.error_code, self.error_message, self.error_phase)
            ):
                raise ValueError("successful operation cannot contain an error")
        elif self.status is CriticalOperationStatus.FAILED:
            if self.finished_at is None:
                raise ValueError("failed operation requires finished_at")
            if self.error_code is None or self.error_message is None:
                raise ValueError("failed operation requires error_code and error_message")
            if (
                (self.result_package_id is not None and not (
                    self.operation_kind is CriticalOperationKind.IFC_MODEL_WRITE
                    and isinstance(self.detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail))
                ))
                or self.result_ifc_revision_id is not None
                or self.result_geometry_resource_version_id is not None
                or self.result_product_family_version_id is not None
                or (self.result_inspection_artifact is not None and not (
                    self.operation_kind is CriticalOperationKind.GEOMETRY_INSPECTION
                    and isinstance(self.detail, GeometryInspectionOperationDetail)
                    and self.detail.result_kind == "case_report"
                ))
            ):
                raise ValueError("failed operation cannot contain a result")
        else:
            if self.finished_at is None:
                raise ValueError("indeterminate operation requires finished_at")
            if self.error_code is None or self.error_message is None:
                raise ValueError("indeterminate operation requires a technical error")
            if self.error_phase is not CriticalOperationErrorPhase.RECONCILE:
                raise ValueError("indeterminate operation requires reconcile phase")
            if (
                self.result_package_id is not None
                or self.result_ifc_revision_id is not None
                or self.result_geometry_resource_version_id is not None
                or self.result_product_family_version_id is not None
                or self.result_inspection_artifact is not None
            ):
                raise ValueError("indeterminate operation cannot contain a result")
        if (
            self.operation_kind is not CriticalOperationKind.GEOMETRY_INSPECTION
            and self.result_inspection_artifact is not None
        ):
            raise ValueError("only a geometry inspection Operation can bind an inspection Artifact")
        if self.operation_kind is CriticalOperationKind.SOURCE_INGESTION:
            if (
                self.base_ifc_revision_id is not None
                or self.expected_ifc_write_epoch is not None
                or self.result_ifc_revision_id is not None
                or self.target_geometry_resource_id is not None
                or self.result_geometry_resource_version_id is not None
                or self.target_product_family_id is not None
                or self.base_product_family_version_id is not None
                or self.result_product_family_version_id is not None
            ):
                raise ValueError(
                    "source_ingestion cannot contain IFC, geometry, or ProductFamily bindings"
                )
            if self.status is CriticalOperationStatus.SUCCEEDED and self.result_package_id is None:
                raise ValueError("successful source_ingestion requires a result package")
            if self.tool_name != "cad_import_source" or self.source_filename is None:
                raise ValueError("source_ingestion requires cad_import_source and source_filename")
            source_intent = f"Import {self.source_filename} as an immutable CAD L0 source package."
            if self.intent_summary is None:
                object.__setattr__(self, "intent_summary", source_intent)
            elif self.intent_summary != source_intent:
                raise ValueError("source_ingestion intent_summary must use the stable template")
            if self.code_artifact is not None:
                raise ValueError("source_ingestion cannot contain transformation code")
            if self.description_artifact is not None or self.repair_parent_operation_id is not None:
                raise ValueError("source_ingestion cannot contain transform repair references")
            if self.detail is not None and not isinstance(
                self.detail, SourceIngestionOperationDetail
            ):
                raise ValueError("source_ingestion detail kind is invalid")
            if self.status is CriticalOperationStatus.FAILED and self.error_phase not in {
                None,
                CriticalOperationErrorPhase.SOURCE_EXTRACTION,
            }:
                raise ValueError("source_ingestion failure phase is invalid")
        elif self.operation_kind is CriticalOperationKind.DATA_TRANSFORM:
            if (
                self.base_ifc_revision_id is not None
                or self.expected_ifc_write_epoch is not None
                or self.result_ifc_revision_id is not None
                or self.target_geometry_resource_id is not None
                or self.result_geometry_resource_version_id is not None
                or self.target_product_family_id is not None
                or self.base_product_family_version_id is not None
                or self.result_product_family_version_id is not None
            ):
                raise ValueError(
                    "data_transform cannot contain IFC, geometry, or ProductFamily bindings"
                )
            if self.status is CriticalOperationStatus.SUCCEEDED and self.result_package_id is None:
                raise ValueError("successful data_transform requires a result package")
            if (
                self.tool_name
                not in {
                    "data_transform",
                    "data_repair_transform",
                    "data_derive",
                    "cad_compute_measurements",
                }
                or self.source_filename is not None
            ):
                raise ValueError(
                    "data_transform requires an approved transform tool and no source_filename"
                )
            if self.intent_summary is None or self.code_artifact is None:
                raise ValueError("data_transform requires intent_summary and exact code")
            if self.tool_name in {"data_transform", "data_repair_transform", "data_derive"} and not isinstance(
                self.detail, DataTransformOperationDetail
            ):
                raise ValueError("data_transform requires typed transform detail")
            if (isinstance(self.detail, DataTransformOperationDetail)
                    and self.detail.ifc_source is not None and self.tool_name != "data_derive"):
                raise ValueError("only data_derive can bind an IFC read source")
            if self.tool_name == "cad_compute_measurements" and not isinstance(
                self.detail, CadMeasurementOperationDetail
            ):
                raise ValueError("cad_compute_measurements requires typed measurement detail")
            if self.tool_name == "data_repair_transform" or (
                self.tool_name in {"data_transform", "data_derive"} and self.repair_parent_operation_id is not None
            ):
                if self.repair_parent_operation_id is None or self.description_artifact is None:
                    raise ValueError(
                        "transform repair requires parent Operation and exact description"
                    )
                if self.repair_parent_operation_id == self.operation_id:
                    raise ValueError("transform repair cannot be its own parent")
            elif self.repair_parent_operation_id is not None:
                raise ValueError("only a Data Transform repair can contain a repair parent")
            if self.status is CriticalOperationStatus.FAILED and self.error_phase not in {
                CriticalOperationErrorPhase.CODE_VALIDATION,
                CriticalOperationErrorPhase.EXECUTION,
                CriticalOperationErrorPhase.OUTPUT_VALIDATION,
                CriticalOperationErrorPhase.COMMIT_FENCE,
            }:
                raise ValueError("data_transform failure phase is invalid")
        elif self.operation_kind is CriticalOperationKind.IFC_MODEL_WRITE:
            if self.tool_name not in {
                "ifc_model_initialize",
                "ifc_model_configure_geometry_basis",
                "ifc_trusted_baseline_load",
                "ifc_project_update",
                "ifc_spatial_structure_apply",
                "ifc_products_apply",
                "ifc_products_edit",
            }:
                raise ValueError("ifc_model_write requires an approved IFC write tool")
            if (
                self.source_filename is not None
                or (self.result_package_id is not None and self.tool_name not in {"ifc_products_apply", "ifc_products_edit"})
                or self.code_artifact is not None
                or self.description_artifact is not None
                or self.repair_parent_operation_id is not None
                or self.target_geometry_resource_id is not None
                or self.result_geometry_resource_version_id is not None
                or self.target_product_family_id is not None
                or self.base_product_family_version_id is not None
                or self.result_product_family_version_id is not None
            ):
                raise ValueError("IFC model write contains invalid cross-domain bindings")
            if self.tool_name == "ifc_model_initialize":
                self._validate_ifc_model_initialize()
            elif self.tool_name == "ifc_model_configure_geometry_basis":
                self._validate_ifc_geometry_basis_configure()
            elif self.tool_name == "ifc_project_update":
                self._validate_ifc_project_update()
            elif self.tool_name == "ifc_trusted_baseline_load":
                self._validate_ifc_trusted_baseline_load()
            elif self.tool_name == "ifc_products_apply":
                self._validate_ifc_products_apply()
            elif self.tool_name == "ifc_products_edit":
                self._validate_ifc_products_edit()
            else:
                self._validate_ifc_spatial_structure_apply()
        elif self.operation_kind is CriticalOperationKind.GEOMETRY_RESOURCE_WRITE:
            self._validate_draft_write("geometry")
        elif self.operation_kind is CriticalOperationKind.PRODUCT_FAMILY_WRITE:
            if isinstance(self.detail, ProductFamilyWriteOperationDetail) and self.detail.mode == "system_copy":
                self._validate_product_family_write()
            else:
                self._validate_draft_write("family")
        elif self.operation_kind is CriticalOperationKind.GEOMETRY_INSPECTION:
            self._validate_geometry_inspection()
        elif self.operation_kind is CriticalOperationKind.AUTHORING_PUBLISH:
            self._validate_authoring_publish()
        else:  # pragma: no cover - StrEnum is a closed set at validation time
            raise ValueError("unsupported critical Operation kind")
        return self

    def _validate_draft_write(self, root_kind: str) -> None:
        detail = self.detail
        root_id = (self.target_geometry_resource_id if root_kind == "geometry"
                   else self.target_product_family_id)
        expected_tool = ("ifc_geometry_resource_write" if root_kind == "geometry"
                         else "ifc_product_family_write")
        if (not isinstance(detail, AuthoringDraftWriteOperationDetail)
            or detail.root_kind != root_kind or detail.root_id != root_id
            or self.tool_name != expected_tool or self.intent_summary is None
            or self.code_artifact is None):
            raise ValueError("draft write requires its exact root, snapshot and typed detail")
        if any(value is not None for value in (
            self.source_filename, self.result_package_id, self.description_artifact,
            self.repair_parent_operation_id, self.base_ifc_revision_id,
            self.expected_ifc_write_epoch, self.result_ifc_revision_id,
            self.result_geometry_resource_version_id, self.base_product_family_version_id,
            self.result_product_family_version_id,
            self.target_product_family_id if root_kind == "geometry" else self.target_geometry_resource_id,
        )):
            raise ValueError("draft save cannot carry published, repair, IFC or Data results")
        if self.status is not CriticalOperationStatus.SUCCEEDED:
            raise ValueError("draft save is committed as one synchronous terminal transaction")

    def _validate_authoring_publish(self) -> None:
        detail = self.detail
        if (not isinstance(detail, AuthoringPublishOperationDetail)
            or self.tool_name != "ifc_authoring_publish" or self.intent_summary is None
            or self.status not in {CriticalOperationStatus.PREPARED, CriticalOperationStatus.SUCCEEDED,
                                   CriticalOperationStatus.FAILED}):
            raise ValueError("authoring publication requires its exact admitted request")
        if any(value is not None for value in (
            self.source_filename, self.result_package_id, self.description_artifact,
            self.repair_parent_operation_id, self.base_ifc_revision_id,
            self.expected_ifc_write_epoch, self.result_ifc_revision_id,
        )):
            raise ValueError("authoring publication cannot carry repair, IFC or Data bindings")
        if self.status is not CriticalOperationStatus.SUCCEEDED:
            if detail.geometry_versions or detail.family_version is not None:
                raise ValueError("uncommitted publication cannot expose formal outputs")
            if self.base_product_family_version_id is not None:
                raise ValueError("uncommitted publication cannot expose a Family result parent")
            if (self.target_geometry_resource_id != (detail.root_id if detail.root_kind == "geometry" else None)
                or self.target_product_family_id != (detail.root_id if detail.root_kind == "family" else None)):
                raise ValueError("publication target differs from its admitted request")
            if self.status is CriticalOperationStatus.FAILED and self.error_phase not in {
                CriticalOperationErrorPhase.CODE_VALIDATION, CriticalOperationErrorPhase.EXECUTION,
                CriticalOperationErrorPhase.OUTPUT_VALIDATION, CriticalOperationErrorPhase.STORAGE_VERIFICATION,
                CriticalOperationErrorPhase.COMMIT_FENCE,
            }:
                raise ValueError("publication failure phase is invalid")
            return
        if detail.root_kind == "geometry":
            if detail.family_version is not None or self.target_geometry_resource_id not in {
                item.resource_id for item in detail.geometry_versions
            }:
                raise ValueError("Geometry publication requires its exact Geometry result")
            result = next(item for item in detail.geometry_versions if item.resource_id == detail.root_id)
            if (self.target_geometry_resource_id != detail.root_id
                or self.result_geometry_resource_version_id != result.resource_version_id
                or self.target_product_family_id is not None
                or self.result_product_family_version_id is not None
                or self.base_product_family_version_id is not None):
                raise ValueError("Geometry publication result differs from its saved plan")
        elif (detail.family_version is None or self.target_product_family_id != detail.root_id
              or self.result_product_family_version_id != detail.family_version.family_version_id
              or self.base_product_family_version_id != detail.family_version.parent_family_version_id
              or self.target_geometry_resource_id is not None
              or self.result_geometry_resource_version_id is not None):
            raise ValueError("Family publication result differs from its saved plan")

    def _validate_geometry_inspection(self) -> None:
        if not isinstance(self.detail, GeometryInspectionOperationDetail):
            raise ValueError("geometry_inspection requires an exact typed source and request Artifact")
        expected_tool = (
            "ifc_geometry_preview" if self.detail.source_kind == "geometry_sample"
            else "ifc_product_realization_preview"
        )
        if self.tool_name != expected_tool or self.intent_summary is None:
            raise ValueError("geometry_inspection requires its exact Preview tool and intent")
        if any(value is not None for value in (
            self.source_filename, self.result_package_id, self.code_artifact,
            self.description_artifact, self.repair_parent_operation_id,
            self.base_ifc_revision_id, self.expected_ifc_write_epoch, self.result_ifc_revision_id,
            self.target_geometry_resource_id, self.result_geometry_resource_version_id,
            self.target_product_family_id, self.base_product_family_version_id,
            self.result_product_family_version_id,
        )):
            raise ValueError("geometry_inspection cannot carry authoring, IFC or Data result bindings")
        if self.status is CriticalOperationStatus.SUCCEEDED and self.result_inspection_artifact is None:
            raise ValueError("successful geometry_inspection requires an exact inspection Artifact")
        if self.status is CriticalOperationStatus.FAILED and self.error_phase not in {
            CriticalOperationErrorPhase.CODE_VALIDATION,
            CriticalOperationErrorPhase.EXECUTION,
            CriticalOperationErrorPhase.OUTPUT_VALIDATION,
            CriticalOperationErrorPhase.STORAGE_VERIFICATION,
            CriticalOperationErrorPhase.COMMIT_FENCE,
        }:
            raise ValueError("geometry_inspection failure phase is invalid")


    def _validate_product_family_write(self) -> None:
        if self.tool_name != "ifc_product_family_write":
            raise ValueError("product_family_write requires its approved authoring tool")
        if (
            self.source_filename is not None
            or self.result_package_id is not None
            or self.description_artifact is not None
            or self.base_ifc_revision_id is not None
            or self.expected_ifc_write_epoch is not None
            or self.result_ifc_revision_id is not None
            or self.target_geometry_resource_id is not None
            or self.result_geometry_resource_version_id is not None
        ):
            raise ValueError("product_family_write contains invalid cross-domain bindings")
        if (
            self.intent_summary is None
            or self.code_artifact is None
            or self.target_product_family_id is None
            or not isinstance(self.detail, ProductFamilyWriteOperationDetail)
        ):
            raise ValueError(
                "product_family_write requires intent, exact code, target root, and typed detail"
            )
        if self.target_product_family_id != self.detail.version_draft.family_id:
            raise ValueError("ProductFamily target root does not match its exact version draft")
        if (self.repair_parent_operation_id is not None
            or self.base_product_family_version_id is not None
            or self.detail.version_draft.parent_family_version_id is not None):
            raise ValueError("SYSTEM copy creates a new root without a repair or base version")
        if self.status is CriticalOperationStatus.SUCCEEDED:
            if (
                self.result_product_family_version_id is None
                or self.result_product_family_version_id
                != self.detail.version_draft.family_version_id
            ):
                raise ValueError("successful ProductFamily write requires its exact result version")
        elif self.result_product_family_version_id is not None:
            raise ValueError("non-successful ProductFamily write cannot contain a result version")
        if self.status is CriticalOperationStatus.FAILED and self.error_phase not in {
            CriticalOperationErrorPhase.CODE_VALIDATION,
            CriticalOperationErrorPhase.EXECUTION,
            CriticalOperationErrorPhase.OUTPUT_VALIDATION,
            CriticalOperationErrorPhase.STORAGE_VERIFICATION,
            CriticalOperationErrorPhase.COMMIT_FENCE,
        }:
            raise ValueError("ProductFamily write failure phase is invalid")

    def _validate_ifc_model_initialize(self) -> None:
        if self.intent_summary != IFC_MODEL_INITIALIZE_INTENT_SUMMARY:
            raise ValueError("ifc_model_initialize intent_summary must use the stable template")
        if (
            self.base_ifc_revision_id is not None
            or self.expected_ifc_write_epoch is not None
            or not isinstance(self.detail, IfcModelInitializeOperationDetail)
        ):
            raise ValueError("ifc_model_initialize contains invalid revision bindings or detail")
        if self.status is CriticalOperationStatus.SUCCEEDED:
            if self.result_ifc_revision_id is None or self.detail.result != "model_initialized":
                raise ValueError("successful IFC initialization requires its durable result")
        elif self.result_ifc_revision_id is not None:
            raise ValueError("non-successful IFC initialization cannot contain a result revision")
        elif self.status is CriticalOperationStatus.FAILED:
            if self.detail.result is not None:
                raise ValueError("failed IFC initialization cannot report a result")
            self._validate_ifc_write_failure_phase("IFC initialization")
        elif self.detail.result is not None:
            raise ValueError("prepared or indeterminate IFC initialization has no known result")

    def _validate_ifc_geometry_basis_configure(self) -> None:
        if self.intent_summary != IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY:
            raise ValueError(
                "ifc_model_configure_geometry_basis intent_summary must use the stable template"
            )
        if (
            self.base_ifc_revision_id is None
            or self.expected_ifc_write_epoch is None
            or not isinstance(self.detail, IfcGeometryBasisConfigureOperationDetail)
        ):
            raise ValueError(
                "ifc_model_configure_geometry_basis requires base revision bindings and typed detail"
            )
        if self.status is CriticalOperationStatus.SUCCEEDED:
            if (
                self.result_ifc_revision_id is None
                or self.detail.result != "geometry_basis_configured"
            ):
                raise ValueError(
                    "successful geometry-basis configuration requires its durable result"
                )
        elif self.result_ifc_revision_id is not None:
            raise ValueError(
                "non-successful geometry-basis configuration cannot contain a result revision"
            )
        elif self.status is CriticalOperationStatus.FAILED:
            if self.detail.result is not None:
                raise ValueError("failed geometry-basis configuration cannot report a result")
            self._validate_ifc_write_failure_phase("geometry-basis configuration")
        elif self.detail.result is not None:
            raise ValueError(
                "prepared or indeterminate geometry-basis configuration has no known result"
            )

    def _validate_ifc_trusted_baseline_load(self) -> None:
        if (self.intent_summary != IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY
                or self.base_ifc_revision_id is None
                or self.expected_ifc_write_epoch is None
                or not isinstance(self.detail, IfcTrustedBaselineLoadOperationDetail)):
            raise ValueError("trusted baseline load requires its exact base and source detail")
        if self.status is CriticalOperationStatus.SUCCEEDED:
            if self.result_ifc_revision_id is None or self.detail.result != "baseline_loaded":
                raise ValueError("successful trusted baseline load requires its durable revision and result")
        elif self.result_ifc_revision_id is not None or self.detail.result is not None:
            raise ValueError("uncommitted trusted baseline load cannot report a result")
        elif self.status is CriticalOperationStatus.FAILED:
            self._validate_ifc_write_failure_phase("trusted baseline load")

    def _validate_ifc_project_update(self) -> None:
        if self.intent_summary != IFC_PROJECT_UPDATE_INTENT_SUMMARY:
            raise ValueError("ifc_project_update intent_summary must use the stable template")
        if (
            self.base_ifc_revision_id is None
            or self.expected_ifc_write_epoch is None
            or not isinstance(self.detail, IfcProjectUpdateOperationDetail)
        ):
            raise ValueError("ifc_project_update requires base revision bindings and typed detail")
        if self.status is CriticalOperationStatus.SUCCEEDED:
            if self.result_ifc_revision_id is None or self.detail.result is None:
                raise ValueError("successful Project update requires its durable result")
        elif self.result_ifc_revision_id is not None:
            raise ValueError("non-successful Project update cannot contain a result revision")
        elif self.status is CriticalOperationStatus.FAILED:
            if self.detail.result is not None:
                raise ValueError("failed Project update cannot report a result")
            self._validate_ifc_write_failure_phase("Project update")
        elif self.detail.result is not None:
            raise ValueError("prepared or indeterminate Project update has no known result")

    def _validate_ifc_spatial_structure_apply(self) -> None:
        if self.intent_summary != IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY:
            raise ValueError(
                "ifc_spatial_structure_apply intent_summary must use the stable template"
            )
        if (
            self.base_ifc_revision_id is None
            or self.expected_ifc_write_epoch is None
            or not isinstance(self.detail, IfcSpatialStructureApplyOperationDetail)
        ):
            raise ValueError(
                "ifc_spatial_structure_apply requires base revision bindings and typed detail"
            )
        if self.status is CriticalOperationStatus.SUCCEEDED:
            if (
                self.result_ifc_revision_id is None
                or self.detail.result is None
                or self.detail.failure_blockers is not None
            ):
                raise ValueError("successful spatial-structure apply requires its durable result")
        elif self.result_ifc_revision_id is not None:
            raise ValueError(
                "non-successful spatial-structure apply cannot contain a result revision"
            )
        elif self.status is CriticalOperationStatus.FAILED:
            if self.detail.result is not None:
                raise ValueError("failed spatial-structure apply cannot report a result")
            self._validate_ifc_write_failure_phase("spatial-structure apply")
        elif self.detail.result is not None or self.detail.failure_blockers is not None:
            raise ValueError(
                "prepared or indeterminate spatial-structure apply has no known result"
            )


    def _validate_ifc_products_edit(self) -> None:
        if (self.intent_summary != IFC_PRODUCTS_EDIT_INTENT_SUMMARY or self.base_ifc_revision_id is None
                or self.expected_ifc_write_epoch is None or not isinstance(self.detail, IfcProductsEditOperationDetail)
                or self.detail.request.base_revision_id != self.base_ifc_revision_id):
            raise ValueError("Edit requires its exact base, input and typed detail")
        result = self.detail.result
        if self.status is CriticalOperationStatus.SUCCEEDED:
            if result is None or self.result_ifc_revision_id is None or self.result_package_id is None:
                raise ValueError("Edit success requires its original report and exact revision")
            if result.committed_count != result.input_count:
                raise ValueError("Edit success commits every input row")
        elif self.status is CriticalOperationStatus.FAILED:
            if self.result_ifc_revision_id is not None or (result is not None and any((
                    result.committed_count, result.created_product_count, result.deleted_product_count))):
                raise ValueError("failed Edit cannot expose committed changes")
            if (result is None) != (self.result_package_id is None):
                raise ValueError("failed Edit summary and report must be present together")
            self._validate_ifc_write_failure_phase("product edit")
        elif result is not None or self.result_package_id is not None or self.result_ifc_revision_id is not None:
            raise ValueError("unsettled edit cannot expose a terminal report")

    def _validate_ifc_products_apply(self) -> None:
        if (self.intent_summary != IFC_PRODUCTS_APPLY_INTENT_SUMMARY
                or self.base_ifc_revision_id is None
                or self.expected_ifc_write_epoch is None
                or not isinstance(self.detail, IfcProductsApplyOperationDetail)):
            raise ValueError("Apply requires exact base, input binding and typed detail")
        result = self.detail.result
        if self.status is CriticalOperationStatus.SUCCEEDED:
            if (result is None or result.outcome != "committed"
                    or self.result_ifc_revision_id is None or self.result_package_id is None):
                raise ValueError("Apply success requires its exact revision and complete report")
        elif self.status is CriticalOperationStatus.FAILED:
            if self.result_ifc_revision_id is not None or (result is not None and result.outcome == "committed"):
                raise ValueError("failed Apply cannot contain committed products")
            if (self.result_package_id is None) != (result is None):
                raise ValueError("failed Apply summary and diagnostic package must be present together")
            self._validate_ifc_write_failure_phase("product Apply")
        elif result is not None or self.result_package_id is not None:
            raise ValueError("unsettled Apply cannot contain a public report")

    def _validate_ifc_write_failure_phase(self, label: str) -> None:
        if self.error_phase not in {
            CriticalOperationErrorPhase.IFC_VALIDATION,
            CriticalOperationErrorPhase.STORAGE_VERIFICATION,
            CriticalOperationErrorPhase.COMMIT_FENCE,
        }:
            raise ValueError(f"{label} failure phase is invalid")


class CatalogResourceKind(StrEnum):
    PACKAGE = "package"
    OPERATION = "operation"


class DataCatalogEntry(DataRuntimeModel):
    """Run-scoped stable alias for an exact package or operation."""

    catalog_entry_id: str = Field(min_length=1, max_length=80)
    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=80)
    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    resource_kind: CatalogResourceKind
    alias: str = Field(min_length=5, max_length=32)
    ordinal: int = Field(ge=1)
    catalog_revision: int = Field(ge=1)
    package_id: str | None = Field(default=None, min_length=1, max_length=80)
    operation_id: str | None = Field(default=None, min_length=1, max_length=80)
    created_at: datetime

    @model_validator(mode="after")
    def validate_target_and_alias(self) -> "DataCatalogEntry":
        if self.resource_kind is CatalogResourceKind.PACKAGE:
            if self.package_id is None or self.operation_id is not None:
                raise ValueError("package Catalog entry requires only package_id")
            if re.fullmatch(_DATA_ALIAS_PATTERN, self.alias) is None:
                raise ValueError("package Catalog alias must use data_NNN")
        else:
            if self.operation_id is None or self.package_id is not None:
                raise ValueError("operation Catalog entry requires only operation_id")
            if re.fullmatch(_OPERATION_ALIAS_PATTERN, self.alias) is None:
                raise ValueError("operation Catalog alias must use op_NNN")
        return self


class ProductFamilyOperationSource(DataRuntimeModel):
    """Exact code ownership of a current Family authoring Operation."""

    operation_id: str = Field(min_length=1, max_length=80)
    detail_kind: Literal["authoring_draft_write", "product_family_write"]
    catalog_entry: DataCatalogEntry
    code_artifact: ArtifactVersionRef
    status: CriticalOperationStatus
    started_at: datetime
    finished_at: datetime
    error_phase: CriticalOperationErrorPhase | None = None
    error_code: str | None = None
    error_message: str | None = None
    parent_owner_alias: str | None = None
    result_product_family_version_id: str | None = None

    @model_validator(mode="after")
    def validate_source_publication(self) -> "ProductFamilyOperationSource":
        if (self.catalog_entry.operation_id != self.operation_id
                or self.status not in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}):
            raise ValueError("Family code source requires an exact terminal Operation")
        return self


class PublishedDataPackage(DataRuntimeModel):
    package: DataPackageRecord
    members: tuple[DataPackageMemberRecord, ...]
    catalog_entry: DataCatalogEntry


class DataTransformExecutionClaim(DataRuntimeModel):
    """Private generation fence for the original Data Transform Operation."""

    operation_id: str = Field(min_length=1, max_length=80)
    owner: str = Field(min_length=1, max_length=160)
    generation: int = Field(ge=1)
    token: str = Field(min_length=32, exclude=True, repr=False)


class AuthoringExecutionClaim(DataRuntimeModel):
    """Private commit fence for one Geometry or Family build/publication Operation."""

    operation_id: str = Field(min_length=1, max_length=80)
    owner: str = Field(min_length=1, max_length=160)
    generation: int = Field(ge=1)
    token: str = Field(min_length=32, exclude=True, repr=False)


class AuthoringExecutionState(DataRuntimeModel):
    """Server-only claim projection of the original build/publication Operation."""

    operation: CriticalOperationRecord
    execution_owner: str | None = None
    execution_generation: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_authoring_operation(self) -> "AuthoringExecutionState":
        if self.operation.operation_kind not in {
            CriticalOperationKind.GEOMETRY_RESOURCE_WRITE,
            CriticalOperationKind.PRODUCT_FAMILY_WRITE,
            CriticalOperationKind.GEOMETRY_INSPECTION,
            CriticalOperationKind.AUTHORING_PUBLISH,
        }:
            raise ValueError("execution claim requires a Geometry or Family build/publication Operation")
        if self.execution_owner is not None and self.execution_generation == 0:
            raise ValueError("claimed authoring Operation requires a positive generation")
        return self


class DataTransformCandidate(DataRuntimeModel):
    """Exact output staged on its Operation before Catalog publication."""

    package: DataPackageRecord
    members: tuple[DataPackageMemberRecord, ...]


class DataTransformOperationState(DataRuntimeModel):
    """Server-only control projection of one persisted Operation row."""

    operation: CriticalOperationRecord
    request_identity: str = Field(min_length=1, max_length=255)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    request_payload: dict[str, object]
    resolved_command: dict[str, object]
    resolved_parent_operation_alias: str | None = None
    deadline_at: datetime
    execution_owner: str | None = None
    execution_generation: int = Field(ge=0)
    execution_lease_expires_at: datetime | None = None
    execution_token_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN, exclude=True, repr=False)
    candidate: DataTransformCandidate | None = None

    @model_validator(mode="after")
    def validate_transform_operation(self) -> "DataTransformOperationState":
        if self.operation.tool_name != "data_derive":
            raise ValueError("transform state requires a Data Transform Operation")
        if self.operation.operation_kind is not CriticalOperationKind.DATA_TRANSFORM:
            raise ValueError("transform state requires the Data Transform kind")
        if (
            self.candidate is not None
            and self.candidate.package.request_fingerprint != self.operation.request_fingerprint
        ):
            raise ValueError("candidate fingerprint does not match its Operation")
        return self



class CriticalOperationCatalogSummary(DataRuntimeModel):
    """Terminal discovery fields independent of any executable resource DTO."""

    operation_id: str
    run_id: str
    operation_kind: CriticalOperationKind
    initiator: CriticalOperationInitiator
    tool_name: str
    intent_summary: str | None
    status: Literal[CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED]
    started_at: datetime
    finished_at: datetime
    error_code: str | None = None


class PublishedOperationCatalogSummary(DataRuntimeModel):
    operation: CriticalOperationCatalogSummary
    catalog_entry: DataCatalogEntry

    @model_validator(mode="after")
    def validate_target(self):
        if self.catalog_entry.operation_id != self.operation.operation_id:
            raise ValueError("Operation summary Catalog entry targets a different Operation")
        return self


class PublishedCriticalOperation(DataRuntimeModel):
    operation: CriticalOperationRecord
    catalog_entry: DataCatalogEntry

    @model_validator(mode="after")
    def validate_terminal_publication(self) -> "PublishedCriticalOperation":
        if self.operation.status not in {
            CriticalOperationStatus.SUCCEEDED,
            CriticalOperationStatus.FAILED,
        }:
            raise ValueError("only terminal business Operations can enter the Catalog")
        if self.catalog_entry.operation_id != self.operation.operation_id:
            raise ValueError("Operation Catalog entry targets a different Operation")
        return self


class IfcProductsRecoveryOperation(DataRuntimeModel):
    """Original product-write identity, including operations without a Catalog entry."""

    operation: CriticalOperationRecord
    created_at: datetime
    catalog_entry: DataCatalogEntry | None = None

    @model_validator(mode="after")
    def validate_product_identity(self):
        if (self.operation.tool_name not in {"ifc_products_apply", "ifc_products_edit"}
                or not isinstance(self.operation.detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail))):
            raise ValueError("product recovery requires a current Apply or Edit Operation")
        if self.catalog_entry is not None:
            if (self.catalog_entry.operation_id != self.operation.operation_id
                    or self.catalog_entry.run_id != self.operation.run_id
                    or self.operation.status not in {
                        CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}):
                raise ValueError("Product recovery Catalog entry differs from its terminal Operation")
        return self


class PublishedImportResult(DataRuntimeModel):
    package: PublishedDataPackage
    operation: PublishedCriticalOperation


class PublishedPackageResult(DataRuntimeModel):
    package: PublishedDataPackage
    operation: PublishedCriticalOperation


class CatalogEntryPage(DataRuntimeModel):
    entries: tuple[DataCatalogEntry, ...] = Field(max_length=50)
    complete: bool
    next_before_sequence: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_page(self) -> "CatalogEntryPage":
        if self.complete != (self.next_before_sequence is None):
            raise ValueError("Catalog page continuation is inconsistent")
        if not self.complete:
            if not self.entries or self.next_before_sequence != self.entries[-1].ordinal:
                raise ValueError("Catalog page continuation must use its minimum sequence")
        if any(
            left.ordinal <= right.ordinal for left, right in zip(self.entries, self.entries[1:])
        ):
            raise ValueError("Catalog page entries must be strictly descending")
        return self


def validate_source_l0_import(
    package: DataPackageRecord,
    members: tuple[DataPackageMemberRecord, ...],
    operation: CriticalOperationRecord,
) -> None:
    """Validate the one first-stage package shape before durable publication."""

    if package.package_kind is not DataPackageKind.SOURCE_L0:
        raise ValueError("first-stage import requires a source_l0 package")
    if operation.operation_kind is not CriticalOperationKind.SOURCE_INGESTION:
        raise ValueError("source_l0 publication requires source_ingestion operation")
    if package.request_fingerprint != operation.request_fingerprint:
        raise ValueError("package and operation request fingerprints must match")
    payload_members = {
        source_l0_payload_member_name(name): name for name in SOURCE_L0_PAYLOAD_NAMES
    }
    expected_names = set(payload_members) | {
        SOURCE_L0_SOURCE_MEMBER_NAME,
        "manifest.json",
        "description.md",
    }
    by_name = {member.member_name: member for member in members}
    if len(by_name) != len(members) or set(by_name) != expected_names:
        raise ValueError(
            "source_l0 members must be source/original.dxf, manifest.json, "
            "description.md, and six payloads"
        )
    expected_contracts = {
        SOURCE_L0_SOURCE_MEMBER_NAME: (
            PackageMemberKind.SOURCE,
            "application/dxf",
            None,
        ),
        "manifest.json": (PackageMemberKind.MANIFEST, "application/json", None),
        "description.md": (PackageMemberKind.DESCRIPTION, "text/markdown", None),
    }
    expected_contracts.update(
        {
            member_name: (
                PackageMemberKind.PAYLOAD,
                "application/vnd.apache.parquet",
                "required",
            )
            for member_name in payload_members
        }
    )
    artifact_refs: set[tuple[str, str]] = set()
    for name, member in by_name.items():
        if (
            member.package_id != package.package_id
            or member.owner_id != package.owner_id
            or member.tenant_id != package.tenant_id
            or member.task_id != package.task_id
        ):
            raise ValueError("source_l0 member scope does not match package")
        expected_kind, expected_media_type, expected_row_count = expected_contracts[name]
        if member.member_kind is not expected_kind or member.media_type != expected_media_type:
            raise ValueError(f"source_l0 member contract is invalid: {name}")
        if expected_row_count == "required" and member.row_count is None:
            raise ValueError(f"source_l0 payload requires row_count: {name}")
        artifact_ref = (member.artifact.artifact_id, member.artifact.version_id)
        if artifact_ref in artifact_refs:
            raise ValueError("source_l0 members must reference distinct Artifact versions")
        artifact_refs.add(artifact_ref)


def validate_package_publication(
    package: DataPackageRecord,
    members: tuple[DataPackageMemberRecord, ...],
    operation: CriticalOperationRecord,
) -> None:
    """Validate one source or derived package before atomic publication."""

    if package.package_kind is DataPackageKind.SOURCE_L0:
        validate_source_l0_import(package, members, operation)
        return
    if isinstance(operation.detail, (IfcProductsApplyOperationDetail, IfcProductsEditOperationDetail)):
        if (operation.status not in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}
                or operation.result_package_id != package.package_id
                or package.request_fingerprint != operation.request_fingerprint
                or operation.detail.result is None):
            raise ValueError("Apply report must belong to the same terminal Operation")
        payload_names = {member.logical_payload_name for member in members
                         if member.member_kind is PackageMemberKind.PAYLOAD}
        if isinstance(operation.detail, IfcProductsApplyOperationDetail):
            if payload_names != {"products", "components", "interfaces", "checks"}:
                raise ValueError("Apply report must contain its complete four payloads")
        elif payload_names not in ({"products", "changes", "checks"}, {"products", "changes", "checks", "interfaces"}):
            raise ValueError("Edit report must contain its complete three core payloads")
        if any(member.package_id != package.package_id for member in members):
            raise ValueError("Apply report members belong to another package")
        return
    if operation.operation_kind is not CriticalOperationKind.DATA_TRANSFORM:
        raise ValueError("derived publication requires data_transform operation")
    if operation.status is not CriticalOperationStatus.SUCCEEDED:
        raise ValueError("derived publication requires succeeded operation")
    if operation.result_package_id != package.package_id:
        raise ValueError("operation result package does not match derived package")
    if package.request_fingerprint != operation.request_fingerprint:
        raise ValueError("package and operation request fingerprints must match")
    if package.description_title is None or not package.section_titles:
        raise ValueError("derived package requires compiled description projection")
    if not isinstance(
        operation.detail,
        (DataTransformOperationDetail, CadMeasurementOperationDetail),
    ):
        raise ValueError("derived package requires transform operation detail")
    if operation.detail.output_package_name != package.name:
        raise ValueError("transform output package name does not match package")
    if operation.intent_summary != f"{package.description_title} — {package.summary}":
        raise ValueError("transform intent_summary must use the compiled Package synopsis")

    by_name = {member.member_name: member for member in members}
    if len(by_name) != len(members):
        raise ValueError("derived package member names must be unique")
    code_members = [member for member in members if member.member_kind is PackageMemberKind.CODE]
    description_members = [
        member for member in members if member.member_kind is PackageMemberKind.DESCRIPTION
    ]
    manifest_members = [
        member for member in members if member.member_kind is PackageMemberKind.MANIFEST
    ]
    payload_members = [
        member for member in members if member.member_kind is PackageMemberKind.PAYLOAD
    ]
    schema_members = [
        member for member in members if member.member_kind is PackageMemberKind.SCHEMA
    ]
    declared_outputs = {item.payload_name: item for item in operation.detail.outputs}
    payloads_by_name = {
        member.logical_payload_name: member
        for member in payload_members
        if member.logical_payload_name is not None
    }
    expected_code_name, expected_code_media_type = (
        ("code/transformation.sql", "application/sql")
        if operation.detail.engine == DataTransformEngine.DUCKDB_SQL
        else ("code/transformation.py", "text/x-python")
    )
    json_output_names = {
        item.payload_name for item in operation.detail.outputs if item.format == "json"
    }
    expected_schema_names = {
        f"schemas/{payload_name}.schema.json" for payload_name in json_output_names
    }
    if (
        len(code_members) != 1
        or code_members[0].member_name != expected_code_name
        or code_members[0].media_type != expected_code_media_type
        or len(description_members) != 1
        or description_members[0].member_name != "description.md"
        or description_members[0].media_type != "text/markdown"
        or len(manifest_members) != 1
        or manifest_members[0].member_name != "manifest.json"
        or manifest_members[0].media_type != "application/json"
        or not 1 <= len(payload_members) <= 16
        or len(payload_members) != len(operation.detail.outputs)
        or len(payloads_by_name) != len(payload_members)
        or set(payloads_by_name) != set(declared_outputs)
        or len(schema_members) != len(json_output_names)
        or {member.member_name for member in schema_members} != expected_schema_names
        or len(members) != len(payload_members) + len(schema_members) + 3
    ):
        raise ValueError(
            "derived members must be manifest, description, transformation code, and 1..16 declared payloads"
        )
    code = code_members[0]
    if code.artifact != operation.code_artifact:
        raise ValueError("Package CODE and Operation must reference the same Artifact version")
    if (
        operation.description_artifact is not None
        and description_members[0].artifact != operation.description_artifact
    ):
        raise ValueError(
            "Package DESCRIPTION and Operation must reference the same Artifact version"
        )
    for payload_name, declared in declared_outputs.items():
        payload = payloads_by_name[payload_name]
        common_metadata_invalid = (
            payload.payload_description != declared.description
            or payload.logical_byte_count is None
        )
        if declared.format == "parquet":
            format_metadata_invalid = (
                payload.member_name != f"payloads/{payload_name}/part-00000.parquet"
                or payload.logical_key_fields != declared.logical_key_fields
                or payload.media_type != "application/vnd.apache.parquet"
                or payload.row_count is None
                or payload.json_kind is not None
                or not 1 <= len(payload.arrow_schema) <= 512
            )
        else:
            format_metadata_invalid = (
                payload.member_name != f"payloads/{payload_name}.json"
                or payload.media_type != "application/json"
                or payload.row_count is not None
                or payload.logical_key_fields
                or payload.arrow_schema
                or payload.json_kind != declared.json_kind
            )
        if common_metadata_invalid or format_metadata_invalid:
            raise ValueError("derived payload metadata does not match its output declaration")
    artifact_refs: set[tuple[str, str]] = set()
    for member in members:
        if (
            member.package_id != package.package_id
            or member.owner_id != package.owner_id
            or member.tenant_id != package.tenant_id
            or member.task_id != package.task_id
        ):
            raise ValueError("derived member scope does not match package")
        artifact_ref = (member.artifact.artifact_id, member.artifact.version_id)
        if artifact_ref in artifact_refs:
            raise ValueError("derived members must reference distinct Artifact versions")
        artifact_refs.add(artifact_ref)


__all__ = [
    "ArrowColumnRecord",
    "ArtifactVersionRef",
    "AuthoringExecutionClaim",
    "AuthoringExecutionState",
    "CatalogEntryPage",
    "CatalogResourceKind",
    "CadMeasurementOperationDetail",
    "CriticalOperationErrorPhase",
    "CriticalOperationInitiator",
    "CriticalOperationKind",
    "CriticalOperationRecord",
    "CriticalOperationStatus",
    "DataTransformEngine",
    "DataTransformOperationDetail",
    "IfcReadSourceRef",
    "IfcSourceChangedError",
    "DataCatalogEntry",
    "DataPackageMemberRecord",
    "DataPackageKind",
    "DataPackageRecord",
    "DirectSessionBinding",
    "IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY",
    "IFC_MODEL_INITIALIZE_INTENT_SUMMARY",
    "IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY",
    "IfcTrustedBaselineLoadOperationDetail",
    "IfcGeometryBasisConfigureOperationDetail",
    "IfcModelInitializeOperationDetail",
    "GeometryInspectionOperationDetail",
    "JsonPayloadKind",
    "OpenDirectSessionRequest",
    "OpenedDirectSession",
    "PackageMemberKind",
    "PublishedCriticalOperation",
    "PublishedDataPackage",
    "PublishedImportResult",
    "PublishedPackageResult",
    "ProductFamilyWriteOperationDetail",
    "SOURCE_L0_PAYLOAD_NAMES",
    "SOURCE_L0_PAYLOAD_ORDER",
    "SOURCE_L0_LOGICAL_KEYS",
    "SOURCE_L0_SOURCE_MEMBER_NAME",
    "SourceIngestionOperationDetail",
    "TransformInputBindingRecord",
    "TransformOutputRecord",
    "TransformParameterKind",
    "TransformParameterRecord",
    "ValidatedSource",
    "normalize_direct_session_name",
    "source_l0_payload_member_name",
    "validate_package_publication",
    "validate_source_l0_import",
]
