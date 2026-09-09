"""Fence the start of an external conflict-resolution push.

Revision ID: a00000000005
Revises: a00000000004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a00000000005"
down_revision = "a00000000004"
branch_labels = None
depends_on = None


def _has_column(bind) -> bool:
    return "resolution_push_started_at" in {
        column["name"]
        for column in sa.inspect(bind).get_columns("integration_promotion_intents")
    }


def upgrade() -> None:
    if not _has_column(op.get_bind()):
        op.add_column(
            "integration_promotion_intents",
            sa.Column("resolution_push_started_at", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    if _has_column(op.get_bind()):
        op.drop_column("integration_promotion_intents", "resolution_push_started_at")
