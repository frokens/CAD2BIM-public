"""Add a scope-bound navigation index without changing referenced domain objects.

Revision ID: 20260907_0079
Revises: 20260906_0078
"""

from alembic import op
import sqlalchemy as sa

revision = "20260907_0079"
down_revision = "20260906_0078"
branch_labels = None
depends_on = None
_TABLE = "opaque_reference_index"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("handle", sa.String(25), primary_key=True),
        sa.Column("tenant_id", sa.String(80), sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("principal_id", sa.String(80), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("reference_kind", sa.String(2), nullable=False),
        sa.Column("reference_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "task_id"], ["tasks.tenant_id", "tasks.task_id"],
                                name="fk_opaque_reference_index_tenant_task_tasks", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id", "principal_id"],
                                ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
                                name="fk_opaque_reference_index_tenant_principal", ondelete="CASCADE"),
        sa.UniqueConstraint("scope_digest", "reference_sha256", name="uq_opaque_reference_index_scope_target"),
        sa.CheckConstraint("reference_kind IN ('gr','gv','fr','fv','sc','gp','pp','gq')", name="reference_kind_valid"),
        sa.CheckConstraint("length(handle) = 25", name="handle_length_valid"),
        sa.CheckConstraint("length(scope_digest) = 64", name="scope_digest_length_valid"),
        sa.CheckConstraint("length(reference_sha256) = 64", name="reference_sha256_length_valid"),
        sa.CheckConstraint("length(sealed_reference) BETWEEN 8 AND 16384", name="reference_length_valid"),
    )
    op.create_index("ix_opaque_reference_index_tenant_id", _TABLE, ["tenant_id"])
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))
    scope = (
        "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "principal_id = NULLIF(current_setting('cad2bim.principal_id', true), '') AND "
        "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')"
    )
    op.execute(sa.text(f"CREATE POLICY opaque_reference_index_read ON {_TABLE} FOR SELECT "
                       f"TO cad2bim_mcp_runtime USING ({scope})"))
    op.execute(sa.text(f"CREATE POLICY opaque_reference_index_insert ON {_TABLE} FOR INSERT "
                       f"TO cad2bim_mcp_runtime WITH CHECK ({scope})"))
    op.execute(sa.text(f"GRANT SELECT, INSERT ON TABLE {_TABLE} TO cad2bim_mcp_runtime"))
    # No UPDATE or DELETE privilege: a handle can never silently select another object.
    op.execute(sa.text("""
        CREATE FUNCTION cad2bim_guard_opaque_reference_index() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'reference index bindings are immutable' USING ERRCODE = '23514';
        END; $$
    """))
    op.execute(sa.text(f"CREATE TRIGGER trg_opaque_reference_index_immutable BEFORE UPDATE ON {_TABLE} "
                       "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_opaque_reference_index()"))


def downgrade() -> None:
    op.drop_table(_TABLE)
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_opaque_reference_index()"))
