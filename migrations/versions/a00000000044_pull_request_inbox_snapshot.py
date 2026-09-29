"""Persist the Reviews-tab GitHub snapshot across daemon restarts.

Revision ID: a00000000044
Revises: a00000000043
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000044"
down_revision = "a00000000043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("pull_request_inbox_snapshot"):
        return
    op.create_table(
        "pull_request_inbox_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_pull_request_inbox_snapshot_singleton"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("pull_request_inbox_snapshot"):
        op.drop_table("pull_request_inbox_snapshot")
