"""operator enable/disable switch on agent profiles

Revision ID: a13a5e1e4f06
Revises: a12a5e1e4f05
Create Date: 2026-09-07

``agent_profiles.enabled`` is the operator kill-switch behind the flock's
pool and agent toggles: a disabled profile is handed no new work (its pools
size down to their busy sessions and ``task_claim`` refuses) while the
definition itself stays intact so it can be switched back on.  Existing rows
are enabled, which is the behaviour every profile had before this column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a13a5e1e4f06"
down_revision: str | Sequence[str] | None = "a12a5e1e4f05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("agent_profiles")}
    if "enabled" in columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("agent_profiles")}
    if "enabled" not in columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.drop_column("enabled")
