"""subagent events and session hooks_provisioned

Native sub-agent telemetry: the append-only ``subagent_events`` log that the
harnesses' ``SubagentStart`` / ``SubagentStop`` hooks write through
``aq subagent event``, plus ``sessions.hooks_provisioned`` — the launch fact
that says whether a given session's hook file was actually wired, which is
what separates "no native sub-agents" from "we cannot see them".

Autogenerate also proposed re-creating the ``agents.current_task_id`` and
``tasks.preferred_workspace_id`` foreign keys with explicit names.  That is
pre-existing naming drift between ``tables.py`` and the deployed schema, not
part of this change, and rewriting two of the busiest tables to fix a
constraint name is not something a telemetry migration should do — it is
removed here.

Revision ID: 33bdb059ceff
Revises: 009793fbb800
Create Date: 2026-09-01 15:59:13.422477

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '33bdb059ceff'
down_revision: Union[str, Sequence[str], None] = '009793fbb800'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'subagent_events',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('session_id', sa.Text(), nullable=False),
        sa.Column('harness', sa.Text(), nullable=False),
        sa.Column('project_id', sa.Text(), nullable=True),
        sa.Column('task_id', sa.Text(), nullable=True),
        sa.Column('subagent_id', sa.Text(), nullable=False),
        sa.Column('agent_type', sa.Text(), nullable=True),
        sa.Column('turn_id', sa.Text(), nullable=True),
        sa.Column('event', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.Float(), nullable=False),
        sa.CheckConstraint("event IN ('start','stop')", name='ck_subagent_events_event'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('subagent_events', schema=None) as batch_op:
        batch_op.create_index('idx_subagent_events_occurred', ['occurred_at'], unique=False)
        batch_op.create_index('idx_subagent_events_session', ['session_id', 'event'], unique=False)

    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'hooks_provisioned',
                sa.Boolean(),
                # sa.false(), not sa.text('0'): SQLite takes 0 for a BOOLEAN,
                # PostgreSQL rejects an integer default on a boolean column.
                server_default=sa.false(),
                nullable=False,
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('hooks_provisioned')

    with op.batch_alter_table('subagent_events', schema=None) as batch_op:
        batch_op.drop_index('idx_subagent_events_session')
        batch_op.drop_index('idx_subagent_events_occurred')

    op.drop_table('subagent_events')
