"""Add operator conversations with the global supervisor (Discord mention routing §4.1).

Revision ID: a00000000027
Revises: a00000000026

Four tables: ``supervisor_conversations`` (one per operator @mention),
``conversation_inputs`` (one per accepted message; its unique external id is
the 90-day dedup tombstone), ``conversation_backfill_cursors`` (reconnect
backfill position per channel or bound thread) and
``conversation_intake_gaps`` (history the backfill could not read).

**The creates are conditional**, for the same reason ``a00000000012``'s are:
the squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after these tables
were declared already has them, and an unconditional ``create_table`` would
fail on every fresh database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000027"
down_revision = "a00000000026"
branch_labels = None
depends_on = None

# Creation order: ``conversation_inputs`` references ``supervisor_conversations``.
_TABLES = (
    "supervisor_conversations",
    "conversation_inputs",
    "conversation_backfill_cursors",
    "conversation_intake_gaps",
)


def upgrade() -> None:
    from src.database.tables import metadata

    bind = op.get_bind()
    for name in _TABLES:
        if not sa.inspect(bind).has_table(name):
            metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_TABLES):
        if sa.inspect(bind).has_table(name):
            op.drop_table(name)
