"""Append-only authored task comments; legacy notes remain untouched.

Revision ID: 7ac492b83fd1
Revises: f81a93bd0264
"""

from alembic import op
import sqlalchemy as sa

revision = "7ac492b83fd1"
down_revision = "f81a93bd0264"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_comments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "author_kind IN ('user','agent','supervisor')", name="ck_task_comment_author_kind"
        ),
        sa.CheckConstraint("length(body) BETWEEN 1 AND 16000", name="ck_task_comment_body_length"),
    )
    op.create_index(
        "idx_task_comments_task_created", "task_comments", ["task_id", "created_at", "id"]
    )


def downgrade():
    op.drop_table("task_comments")
