"""Shared outbound message delivery ledger.

Revision ID: a00000000031
Revises: a00000000030
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000031"
down_revision = "a00000000030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import outbound_deliveries

    bind = op.get_bind()
    if not sa.inspect(bind).has_table(outbound_deliveries.name):
        outbound_deliveries.create(bind, checkfirst=True)


def downgrade() -> None:
    # Preserve delivery receipts and unknown outcomes across rollbacks.
    return
