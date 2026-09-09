"""Add durable verified-reply action reservations.

Revision ID: a00000000006
Revises: a00000000005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000006"
down_revision = "a00000000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import escalation_actions

    bind = op.get_bind()
    if "escalation_actions" not in set(sa.inspect(bind).get_table_names()):
        escalation_actions.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "escalation_actions" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("escalation_actions")
