"""Persist explicit branch retirement and pre-delete SHA audit.

Revision ID: a00000000089
Revises: a00000000088
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000089"
down_revision = "a00000000088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("branch_retirements"):
        return
    op.create_table(
        "branch_retirements",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("claim_epoch", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.Float(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("evidence", JSONB(), nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "state IN ('pending', 'complete', 'conflict', 'withdrawn')",
            name="ck_branch_retirements_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_branch_retirements_attempts"),
        sa.UniqueConstraint("request_id", "repository_id", "branch"),
    )
    op.create_index(
        "ix_branch_retirements_due", "branch_retirements", ["state", "next_attempt_at"]
    )


def downgrade() -> None:
    # Keep the deletion history and restorable-bundle identities on rollback.
    return
