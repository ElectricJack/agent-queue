"""Record every abandoned branch before its ref is deleted.

Revision ID: a00000000088
Revises: a00000000087
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000088"
down_revision = "a00000000087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("branch_deletion_audit"):
        return
    op.create_table(
        "branch_deletion_audit",
        sa.Column("id", sa.Text(), primary_key=True),
        # No foreign key to tasks: an abandoned task can be deleted outright and
        # the record of what its branch was must outlive it.
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("detail_reason", sa.Text(), nullable=True),
        sa.Column("backup_path", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Float(), nullable=True),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.Column("deleted_at", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('obsolete_close', 'cancel', 'fail_close', 'supersede', 'abort', 'delete')",
            name="ck_branch_deletion_audit_reason",
        ),
        sa.CheckConstraint(
            "outcome IN ('pending', 'deleted', 'held', 'failed', 'moved')",
            name="ck_branch_deletion_audit_outcome",
        ),
        sa.CheckConstraint(
            "length(head_sha) = 40 AND head_sha = lower(head_sha)",
            name="ck_branch_deletion_audit_head_sha",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_branch_deletion_audit_attempts",
        ),
    )
    op.create_index(
        "ix_branch_deletion_audit_branch",
        "branch_deletion_audit",
        ["project_id", "branch"],
    )
    op.create_index(
        "ix_branch_deletion_audit_due",
        "branch_deletion_audit",
        ["next_attempt_at"],
        postgresql_where=sa.text("outcome = 'pending'"),
    )


def downgrade() -> None:
    # The audit trail is the only record of an abandoned branch's tip; keep it.
    return
