"""Allow attempt-specific ProductFamily repair draft identity.

Revision ID: 20260905_0076
Revises: 20260905_0075
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260905_0076"
down_revision: str | None = "20260905_0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_guard(*, ignore_attempt_identity: bool) -> None:
    draft_comparison = (
        """\
                      AND ((parent.operation_detail_json -> 'version_draft')
                           - 'family_version_id' - 'created_at') =
                          ((NEW.operation_detail_json -> 'version_draft')
                           - 'family_version_id' - 'created_at')"""
        if ignore_attempt_identity
        else """\
                      AND parent.operation_detail_json -> 'version_draft' =
                          NEW.operation_detail_json -> 'version_draft'"""
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_authoring_operation()
            RETURNS trigger LANGUAGE plpgsql AS $guard$
            BEGIN
                IF NEW.operation_kind <> 'product_family_write' THEN
                    RETURN NEW;
                END IF;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.status <> 'prepared' THEN
                        RAISE EXCEPTION 'ProductFamily Operation must be inserted prepared'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF NOT (
                        (OLD.status = 'prepared' AND
                         NEW.status IN ('succeeded', 'failed', 'indeterminate')) OR
                        (OLD.status = 'indeterminate' AND NEW.status = 'prepared')
                    ) THEN
                        RAISE EXCEPTION 'ProductFamily Operation terminal state is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    IF ROW(
                        NEW.operation_id, NEW.owner_id, NEW.tenant_id, NEW.task_id,
                        NEW.run_id, NEW.operation_kind, NEW.initiator, NEW.tool_name,
                        NEW.intent_summary, NEW.idempotency_key, NEW.request_fingerprint,
                        NEW.code_artifact_id, NEW.code_artifact_version_id,
                        NEW.code_artifact_sha256, NEW.repair_parent_operation_id,
                        NEW.target_product_family_id,
                        NEW.base_product_family_version_id,
                        NEW.operation_detail_json, NEW.started_at, NEW.created_at
                    ) IS DISTINCT FROM ROW(
                        OLD.operation_id, OLD.owner_id, OLD.tenant_id, OLD.task_id,
                        OLD.run_id, OLD.operation_kind, OLD.initiator, OLD.tool_name,
                        OLD.intent_summary, OLD.idempotency_key, OLD.request_fingerprint,
                        OLD.code_artifact_id, OLD.code_artifact_version_id,
                        OLD.code_artifact_sha256, OLD.repair_parent_operation_id,
                        OLD.target_product_family_id,
                        OLD.base_product_family_version_id,
                        OLD.operation_detail_json, OLD.started_at, OLD.created_at
                    ) THEN
                        RAISE EXCEPTION 'ProductFamily Operation identity is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM artifact_versions AS source_version
                    JOIN artifacts AS source_artifact
                      ON source_artifact.artifact_id = source_version.artifact_id
                     AND source_artifact.owner_id = source_version.owner_id
                     AND source_artifact.task_id = source_version.task_id
                    WHERE source_version.owner_id = NEW.owner_id
                      AND source_version.tenant_id = NEW.tenant_id
                      AND source_version.task_id = NEW.task_id
                      AND source_version.artifact_id = NEW.code_artifact_id
                      AND source_version.version_id = NEW.code_artifact_version_id
                      AND source_version.sha256 = NEW.code_artifact_sha256
                      AND source_artifact.kind = 'product_family_authoring_source'
                      AND source_artifact.status IN ('ready', 'available')
                ) THEN
                    RAISE EXCEPTION 'ProductFamily exact source ArtifactVersion is invalid'
                        USING ERRCODE = '23503';
                END IF;
                IF NEW.repair_parent_operation_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM critical_operations AS parent
                    WHERE parent.operation_id = NEW.repair_parent_operation_id
                      AND parent.owner_id = NEW.owner_id
                      AND parent.tenant_id = NEW.tenant_id
                      AND parent.task_id = NEW.task_id
                      AND parent.run_id = NEW.run_id
                      AND parent.operation_kind = 'product_family_write'
                      AND parent.status = 'failed'
                      AND parent.error_phase IN (
                          'code_validation', 'execution', 'output_validation'
                      )
                      AND parent.target_product_family_id = NEW.target_product_family_id
                      AND parent.base_product_family_version_id IS NOT DISTINCT FROM
                          NEW.base_product_family_version_id
                      AND parent.operation_detail_json -> 'version_draft' IS NOT NULL
                      AND NEW.operation_detail_json -> 'version_draft' IS NOT NULL
{draft_comparison}
                      AND parent.code_artifact_id IS NOT NULL
                      AND parent.code_artifact_version_id IS NOT NULL
                      AND parent.code_artifact_sha256 IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'ProductFamily repair parent is not an exact failed chain parent'
                        USING ERRCODE = '23503';
                END IF;
                RETURN NEW;
            END
            $guard$
            """
        )
    )


def upgrade() -> None:
    _install_guard(ignore_attempt_identity=True)


def downgrade() -> None:
    _install_guard(ignore_attempt_identity=False)
