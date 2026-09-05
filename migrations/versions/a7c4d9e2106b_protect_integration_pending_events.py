"""Protect correctness-critical integration pending events.

Revision ID: a7c4d9e2106b
Revises: 3f30b34c7e7c
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4d9e2106b"
down_revision: str | Sequence[str] | None = "3f30b34c7e7c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.add_column(
            sa.Column("protected", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.drop_column("protected")
