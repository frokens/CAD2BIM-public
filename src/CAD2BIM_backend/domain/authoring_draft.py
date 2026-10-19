"""Exact identity of the one editable draft attached to a resource root.

The snapshot bytes belong to Artifact. Geometry and Family continue to own
their respective snapshot contents; this module contains no content schema.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef, PublishedCriticalOperation
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceVersionBundle
from CAD2BIM_backend.domain.product_family import ProductFamilyVersionBundle
from CAD2BIM_backend.domain.product_family_authoring_contracts import ProductFamilyAuthoringVersionDraft
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope
from CAD2BIM_backend.domain.control_plane import ConflictError


class AuthoringPublicationCommitRejected(RuntimeError):
    """A database integrity response proves the publication transaction rolled back."""

    code = "AUTHORING_PUBLICATION_COMMIT_REJECTED"


class AuthoringPublicationPending(ConflictError):
    """An authorized root has an active publication owned by another action."""

    code = "PUBLICATION_RECONCILIATION_REQUIRED"


class AuthoringDraftChanged(ConflictError):
    """The root's monotonic generation no longer matches this exact request."""

    code = "DRAFT_CHANGED"


class AuthoringDraftRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root_kind: Literal["geometry", "family"]
    root_id: str = Field(min_length=1, max_length=80)
    owner_scope: ResourceOwnerScope
    generation: int = Field(ge=1, strict=True)
    snapshot: ArtifactVersionRef


class AuthoringDraftInfoTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_kind: Literal["draft"]
    draft_ref: str = Field(min_length=1, max_length=16_384)


class AuthoringDraftCommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: PublishedCriticalOperation
    draft: AuthoringDraftRecord


class AuthoringPublicationResources(BaseModel):
    """Exact formal results without another Task's private Operation receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    geometry_versions: tuple[GeometryResourceVersionBundle, ...] = ()
    family_version: ProductFamilyAuthoringVersionDraft | None = None


class AuthoringPublicationCommitResult(AuthoringPublicationResources):
    operation: PublishedCriticalOperation
    family_version: ProductFamilyVersionBundle | None = None
