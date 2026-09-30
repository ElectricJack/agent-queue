"""Persist object-script loop state and reservations.

Revision ID: a00000000045
Revises: a00000000044
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000045"
down_revision = "a00000000044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("object_loops"):
        return
    op.create_table(
        "object_loops",
        sa.Column("object_id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("epic_task_id", sa.Text(), nullable=False),
        sa.Column("finalization_task_id", sa.Text(), nullable=False),
        sa.Column("terminal_gate_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", JSONB(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_object_loops_version"),
        sa.UniqueConstraint("epic_task_id", name="uq_object_loops_epic_task_id"),
    )
    op.create_index("idx_object_loops_project", "object_loops", ["project_id"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("object_loops"):
        op.drop_index("idx_object_loops_project", table_name="object_loops")
        op.drop_table("object_loops")
