"""Durable daily morning report snapshots and coverage.

Revision ID: a00000000030
Revises: a00000000029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000030"
down_revision = "a00000000029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import morning_report_coverage, morning_report_facts, morning_reports

    bind = op.get_bind()
    for table in (morning_reports, morning_report_coverage, morning_report_facts):
        if not sa.inspect(bind).has_table(table.name):
            table.create(bind, checkfirst=True)


def downgrade() -> None:
    # Retain immutable reports, provenance and coverage through rollback.
    return
