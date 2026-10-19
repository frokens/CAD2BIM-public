"""Immutable, server-compiled input closure for one Run."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import sha256_digest

if TYPE_CHECKING:
    from CAD2BIM_backend.domain.control_plane import ArtifactRecord


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_INPUT_SUFFIXES = frozenset({".dxf", ".dwg", ".ifc"})


class RunInputManifestError(ValueError):
    """The requested input is outside the immutable Run closure."""


class RunInputMediaKind(StrEnum):
    DXF = "dxf"
    DWG = "dwg"
    IFC = "ifc"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunInputEntry(_FrozenModel):
    artifact_id: str = Field(min_length=1, max_length=255)
    artifact_version_id: str = Field(min_length=1, max_length=255)
    artifact_version_no: int = Field(ge=1)
    logical_handle: str = Field(min_length=1, max_length=255)
    media_kind: RunInputMediaKind
    original_filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    source_run_id: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("artifact_id", "artifact_version_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        return _plain_component("Run input identity", value)

    @field_validator("original_filename", "logical_handle")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        filename = _plain_component("Run input filename", value)
        if Path(filename).suffix.lower() not in _INPUT_SUFFIXES:
            raise ValueError("Run input filename has an unsupported suffix")
        return filename

    @field_validator("content_type")
    @classmethod
    def normalize_content_type(cls, value: str) -> str:
        normalized = value.split(";", 1)[0].strip().lower()
        if not normalized:
            raise ValueError("Run input content_type must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_server_handle(self) -> "RunInputEntry":
        expected = derive_run_input_logical_handle(
            artifact_id=self.artifact_id,
            artifact_version_id=self.artifact_version_id,
            original_filename=self.original_filename,
        )
        if self.logical_handle != expected:
            raise ValueError("Run input logical_handle is not server-derived")
        if self.media_kind.value != Path(self.original_filename).suffix.lower().removeprefix("."):
            raise ValueError("Run input media_kind does not match its filename")
        return self


class RunInputManifest(_FrozenModel):
    manifest_id: str = Field(pattern=r"^run_input_manifest_[0-9a-f]{64}$")
    tenant_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=255)
    bundle_id: str = Field(min_length=1, max_length=255)
    entries: tuple[RunInputEntry, ...] = Field(max_length=128)
    digest: str = Field(pattern=_SHA256_PATTERN)
    created_at: datetime

    @field_validator("tenant_id", "task_id", "run_id", "bundle_id")
    @classmethod
    def validate_scope_identity(cls, value: str) -> str:
        return _plain_component("Run input manifest identity", value)

    @model_validator(mode="after")
    def validate_identity_and_digest(self) -> "RunInputManifest":
        expected_id = derive_run_input_manifest_id(self.run_id)
        if self.manifest_id != expected_id:
            raise ValueError("Run input manifest_id is not server-derived")
        if self.digest != run_input_manifest_digest(self):
            raise ValueError("Run input manifest digest changed")
        artifact_ids = tuple(entry.artifact_id for entry in self.entries)
        version_ids = tuple(entry.artifact_version_id for entry in self.entries)
        handles = tuple(entry.logical_handle for entry in self.entries)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("Run input artifact IDs must be unique")
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("Run input Artifact versions must be unique")
        if len(handles) != len(set(handles)):
            raise ValueError("Run input logical handles must be unique")
        return self

    def require_entry(
        self,
        *,
        artifact_id: str,
        artifact_version_id: str,
        logical_handle: str,
        sha256: str,
    ) -> RunInputEntry:
        for entry in self.entries:
            if entry.logical_handle != logical_handle:
                continue
            if (
                entry.artifact_id == artifact_id
                and entry.artifact_version_id == artifact_version_id
                and entry.sha256 == sha256
            ):
                return entry
            break
        raise RunInputManifestError("input is not a member of the immutable Run manifest")


def compile_run_input_manifest(
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    bundle_id: str,
    artifacts: Sequence[ArtifactRecord],
    created_at: datetime,
) -> RunInputManifest:
    entries = tuple(_entry_from_artifact(artifact) for artifact in artifacts)
    payload = {
        "manifest_id": derive_run_input_manifest_id(run_id),
        "tenant_id": tenant_id,
        "task_id": task_id,
        "run_id": run_id,
        "bundle_id": bundle_id,
        "entries": entries,
        "created_at": created_at,
    }
    return RunInputManifest(**payload, digest=sha256_digest(payload))


def rebind_run_input_manifest(
    source: RunInputManifest,
    *,
    run_id: str,
    created_at: datetime,
) -> RunInputManifest:
    """Copy the exact immutable entries to a new resume Run identity."""

    payload = {
        "manifest_id": derive_run_input_manifest_id(run_id),
        "tenant_id": source.tenant_id,
        "task_id": source.task_id,
        "run_id": run_id,
        "bundle_id": source.bundle_id,
        "entries": source.entries,
        "created_at": created_at,
    }
    return RunInputManifest(**payload, digest=sha256_digest(payload))


def run_input_manifest_digest(manifest: RunInputManifest) -> str:
    return sha256_digest(manifest.model_dump(mode="python", exclude={"digest"}))


def derive_run_input_manifest_id(run_id: str) -> str:
    run = _plain_component("run_id", run_id)
    return "run_input_manifest_" + hashlib.sha256(run.encode("utf-8")).hexdigest()


def derive_run_input_logical_handle(
    *, artifact_id: str, artifact_version_id: str, original_filename: str
) -> str:
    artifact = _plain_component("artifact_id", artifact_id)
    version = _plain_component("artifact_version_id", artifact_version_id)
    filename = _plain_component("original_filename", original_filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in _INPUT_SUFFIXES:
        raise RunInputManifestError("Run input filename has an unsupported suffix")
    digest = hashlib.sha256(f"{artifact}\0{version}".encode("utf-8")).hexdigest()
    return f"artifact-{digest}{suffix}"


def _entry_from_artifact(artifact: Any) -> RunInputEntry:
    try:
        media_kind = RunInputMediaKind(Path(artifact.filename).suffix.lower().removeprefix("."))
    except ValueError as exc:
        raise RunInputManifestError("Run input Artifact type is unsupported") from exc
    return RunInputEntry(
        artifact_id=artifact.artifact_id,
        artifact_version_id=artifact.version_id,
        artifact_version_no=artifact.version_no,
        logical_handle=derive_run_input_logical_handle(
            artifact_id=artifact.artifact_id,
            artifact_version_id=artifact.version_id,
            original_filename=artifact.filename,
        ),
        media_kind=media_kind,
        original_filename=artifact.filename,
        content_type=artifact.content_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        source_run_id=artifact.run_id,
    )


def _plain_component(label: str, value: str) -> str:
    component = str(value).strip()
    if (
        not component
        or len(component) > 255
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
    ):
        raise RunInputManifestError(f"invalid {label}")
    return component


__all__ = [
    "RunInputEntry",
    "RunInputManifestError",
    "RunInputManifest",
    "RunInputMediaKind",
    "compile_run_input_manifest",
    "derive_run_input_logical_handle",
    "derive_run_input_manifest_id",
    "rebind_run_input_manifest",
    "run_input_manifest_digest",
]
