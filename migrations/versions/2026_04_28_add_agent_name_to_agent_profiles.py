"""add agent_name column to agent_profiles

Revision ID: d8e4b2c5f1a7
Revises: c7d2a4f9e3b1
Create Date: 2026-04-28 21:00:00.000000

OPERATIONAL NOTE
================

Adds the ``agent_name`` column to ``agent_profiles``.  Only meaningful
for profiles whose ``runtime`` is ``"acpx"`` — ACPX dispatches to the
underlying coding agent named here (``"claude"`` / ``"codex"`` /
``"gemini"`` / ...).  For every other runtime the field is the empty
string.

Idempotent: inspects the schema first and skips ``add_column`` if the
column already exists (handles partially-migrated DBs from prior
rebases / metadata.create_all() paths).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e4b2c5f1a7'
down_revision: Union[str, Sequence[str], None] = 'c7d2a4f9e3b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "agent_name" in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "agent_name",
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "agent_name" not in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.drop_column("agent_name")
