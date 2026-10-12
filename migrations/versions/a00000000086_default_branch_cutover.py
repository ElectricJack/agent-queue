"""Record default-branch cutover without rewriting materialized origins.

Revision ID: a00000000086
Revises: a00000000085
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000086"
down_revision = "a00000000085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("projects") and "default_branch_cutover" not in {
        column["name"] for column in inspector.get_columns("projects")
    }:
        op.add_column(
            "projects", sa.Column("default_branch_cutover", JSONB(none_as_null=True), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("projects") and "default_branch_cutover" in {
        column["name"] for column in inspector.get_columns("projects")
    }:
        op.drop_column("projects", "default_branch_cutover")
