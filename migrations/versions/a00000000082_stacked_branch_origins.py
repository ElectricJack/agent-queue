"""Record exact stacked prerequisite bases on task branch origins.

Revision ID: a00000000082
Revises: a00000000081
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000082"
down_revision = "a00000000081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("task_branch_origins") and "stack_snapshot" not in {
        column["name"] for column in inspector.get_columns("task_branch_origins")
    }:
        op.add_column("task_branch_origins", sa.Column("stack_snapshot", JSONB(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("task_branch_origins") and "stack_snapshot" in {
        column["name"] for column in inspector.get_columns("task_branch_origins")
    }:
        op.drop_column("task_branch_origins", "stack_snapshot")
