"""Add ``agent_profiles.min_per_project`` — the per-project warm floor.

Revision ID: a00000000003
Revises: a00000000002

Pool sizing is fleet-wide (global-worker-pools §1.1), so ``min_active`` can no
longer express "keep one worker resident in *every* project" — it names a
single fleet-wide floor. ``min_per_project`` (§2.1) is the explicit way to buy
that back: the effective global floor becomes
``max(min_active, sum(min_per_project) over eligible projects)``.

NULL is read as 0, so existing rows need no backfill and an operator who never
sets the key keeps today's behaviour.

**The add is conditional**, for the same reason ``a00000000002``'s guard
installer is idempotent: the squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after this column was
declared already has it and only databases created *before* need the
``ALTER``. An unconditional ``add_column`` would fail on every fresh database.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000003"
down_revision = "a00000000002"
branch_labels = None
depends_on = None

_TABLE = "agent_profiles"
_COLUMN = "min_per_project"


def _has_column(bind) -> bool:
    return _COLUMN in {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    if not _has_column(op.get_bind()):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    # The baseline now declares the column, so a database rebuilt from it will
    # have it regardless of stamp; only drop what is actually there.
    if _has_column(op.get_bind()):
        op.drop_column(_TABLE, _COLUMN)
