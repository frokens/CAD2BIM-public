"""Keep standard construction assets on the existing immutable Family version.

Revision ID: 20260909_0080
Revises: 20260907_0079
"""

from alembic import op
import sqlalchemy as sa

revision = "20260909_0080"
down_revision = "20260907_0079"
branch_labels = None
depends_on = None


def _guard(*, standard: bool) -> None:
    additional = """
        IF NEW.family_contract_json -> 'standard_model' IS NOT NULL
           AND NEW.family_contract_json -> 'standard_model' <> 'null'::jsonb THEN
            IF (SELECT count(*) FROM product_family_version_assets
                WHERE family_version_id = NEW.family_version_id
                AND asset_role IN ('standard_model_geometry', 'standard_model_manifest',
                                   'validation_report')) <> 3 THEN
                RAISE EXCEPTION 'ProductFamily standard model assets are incomplete'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF EXISTS (SELECT 1 FROM product_family_version_assets
                      WHERE family_version_id = NEW.family_version_id
                      AND asset_role IN ('standard_model_geometry', 'standard_model_manifest')) THEN
            RAISE EXCEPTION 'Standard assets require a Family standard model declaration'
                USING ERRCODE = '23514';
        END IF;
    """ if standard else ""
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_version_complete()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        DECLARE binding_count bigint; source_count bigint;
        BEGIN
            SELECT count(*) INTO binding_count FROM product_family_geometry_bindings
                WHERE family_version_id = NEW.family_version_id;
            SELECT count(*) INTO source_count FROM product_family_version_assets
                WHERE family_version_id = NEW.family_version_id
                AND asset_role = 'realization_source_bundle';
            IF binding_count < 1 OR source_count <> 1 THEN
                RAISE EXCEPTION 'ProductFamily version is incomplete: bindings=%, realization_source_bundles=%',
                    binding_count, source_count USING ERRCODE = '23514';
            END IF;
    """ + additional + "RETURN NEW; END $guard$"))


def upgrade() -> None:
    op.drop_constraint(op.f("ck_product_family_version_assets_asset_role_valid"), "product_family_version_assets", type_="check")
    op.create_check_constraint("asset_role_valid", "product_family_version_assets",
        "asset_role IN ('realization_source_bundle', 'validation_report', 'standard_model_geometry', 'standard_model_manifest')")
    _guard(standard=True)


def downgrade() -> None:
    op.execute(sa.text("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM product_family_version_assets
                   WHERE asset_role IN ('standard_model_geometry', 'standard_model_manifest'))
           OR EXISTS (SELECT 1 FROM product_family_versions
                      WHERE family_contract_json -> 'standard_model' IS NOT NULL
                      AND family_contract_json -> 'standard_model' <> 'null'::jsonb) THEN
            RAISE EXCEPTION 'Cannot downgrade while immutable standard model versions exist';
        END IF;
    END $$"""))
    _guard(standard=False)
    op.drop_constraint(op.f("ck_product_family_version_assets_asset_role_valid"), "product_family_version_assets", type_="check")
    op.create_check_constraint("asset_role_valid", "product_family_version_assets",
        "asset_role IN ('realization_source_bundle', 'validation_report')")
