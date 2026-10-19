"""Switch current Family admission to v5 while preserving immutable old facts.

Revision ID: 20261001_0104
Revises: 20261001_0103
"""

from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision = "20261001_0104"
down_revision = "20261001_0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extend only the historical storage constraints. Online admission below
    # accepts one current ABI, and no saved object is relabelled or rewritten.
    historical = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions.20260909_0082_family_self_geometry_contract")
    system = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions.20260929_0097_system_family_publication")
    drafts = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions.20260930_0100_authoring_drafts")
    publication = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions.20261001_0101_independent_authoring_publication")
    contract = historical._contract_shape(self_geometry=True)
    marker = "'product_family_contract_v2')"
    if contract.count(marker) != 1:
        raise RuntimeError("Historical Family contract check marker changed")
    contract = contract.replace(marker,
        "'product_family_contract_v2', 'product_family_contract_v3', 'product_family_contract_v4', 'product_family_contract_v5')", 1)
    # Reuse the complete current expression assembled at 0101. Rebuilding only
    # the historical 0088 clause would remove current drafts, inspections and
    # independent publication from this shared Operation constraint.
    detail = system._detail_check(system_copy=True)
    clause = ("(operation_detail_json #>> '{version_draft,abi_profile}' = "
              "'product_realization_v4' AND (operation_detail_json #>> "
              "'{version_draft,family_contract,schema}' = 'product_family_contract_v4') IS TRUE)")
    if detail.count(clause) != 1:
        raise RuntimeError("Historical Family operation check marker changed")
    detail = detail.replace(clause, clause + " OR " + clause.replace("_v4", "_v5"), 1)
    detail = (f"({detail}) OR operation_kind='geometry_inspection' OR "
        f"COALESCE(({drafts._DRAFT_DETAIL}),FALSE) OR COALESCE(({publication._PUBLISH_DETAIL}),FALSE)")
    version_check = "ck_product_family_versions_contract_shape"
    op.drop_constraint(op.f(version_check), "product_family_versions", type_="check")
    op.create_check_constraint(op.f(version_check), "product_family_versions", contract)
    operation_check = "ck_critical_operations_operation_kind_detail_valid"
    op.drop_constraint(op.f(operation_check), "critical_operations", type_="check")
    # Keep 0101's historical-row preservation; INSERT/UPDATE still enforce the
    # full expression, and all current admission/ownership guards remain active.
    op.execute(sa.text(f"ALTER TABLE critical_operations ADD CONSTRAINT {operation_check} CHECK ({detail}) NOT VALID"))
    for name in (
        "cad2bim_guard_product_family_current_version",
        "cad2bim_guard_product_family_current_operation",
        "cad2bim_guard_product_family_authoring_operation",
        "cad2bim_guard_geometry_query_attempt",
    ):
        op.execute(sa.text(f"""
            DO $family_v5_guard$
            DECLARE definition text;
            BEGIN
                SELECT pg_get_functiondef('{name}()'::regprocedure) INTO definition;
                IF position('product_family_contract_v4' IN definition)=0
                   OR position('product_realization_v4' IN definition)=0 THEN
                    RAISE EXCEPTION 'Expected current Family admission guard was not found';
                END IF;
                EXECUTE replace(replace(definition,
                    'product_family_contract_v4', 'product_family_contract_v5'),
                    'product_realization_v4', 'product_realization_v5');
            END
            $family_v5_guard$
        """))


def downgrade() -> None:
    raise RuntimeError("Family v5 assets require the current ABI; restore the pre-cutover database backup")
