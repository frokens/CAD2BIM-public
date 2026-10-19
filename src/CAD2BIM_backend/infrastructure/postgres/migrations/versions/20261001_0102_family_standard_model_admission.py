"""Allow current Family standard inputs through the existing formal guards.

Revision ID: 20261001_0102
Revises: 20261001_0101
"""

from importlib import import_module


revision = "20261001_0102"
down_revision = "20261001_0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    prior = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260920_0087_ephemeral_authoring_validation"
    )
    obsolete_standard_checks = (
        (
            "cad2bim_guard_product_family_current_version",
            prior._VERSION_ADMISSION,
            "NEW.family_contract_json -> 'standard_model' IS DISTINCT FROM '{}'::jsonb\n"
            "               OR ",
        ),
        (
            "cad2bim_guard_product_family_current_operation",
            prior._OPERATION_ADMISSION,
            "                OR NEW.operation_detail_json #> "
            "'{version_draft,family_contract,standard_model}' IS DISTINCT FROM '{}'::jsonb\n",
        ),
    )
    for name, admission, obsolete in obsolete_standard_checks:
        if admission.count(obsolete) != 1:
            raise RuntimeError(f"Expected one obsolete standard-model check in {name}")
        prior._replace_function(name, admission, admission.replace(obsolete, "", 1))


def downgrade() -> None:
    raise RuntimeError(
        "formal Family versions may now have standard inputs; restore the pre-cutover database backup"
    )
