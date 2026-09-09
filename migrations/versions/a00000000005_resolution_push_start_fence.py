"""Fence the start of an external conflict-resolution push.

Pre-marker ``resolution_reserved`` rows have no durable evidence that their
external push *did not* begin.  Upgrade marks each unresolved legacy row with
the conservative ``0.0`` sentinel: it means "push-start time unknown", not
"the push started at the Unix epoch".  Runtime code treats every non-null
value alike, so a human resume cannot replay that write.  The frozen intent is
unchanged and can still be reconciled exactly if its reserved head is already
on the remote.

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
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            "integration_promotion_intents",
            sa.Column("resolution_push_started_at", sa.Float(), nullable=True),
        )
    # A NULL marker is the post-migration protocol's durable proof that no
    # external write has begun.  It cannot carry that meaning for a row that
    # predates the column, even when the squashed baseline already supplied
    # the physical column.  Keep the frozen resolution identity intact and
    # make the legacy uncertainty explicit instead of granting a replay.
    op.execute(
        sa.text(
            "UPDATE integration_promotion_intents "
            "SET resolution_push_started_at = 0.0 "
            "WHERE state = 'resolution_reserved' "
            "AND resolution_push_started_at IS NULL"
        )
    )


def downgrade() -> None:
    if _has_column(op.get_bind()):
        op.drop_column("integration_promotion_intents", "resolution_push_started_at")
