"""Allow a new publication request after an earlier request has failed.

Revision ID: 20261001_0103
Revises: 20261001_0102
"""

from alembic import op
import sqlalchemy as sa


revision = "20261001_0103"
down_revision = "20261001_0102"
branch_labels = None
depends_on = None


_INDEX = "uq_critical_operations_run_tool_fingerprint"
_OTHER_KINDS = (
    "operation_kind IN ('source_ingestion', 'data_transform', "
    "'geometry_resource_write', 'product_family_write')"
)
_PREVIOUS_KINDS = (
    "operation_kind IN ('source_ingestion', 'data_transform', "
    "'geometry_resource_write', 'product_family_write', 'authoring_publish')"
)


def _replace_index(predicate: str) -> None:
    op.drop_index(_INDEX, table_name="critical_operations")
    op.create_index(
        _INDEX,
        "critical_operations",
        ["run_id", "tool_name", "request_fingerprint"],
        unique=True,
        postgresql_where=sa.text(predicate),
    )


def upgrade() -> None:
    _replace_index(_OTHER_KINDS)


def downgrade() -> None:
    duplicate = op.get_bind().scalar(sa.text("""
        SELECT 1 FROM critical_operations
        WHERE operation_kind IN ('source_ingestion', 'data_transform',
                                 'geometry_resource_write', 'product_family_write',
                                 'authoring_publish')
        GROUP BY run_id, tool_name, request_fingerprint
        HAVING count(*) > 1
        LIMIT 1
    """))
    if duplicate is not None:
        raise RuntimeError("0102 fingerprint index cannot admit existing publication retries")
    _replace_index(_PREVIOUS_KINDS)
