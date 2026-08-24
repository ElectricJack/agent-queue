"""drop agent_profiles.agent_name; runtime defaults to empty (session-routed)

Revision ID: 2ea52ac3da6c
Revises: 02e73de1714a
Create Date: 2026-08-24 12:40:00.000000

The tmux-harness migration deleted the ``claude_sdk`` and ``acpx`` runtimes.
Every coding agent now runs as a session — a CLI wrapped in tmux, selected by
the profile's ``harness`` — leaving ``supervisor`` (in-process, tool-call-only,
no workspace) as the only Runtime.

Two consequences for this table:

* ``agent_name`` existed solely to tell ACPX which underlying agent to dispatch
  to.  With ACPX gone the column can never be meaningful again, so it is
  dropped rather than left as a permanently-empty field that reads like a live
  knob.  The parser now *rejects* the key instead of ignoring it.
* ``runtime`` defaults to ``''`` — "run as a session".  Existing rows naming a
  deleted runtime are rewritten to ``''``; ``supervisor`` rows are preserved.
  Without the backfill every migrated profile would name a runtime that no
  longer resolves, and dispatch would fail at task launch rather than at load.

Autogenerate additionally proposed two ``use_alter`` foreign keys on
agents/tasks that are unrelated to this change and carry ``None`` constraint
names (which would fail on downgrade); they are omitted deliberately.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ea52ac3da6c'
down_revision: Union[str, Sequence[str], None] = '02e73de1714a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('agent_profiles', 'agent_name')

    # Anything that is not the surviving in-process runtime becomes
    # session-routed.  Covers 'claude_sdk', 'acpx' and the older
    # 'claude_cli' / 'codex_cli' values retired earlier.
    op.execute(
        "UPDATE agent_profiles SET runtime = '' WHERE runtime <> 'supervisor'"
    )

    with op.batch_alter_table('agent_profiles') as batch:
        batch.alter_column(
            'runtime',
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default='',
        )


def downgrade() -> None:
    """Downgrade schema.

    Restores the column and the old default.  The *values* cannot be restored:
    which ACP agent a profile once dispatched to is not recoverable from
    ``harness`` alone, and session-routed profiles never had a runtime to
    return to.  Empty ``runtime`` becomes 'claude_sdk' again, which is what the
    old default would have produced.
    """
    with op.batch_alter_table('agent_profiles') as batch:
        batch.alter_column(
            'runtime',
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default='claude_sdk',
        )
    op.execute(
        "UPDATE agent_profiles SET runtime = 'claude_sdk' WHERE runtime = ''"
    )
    op.add_column(
        'agent_profiles',
        # Plain '' rather than autogenerate's ``sa.text("''::text")`` — the
        # ``::text`` cast is Postgres-only and SQLite rejects it outright
        # ("unrecognized token"), which breaks every downgrade round-trip test.
        sa.Column('agent_name', sa.Text(), server_default='', nullable=False),
    )
