"""Bind Data derivation to its exact IFC read source on the existing Operation."""

from importlib import import_module

from alembic import op
import sqlalchemy as sa


revision = "20261017_0111"
down_revision = "20261014_0110"
branch_labels = None
depends_on = None

_DETAIL_NAME = "ck_critical_operations_operation_kind_detail_valid"
_CONTROL_NAME = "ck_critical_operations_transform_control_consistent"
_ERROR_PHASE_NAME = "ck_critical_operations_error_phase_valid"
_PREVIOUS_ERROR_PHASE = import_module(
    "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
    "20260904_0074_product_family_authoring"
)._error_phase_check(product_family=True) + (
    " OR (status='failed' AND operation_kind IN ('geometry_inspection','authoring_publish') "
    "AND error_phase IN ('code_validation','execution','output_validation','storage_verification','commit_fence'))"
)
_ERROR_PHASE_CHECK = _PREVIOUS_ERROR_PHASE.replace(
    "operation_kind = 'data_transform' AND error_phase IN ('code_validation', 'execution', 'output_validation')",
    "operation_kind = 'data_transform' AND error_phase IN ('code_validation', 'execution', 'output_validation', 'commit_fence')",
)
_PREVIOUS_DETAIL = import_module(
    "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
    "20261013_0108_atomic_product_apply_reports"
)._CHECKS[_DETAIL_NAME]
_DETAIL_CHECK = _PREVIOUS_DETAIL.replace(
    "('data_transform', 'data_repair_transform', 'cad_compute_measurements')",
    "('data_transform', 'data_repair_transform', 'data_derive', 'cad_compute_measurements')",
).replace("(tool_name = 'data_transform' AND", "(tool_name IN ('data_transform', 'data_derive') AND")
_PREVIOUS_CONTROL = (
    "((transform_request_identity IS NULL AND transform_request_digest IS NULL "
    "AND transform_request_json IS NULL AND transform_resolved_json IS NULL "
    "AND transform_deadline_at IS NULL AND transform_execution_owner IS NULL "
    "AND transform_execution_generation = 0 "
    "AND transform_execution_lease_expires_at IS NULL "
    "AND transform_execution_token_sha256 IS NULL AND transform_candidate_json IS NULL) "
    "OR (tool_name IN ('data_transform', 'data_repair_transform') "
    "AND transform_request_identity IS NOT NULL "
    "AND transform_request_digest IS NOT NULL "
    "AND length(transform_request_digest) = 64 "
    "AND transform_request_json IS NOT NULL AND transform_resolved_json IS NOT NULL "
    "AND transform_deadline_at IS NOT NULL AND transform_execution_generation >= 0 "
    "AND ((transform_execution_owner IS NULL "
    "AND transform_execution_lease_expires_at IS NULL "
    "AND transform_execution_token_sha256 IS NULL) "
    "OR (transform_execution_owner IS NOT NULL "
    "AND transform_execution_generation > 0 "
    "AND transform_execution_lease_expires_at IS NOT NULL "
    "AND transform_execution_token_sha256 IS NOT NULL "
    "AND length(transform_execution_token_sha256) = 64))))"
)
_CONTROL_CHECK = _PREVIOUS_CONTROL.replace(
    "('data_transform', 'data_repair_transform')",
    "('data_transform', 'data_repair_transform', 'data_derive')",
)
_SOURCE_CHECK = (
    "(source_ifc_model_id IS NULL AND source_ifc_revision_id IS NULL) OR "
    "(source_ifc_model_id IS NOT NULL AND source_ifc_revision_id IS NOT NULL "
    "AND operation_kind = 'data_transform' AND tool_name = 'data_derive' "
    "AND COALESCE(operation_detail_json ->> 'detail_kind', '') = 'data_transform' "
    "AND COALESCE(operation_detail_json ->> 'engine', '') = 'python_arrow')"
)


def _replace_check(name, expression):
    op.drop_constraint(op.f(name), "critical_operations", type_="check")
    op.create_check_constraint(op.f(name), "critical_operations", expression)


def upgrade() -> None:
    op.add_column("critical_operations", sa.Column("source_ifc_model_id", sa.String(80)))
    op.add_column("critical_operations", sa.Column("source_ifc_revision_id", sa.String(80)))
    op.create_foreign_key(
        "fk_critical_operations_ifc_read_source", "critical_operations", "ifc_model_revisions",
        ["owner_id", "task_id", "run_id", "source_ifc_model_id", "source_ifc_revision_id"],
        ["owner_id", "task_id", "run_id", "ifc_model_id", "revision_id"], ondelete="RESTRICT",
    )
    op.create_check_constraint(op.f("ck_critical_operations_ifc_read_source_consistent"),
        "critical_operations", _SOURCE_CHECK)
    _replace_check(_DETAIL_NAME, _DETAIL_CHECK)
    _replace_check(_CONTROL_NAME, _CONTROL_CHECK)
    _replace_check(_ERROR_PHASE_NAME, _ERROR_PHASE_CHECK)


def downgrade() -> None:
    op.execute(sa.text(
        "DO $guard$ BEGIN IF EXISTS (SELECT 1 FROM critical_operations "
        "WHERE tool_name = 'data_derive' OR source_ifc_model_id IS NOT NULL "
        "OR source_ifc_revision_id IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Cannot discard Data derivation Operations or their IFC read sources'; "
        "END IF; END $guard$"
    ))
    _replace_check(_CONTROL_NAME, _PREVIOUS_CONTROL)
    _replace_check(_ERROR_PHASE_NAME, _PREVIOUS_ERROR_PHASE)
    _replace_check(_DETAIL_NAME, _PREVIOUS_DETAIL)
    op.drop_constraint(op.f("ck_critical_operations_ifc_read_source_consistent"), "critical_operations", type_="check")
    op.drop_constraint("fk_critical_operations_ifc_read_source", "critical_operations", type_="foreignkey")
    op.drop_column("critical_operations", "source_ifc_revision_id")
    op.drop_column("critical_operations", "source_ifc_model_id")
