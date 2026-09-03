"""Add recoverable dispatch claims to retained Playbook V2 events.

Revision ID: f4f2c0de0007
Revises: e3f2c0de0006

Pending events remain unresolved while an operator dispatch is in flight, so
the unresolved-event deduplication index continues protecting their key.  The
token and renewable timestamp let a later operator replace a claim abandoned
by a dead process without racing a live dispatch.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4f2c0de0007"
down_revision: str | Sequence[str] | None = "e3f2c0de0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM_CHECK = (
    "(resolved_at IS NULL AND ((dispatch_claim_token IS NULL AND "
    "dispatch_claimed_by IS NULL AND dispatch_claimed_at IS NULL) OR "
    "(dispatch_claim_token IS NOT NULL AND dispatch_claimed_by IS NOT NULL AND "
    "dispatch_claimed_at IS NOT NULL))) OR (resolved_at IS NOT NULL AND "
    "dispatch_claim_token IS NULL AND dispatch_claimed_by IS NULL AND "
    "dispatch_claimed_at IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.add_column(sa.Column("dispatch_claim_token", sa.Text(), nullable=True))
        batch.add_column(sa.Column("dispatch_claimed_by", sa.Text(), nullable=True))
        batch.add_column(sa.Column("dispatch_claimed_at", sa.Float(), nullable=True))
        batch.create_check_constraint(
            "ck_playbook_pending_events_dispatch_claim", _CLAIM_CHECK
        )


def downgrade() -> None:
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.drop_constraint(
            "ck_playbook_pending_events_dispatch_claim", type_="check"
        )
        batch.drop_column("dispatch_claimed_at")
        batch.drop_column("dispatch_claimed_by")
        batch.drop_column("dispatch_claim_token")
