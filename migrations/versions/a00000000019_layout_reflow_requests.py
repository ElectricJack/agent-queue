"""Add durable deferred active-layout reflow requests.

Revision ID: a00000000019
Revises: a00000000018

The squashed baseline builds this table from current metadata.  Inspect first
so upgrading either a fresh baseline or an existing operator database is
idempotent.  Downgrade intentionally preserves requests: old code simply
does not read the separate ledger, while deleting it would discard durable
evidence and work that a later upgrade can safely resume.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000019"
down_revision = "a00000000018"
branch_labels = None
depends_on = None

_TABLE = "layout_reflow_requests"


def upgrade() -> None:
    from src.database.tables import layout_reflow_requests

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        layout_reflow_requests.create(bind, checkfirst=True)
        return
    indexes = {item["name"] for item in inspector.get_indexes(_TABLE)}
    for index in layout_reflow_requests.indexes:
        if index.name not in indexes:
            index.create(bind, checkfirst=True)


def downgrade() -> None:
    # Preserve queued work across a code rollback.  The next upgrade reuses
    # this inspector-guarded table rather than silently losing reflow evidence.
    return
