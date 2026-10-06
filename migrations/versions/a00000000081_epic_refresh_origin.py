"""Record the refreshed parent base used to start a cross-epic dependent.

Revision ID: a00000000081
Revises: a00000000080
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000081"
down_revision = "a00000000080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("task_branch_origins") and "base_refresh" not in {
        column["name"] for column in inspector.get_columns("task_branch_origins")
    }:
        op.add_column("task_branch_origins", sa.Column("base_refresh", JSONB, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("task_branch_origins") and "base_refresh" in {
        column["name"] for column in inspector.get_columns("task_branch_origins")
    }:
        op.drop_column("task_branch_origins", "base_refresh")
