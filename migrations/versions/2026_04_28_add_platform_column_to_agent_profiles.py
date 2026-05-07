"""add platform column to agent_profiles

Revision ID: b5e9c1f8d3a4
Revises: ade0c5e7b25e
Create Date: 2026-04-28 17:00:00.000000

OPERATIONAL NOTE
================

Adds the ``platform`` column to ``agent_profiles``.  The field is
non-null with a default of ``'claude_sdk'`` so existing rows pick up
the legacy behaviour automatically (matching ``config.default_platform``)
— no data migration required.

Idempotent: inspects the schema first and skips ``add_column`` if the
column already exists (handles partially-migrated DBs from prior
rebases / metadata.create_all() paths).

To enable the in-process supervisor platform for a profile, edit the
profile markdown and add ``"platform": "supervisor"`` to the
``## Config`` JSON block.  The vault watcher syncs the change to the
DB on save.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5e9c1f8d3a4'
down_revision: Union[str, Sequence[str], None] = 'ade0c5e7b25e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "platform" in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "platform",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'claude_sdk'"),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "platform" not in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.drop_column("platform")
