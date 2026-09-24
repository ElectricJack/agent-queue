"""Permit a safe operator retry of a failed integration cleanup item.

Revision ID: a00000000021
Revises: a00000000020

The baseline guard treats failed items as immutable, although retry-cleanup
selects them. Keep complete and conflict terminal, and admit only the exact
failed-to-retryable reset with no outstanding execution or irreversible marker.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a00000000021"
down_revision = "a00000000020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION integration_cleanup_item_guard()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.batch_id IS DISTINCT FROM OLD.batch_id OR
               NEW.kind IS DISTINCT FROM OLD.kind OR
               NEW.identity IS DISTINCT FROM OLD.identity OR
               NEW.domain_key IS DISTINCT FROM OLD.domain_key OR
               NEW.project_id IS DISTINCT FROM OLD.project_id OR
               NEW.repository_id IS DISTINCT FROM OLD.repository_id OR
               NEW.repository_numeric_id IS DISTINCT FROM OLD.repository_numeric_id OR
               NEW.repository_full_name IS DISTINCT FROM OLD.repository_full_name OR
               NEW.revision IS DISTINCT FROM OLD.revision OR
               NEW.member_ordinal IS DISTINCT FROM OLD.member_ordinal OR
               NEW.receipt_id IS DISTINCT FROM OLD.receipt_id OR
               NEW.target_ref IS DISTINCT FROM OLD.target_ref OR
               NEW.target_pr_number IS DISTINCT FROM OLD.target_pr_number OR
               NEW.target_pr_url IS DISTINCT FROM OLD.target_pr_url OR
               NEW.workspace_path IS DISTINCT FROM OLD.workspace_path OR
               NEW.expected_sha IS DISTINCT FROM OLD.expected_sha OR
               NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'integration cleanup identity is immutable';
            END IF;
            IF OLD.state IN ('complete', 'conflict') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'terminal integration cleanup item is immutable';
            END IF;
            IF OLD.state = 'failed' AND NEW IS DISTINCT FROM OLD AND NOT (
                NEW.state = 'retryable' AND
                NEW.attempts = 0 AND
                NEW.terminal_at IS NULL AND
                OLD.execution_nonce IS NULL AND
                OLD.claim_expires_at IS NULL AND
                OLD.irreversible_nonce IS NULL AND
                OLD.irreversible_prewrite_at IS NULL AND
                NEW.execution_nonce IS NULL AND
                NEW.claim_expires_at IS NULL AND
                NEW.irreversible_nonce IS NULL AND
                NEW.irreversible_prewrite_at IS NULL AND
                NEW.last_error IS NOT DISTINCT FROM OLD.last_error
            ) THEN
                RAISE EXCEPTION 'terminal integration cleanup item is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))


def downgrade() -> None:
    from migrations.integration_guards import FUNCTIONS

    original = next(
        statement for statement in FUNCTIONS
        if "FUNCTION integration_cleanup_item_guard()" in statement
    )
    op.execute(sa.text(original))
