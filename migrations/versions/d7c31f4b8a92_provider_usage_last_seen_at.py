"""provider usage last_seen_at

Revision ID: d7c31f4b8a92
Revises: c6b64a925c90
Create Date: 2026-09-07

Adds ``provider_usage_snapshots.last_seen_at`` --- when a reading was last
*confirmed*, as opposed to ``observed_at``, when it first appeared.

The two have to be separate because the writer drops a reading identical to the
newest row already stored for its series: a Claude week window sitting at 81%
all afternoon writes one row at 13:00 and nothing after it, so by 18:00 its
``observed_at`` is five hours old while the probe has confirmed the number
every ten minutes.  Anything measuring freshness from ``observed_at`` -- the
usage API, the dashboard card, the doctor check -- would call that healthy
account stale, which is the exact "frozen and live look identical" failure the
feature exists to prevent, inverted (spec amendment A3).

Added nullable, backfilled from ``observed_at`` (the only honest value for a
row written before confirmations were tracked), then made ``NOT NULL``.  A
plain non-null add would need a server default, and there is no constant that
is not a lie about when a reading was seen.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7c31f4b8a92"
down_revision: str | Sequence[str] | None = "c6b64a925c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "provider_usage_snapshots",
        sa.Column("last_seen_at", sa.Float(), nullable=True),
    )
    op.execute(
        "UPDATE provider_usage_snapshots SET last_seen_at = observed_at "
        "WHERE last_seen_at IS NULL"
    )
    # batch_alter_table so SQLite gets the table rebuild it needs for a
    # nullability change; a no-op wrapper on PostgreSQL.
    with op.batch_alter_table("provider_usage_snapshots") as batch:
        batch.alter_column("last_seen_at", existing_type=sa.Float(), nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("provider_usage_snapshots") as batch:
        batch.drop_column("last_seen_at")
