"""add memory_scope_id to agent_profiles

Lets a profile declare an alternative agent-type memory scope so
multiple profiles can share one memory collection + vault directory.
When ``memory_scope_id`` is NULL, memory uses the profile id as before
(fully backwards compatible).  When set, memory reads/writes target
``agenttype_{memory_scope_id}`` instead of ``agenttype_{id}``.

Example: both ``claude-opus`` and ``claude-sonnet`` profiles set
``memory_scope_id='claude'`` so insights accumulate in one pool.

Revision ID: 60aa01bc1080
Revises: 3133bc141e0e
Create Date: 2026-04-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "60aa01bc1080"
down_revision: Union[str, Sequence[str], None] = "3133bc141e0e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable memory_scope_id column to agent_profiles."""
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("memory_scope_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop memory_scope_id column from agent_profiles."""
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.drop_column("memory_scope_id")
