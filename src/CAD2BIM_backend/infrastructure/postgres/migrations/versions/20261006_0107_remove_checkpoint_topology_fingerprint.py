"""Remove the duplicate topology field from exact checkpoint protocol bindings.

Revision ID: 20261006_0107
Revises: 20261006_0106

Protocol ID/digest and all Run, cursor, artifact and scope bindings remain intact.
"""

from alembic import op
import sqlalchemy as sa

revision = "20261006_0107"
down_revision = "20261006_0106"
branch_labels = None
depends_on = None


def upgrade():
    # The Component branch removes the complete artificial protocol proof.
    # An existing Component head has already retired this narrower MCP field.
    if not op.get_bind().execute(sa.text("""
        SELECT EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='checkpoint_links'
              AND column_name='topology_fingerprint')
    """)).scalar_one():
        return
    op.execute(sa.text("""
        DROP TRIGGER trg_checkpoint_links_execution_protocol_immutable ON checkpoint_links;
        CREATE OR REPLACE FUNCTION prevent_checkpoint_link_protocol_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF OLD.execution_protocol_id IS NOT NULL AND (
                NEW.execution_protocol_id IS DISTINCT FROM OLD.execution_protocol_id
                OR NEW.execution_protocol_digest IS DISTINCT FROM OLD.execution_protocol_digest
            ) THEN
                RAISE EXCEPTION 'checkpoint execution protocol fields are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $guard$;
        CREATE TRIGGER trg_checkpoint_links_execution_protocol_immutable
        BEFORE UPDATE OF execution_protocol_id, execution_protocol_digest ON checkpoint_links
        FOR EACH ROW EXECUTE FUNCTION prevent_checkpoint_link_protocol_mutation();
    """))
    op.drop_constraint(op.f("ck_checkpoint_links_execution_protocol_binding_complete"),
                       "checkpoint_links", type_="check")
    op.create_check_constraint(op.f("ck_checkpoint_links_execution_protocol_binding_complete"),
        "checkpoint_links", "(execution_protocol_id IS NULL AND execution_protocol_digest IS NULL) OR "
        "(execution_protocol_id IS NOT NULL AND execution_protocol_digest IS NOT NULL)")
    op.drop_constraint(op.f("ck_checkpoint_links_topology_fingerprint_shape"),
                       "checkpoint_links", type_="check")
    op.drop_column("checkpoint_links", "topology_fingerprint")


def downgrade():
    raise RuntimeError("Restore the pre-cleanup database snapshot; duplicate topology fields are retired")
