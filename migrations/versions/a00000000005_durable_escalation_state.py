"""Add transport-neutral escalation and digest persistence.

Revision ID: a00000000005
Revises: a00000000004

The four relations implement the durable incident identity/conversation CAS,
immutable verified message history, external-delivery lease/receipt state, and
digest window/cursor state from the Discord simplification implementation
specification §4.

As with revisions a00000000003 and a00000000004, creation is conditional.  A
fresh database is built by the squashed baseline from live SQLAlchemy metadata
and therefore already contains these tables by the time this revision runs;
an existing database stamped at a00000000004 needs them created here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000005"
down_revision = "a00000000004"
branch_labels = None
depends_on = None

_TABLES = (
    "escalations",
    "escalation_messages",
    "escalation_deliveries",
    "digest_windows",
)


def upgrade() -> None:
    from src.database.tables import metadata

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    for name in _TABLES:
        if name not in present:
            metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    for name in reversed(_TABLES):
        if name in present:
            op.drop_table(name)
