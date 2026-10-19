"""Admit numeric Family execution while preserving all historical immutable rows.

Revision ID: 20260920_0086
Revises: 20260913_0085
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa


revision = "20260920_0086"
down_revision = "20260913_0085"
branch_labels = None
depends_on = None


def _historical_checks(*, numeric: bool) -> None:
    previous = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260909_0082_family_self_geometry_contract"
    )
    contract = previous._contract_shape(self_geometry=True)
    detail = previous._operation_detail_check(self_geometry=True)
    if numeric:
        contract = contract.replace(
            "'product_family_contract_v2')", "'product_family_contract_v2', 'product_family_contract_v3')",
        )
        clause = (
            "(operation_detail_json #>> '{version_draft,abi_profile}' = "
            "'product_realization_v2' AND (operation_detail_json #>> "
            "'{version_draft,family_contract,schema}' = 'product_family_contract_v2') IS TRUE)"
        )
        if detail.count(clause) != 1:
            raise RuntimeError("Immutable 0082 Family ABI clause must occur exactly once")
        detail = detail.replace(clause, clause + " OR " + clause.replace("_v2", "_v3"), 1)
    for table, name, check in (
        ("product_family_versions", "ck_product_family_versions_contract_shape", contract),
        ("critical_operations", "ck_critical_operations_operation_kind_detail_valid", detail),
    ):
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, check)


def _current_guards(*, numeric: bool) -> None:
    # Patch only the two ABI literals in the existing 0085 guards, retaining its
    # independently owned usage-instruction and current INSERT requirements.
    old, new = ("v2", "v3") if numeric else ("v3", "v2")
    for name in ("cad2bim_guard_product_family_current_version", "cad2bim_guard_product_family_current_operation"):
        op.execute(sa.text(f"""
            DO $numeric_guard$
            DECLARE definition text;
            BEGIN
                SELECT pg_get_functiondef('{name}()'::regprocedure) INTO definition;
                IF position('product_family_contract_{old}' IN definition) = 0
                   OR position('product_realization_{old}' IN definition) = 0
                   OR position('usage_instructions' IN definition) = 0 THEN
                    RAISE EXCEPTION 'Expected current Family admission guard was not found'
                        USING ERRCODE = '23514';
                END IF;
                EXECUTE replace(replace(definition,
                    'product_family_contract_{old}', 'product_family_contract_{new}'),
                    'product_realization_{old}', 'product_realization_{new}');
            END
            $numeric_guard$
        """))


def upgrade() -> None:
    _historical_checks(numeric=True)
    _current_guards(numeric=True)


def downgrade() -> None:
    op.execute(sa.text("""
        DO $numeric_guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM product_family_versions
                       WHERE abi_profile = 'product_realization_v3'
                          OR family_contract_json ->> 'schema' = 'product_family_contract_v3')
               OR EXISTS (SELECT 1 FROM critical_operations
                          WHERE operation_kind = 'product_family_write'
                            AND (operation_detail_json #>> '{version_draft,abi_profile}' = 'product_realization_v3'
                              OR operation_detail_json #>> '{version_draft,family_contract,schema}' = 'product_family_contract_v3')) THEN
                RAISE EXCEPTION 'Cannot downgrade retained numeric Family facts'
                    USING ERRCODE = '23514';
            END IF;
        END
        $numeric_guard$
    """))
    _current_guards(numeric=False)
    _historical_checks(numeric=False)
