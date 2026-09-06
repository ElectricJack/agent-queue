"""normalized integration cleanup

Revision ID: 18cd4540cd0d
Revises: f0a1b2c3d4e5
Create Date: 2026-09-06 01:05:38.398283

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "18cd4540cd0d"
down_revision: str | Sequence[str] | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_IMMUTABLE = (
    "batch_id", "kind", "identity", "domain_key", "project_id", "repository_id",
    "repository_numeric_id", "repository_full_name", "revision", "member_ordinal",
    "receipt_id", "target_ref", "target_pr_number", "target_pr_url", "workspace_path",
    "expected_sha", "created_at",
)


def _create_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE FUNCTION integration_cleanup_item_guard()
            RETURNS trigger AS $$ BEGIN
            IF """ + " OR ".join(
                f"NEW.{name} IS DISTINCT FROM OLD.{name}" for name in _IMMUTABLE
            ) + """ THEN
              RAISE EXCEPTION 'integration cleanup identity is immutable'; END IF;
            IF OLD.state IN ('complete', 'conflict', 'failed') AND NEW IS DISTINCT FROM OLD THEN
              RAISE EXCEPTION 'terminal integration cleanup item is immutable'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            "CREATE TRIGGER trg_integration_cleanup_item_guard BEFORE UPDATE ON "
            "integration_cleanup_items FOR EACH ROW EXECUTE FUNCTION "
            "integration_cleanup_item_guard()"
        )
        return
    op.execute(
        "CREATE TRIGGER trg_integration_cleanup_item_guard BEFORE UPDATE ON "
        "integration_cleanup_items WHEN "
        + " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in _IMMUTABLE)
        + " OR OLD.state IN ('complete', 'conflict', 'failed') "
        "BEGIN SELECT RAISE(ABORT, 'integration cleanup identity is immutable'); END"
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "integration_cleanup_items",
        sa.Column("batch_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("identity", sa.Text(), primary_key=True),
        sa.Column("domain_key", sa.Text(), nullable=False, unique=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("repository_numeric_id", sa.Integer(), nullable=False),
        sa.Column("repository_full_name", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("member_ordinal", sa.Integer(), nullable=True),
        sa.Column("receipt_id", sa.Text(), nullable=True),
        sa.Column("target_ref", sa.Text(), nullable=True),
        sa.Column("target_pr_number", sa.Integer(), nullable=True),
        sa.Column("target_pr_url", sa.Text(), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=True),
        sa.Column("expected_sha", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("execution_nonce", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.Float(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("terminal_at", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('source_pr', 'audit_pr', 'remote_ref', 'local_ref', 'worktree')",
            name="ck_integration_cleanup_items_kind",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'retryable', 'complete', 'conflict', 'failed')",
            name="ck_integration_cleanup_items_state",
        ),
        sa.CheckConstraint(
            "revision >= 0 AND attempts >= 0 AND repository_numeric_id > 0",
            name="ck_integration_cleanup_items_numbers",
        ),
        sa.CheckConstraint(
            "length(expected_sha) = 40 AND expected_sha = lower(expected_sha)",
            name="ck_integration_cleanup_items_expected_sha",
        ),
        sa.CheckConstraint(
            "(execution_nonce IS NULL AND claim_expires_at IS NULL) OR "
            "(execution_nonce IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_integration_cleanup_items_claim",
        ),
        sa.CheckConstraint(
            "((state IN ('complete', 'conflict', 'failed')) AND terminal_at IS NOT NULL "
            "AND execution_nonce IS NULL AND claim_expires_at IS NULL) OR "
            "((state IN ('pending', 'retryable')) AND terminal_at IS NULL)",
            name="ck_integration_cleanup_items_terminal",
        ),
        sa.CheckConstraint(
            "(kind = 'source_pr' AND member_ordinal IS NOT NULL AND member_ordinal >= 0 "
            "AND receipt_id IS NOT NULL AND target_pr_number IS NOT NULL "
            "AND target_pr_number > 0 AND target_pr_url IS NOT NULL "
            "AND target_ref IS NULL AND workspace_path IS NULL) OR "
            "(kind = 'audit_pr' AND member_ordinal IS NULL AND receipt_id IS NULL "
            "AND target_pr_number IS NOT NULL AND target_pr_number > 0 "
            "AND target_pr_url IS NOT NULL AND target_ref IS NULL "
            "AND workspace_path IS NULL) OR "
            "(kind IN ('remote_ref', 'local_ref') AND member_ordinal IS NULL "
            "AND receipt_id IS NULL AND target_pr_number IS NULL AND target_pr_url IS NULL "
            "AND target_ref IS NOT NULL AND workspace_path IS NULL) OR "
            "(kind = 'worktree' AND member_ordinal IS NULL AND receipt_id IS NULL "
            "AND target_pr_number IS NULL AND target_pr_url IS NULL AND target_ref IS NULL "
            "AND workspace_path IS NOT NULL)",
            name="ck_integration_cleanup_items_target",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["integration_batches.id"],
            name="fk_integration_cleanup_items_batch", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_integration_cleanup_items_project", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["repos.id"],
            name="fk_integration_cleanup_items_repository", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["task_delivery_receipts.id"],
            name="fk_integration_cleanup_items_receipt", ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_integration_cleanup_items_due",
        "integration_cleanup_items",
        ["next_attempt_at", "batch_id", "domain_key"],
        sqlite_where=sa.text("state IN ('pending', 'retryable')"),
        postgresql_where=sa.text("state IN ('pending', 'retryable')"),
    )
    _create_guards()


def downgrade() -> None:
    """Downgrade schema."""
    item = op.get_bind().execute(
        sa.text(
            "SELECT batch_id || ':' || kind || ':' || identity "
            "FROM integration_cleanup_items ORDER BY batch_id, kind, identity LIMIT 1"
        )
    ).scalar_one_or_none()
    if item is not None:
        raise RuntimeError(f"drain integration cleanup item {item} before downgrade")
    batch = op.get_bind().execute(
        sa.text(
            "SELECT id FROM integration_batches WHERE lifecycle = 'promoted' "
            "AND cleanup_state <> 'complete' ORDER BY id LIMIT 1"
        )
    ).scalar_one_or_none()
    if batch is not None:
        raise RuntimeError(
            f"complete terminal integration cleanup for batch {batch} before downgrade"
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_integration_cleanup_item_guard ON "
            "integration_cleanup_items"
        )
        op.execute("DROP FUNCTION IF EXISTS integration_cleanup_item_guard()")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_integration_cleanup_item_guard")
    op.drop_index("idx_integration_cleanup_items_due", table_name="integration_cleanup_items")
    op.drop_table("integration_cleanup_items")
