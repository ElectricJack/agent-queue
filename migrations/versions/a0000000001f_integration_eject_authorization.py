"""Require the locked, audited eject transaction for sealed-batch edits.

Revision ID: a0000000001f
Revises: a0000000001e

The preceding guard also let source_manifest_digest/base_sha edits through when
the eject setting was absent: SQL NOT(NULL) is NULL, and PL/pgSQL IF treats that
as false. The authorization helper always returns a non-null boolean.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a0000000001f"
down_revision = "a0000000001e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text("""
        CREATE FUNCTION integration_eject_authorized(
            p_batch_id text, p_task_id text DEFAULT NULL
        ) RETURNS boolean AS $$
            SELECT EXISTS (
                SELECT 1
                FROM integration_batches AS b
                JOIN events AS e
                  ON e.id = NULLIF(current_setting('aq.integration_eject_event', true), '')::integer
                WHERE b.id = p_batch_id
                  AND b.lifecycle = 'sealed'
                  AND current_setting('aq.integration_eject_batch', true) = p_batch_id
                  AND e.event_type = 'integration.batch_ejected'
                  AND e.project_id = b.project_id
                  AND (p_task_id IS NULL OR e.task_id = p_task_id)
                  AND e.payload::jsonb ->> 'batch_id' = p_batch_id
                  AND NULLIF(e.payload::jsonb ->> 'reason', '') IS NOT NULL
                  AND NULLIF(e.payload::jsonb ->> 'operator_id', '') IS NOT NULL
                  AND e.xmin = pg_current_xact_id()::xid
                  AND NOT EXISTS (
                      SELECT 1 FROM integration_candidate_revisions AS r
                      WHERE r.batch_id = p_batch_id
                  )
                  AND EXISTS (
                      SELECT 1 FROM pg_locks AS l
                      WHERE l.pid = pg_backend_pid()
                        AND l.locktype = 'advisory'
                        AND l.mode = 'ExclusiveLock'
                        AND l.granted
                        -- HIERARCHY_LOCK_NAMESPACE in hierarchy_queries.py.
                        AND l.classid = 1095845961::oid
                        AND l.objid = hashtext(b.project_id)::oid
                        AND l.objsubid = 2
                  )
            );
        $$ LANGUAGE sql
    """)
    )
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION integration_member_is_mutable() RETURNS trigger AS $$
        DECLARE
            member_batch_id text;
            ejected_task_id text;
        BEGIN
            member_batch_id := CASE WHEN TG_OP = 'INSERT' THEN NEW.batch_id ELSE OLD.batch_id END;
            IF TG_OP = 'DELETE' THEN ejected_task_id := OLD.task_id; END IF;
            IF TG_OP <> 'INSERT' AND
                integration_eject_authorized(member_batch_id, ejected_task_id) THEN
                IF TG_OP = 'UPDATE' AND (
                    NEW.batch_id IS DISTINCT FROM OLD.batch_id OR
                    NEW.task_id IS DISTINCT FROM OLD.task_id OR
                    NEW.pr_url IS DISTINCT FROM OLD.pr_url OR
                    NEW.repository_id IS DISTINCT FROM OLD.repository_id OR
                    NEW.source_base_sha IS DISTINCT FROM OLD.source_base_sha OR
                    NEW.reviewed_head_sha IS DISTINCT FROM OLD.reviewed_head_sha OR
                    NEW.reviewed_tree_sha IS DISTINCT FROM OLD.reviewed_tree_sha OR
                    NEW.source_ref IS DISTINCT FROM OLD.source_ref OR
                    NEW.source_ref_retention IS DISTINCT FROM OLD.source_ref_retention OR
                    NEW.review_evidence_id IS DISTINCT FROM OLD.review_evidence_id OR
                    NEW.review_evidence::text IS DISTINCT FROM OLD.review_evidence::text
                ) THEN
                    RAISE EXCEPTION 'sealed integration batch membership is immutable';
                END IF;
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP <> 'INSERT' AND NOT EXISTS (
                SELECT 1 FROM integration_batches WHERE id = OLD.batch_id AND lifecycle = 'sealing'
            ) THEN
                RAISE EXCEPTION 'sealed integration batch membership is immutable';
            END IF;
            IF TG_OP <> 'DELETE' AND NOT EXISTS (
                SELECT 1 FROM integration_batches WHERE id = NEW.batch_id AND lifecycle = 'sealing'
            ) THEN
                RAISE EXCEPTION 'sealed integration batch membership is immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    )
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION integration_batch_identity_is_immutable() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF (OLD.lifecycle <> 'sealing' OR NEW.lifecycle <> 'sealing') AND (
                    NEW.project_id IS DISTINCT FROM OLD.project_id OR
                    NEW.repository_id IS DISTINCT FROM OLD.repository_id OR
                    NEW.request_id IS DISTINCT FROM OLD.request_id OR
                    NEW.trigger IS DISTINCT FROM OLD.trigger OR
                    NEW.integration_branch IS DISTINCT FROM OLD.integration_branch OR
                    NEW.policy_snapshot::text IS DISTINCT FROM OLD.policy_snapshot::text OR
                    NEW.artifact_snapshot::text IS DISTINCT FROM OLD.artifact_snapshot::text OR
                    NEW.created_at IS DISTINCT FROM OLD.created_at OR
                    (
                        NEW.source_manifest_digest IS DISTINCT FROM OLD.source_manifest_digest OR
                        NEW.base_sha IS DISTINCT FROM OLD.base_sha
                    ) AND NOT integration_eject_authorized(OLD.id)
                ) THEN
                    RAISE EXCEPTION 'sealed integration batch identity is immutable';
                END IF;
                IF OLD.lifecycle <> 'sealing' AND NEW.lifecycle = 'sealing' THEN
                    RAISE EXCEPTION 'integration batch cannot return to sealing';
                END IF;
            END IF;
            IF NEW.lifecycle = 'empty' AND (
                EXISTS (SELECT 1 FROM integration_batch_members WHERE batch_id = NEW.id) OR
                EXISTS (SELECT 1 FROM integration_repair_operations WHERE batch_id = NEW.id) OR
                EXISTS (SELECT 1 FROM project_integration_leases WHERE batch_id = NEW.id)
            ) THEN
                RAISE EXCEPTION 'empty integration batch cannot retain members, repair operations, or leases';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    )


def downgrade() -> None:
    # Restore the exact pre-authorization definitions from their source revision.
    from migrations.versions.a0000000001d_integration_eject_guards import upgrade as restore_guards

    restore_guards()
    op.execute(sa.text("DROP FUNCTION integration_eject_authorized(text, text)"))
