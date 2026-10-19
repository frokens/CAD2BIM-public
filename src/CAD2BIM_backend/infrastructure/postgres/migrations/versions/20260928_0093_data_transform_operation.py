"""Keep one Data Transform request, execution claim, and candidate on its Operation.

Revision ID: 20260928_0093
Revises: 20260928_0092
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260928_0093"
down_revision = "20260928_0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "critical_operations"
    op.add_column(table, sa.Column("transform_request_identity", sa.String(255)))
    op.add_column(table, sa.Column("transform_request_digest", sa.String(64)))
    op.add_column(table, sa.Column("transform_request_json", JSONB()))
    op.add_column(table, sa.Column("transform_resolved_json", JSONB()))
    op.add_column(table, sa.Column("transform_deadline_at", sa.DateTime(timezone=True)))
    op.add_column(table, sa.Column("transform_execution_owner", sa.String(160)))
    op.add_column(
        table,
        sa.Column("transform_execution_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        table, sa.Column("transform_execution_lease_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column(table, sa.Column("transform_execution_token_sha256", sa.String(64)))
    op.add_column(table, sa.Column("transform_candidate_json", JSONB()))
    op.create_index(
        "uq_critical_operations_transform_request_identity",
        table,
        ["run_id", "transform_request_identity"],
        unique=True,
        postgresql_where=sa.text("transform_request_identity IS NOT NULL"),
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_transform_control_consistent"),
        table,
        "((transform_request_identity IS NULL AND transform_request_digest IS NULL "
        "AND transform_request_json IS NULL AND transform_resolved_json IS NULL "
        "AND transform_deadline_at IS NULL AND transform_execution_owner IS NULL "
        "AND transform_execution_generation = 0 "
        "AND transform_execution_lease_expires_at IS NULL "
        "AND transform_execution_token_sha256 IS NULL AND transform_candidate_json IS NULL) "
        "OR (tool_name IN ('data_transform', 'data_repair_transform') "
        "AND transform_request_identity IS NOT NULL "
        "AND transform_request_digest IS NOT NULL "
        "AND length(transform_request_digest) = 64 "
        "AND transform_request_json IS NOT NULL AND transform_resolved_json IS NOT NULL "
        "AND transform_deadline_at IS NOT NULL AND transform_execution_generation >= 0 "
        "AND ((transform_execution_owner IS NULL "
        "AND transform_execution_lease_expires_at IS NULL "
        "AND transform_execution_token_sha256 IS NULL) "
        "OR (transform_execution_owner IS NOT NULL "
        "AND transform_execution_generation > 0 "
        "AND transform_execution_lease_expires_at IS NOT NULL "
        "AND transform_execution_token_sha256 IS NOT NULL "
        "AND length(transform_execution_token_sha256) = 64))))",
    )
    op.execute(sa.text(
        "GRANT UPDATE (transform_execution_owner, transform_execution_generation, "
        "transform_execution_lease_expires_at, transform_execution_token_sha256, "
        "transform_candidate_json, status, result_package_id, error_code, "
        "error_message, error_phase, finished_at) ON TABLE critical_operations "
        "TO cad2bim_mcp_runtime"
    ))

    op.execute(sa.text(
        "GRANT UPDATE (transform_execution_owner, transform_execution_generation, "
        "transform_execution_lease_expires_at, transform_execution_token_sha256, "
        "transform_candidate_json) ON TABLE critical_operations TO cad2bim_runner_runtime"
    ))


def downgrade() -> None:
    table = "critical_operations"
    op.execute(sa.text(
        "DO $guard$ BEGIN "
        "IF EXISTS (SELECT 1 FROM critical_operations "
        "WHERE transform_request_identity IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Cannot discard admitted Data Transform Operations'; "
        "END IF; END $guard$"
    ))
    op.execute(sa.text(
        "REVOKE UPDATE (transform_execution_owner, transform_execution_generation, "
        "transform_execution_lease_expires_at, transform_execution_token_sha256, "
        "transform_candidate_json, status, result_package_id, error_code, "
        "error_message, error_phase, finished_at) ON TABLE critical_operations "
        "FROM cad2bim_mcp_runtime"
    ))
    op.execute(sa.text(
        "REVOKE UPDATE (transform_execution_owner, transform_execution_generation, "
        "transform_execution_lease_expires_at, transform_execution_token_sha256, "
        "transform_candidate_json) ON TABLE critical_operations FROM cad2bim_runner_runtime"
    ))
    op.drop_constraint(op.f("ck_critical_operations_transform_control_consistent"), table, type_="check")
    op.drop_index("uq_critical_operations_transform_request_identity", table_name=table)
    for column in (
        "transform_candidate_json",
        "transform_execution_token_sha256",
        "transform_execution_lease_expires_at",
        "transform_execution_generation",
        "transform_execution_owner",
        "transform_deadline_at",
        "transform_resolved_json",
        "transform_request_json",
        "transform_request_digest",
        "transform_request_identity",
    ):
        op.drop_column(table, column)
