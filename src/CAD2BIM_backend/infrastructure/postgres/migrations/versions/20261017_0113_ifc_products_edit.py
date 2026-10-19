"""Admit typed product edits on the existing atomic IFC/report transaction."""
from importlib import import_module
from alembic import op
import sqlalchemy as sa

revision = "20261017_0113"
down_revision = "20261017_0112"
branch_labels = None
depends_on = None
_DETAIL_NAME = "ck_critical_operations_operation_kind_detail_valid"
_TERMINAL_NAME = "ck_critical_operations_terminal_result_valid"
_EDIT_NAME = "ck_critical_operations_products_edit_detail_valid"
_PREVIOUS_DETAIL = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions.20261017_0112_ifc_trusted_baseline")._DETAIL_CHECK
_PREVIOUS_TERMINAL = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions.20261013_0108_atomic_product_apply_reports")._CHECKS[_TERMINAL_NAME]


def _with_edit(expression):
    return expression.replace("tool_name='ifc_products_apply'", "tool_name IN ('ifc_products_apply','ifc_products_edit')").replace(
        "tool_name<>'ifc_products_apply'", "tool_name NOT IN ('ifc_products_apply','ifc_products_edit')")


_DETAIL_CHECK = _with_edit(_PREVIOUS_DETAIL).replace("'ifc_trusted_baseline_load')", "'ifc_trusted_baseline_load', 'ifc_products_edit')")
_TERMINAL_CHECK = _with_edit(_PREVIOUS_TERMINAL)
_EDIT_CHECK = (
    "tool_name <> 'ifc_products_edit' OR COALESCE((operation_kind='ifc_model_write' "
    "AND operation_detail_json ->> 'detail_kind'='ifc_products_edit' "
    "AND operation_detail_json -> 'request' ->> 'base_revision_id'=base_ifc_revision_id "
    "AND operation_detail_json -> 'request' ->> 'package_id'=operation_detail_json -> 'input_binding' ->> 'package_id' "
    "AND operation_detail_json -> 'request' ->> 'payload_name'=operation_detail_json -> 'input_binding' ->> 'payload_name' "
    "AND operation_detail_json -> 'input_binding' ->> 'member_id' IS NOT NULL "
    "AND ((status IN ('prepared','indeterminate') AND operation_detail_json -> 'result' IS NULL AND result_package_id IS NULL) "
    "OR (status='succeeded' AND result_package_id IS NOT NULL AND operation_detail_json -> 'result' IS NOT NULL "
    "AND (operation_detail_json -> 'result' ->> 'committed_count')::bigint=(operation_detail_json -> 'result' ->> 'input_count')::bigint) "
    "OR (status='failed' AND ((result_package_id IS NULL AND operation_detail_json -> 'result' IS NULL) "
    "OR (result_package_id IS NOT NULL AND (operation_detail_json -> 'result' ->> 'committed_count')::bigint=0 "
    "AND (operation_detail_json -> 'result' ->> 'created_product_count')::bigint=0 "
    "AND (operation_detail_json -> 'result' ->> 'deleted_product_count')::bigint=0))))), false)"
)


def _replace(name, expression):
    op.drop_constraint(op.f(name), "critical_operations", type_="check")
    op.create_check_constraint(op.f(name), "critical_operations", expression)


def upgrade():
    _replace(_DETAIL_NAME, _DETAIL_CHECK)
    _replace(_TERMINAL_NAME, _TERMINAL_CHECK)
    op.create_check_constraint(op.f(_EDIT_NAME), "critical_operations", _EDIT_CHECK)


def downgrade():
    op.execute(sa.text("DO $guard$ BEGIN IF EXISTS (SELECT 1 FROM critical_operations WHERE tool_name='ifc_products_edit') "
        "THEN RAISE EXCEPTION 'Cannot discard product Edit Operations'; END IF; END $guard$"))
    op.drop_constraint(op.f(_EDIT_NAME), "critical_operations", type_="check")
    _replace(_TERMINAL_NAME, _PREVIOUS_TERMINAL)
    _replace(_DETAIL_NAME, _PREVIOUS_DETAIL)
