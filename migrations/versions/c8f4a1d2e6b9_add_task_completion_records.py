"""add durable task completion records

Revision ID: c8f4a1d2e6b9
Revises: 882b77dc8495
Create Date: 2026-08-30

Completion records intentionally keep a logical ``task_id`` rather than a
foreign key. Archiving removes the active ``tasks`` row, but the account of
how that task was completed must remain available if the task is restored.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8f4a1d2e6b9"
down_revision: Union[str, Sequence[str], None] = "882b77dc8495"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_completion_records",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("work_outcome", sa.Text(), nullable=True),
        sa.Column("failure_class", sa.Text(), nullable=True),
        sa.Column("changes", sa.Text(), nullable=False, server_default=""),
        sa.Column("verification", sa.Text(), nullable=False, server_default=""),
        sa.Column("tests", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("commands", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("commits", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("completed_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_task_completion_records_task_time",
        "task_completion_records",
        ["task_id", "completed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_task_completion_records_task_time",
        table_name="task_completion_records",
    )
    op.drop_table("task_completion_records")
