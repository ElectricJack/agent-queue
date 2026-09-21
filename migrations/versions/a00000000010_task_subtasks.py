"""Add ``task_subtasks`` — durable, non-schedulable checklist rows.

Revision ID: a00000000010
Revises: a0000000000f

A subtask is a checklist item a single agent ticks off inside one task — it
is never scheduled, claimed or assigned on its own, unlike a child task in
the hierarchy. It has no FK to ``tasks.id`` so it survives archive exactly
like ``task_comments`` does.

**The create is conditional**, for the same reason ``a0000000000e``'s is: the
squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after this table was
declared already has it, and an unconditional ``create_table`` would fail on
every fresh database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000010"
down_revision = "a0000000000f"
branch_labels = None
depends_on = None

_TABLE = "task_subtasks"


def upgrade() -> None:
    from src.database.tables import task_subtasks

    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        task_subtasks.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
