"""Retire the product-apply candidate without changing retained IFC facts.

Revision ID: 20260920_0088
Revises: 20260920_0087
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa


revision = "20260920_0088"
down_revision = "20260920_0087"
branch_labels = None
depends_on = None

_CHECK = "ck_critical_operations_operation_kind_detail_valid"


def _operation_detail_check(*, products: bool) -> str:
    previous = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260909_0082_family_self_geometry_contract"
    )
    detail = previous._operation_detail_check(self_geometry=True)
    # Reproduce the immutable 0086 extension; 0087 did not change this check.
    clause = (
        "(operation_detail_json #>> '{version_draft,abi_profile}' = "
        "'product_realization_v2' AND (operation_detail_json #>> "
        "'{version_draft,family_contract,schema}' = 'product_family_contract_v2') IS TRUE)"
    )
    if detail.count(clause) != 1:
        raise RuntimeError("Immutable 0082 Family ABI clause must occur exactly once")
    detail = detail.replace(clause, clause + " OR " + clause.replace("_v2", "_v3"), 1)
    tools = "'ifc_project_update', 'ifc_spatial_structure_apply', 'ifc_products_apply'"
    if detail.count(tools) != 1:
        raise RuntimeError("Immutable product-apply tool clause must occur exactly once")
    return detail if products else detail.replace(
        tools, "'ifc_project_update', 'ifc_spatial_structure_apply'", 1
    )


def _replace_check(*, products: bool) -> None:
    op.drop_constraint(op.f(_CHECK), "critical_operations", type_="check")
    op.create_check_constraint(
        op.f(_CHECK), "critical_operations", _operation_detail_check(products=products)
    )


def upgrade() -> None:
    # Keep the precondition and validated replacement atomic against new writes.
    op.execute(sa.text("LOCK TABLE critical_operations IN ACCESS EXCLUSIVE MODE"))
    op.execute(sa.text("""
        DO $retire_products$
        BEGIN
            IF EXISTS (SELECT 1 FROM critical_operations
                       WHERE tool_name = 'ifc_products_apply') THEN
                RAISE EXCEPTION 'Cannot retire product apply while retained Operations exist'
                    USING ERRCODE = '23514';
            END IF;
        END
        $retire_products$
    """))
    _replace_check(products=False)


def downgrade() -> None:
    # Restoring physical admission does not restore the retired application.
    _replace_check(products=True)
