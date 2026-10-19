"""Require the current Family contract on new writes, preserving historical rows.

Revision ID: 20260909_0083
Revises: 20260909_0082
"""

from alembic import op
import sqlalchemy as sa


revision = "20260909_0083"
down_revision = "20260909_0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CHECK constraints remain historical storage shapes. INSERT guards add the
    # current admission boundary without validating or rewriting immutable rows.
    # Domain validation remains the authority for the complete contract structure.
    op.execute(sa.text("""
        CREATE FUNCTION cad2bim_guard_product_family_current_version()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.family_contract_json ->> 'schema'
                    IS DISTINCT FROM 'product_family_contract_v2'
                OR NEW.abi_profile IS DISTINCT FROM 'product_realization_v2' THEN
                RAISE EXCEPTION 'New ProductFamily versions require the current contract and ABI'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $guard$
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_product_family_current_version_guard
        BEFORE INSERT ON product_family_versions FOR EACH ROW
        EXECUTE FUNCTION cad2bim_guard_product_family_current_version()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION cad2bim_guard_product_family_current_operation()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.operation_kind = 'product_family_write' AND (
                NEW.operation_detail_json #>> '{version_draft,family_contract,schema}'
                    IS DISTINCT FROM 'product_family_contract_v2'
                OR NEW.operation_detail_json #>> '{version_draft,abi_profile}'
                    IS DISTINCT FROM 'product_realization_v2'
            ) THEN
                RAISE EXCEPTION 'New ProductFamily Operations require the current contract and ABI'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $guard$
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_product_family_current_operation_guard
        BEFORE INSERT ON critical_operations FOR EACH ROW
        EXECUTE FUNCTION cad2bim_guard_product_family_current_operation()
    """))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP TRIGGER trg_product_family_current_operation_guard ON critical_operations"
    ))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_product_family_current_operation()"))
    op.execute(sa.text(
        "DROP TRIGGER trg_product_family_current_version_guard ON product_family_versions"
    ))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_product_family_current_version()"))
