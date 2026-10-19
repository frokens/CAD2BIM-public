"""Select one complete private IFC candidate on the original Operation.

Revision ID: 20260928_0096
Revises: 20260928_0095
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260928_0096"
down_revision = "20260928_0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("critical_operations", sa.Column("ifc_candidate_json", JSONB()))
    op.create_check_constraint(
        op.f("ck_critical_operations_ifc_candidate_only_on_ifc_write"),
        "critical_operations",
        "operation_kind = 'ifc_model_write' OR ifc_candidate_json IS NULL",
    )
    for role in (
        "cad2bim_api_runtime", "cad2bim_mcp_runtime", "cad2bim_runner_runtime",
    ):
        op.execute(sa.text(
            f"GRANT UPDATE (ifc_candidate_json) ON TABLE critical_operations TO {role}"
        ))


def downgrade() -> None:
    op.execute(sa.text("""
        DO $guard$ BEGIN
            IF EXISTS (SELECT 1 FROM critical_operations
                       WHERE ifc_candidate_json IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot discard selected IFC candidates'
                    USING ERRCODE = '23514';
            END IF;
        END $guard$
    """))
    for role in (
        "cad2bim_api_runtime", "cad2bim_mcp_runtime", "cad2bim_runner_runtime",
    ):
        op.execute(sa.text(
            f"REVOKE UPDATE (ifc_candidate_json) ON TABLE critical_operations FROM {role}"
        ))
    op.drop_constraint(
        op.f("ck_critical_operations_ifc_candidate_only_on_ifc_write"),
        "critical_operations", type_="check",
    )
    op.drop_column("critical_operations", "ifc_candidate_json")
