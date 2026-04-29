"""rename platform column to runtime on agent_profiles

Revision ID: c7d2a4f9e3b1
Revises: b5e9c1f8d3a4
Create Date: 2026-04-28 19:00:00.000000

OPERATIONAL NOTE
================

Renames the ``platform`` column on ``agent_profiles`` to ``runtime``.
The column was added by ``b5e9c1f8d3a4`` (supervisor-as-runtime port)
under the interim name ``platform``; this revision settles on the
final name to match :class:`AgentProfile.runtime` and the renamed
:mod:`src.runtimes` module.

Idempotent: inspects the schema first.  Three cases:

1. ``runtime`` column already exists → no-op (rename already done, or
   a fresh DB where the original add-column migration was edited to
   use the final name).
2. ``platform`` column exists but ``runtime`` does not → rename.
3. Neither exists → no-op (the prior add-column migration must run
   first; alembic ordering guarantees that).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2a4f9e3b1'
down_revision: Union[str, Sequence[str], None] = 'b5e9c1f8d3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "runtime" in existing_columns:
        return  # Already renamed (or fresh DB with final name)
    if "platform" not in existing_columns:
        return  # Nothing to rename
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.alter_column("platform", new_column_name="runtime")


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "platform" in existing_columns:
        return
    if "runtime" not in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.alter_column("runtime", new_column_name="platform")
