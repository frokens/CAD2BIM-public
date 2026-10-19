"""Composed concurrency-safe in-memory control-plane repository."""

from CAD2BIM_backend.infrastructure.memory.artifact_repository import (
    ArtifactRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.memory.data_runtime_repository import (
    DataRuntimeRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.memory.data_query_attempt_repository import (
    DataQueryAttemptRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.memory.task_run_repository import (
    TaskRunRepositoryMixin,
)
class InMemoryControlPlaneRepository(
    TaskRunRepositoryMixin,
    ArtifactRepositoryMixin,
    DataRuntimeRepositoryMixin,
    DataQueryAttemptRepositoryMixin,
):
    """Reference adapter with the same async contract as durable persistence."""


__all__ = ["InMemoryControlPlaneRepository"]
