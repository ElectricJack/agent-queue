"""Add ``provider_availability`` and its transition log (provider-failover D7).

Revision ID: a00000000012
Revises: a00000000011

One row per provider key (the harness login) holding the derived state, the
operator override and the bounded evidence ring the reducer counts over, plus
an append-only log of effective state changes.

**The creates are conditional**, for the same reason ``a00000000010``'s is:
the squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after these tables
were declared already has them, and an unconditional ``create_table`` would
fail on every fresh database.

Chained onto ``a00000000011`` (``integration_delegate_release``), which
landed on main while this branch was in flight.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000012"
down_revision = "a00000000011"
branch_labels = None
depends_on = None

_TABLES = ("provider_availability", "provider_availability_transitions")


def upgrade() -> None:
    from src.database.tables import provider_availability, provider_availability_transitions

    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in (provider_availability, provider_availability_transitions):
        if table.name not in existing:
            table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            op.drop_table(name)
