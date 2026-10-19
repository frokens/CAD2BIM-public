"""Ordered user content and original Run controls; remove artificial graph proofs.

Revision ID: 20261002_0097
Revises: 20260928_0096
"""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261002_0097"
down_revision = "20260928_0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feedback_commands", sa.Column("content", JSONB()))
    # One bounded conversion preserves the exact text of existing inbox rows.
    # No old body parser remains in the online contract.
    op.execute(sa.text("UPDATE feedback_commands SET content = jsonb_build_array(jsonb_build_object('type', 'text', 'text', body))"))
    op.alter_column("feedback_commands", "content", nullable=False)
    op.drop_column("feedback_commands", "body")
    op.add_column("runs", sa.Column("initial_feedback", JSONB()))
    op.add_column("runs", sa.Column("pause_request_id", sa.String(255)))
    op.add_column("runs", sa.Column("continue_request_id", sa.String(255)))

    op.execute(sa.text("DROP TRIGGER trg_runs_pinned_execution_protocol_immutable ON runs"))
    op.execute(sa.text("DROP FUNCTION prevent_pinned_execution_protocol_mutation()"))
    op.execute(sa.text("DROP TRIGGER trg_checkpoint_links_execution_protocol_immutable ON checkpoint_links"))
    op.execute(sa.text("DROP FUNCTION prevent_checkpoint_link_protocol_mutation()"))
    # 0016 supplied complete names without op.f, so the naming convention
    # prefixed them a second time. 0059 replaced only the mapping constraint.
    for name in ("ck_runs_execution_protocol_mapping_valid",
                 "ck_runs_ck_runs_execution_protocol_digest_shape",
                 "ck_runs_ck_runs_execution_protocol_identity_consistent"):
        op.drop_constraint(op.f(name), "runs", type_="check")
    for name in ("execution_protocol_binding_complete", "execution_protocol_digest_shape"):
        op.drop_constraint(op.f(f"ck_checkpoint_links_{name}"), "checkpoint_links", type_="check")
    # The MCP branch already removed this duplicate topology field in 0107.
    # Both historical heads converge on the same absent-column schema.
    op.execute(sa.text("ALTER TABLE checkpoint_links DROP CONSTRAINT IF EXISTS ck_checkpoint_links_topology_fingerprint_shape"))
    for table in ("runs", "checkpoint_links"):
        op.drop_column(table, "execution_protocol_id")
        op.drop_column(table, "execution_protocol_digest")
    op.execute(sa.text("ALTER TABLE checkpoint_links DROP COLUMN IF EXISTS topology_fingerprint"))
    op.create_check_constraint(op.f("ck_runs_execution_protocol_mapping_valid"), "runs",
        "(execution_mode = 'graph' AND ((execution_protocol_status = 'pinned' "
        "AND execution_protocol IS NOT NULL AND execution_protocol_quarantine_reason IS NULL) "
        "OR (execution_protocol_status = 'quarantined' AND execution_protocol IS NULL "
        "AND execution_protocol_quarantine_reason IS NOT NULL))) OR "
        "(execution_mode = 'direct_mcp' AND execution_protocol_status = 'not_applicable' "
        "AND execution_protocol IS NULL AND execution_protocol_quarantine_reason IS NULL)")
    # Existing protocol/manifest JSON is deliberately not relabelled or rewritten.
    # Old records must be recreated under the current actual route contract.
    op.execute(sa.text("""
        CREATE FUNCTION prevent_pinned_execution_protocol_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF OLD.execution_protocol_status = 'pinned' AND (
                NEW.execution_protocol_status IS DISTINCT FROM OLD.execution_protocol_status OR
                NEW.execution_protocol IS DISTINCT FROM OLD.execution_protocol OR
                NEW.execution_protocol_quarantine_reason IS DISTINCT FROM OLD.execution_protocol_quarantine_reason)
            THEN RAISE EXCEPTION 'selected graph route is immutable' USING ERRCODE = '23514'; END IF;
            IF OLD.initial_feedback IS NOT NULL AND NEW.initial_feedback IS DISTINCT FROM OLD.initial_feedback
            THEN RAISE EXCEPTION 'initial user input identity is immutable' USING ERRCODE = '23514'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_runs_pinned_execution_protocol_immutable
        BEFORE UPDATE OF execution_protocol_status, execution_protocol, execution_protocol_quarantine_reason, initial_feedback
        ON runs FOR EACH ROW EXECUTE FUNCTION prevent_pinned_execution_protocol_mutation();
    """))
    for role in ("cad2bim_api_runtime", "cad2bim_runner_runtime", "cad2bim_mcp_runtime"):
        op.execute(sa.text(f"GRANT UPDATE (initial_feedback, pause_request_id, continue_request_id) ON runs TO {role}"))

    schema = "public" if context.is_offline_mode() else op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
    quoted = '"' + schema.replace('"', '""') + '"'
    op.execute(sa.text(f"""
        CREATE FUNCTION {quoted}.cad2bim_observed_run_identity(p_run_id text, p_thread_id text)
        RETURNS TABLE(tenant_id text, principal_id text, task_id text)
        LANGUAGE sql SECURITY DEFINER SET search_path = {quoted}, pg_temp AS $$
            SELECT r.tenant_id::text, b.principal_id::text, r.task_id::text
            FROM runs r JOIN task_tenant_bindings b ON b.tenant_id = r.tenant_id AND b.task_id = r.task_id
            WHERE r.run_id = p_run_id AND r.thread_id = p_thread_id AND r.execution_mode = 'graph'
        $$;
        REVOKE ALL ON FUNCTION {quoted}.cad2bim_observed_run_identity(text,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION {quoted}.cad2bim_observed_run_identity(text,text) TO cad2bim_runner_runtime;
    """))


def downgrade() -> None:
    raise RuntimeError("ordered input and current graph route must not restore the retired contracts")
