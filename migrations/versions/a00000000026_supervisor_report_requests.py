"""Add durable supervisor report author requests.

Revision ID: a00000000026
Revises: compact_handoff_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000026"
down_revision = "compact_handoff_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import supervisor_report_requests

    bind = op.get_bind()
    if not sa.inspect(bind).has_table(supervisor_report_requests.name):
        supervisor_report_requests.create(bind, checkfirst=True)


def downgrade() -> None:
    # A rollback must not discard reserved requests or authored submissions.
    return
