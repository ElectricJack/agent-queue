"""Add revisioned durable dashboard state documents.

Revision ID: a0000000000e
Revises: a0000000000d
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a0000000000e"
down_revision = "a0000000000d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import dashboard_state_documents

    bind = op.get_bind()
    if "dashboard_state_documents" not in set(sa.inspect(bind).get_table_names()):
        dashboard_state_documents.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "dashboard_state_documents" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("dashboard_state_documents")
