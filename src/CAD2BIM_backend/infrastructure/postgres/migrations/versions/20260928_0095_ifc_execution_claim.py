"""Fence IFC execution on its original Operation and fixed deadline.

Revision ID: 20260928_0095
Revises: 20260928_0094
"""

from alembic import op
import sqlalchemy as sa


revision = "20260928_0095"
down_revision = "20260928_0094"
branch_labels = None
depends_on = None


_CLAIM_COLUMNS = (
    "ifc_execution_owner, ifc_execution_generation, ifc_execution_token_sha256, "
    "ifc_guard_pid, ifc_guard_birth_us"
)

_CONTROL_CHECK = (
    "((operation_kind <> 'ifc_model_write' AND ifc_deadline_at IS NULL "
    "AND ifc_execution_owner IS NULL AND ifc_execution_generation = 0 "
    "AND ifc_execution_token_sha256 IS NULL AND ifc_guard_pid IS NULL "
    "AND ifc_guard_birth_us IS NULL) OR (operation_kind = 'ifc_model_write' "
    "AND ifc_deadline_at IS NOT NULL AND ifc_execution_generation >= 0 "
    "AND ((ifc_execution_owner IS NULL "
    "AND ifc_execution_token_sha256 IS NULL AND ifc_guard_pid IS NULL "
    "AND ifc_guard_birth_us IS NULL) OR (ifc_execution_owner IS NOT NULL "
    "AND ifc_execution_generation > 0 "
    "AND ifc_execution_token_sha256 IS NOT NULL "
    "AND length(ifc_execution_token_sha256) = 64 "
    "AND ((ifc_guard_pid IS NULL AND ifc_guard_birth_us IS NULL) "
    "OR (ifc_guard_pid IS NOT NULL AND ifc_guard_birth_us IS NOT NULL "
    "AND ifc_guard_pid > 0 AND ifc_guard_birth_us > 0))))))"
)


def upgrade() -> None:
    op.add_column("critical_operations", sa.Column("ifc_deadline_at", sa.DateTime(timezone=True)))
    op.add_column("critical_operations", sa.Column("ifc_execution_owner", sa.String(160)))
    op.add_column("critical_operations", sa.Column(
        "ifc_execution_generation", sa.Integer(), nullable=False, server_default="0",
    ))
    op.add_column("critical_operations", sa.Column("ifc_execution_token_sha256", sa.String(64)))
    op.add_column("critical_operations", sa.Column("ifc_guard_pid", sa.BigInteger()))
    op.add_column("critical_operations", sa.Column("ifc_guard_birth_us", sa.BigInteger()))
    op.execute(sa.text(
        "UPDATE critical_operations SET ifc_deadline_at = started_at + INTERVAL '1130 seconds' "
        "WHERE operation_kind = 'ifc_model_write'"
    ))
    op.create_check_constraint(
        op.f("ck_critical_operations_ifc_execution_control_consistent"),
        "critical_operations", _CONTROL_CHECK,
    )
    for role in (
        "cad2bim_api_runtime", "cad2bim_mcp_runtime", "cad2bim_runner_runtime",
    ):
        op.execute(sa.text(
            f"GRANT UPDATE ({_CLAIM_COLUMNS}) ON TABLE critical_operations TO {role}"
        ))


def downgrade() -> None:
    op.execute(sa.text("""
        DO $guard$ BEGIN
            IF EXISTS (SELECT 1 FROM critical_operations
                       WHERE ifc_execution_generation > 0) THEN
                RAISE EXCEPTION 'Cannot discard admitted IFC execution claims'
                    USING ERRCODE = '23514';
            END IF;
        END $guard$
    """))
    for role in (
        "cad2bim_api_runtime", "cad2bim_mcp_runtime", "cad2bim_runner_runtime",
    ):
        op.execute(sa.text(
            f"REVOKE UPDATE ({_CLAIM_COLUMNS}) ON TABLE critical_operations FROM {role}"
        ))
    op.drop_constraint(
        op.f("ck_critical_operations_ifc_execution_control_consistent"),
        "critical_operations", type_="check",
    )
    for name in (
        "ifc_guard_birth_us", "ifc_guard_pid", "ifc_execution_token_sha256",
        "ifc_execution_generation", "ifc_execution_owner", "ifc_deadline_at",
    ):
        op.drop_column("critical_operations", name)
