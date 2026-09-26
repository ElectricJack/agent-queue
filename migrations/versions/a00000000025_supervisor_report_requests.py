"""Add durable supervisor report author requests.

Revision ID: a00000000025
Revises: a00000000024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000025"
down_revision = "a00000000024"
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
