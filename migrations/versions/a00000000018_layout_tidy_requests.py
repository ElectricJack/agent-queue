"""Add durable, one-pair-at-a-time all-project layout Tidy requests.

Revision ID: a00000000018
Revises: a00000000017

``layout_jobs`` is the per-project worker queue.  A fleet Tidy must not fill
that queue with every project at once, so the request and pair tables retain
the remaining work and release only one ordinary Tidy job at a time.  The
squashed baseline already creates both tables from current metadata; inspect
first so an operator upgrade is idempotent on either kind of database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000018"
down_revision = "a00000000017"
branch_labels = None
depends_on = None

_TABLES = ("layout_tidy_requests", "layout_tidy_request_pairs")


def upgrade() -> None:
    from src.database.tables import layout_tidy_request_pairs, layout_tidy_requests

    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "layout_tidy_requests" not in existing:
        layout_tidy_requests.create(bind, checkfirst=True)
    if "layout_tidy_request_pairs" not in existing:
        layout_tidy_request_pairs.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in reversed(_TABLES):
        if table in existing:
            op.drop_table(table)
