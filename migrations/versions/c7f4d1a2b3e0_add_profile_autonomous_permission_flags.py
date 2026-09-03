"""Add explicit autonomous permission opt-ins to agent profiles.

Revision ID: c7f4d1a2b3e0
Revises: b6e3c0de0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7f4d1a2b3e0"
down_revision: str | Sequence[str] | None = "b6e3c0de0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_profiles") as batch:
        batch.add_column(
            sa.Column("codex_full_auto", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column(
                "claude_dangerously_skip_permissions",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_profiles") as batch:
        batch.drop_column("claude_dangerously_skip_permissions")
        batch.drop_column("codex_full_auto")
