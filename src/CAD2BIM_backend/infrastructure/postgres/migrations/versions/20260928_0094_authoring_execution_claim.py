"""Fence current Geometry and Family execution on their original Operation.

Revision ID: 20260928_0094
Revises: 20260928_0093
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "20260928_0094"
down_revision = "20260928_0093"
branch_labels = None
depends_on = None


def _replace_function(name, old, new):
    # Reuse the existing bounded SQL function patcher after A's final v4 guards.
    guards = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260920_0087_ephemeral_authoring_validation"
    )
    guards._replace_function(name, old, new)


_AUTHORING_CLAIM_UPDATE = """IF OLD.status = NEW.status
                       AND OLD.status IN ('prepared', 'indeterminate')
                       AND to_jsonb(NEW) - ARRAY[
                           'authoring_execution_owner', 'authoring_execution_generation',
                           'authoring_execution_token_sha256'
                       ] = to_jsonb(OLD) - ARRAY[
                           'authoring_execution_owner', 'authoring_execution_generation',
                           'authoring_execution_token_sha256'
                       ]
                       AND (
                           (NEW.authoring_execution_owner IS NOT NULL
                            AND NEW.authoring_execution_token_sha256 IS NOT NULL
                            AND NEW.authoring_execution_generation =
                                OLD.authoring_execution_generation + 1)
                           OR (OLD.status = 'indeterminate'
                               AND OLD.authoring_execution_owner IS NOT NULL
                               AND NEW.authoring_execution_owner IS NULL
                               AND NEW.authoring_execution_token_sha256 IS NULL
                               AND NEW.authoring_execution_generation =
                                   OLD.authoring_execution_generation)
                       ) THEN
                        RETURN NEW;
                    END IF;
                    """


def _authoring_claim_guards(*, enable):
    # Existing identity/source and terminal checks remain in the original guards.
    # Only an in-flight update of the three claim columns may keep the status.
    for name, marker in (
        ("cad2bim_guard_geometry_authoring_operation",
         "IF NOT ((OLD.status = 'prepared' AND"),
        ("cad2bim_guard_product_family_authoring_operation",
         "IF NOT (\n                        (OLD.status = 'prepared' AND"),
    ):
        old, new = marker, _AUTHORING_CLAIM_UPDATE + marker
        _replace_function(name, old if enable else new, new if enable else old)


def upgrade():
    op.add_column("critical_operations", sa.Column("authoring_execution_owner", sa.String(160)))
    op.add_column(
        "critical_operations",
        sa.Column("authoring_execution_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("critical_operations", sa.Column("authoring_execution_token_sha256", sa.String(64)))
    op.create_check_constraint(
        op.f("ck_critical_operations_authoring_control_consistent"),
        "critical_operations",
        "((operation_kind NOT IN ('geometry_resource_write', 'product_family_write') "
        "AND authoring_execution_owner IS NULL AND authoring_execution_generation = 0 "
        "AND authoring_execution_token_sha256 IS NULL) "
        "OR (operation_kind IN ('geometry_resource_write', 'product_family_write') "
        "AND authoring_execution_generation >= 0 "
        "AND ((authoring_execution_owner IS NULL "
        "AND authoring_execution_token_sha256 IS NULL) "
        "OR (authoring_execution_owner IS NOT NULL "
        "AND authoring_execution_token_sha256 IS NOT NULL "
        "AND length(authoring_execution_token_sha256) = 64 "
        "AND authoring_execution_generation > 0))))",
    )
    for role in ("cad2bim_mcp_runtime", "cad2bim_runner_runtime"):
        op.execute(sa.text(
            "GRANT UPDATE (authoring_execution_owner, authoring_execution_generation, "
            f"authoring_execution_token_sha256) ON TABLE critical_operations TO {role}"
        ))
    _authoring_claim_guards(enable=True)


def downgrade():
    op.execute(sa.text("""
        DO $guard$ BEGIN
            IF EXISTS (SELECT 1 FROM critical_operations
                       WHERE authoring_execution_generation > 0) THEN
                RAISE EXCEPTION 'Cannot discard admitted authoring execution claims' USING ERRCODE = '23514';
            END IF;
        END $guard$
    """))
    for role in ("cad2bim_mcp_runtime", "cad2bim_runner_runtime"):
        op.execute(sa.text(
            "REVOKE UPDATE (authoring_execution_owner, authoring_execution_generation, "
            f"authoring_execution_token_sha256) ON TABLE critical_operations FROM {role}"
        ))
    _authoring_claim_guards(enable=False)
    op.drop_constraint(
        op.f("ck_critical_operations_authoring_control_consistent"),
        "critical_operations",
        type_="check",
    )
    for name in (
        "authoring_execution_token_sha256",
        "authoring_execution_generation",
        "authoring_execution_owner",
    ):
        op.drop_column("critical_operations", name)
