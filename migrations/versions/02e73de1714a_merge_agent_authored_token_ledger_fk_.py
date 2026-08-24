"""merge agent-authored token_ledger fk drop with discord_thread_id

Revision ID: 02e73de1714a
Revises: abb3cfc9b4a2, c4e1a9d7b310
Create Date: 2026-08-24 11:53:37.645171

Two revisions branched from ``a1c7f3e08b42`` independently:

* ``abb3cfc9b4a2`` — adds ``tasks.discord_thread_id`` so the Discord bot can
  recover its task -> thread mapping across a restart.
* ``c4e1a9d7b310`` — drops the ``token_ledger`` FKs to ``agents.id`` /
  ``tasks.id`` so archiving a task no longer erases its spend record.
  Authored in a separate workspace clone off ``origin/main``.

They touch disjoint tables and neither reads the other's schema, so joining
the branches needs no schema work — this revision exists only to give Alembic
a single head again. ``alembic upgrade head`` refuses to run with two.

Empty upgrade/downgrade is intentional; do not add operations here.
"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = '02e73de1714a'
down_revision: Union[str, Sequence[str], None] = ('abb3cfc9b4a2', 'c4e1a9d7b310')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
