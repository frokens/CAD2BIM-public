"""Admit finite Family Geometry choices while retaining immutable older rows.

Revision ID: 20260927_0090
Revises: 20260924_0089
"""

from importlib import import_module

import sqlalchemy as sa
from alembic import op


revision = "20260927_0090"
down_revision = "20260924_0089"
branch_labels = None
depends_on = None


def _replace_checks(*, binding_options: bool) -> None:
    self_geometry = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260909_0082_family_self_geometry_contract"
    )
    retired_products = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260920_0088_retire_ifc_products_apply"
    )
    contract = self_geometry._contract_shape(self_geometry=True)
    marker = "'product_family_contract_v2')"
    if contract.count(marker) != 1:
        raise RuntimeError("Immutable Family contract check marker changed")
    versions = "'product_family_contract_v2', 'product_family_contract_v3'"
    if binding_options:
        versions += ", 'product_family_contract_v4'"
    contract = contract.replace(marker, versions + ")", 1)

    detail = retired_products._operation_detail_check(products=False)
    clause = (
        "(operation_detail_json #>> '{version_draft,abi_profile}' = "
        "'product_realization_v3' AND (operation_detail_json #>> "
        "'{version_draft,family_contract,schema}' = 'product_family_contract_v3') IS TRUE)"
    )
    if detail.count(clause) != 1:
        raise RuntimeError("Immutable Family operation check marker changed")
    if binding_options:
        detail = detail.replace(clause, clause + " OR " + clause.replace("_v3", "_v4"), 1)

    for table, name, check in (
        ("product_family_versions", "ck_product_family_versions_contract_shape", contract),
        ("critical_operations", "ck_critical_operations_operation_kind_detail_valid", detail),
    ):
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, check)


def _replace_current_guard_markers(*, binding_options: bool) -> None:
    old, new = ("v3", "v4") if binding_options else ("v4", "v3")
    for name in (
        "cad2bim_guard_product_family_current_version",
        "cad2bim_guard_product_family_current_operation",
        "cad2bim_guard_product_family_authoring_operation",
    ):
        op.execute(sa.text(f"""
            DO $family_choice_guard$
            DECLARE definition text;
            BEGIN
                SELECT pg_get_functiondef('{name}()'::regprocedure) INTO definition;
                IF position('product_family_contract_{old}' IN definition) = 0
                   OR position('product_realization_{old}' IN definition) = 0 THEN
                    RAISE EXCEPTION 'Expected current Family admission guard was not found'
                        USING ERRCODE = '23514';
                END IF;
                EXECUTE replace(replace(definition,
                    'product_family_contract_{old}', 'product_family_contract_{new}'),
                    'product_realization_{old}', 'product_realization_{new}');
            END
            $family_choice_guard$
        """))


def upgrade() -> None:
    _replace_checks(binding_options=True)
    _replace_current_guard_markers(binding_options=True)


def downgrade() -> None:
    op.execute(sa.text("""
        DO $family_choice_guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM product_family_versions
                       WHERE abi_profile = 'product_realization_v4'
                          OR family_contract_json ->> 'schema' = 'product_family_contract_v4')
               OR EXISTS (SELECT 1 FROM critical_operations
                          WHERE operation_kind = 'product_family_write'
                            AND (operation_detail_json #>> '{version_draft,abi_profile}' = 'product_realization_v4'
                              OR operation_detail_json #>> '{version_draft,family_contract,schema}' = 'product_family_contract_v4')) THEN
                RAISE EXCEPTION 'Cannot downgrade retained binding-option Family facts'
                    USING ERRCODE = '23514';
            END IF;
        END
        $family_choice_guard$
    """))
    _replace_current_guard_markers(binding_options=False)
    _replace_checks(binding_options=False)
