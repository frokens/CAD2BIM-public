"""Composed async PostgreSQL control-plane repository."""

from CAD2BIM_backend.infrastructure.postgres.repository_artifact import (
    PostgresArtifactRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    PostgresDataRuntimeRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_ifc_model import (
    PostgresIfcModelRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import (
    PostgresGeometryResourceRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_query import (
    PostgresGeometryQueryRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_authoring import (
    PostgresGeometryAuthoringRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_ifc_query import (
    PostgresIfcQueryRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_data_query_attempt import (
    PostgresDataQueryAttemptRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_task_run import (
    PostgresTaskRunRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_feedback import (
    PostgresFeedbackRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_product_family import (
    PostgresProductFamilyRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_product_family_authoring import (
    PostgresProductFamilyAuthoringRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_reference_index import (
    PostgresReferenceIndexRepositoryMixin,
)


class PostgresControlPlaneRepository(
    PostgresTaskRunRepositoryMixin,
    PostgresFeedbackRepositoryMixin,
    PostgresArtifactRepositoryMixin,
    PostgresDataRuntimeRepositoryMixin,
    PostgresIfcModelRepositoryMixin,
    PostgresGeometryResourceRepositoryMixin,
    PostgresGeometryAuthoringRepositoryMixin,
    PostgresGeometryQueryRepositoryMixin,
    PostgresIfcQueryRepositoryMixin,
    PostgresDataQueryAttemptRepositoryMixin,
    PostgresProductFamilyRepositoryMixin,
    PostgresProductFamilyAuthoringRepositoryMixin,
    PostgresReferenceIndexRepositoryMixin,
):
    """Production repository using one transaction per public operation."""


__all__ = ["PostgresControlPlaneRepository"]
