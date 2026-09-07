"""provider usage snapshots

Revision ID: c6b64a925c90
Revises: a12a5e1e4f05
Create Date: 2026-09-07

Creates ``provider_usage_snapshots``: one append-only row per observation of
one provider limit window, ``(provider, window, scope) -> used_percent,
resets_at`` at ``observed_at``, plus ``last_seen_at`` -- when that value was
last confirmed, which is where staleness is read from.  A repeat of an
unchanged reading writes no row and only pushes ``last_seen_at`` forward, so
``observed_at`` cannot be used for staleness without calling a healthy idle
quota dead.  Codex readings ride in on the transcript
watcher, Claude readings on a probe; ``source`` is the only thing that
distinguishes them, and it exists for staleness budgets rather than for
branching.

Hand-trimmed from ``--autogenerate``.  The generated draft also carried four
edits that have nothing to do with this change -- re-creating the ``agents``
and ``tasks`` self-referential foreign keys under explicit names, and adding
the ``integration_cleanup_items`` / ``task_dependencies`` check constraints --
which is autogenerate reporting long-standing drift between ``tables.py`` and
what SQLite actually reflects for unnamed constraints.  Repairing that is not
this revision's job and doing it here would make an additive migration a
table rebuild of two of the busiest tables in the schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6b64a925c90"
down_revision: str | Sequence[str] | None = "a12a5e1e4f05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "provider_usage_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account_label", sa.Text(), server_default="", nullable=False),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), server_default="", nullable=False),
        sa.Column("used_percent", sa.Float(), nullable=False),
        sa.Column("resets_at", sa.Float(), nullable=True),
        sa.Column("observed_at", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.Float(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source IN ('transcript','probe')",
            name="ck_provider_usage_snapshots_source",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Series key first, clock last and descending: every read is "newest in
    # this series".
    op.create_index(
        "idx_provider_usage_snapshots_series",
        "provider_usage_snapshots",
        ["provider", "window", "scope", sa.text("observed_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "idx_provider_usage_snapshots_series",
        table_name="provider_usage_snapshots",
    )
    op.drop_table("provider_usage_snapshots")
