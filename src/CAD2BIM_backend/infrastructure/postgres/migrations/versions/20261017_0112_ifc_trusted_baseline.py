"""Admit internal trusted baseline writes on the existing IFC Operation kernel."""

from importlib import import_module

from alembic import op
import sqlalchemy as sa


revision = "20261017_0112"
down_revision = "20261017_0111"
branch_labels = None
depends_on = None

_DETAIL_NAME = "ck_critical_operations_operation_kind_detail_valid"
_BASELINE_NAME = "ck_critical_operations_ifc_trusted_baseline_detail_valid"
_PREVIOUS_DETAIL = import_module(
    "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
    "20261017_0111_data_derive_ifc_source"
)._DETAIL_CHECK
_DETAIL_CHECK = _PREVIOUS_DETAIL.replace(
    "'ifc_spatial_structure_apply', 'ifc_products_apply')",
    "'ifc_spatial_structure_apply', 'ifc_products_apply', 'ifc_trusted_baseline_load')",
)
_BASELINE_CHECK = (
    "tool_name <> 'ifc_trusted_baseline_load' OR COALESCE("
    "(operation_kind = 'ifc_model_write' "
    "AND operation_detail_json ->> 'detail_kind' = 'ifc_trusted_baseline_load' "
    "AND jsonb_typeof(operation_detail_json -> 'source_asset') = 'object' "
    "AND length(operation_detail_json -> 'source_asset' ->> 'artifact_id') > 0 "
    "AND length(operation_detail_json -> 'source_asset' ->> 'version_id') > 0 "
    "AND (operation_detail_json -> 'source_asset' ->> 'sha256') ~ '^[0-9a-f]{64}$' "
    "AND ((status = 'succeeded' AND operation_detail_json ->> 'result' = 'baseline_loaded') "
    "OR (status <> 'succeeded' AND NOT (operation_detail_json ? 'result')))), false)"
)


def _replace_detail(expression: str) -> None:
    op.drop_constraint(op.f(_DETAIL_NAME), "critical_operations", type_="check")
    op.create_check_constraint(op.f(_DETAIL_NAME), "critical_operations", expression)


def upgrade() -> None:
    _replace_detail(_DETAIL_CHECK)
    op.create_check_constraint(op.f(_BASELINE_NAME), "critical_operations", _BASELINE_CHECK)


def downgrade() -> None:
    op.execute(sa.text(
        "DO $guard$ BEGIN IF EXISTS (SELECT 1 FROM critical_operations "
        "WHERE tool_name = 'ifc_trusted_baseline_load') THEN "
        "RAISE EXCEPTION 'Cannot discard trusted baseline Operations'; "
        "END IF; END $guard$"
    ))
    op.drop_constraint(op.f(_BASELINE_NAME), "critical_operations", type_="check")
    _replace_detail(_PREVIOUS_DETAIL)
