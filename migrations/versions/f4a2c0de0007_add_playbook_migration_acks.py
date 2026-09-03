"""Add ``playbook_migration_acks``.

Revision ID: f4a2c0de0007
Revises: e3f2c0de0006

Package 6 of the Playbook V2 roadmap stores one row per operator waiver: a
written acknowledgement that a playbook cannot be migrated to V2 and the fleet
may cut over without it.  Additive only — a new table with no data migration,
because "no playbook has been acknowledged" is the correct initial state on
every existing install.

``downgrade`` drops the table and therefore loses acknowledgement history.
That is acceptable: the cutover report that consumes these rows is regenerated
from the inventory, and reverting Package 6 means the fleet is not cutting
over.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a2c0de0007"
down_revision: str | Sequence[str] | None = "e3f2c0de0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playbook_migration_acks",
        sa.Column("playbook_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_identifier", sa.Text(), server_default="", nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("acknowledged_by", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Float(), nullable=False),
        sa.CheckConstraint("length(reason) >= 12", name="ck_playbook_migration_acks_reason"),
        sa.PrimaryKeyConstraint("playbook_id", "scope", "scope_identifier"),
    )
    op.create_index(
        "idx_playbook_migration_acks_source",
        "playbook_migration_acks",
        ["source_sha256"],
    )


def downgrade() -> None:
    op.drop_index("idx_playbook_migration_acks_source", table_name="playbook_migration_acks")
    op.drop_table("playbook_migration_acks")
