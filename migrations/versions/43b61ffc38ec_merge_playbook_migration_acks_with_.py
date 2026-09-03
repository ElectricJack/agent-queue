"""merge playbook migration acks with pending event dispatch claims

Revision ID: 43b61ffc38ec
Revises: f4a2c0de0007, f4f2c0de0007
Create Date: 2026-09-03 02:41:20.401605

Two Playbook V2 branches landed on ``main`` independently and both declared
``e3f2c0de0006`` as their parent, so ``alembic upgrade head`` failed with
"Multiple head revisions are present" and every test that builds a database
errored in ``migrations/env.py``.

The two branches touch disjoint schema — ``f4a2c0de0007`` creates the new
``playbook_migration_acks`` table, ``f4f2c0de0007`` adds nullable dispatch-claim
columns to ``playbook_pending_events`` — so joining them needs no schema work of
its own and this revision is an empty merge point.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "43b61ffc38ec"
down_revision: str | Sequence[str] | None = ("f4a2c0de0007", "f4f2c0de0007")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
