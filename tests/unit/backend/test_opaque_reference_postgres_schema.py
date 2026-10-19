from __future__ import annotations

import io

from alembic import command
from alembic.config import Config
from sqlalchemy import UniqueConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


def test_reference_migration_has_scoped_insert_read_only_and_reversible_index():
    config = Config()
    config.set_main_option("script_location", "src/CAD2BIM_backend/infrastructure/postgres/migrations")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/test")
    out = io.StringIO()
    config.output_buffer = out
    command.upgrade(config, "20260906_0078:20260907_0079", sql=True)
    sql = " ".join(out.getvalue().split())
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "GRANT SELECT, INSERT ON TABLE opaque_reference_index TO cad2bim_mcp_runtime" in sql
    assert "GRANT UPDATE" not in sql and "GRANT DELETE" not in sql
    for key in ("tenant_id", "principal_id", "task_id"):
        assert f"current_setting('cad2bim.{key}', true)" in sql
    assert "BEFORE UPDATE ON opaque_reference_index" in sql
    table = Base.metadata.tables["opaque_reference_index"]
    assert ("scope_digest", "reference_sha256") in {
        tuple(c.columns.keys()) for c in table.constraints if isinstance(c, UniqueConstraint)
    }
    assert not ({"current", "version_no", "geometry", "family_contract"} & set(table.c.keys()))
    out = io.StringIO()
    config.output_buffer = out
    command.downgrade(config, "20260907_0079:20260906_0078", sql=True)
    assert "DROP TABLE opaque_reference_index" in out.getvalue()
    assert "DROP FUNCTION cad2bim_guard_opaque_reference_index()" in out.getvalue()
