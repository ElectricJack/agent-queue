"""agents.origin — who defined a worker row

The Agent Flock rail classified a worker as a pool instance whenever its
profile appeared in ``pool_status``.  That list names every ``lifecycle: pool``
profile in every active project even at zero supply, so a worker an operator
added by hand on a pool-backed profile vanished from the rail.  Profile id is
not an origin signal anyway: ``_launch_pool_session`` reserves *any* idle
compatible worker for a pool session, and a minted row returns to ``IDLE``
between sessions to be reused.

``origin`` records who created the row — ``manual`` (Add Agent), ``pool``
(minted by ``_launch_pool_session``), ``reconciler`` (lazy capacity bootstrap)
— and the rail lists a ``pool``-origin row only through its pool entry.

Backfill: the table never recorded this, so legacy rows are classified from
their session history — a row only ever owned by ``lifecycle: pool`` sessions
was minted by a pool (a rolled-back mint deletes its row, so every surviving
minted row has at least one).  A row with a ``task`` or ``named`` session was
launched some other way and stays ``manual``; so does a row with no sessions
at all, which is exactly the hand-added idle worker this repairs.  A hand-added
worker that a pool has reused and nothing else has run is indistinguishable
from a minted one and keeps its current (hidden) behaviour.

Revision ID: 2ea8c22b566b
Revises: e6a1b2c3d4f5
Create Date: 2026-09-03

"""

import sqlalchemy as sa
from alembic import op

revision = "2ea8c22b566b"
down_revision = "e6a1b2c3d4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("origin", sa.Text(), nullable=False, server_default="manual"),
    )
    op.execute(
        "UPDATE agents SET origin = 'pool' WHERE id IN ("
        "SELECT agent_id FROM sessions WHERE lifecycle = 'pool' AND agent_id IS NOT NULL"
        ") AND id NOT IN ("
        "SELECT agent_id FROM sessions WHERE lifecycle <> 'pool' AND agent_id IS NOT NULL"
        ")"
    )


def downgrade() -> None:
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_column("origin")
