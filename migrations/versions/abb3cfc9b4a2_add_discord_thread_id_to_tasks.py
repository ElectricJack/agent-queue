"""add discord_thread_id to tasks

Persists the Discord thread opened for a task.  The bot previously kept the
task -> thread map only in memory (``AgentQueueBot._task_thread_objects``),
so a daemon restart lost it and the reuse check at bot.py:630 missed — the
bot opened a *new* thread for a task it had already threaded.  66% of tasks
in one archived log had more than one thread this way, some up to nine.

Revision ID: abb3cfc9b4a2
Revises: 2c6fdbaf1d2e
Create Date: 2026-08-24 09:56:19.212255

Autogenerate also proposed two ``use_alter`` foreign keys on agents/tasks
that are unrelated to this change and carry ``None`` constraint names (which
would fail on downgrade).  They are omitted deliberately.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'abb3cfc9b4a2'
down_revision: Union[str, Sequence[str], None] = '2c6fdbaf1d2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('tasks', sa.Column('discord_thread_id', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tasks', 'discord_thread_id')
