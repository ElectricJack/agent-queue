"""Persist declared and evaluated task deliverables.

Revision ID: 0a1c2d3e4f50
Revises: b3f2c0de0003
"""

from alembic import op
import sqlalchemy as sa


revision = "0a1c2d3e4f50"
down_revision = "b3f2c0de0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tasks",
        sa.Column("deliverables", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "task_completion_records",
        sa.Column("deliverables", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade():
    op.drop_column("task_completion_records", "deliverables")
    op.drop_column("tasks", "deliverables")
