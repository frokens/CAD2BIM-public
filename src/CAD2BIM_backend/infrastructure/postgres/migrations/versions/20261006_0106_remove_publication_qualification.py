"""Remove publication qualification flags; retain actual contracts and assets.

Revision ID: 20261006_0106
Revises: 20261001_0105

No saved resource, Operation or asset content is rewritten.
"""

from importlib import import_module

from alembic import op

revision = "20261006_0106"
down_revision = "20261001_0105"
branch_labels = None
depends_on = None


def upgrade():
    guards = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20261001_0105_authoring_contract_cleanup"
    )
    guards._remove_function_clauses("cad2bim_guard_product_family_current_version", [
        (r"\s+OR NEW\.validation_summary_json IS DISTINCT FROM '\{\"valid\": true\}'::jsonb", "", 1),
        ("usage instructions and successful construction", "usage instructions", 1),
    ])
    guards._remove_function_clauses("cad2bim_guard_geometry_formal_version", [
        (r"NEW\.validation_summary_json IS DISTINCT FROM '\{\"valid\": true\}'::jsonb\s+OR ", "", 1),
    ])
    guards._remove_function_clauses("cad2bim_guard_geometry_authoring_operation", [
        (r"\s+AND result_version\.validation_summary_json = '\{\"valid\": true\}'::jsonb", "", 1),
    ])
    op.drop_constraint(op.f("ck_product_family_versions_validation_summary_object"),
                       "product_family_versions", type_="check")
    op.drop_column("product_family_versions", "validation_summary_json")
    op.drop_column("geometry_resource_versions", "validation_summary_json")


def downgrade():
    raise RuntimeError("Restore the pre-cleanup database snapshot; publication qualification flags are retired")
