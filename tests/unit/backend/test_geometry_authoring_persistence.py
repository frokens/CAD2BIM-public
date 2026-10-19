from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.exc import OperationalError

from CAD2BIM_backend.domain.control_plane import IdempotencyConflictError
from CAD2BIM_backend.domain.geometry_authoring import (
    GeometryAuthoringInputMetadata,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.infrastructure.postgres.models import Base


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disconnect", "serialization", "permission", "pool_timeout"])
async def test_exact_geometry_read_classifies_only_temporary_database_failures(monkeypatch, kind):
    from unittest.mock import AsyncMock, Mock
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
    from CAD2BIM_backend.infrastructure.postgres import repository_geometry_resource as module

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False

    repository = object.__new__(module.PostgresGeometryResourceRepositoryMixin)
    repository._sessions = Session
    repository._require_geometry_context = Mock()
    monkeypatch.setattr(module, "set_tenant_execution_context", AsyncMock())
    original = RuntimeError("database diagnostic")
    original.sqlstate = "40001" if kind == "serialization" else "42501"
    error = (SQLAlchemyTimeoutError("pool wait") if kind == "pool_timeout" else
             OperationalError("SELECT", {}, original, connection_invalidated=kind == "disconnect"))
    repository._load_geometry_bundle = AsyncMock(side_effect=error)
    expected = (OperationalError if kind == "permission" else
                TimeoutError if kind == "pool_timeout" else ConnectionError)
    with pytest.raises(expected):
        await repository.get_exact_geometry_resource_version(object(), resource_id="g", resource_version_id="v")
    repository._load_geometry_bundle.assert_awaited_once()


def _render_upgrade() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260902_0067:20260902_0068", sql=True)
    return " ".join(output.getvalue().split())


def _render_downgrade() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.downgrade(config, "20260902_0068:20260902_0067", sql=True)
    return " ".join(output.getvalue().split())


def test_geometry_authoring_input_metadata_is_closed_and_kind_specific() -> None:
    mapped = GeometryAuthoringInputMetadata(resource_kind=GeometryResourceKind.MAPPED_MODEL)
    assert GeometryAuthoringInputMetadata.model_validate(mapped.version_metadata()) == mapped

    schema = {
        "type": "object",
        "properties": {"diameter": {"type": "number", "minimum": 0.01}},
        "required": ["diameter"],
        "additionalProperties": False,
    }
    function = GeometryAuthoringInputMetadata(
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        parameter_schema=schema,
    )
    assert "metadata_schema" not in function.version_metadata()
    assert "validation_samples" not in function.version_metadata()
    with pytest.raises(ValidationError, match="Extra inputs"):
        GeometryAuthoringInputMetadata.model_validate(function.version_metadata() | {"validation_samples": []})

    with pytest.raises(ValidationError, match="cannot contain function inputs"):
        GeometryAuthoringInputMetadata(
            resource_kind=GeometryResourceKind.MAPPED_MODEL,
            parameter_schema=schema,
        )
    with pytest.raises(ValidationError, match="requires a parameter schema"):
        GeometryAuthoringInputMetadata(resource_kind=GeometryResourceKind.MODELING_FUNCTION)


def test_geometry_authoring_orm_has_exact_result_relation_and_closed_kind() -> None:
    operations = Base.metadata.tables["critical_operations"]
    assert operations.c.target_geometry_resource_id.nullable is True
    assert operations.c.result_geometry_resource_version_id.nullable is True
    checks = " ".join(
        str(item.sqltext) for item in operations.constraints if isinstance(item, CheckConstraint)
    )
    assert "geometry_resource_write" in checks
    assert "ifc_geometry_resource_create" in checks
    assert "ifc_geometry_resource_repair" in checks
    assert "ifc_geometry_resource_write" in checks
    assert "result_geometry_resource_version_id IS NOT NULL" in checks
    assert any(
        tuple(item.column_keys)
        == ("target_geometry_resource_id", "result_geometry_resource_version_id")
        and tuple(element.target_fullname for element in item.elements)
        == (
            "geometry_resource_versions.resource_id",
            "geometry_resource_versions.resource_version_id",
        )
        for item in operations.constraints
        if isinstance(item, ForeignKeyConstraint)
    )
    versions = Base.metadata.tables["geometry_resource_versions"]
    assert any(
        tuple(column.name for column in item.columns) == ("resource_id", "resource_version_id")
        for item in versions.constraints
        if isinstance(item, UniqueConstraint)
    )


def test_0068_upgrade_adds_atomic_geometry_operation_fences() -> None:
    sql = _render_upgrade()
    assert "ADD COLUMN target_geometry_resource_id VARCHAR(80)" in sql
    assert "ADD COLUMN result_geometry_resource_version_id VARCHAR(80)" in sql
    assert "fk_critical_operations_exact_geometry_result_version" in sql
    assert "uq_geometry_resource_versions_resource_exact_version" in sql
    assert "geometry_resource_write" in sql
    assert "ifc_geometry_resource_create" in sql
    assert "ifc_geometry_resource_repair" in sql
    assert "OLD.status = 'indeterminate' AND NEW.status = 'prepared'" in sql
    assert "geometry authoring exact source ArtifactVersion is invalid" in sql
    assert "source_artifact.current_version_id = source_version.version_id" not in sql
    assert "parent.status IN ('succeeded', 'failed')" in sql
    assert "CREATE POLICY geometry_authoring_task_isolation" in sql
    assert "CREATE POLICY geometry_authoring_source_task_isolation ON artifacts" in sql
    assert (
        "CREATE POLICY geometry_authoring_source_version_task_isolation ON artifact_versions"
    ) in sql
    assert (
        "CREATE POLICY geometry_authoring_catalog_task_isolation ON data_catalog_entries"
    ) in sql
    assert "CREATE FUNCTION public.cad2bim_geometry_source_artifact_visible" in sql
    assert "CREATE FUNCTION public.cad2bim_geometry_operation_catalog_visible" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET row_security = off" in sql
    assert "artifact.kind <> 'geometry_authoring_source'" in sql
    assert "operation.operation_kind <> 'geometry_resource_write'" in sql
    assert "resource_kind <> 'operation' OR" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "AS RESTRICTIVE" in sql
    assert "GRANT UPDATE (result_geometry_resource_version_id)" in sql


def test_0068_downgrade_is_fail_closed_for_any_authoring_operation() -> None:
    sql = _render_downgrade()
    assert "IN ACCESS EXCLUSIVE MODE" in sql
    assert "operation_kind = 'geometry_resource_write'" in sql
    assert "kind = 'geometry_authoring_source'" in sql
    assert "ERRCODE = '23514'" in sql
    assert "geometry authoring facts are not empty" in sql
    assert "DROP POLICY geometry_authoring_catalog_task_isolation" in sql
    assert "DROP POLICY geometry_authoring_source_version_task_isolation" in sql
    assert "DROP POLICY geometry_authoring_source_task_isolation" in sql
    assert "DROP FUNCTION public.cad2bim_geometry_source_artifact_visible" in sql
    assert "DROP FUNCTION public.cad2bim_geometry_operation_catalog_visible" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "expected_ifc_write_epoch IS NOT NULL)))))" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ("system", "task"))
async def test_current_geometry_replay_preserves_identity_and_new_writes_require_clean_facts(monkeypatch, scope_kind):
    from unittest.mock import AsyncMock
    from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope
    from CAD2BIM_backend.infrastructure.postgres import repository_geometry_resource as module
    from tests.unit.backend.test_geometry_resource_contracts import _bundle

    publication = _bundle(GeometryResourceKind.MAPPED_MODEL)
    scope = ResourceOwnerScope(scope_kind=scope_kind,
        tenant_id="tenant_a" if scope_kind == "task" else None,
        task_id="task_a" if scope_kind == "task" else None)
    publication = publication.model_copy(update={"resource": publication.resource.model_copy(update={"owner_scope": scope})})

    class Session:
        execute = AsyncMock()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def begin(self): return self

    repository = object.__new__(module.PostgresGeometryResourceRepositoryMixin)
    repository._sessions = Session
    repository.require_geometry_resource_publication_allowed = AsyncMock()
    repository._load_geometry_bundle = AsyncMock(return_value=publication)
    monkeypatch.setattr(module, "set_tenant_execution_context", AsyncMock())
    assert await repository.publish_geometry_resource_version(object(), publication) == publication
    repository.require_geometry_resource_publication_allowed.assert_awaited_once()
    changed = publication.model_copy(update={"version": publication.version.model_copy(update={"content_digest": "f" * 64})})
    with pytest.raises(IdempotencyConflictError):
        await repository.publish_geometry_resource_version(object(), changed)
