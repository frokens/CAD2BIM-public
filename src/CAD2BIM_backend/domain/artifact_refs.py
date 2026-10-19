"""Small immutable references to exact Artifact Registry versions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IFCArtifactRef(BaseModel):
    """Exact immutable IFC ArtifactVersion referenced by a Run record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_run_id: str = Field(min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    filename: str = Field(min_length=1, max_length=500)


class FinalIFCArtifactRef(IFCArtifactRef):
    """Exact immutable IFC file published as a Run's final Artifact."""

    @field_validator("filename")
    @classmethod
    def validate_ifc_filename(cls, value: str) -> str:
        if not value.lower().endswith(".ifc"):
            raise ValueError("final IFC Artifact filename must end with .ifc")
        return value


__all__ = ["FinalIFCArtifactRef", "IFCArtifactRef"]
