"""Protect exact named geometry inputs without rewriting historical versions.

Revision ID: 20260910_0084
Revises: 20260909_0083
"""

from alembic import op
import sqlalchemy as sa

revision = "20260910_0084"
down_revision = "20260909_0083"
branch_labels = None
depends_on = None

_TABLE = "geometry_resource_version_inputs"
_READ = "cad2bim_api_runtime, cad2bim_runner_runtime, cad2bim_mcp_runtime, cad2bim_system_curator"
_WRITE = "cad2bim_runner_runtime, cad2bim_mcp_runtime"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("resource_version_id", sa.String(80), nullable=False),
        sa.Column("input_key", sa.String(64), nullable=False),
        sa.Column("target_resource_version_id", sa.String(80), nullable=False),
        sa.PrimaryKeyConstraint("resource_version_id", "input_key", name=op.f("pk_" + _TABLE)),
        sa.ForeignKeyConstraint(["resource_version_id"], ["geometry_resource_versions.resource_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_resource_version_id"], ["geometry_resource_versions.resource_version_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("input_key ~ '^[a-z][a-z0-9_]{0,63}$'", name=op.f("ck_" + _TABLE + "_input_key_valid")),
    )
    op.create_index("ix_geometry_resource_version_inputs_target", _TABLE, ["target_resource_version_id"])
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))
    visible_owner = f"EXISTS (SELECT 1 FROM geometry_resource_versions v WHERE v.resource_version_id = {_TABLE}.resource_version_id)"
    target = (
        "EXISTS (SELECT 1 FROM geometry_resource_versions v "
        "JOIN geometry_resources r ON r.resource_id = v.resource_id "
        "JOIN geometry_resource_version_assets a ON a.resource_version_id = v.resource_version_id "
        f"WHERE v.resource_version_id = {_TABLE}.target_resource_version_id "
        "AND r.resource_kind = 'mapped_model' AND a.asset_role = 'canonical_geometry' "
        "AND a.asset_format = 'occt_brep_v1')"
    )
    owner_base = (
        "EXISTS (SELECT 1 FROM geometry_resource_versions v "
        "JOIN geometry_resources r ON r.resource_id = v.resource_id "
        f"WHERE v.resource_version_id = {_TABLE}.resource_version_id "
    )
    task_owner = owner_base + (
        "AND r.owner_scope = 'TASK' "
        "AND r.owner_tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '') "
        "AND r.owner_task_id = NULLIF(current_setting('cad2bim.task_id', true), ''))"
    )
    system_owner = owner_base + "AND r.owner_scope = 'SYSTEM')"
    op.execute(sa.text(f"CREATE POLICY geometry_inputs_select ON {_TABLE} FOR SELECT TO {_READ} USING ({visible_owner})"))
    op.execute(sa.text(f"CREATE POLICY geometry_inputs_task_insert ON {_TABLE} FOR INSERT TO {_WRITE} WITH CHECK ({task_owner} AND {target})"))
    op.execute(sa.text(f"CREATE POLICY geometry_inputs_system_insert ON {_TABLE} FOR INSERT TO cad2bim_system_curator WITH CHECK ({system_owner} AND {target})"))
    op.execute(sa.text(f"REVOKE ALL ON {_TABLE} FROM PUBLIC"))
    op.execute(sa.text(f"GRANT SELECT ON {_TABLE} TO {_READ}"))
    op.execute(sa.text(f"GRANT INSERT ON {_TABLE} TO {_WRITE}, cad2bim_system_curator"))


def downgrade() -> None:
    # Removing the FK protection of live dependencies requires explicit migration.
    op.execute(sa.text(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {_TABLE}) THEN RAISE EXCEPTION 'geometry inputs are in use'; END IF; END $$"))
    op.drop_table(_TABLE)
