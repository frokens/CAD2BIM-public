"""Grant Runner navigation access through the existing scope-bound index.

Revision ID: 20261002_0108
Revises: 20261002_0107
"""

from alembic import op
import sqlalchemy as sa

revision = "20261002_0108"
down_revision = "20261002_0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retain each original Tenant/Principal/Task predicate and immutable binding.
    for policy in ("opaque_reference_index_read", "opaque_reference_index_insert"):
        op.execute(sa.text(
            f"ALTER POLICY {policy} ON opaque_reference_index "
            "TO cad2bim_mcp_runtime, cad2bim_runner_runtime"
        ))
    op.execute(sa.text(
        "GRANT SELECT, INSERT ON TABLE opaque_reference_index TO cad2bim_runner_runtime"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "REVOKE SELECT, INSERT ON TABLE opaque_reference_index FROM cad2bim_runner_runtime"
    ))
    for policy in ("opaque_reference_index_read", "opaque_reference_index_insert"):
        op.execute(sa.text(
            f"ALTER POLICY {policy} ON opaque_reference_index TO cad2bim_mcp_runtime"
        ))
