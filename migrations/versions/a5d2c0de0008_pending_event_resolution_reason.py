"""Record why a retained Playbook V2 event was resolved.

Revision ID: a5d2c0de0008
Revises: 43b61ffc38ec

Playbook V2 Package 6 T-16 makes both unattended drops auditable — an
overflow that evicts the oldest held event, and a retention sweep that
expires one — and gives an operator's discard a mandatory justification with
the same 12-character floor as a migration acknowledgement.  ``resolution``
already says *what* happened to a row; this column says *why*, which is what
turns the pending-event table into the audit trail the cutover report reads.

Nullable with no backfill: rows resolved before this revision were resolved
by a code path that had no reason to record, and inventing one for them
would be the opposite of an audit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5d2c0de0008"
down_revision: str | Sequence[str] | None = "43b61ffc38ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.add_column(sa.Column("resolution_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.drop_column("resolution_reason")
