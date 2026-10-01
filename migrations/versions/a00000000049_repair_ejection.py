"""Permit audited repair ejection while retaining the old candidate manifest.

Revision ID: a00000000049
Revises: a00000000048
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000049"
down_revision = "a00000000048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "integration_candidate_revisions"
    if "source_manifest" not in {col["name"] for col in inspector.get_columns(table)}:
        op.add_column(table, sa.Column("source_manifest", JSONB(), nullable=True))
    op.execute(
        sa.text("""
        UPDATE integration_candidate_revisions AS r
        SET source_manifest = COALESCE((
            SELECT jsonb_agg(to_jsonb(m) ORDER BY m.ordinal)
            FROM integration_batch_members AS m WHERE m.batch_id = r.batch_id
        ), '[]'::jsonb) WHERE r.source_manifest IS NULL
    """)
    )
    if "fk_integration_candidate_member_results_member" in {
        fk["name"] for fk in inspector.get_foreign_keys("integration_candidate_member_results")
    }:
        op.drop_constraint(
            "fk_integration_candidate_member_results_member",
            "integration_candidate_member_results",
            type_="foreignkey",
        )
    # Old results retain their revision's frozen manifest, not the current
    # member ordinals. New construction still requires a real current member.
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION integration_result_has_current_member() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM integration_batch_members m
                JOIN integration_batches b ON b.id = m.batch_id
                JOIN integration_candidate_revisions r
                  ON r.batch_id = b.id AND r.revision = NEW.revision
                WHERE m.batch_id = NEW.batch_id AND m.ordinal = NEW.member_ordinal
                  AND r.state <> 'superseded'
            ) THEN
                RAISE EXCEPTION 'candidate result requires a current batch member';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS integration_result_current_member "
            "ON integration_candidate_member_results"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER integration_result_current_member BEFORE INSERT OR UPDATE "
            "ON integration_candidate_member_results FOR EACH ROW "
            "EXECUTE FUNCTION integration_result_has_current_member()"
        )
    )
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION integration_revision_manifest_is_immutable() RETURNS trigger AS $$
        BEGIN
            IF OLD.source_manifest IS NOT NULL AND
                NEW.source_manifest IS DISTINCT FROM OLD.source_manifest THEN
                RAISE EXCEPTION 'candidate revision source manifest is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS integration_revision_manifest_immutable "
            "ON integration_candidate_revisions"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER integration_revision_manifest_immutable BEFORE UPDATE "
            "ON integration_candidate_revisions FOR EACH ROW "
            "EXECUTE FUNCTION integration_revision_manifest_is_immutable()"
        )
    )
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION integration_eject_authorized(
            p_batch_id text, p_task_id text DEFAULT NULL
        ) RETURNS boolean AS $$
            SELECT EXISTS (
                SELECT 1 FROM integration_batches b
                JOIN events e ON e.id =
                    NULLIF(current_setting('aq.integration_eject_event', true), '')::integer
                WHERE b.id = p_batch_id
                  AND b.lifecycle IN ('sealed', 'repairing', 'human_blocked')
                  AND current_setting('aq.integration_eject_batch', true) = p_batch_id
                  AND e.event_type = 'integration.batch_ejected'
                  AND e.project_id = b.project_id
                  AND (p_task_id IS NULL OR e.task_id = p_task_id)
                  AND e.payload::jsonb ->> 'batch_id' = p_batch_id
                  AND NULLIF(e.payload::jsonb ->> 'reason', '') IS NOT NULL
                  AND NULLIF(e.payload::jsonb ->> 'operator_id', '') IS NOT NULL
                  AND e.xmin = pg_current_xact_id()::xid
                  AND NOT EXISTS (
                      SELECT 1 FROM integration_candidate_revisions r
                      WHERE r.batch_id = p_batch_id AND (
                          r.state <> 'superseded' OR r.source_manifest IS NULL
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM integration_promotion_intents i
                      WHERE i.root_batch_id = p_batch_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM integration_branch_owners o
                      WHERE o.repository_id = b.repository_id AND o.ref = b.integration_branch
                        AND (o.handoff_state NOT IN ('reserved', 'released')
                             OR o.session_id IS NOT NULL OR o.workspace_id IS NOT NULL)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM integration_candidate_ref_mutations m
                      WHERE m.batch_id = p_batch_id AND m.state = 'reserved'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM integration_candidate_resolutions r
                      WHERE r.batch_id = p_batch_id AND r.state IN ('reserved', 'pushed')
                  )
                  AND EXISTS (
                      SELECT 1 FROM pg_locks l WHERE l.pid = pg_backend_pid()
                        AND l.locktype = 'advisory' AND l.mode = 'ExclusiveLock' AND l.granted
                        AND l.classid = 1095845961::oid
                        AND l.objid = hashtext(b.project_id)::oid AND l.objsubid = 2
                  )
            );
        $$ LANGUAGE sql
    """)
    )


def downgrade() -> None:
    # Ejected sources cannot be reconstructed into the live member table;
    # restoring its FK would destroy the retained construction evidence.
    raise NotImplementedError("repair ejection retains versioned membership history")
