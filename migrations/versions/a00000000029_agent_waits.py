"""Add durable agent waits and server message sequence.

Revision ID: a00000000029
Revises: a00000000028

Conditional additions also work after the live-metadata squashed baseline.
Operator upgrade only; rollback retains pending waits and their result history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000029"
down_revision = "a00000000028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import agent_waits, messages

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "created_seq" not in {column["name"] for column in inspector.get_columns("messages")}:
        op.add_column(
            "messages", sa.Column("created_seq", sa.BigInteger(), sa.Identity(), nullable=False)
        )
    if "idx_messages_thread_sequence" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("messages")
    }:
        next(i for i in messages.indexes if i.name == "idx_messages_thread_sequence").create(bind)
    if not sa.inspect(bind).has_table(agent_waits.name):
        agent_waits.create(bind, checkfirst=True)


def downgrade() -> None:
    # Never silently forget exemptions or results. Cancel active waits through
    # the command boundary and restore lease baselines before rolling back code.
    return
