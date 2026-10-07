"""Store promotion flows separately from frozen integration policy snapshots.

Revision ID: a00000000085
Revises: a00000000084
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000085"
down_revision = "a00000000084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("projects") and "promotion_flow" not in {
        column["name"] for column in inspector.get_columns("projects")
    }:
        op.add_column(
            "projects", sa.Column("promotion_flow", JSONB(none_as_null=True), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("projects") and "promotion_flow" in {
        column["name"] for column in inspector.get_columns("projects")
    }:
        op.drop_column("projects", "promotion_flow")
