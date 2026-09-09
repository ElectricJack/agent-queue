"""Keep an audited remote observation for legacy resolution recovery.

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
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("integration_promotion_intents")
    }
    if "resolution_recovery_evidence" not in columns:
        op.add_column(
            "integration_promotion_intents",
            sa.Column("resolution_recovery_evidence", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("integration_promotion_intents")
    }
    if "resolution_recovery_evidence" in columns:
        op.drop_column("integration_promotion_intents", "resolution_recovery_evidence")
