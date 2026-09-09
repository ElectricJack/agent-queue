"""Add ``task_comments.kind`` — the durable meaningful-progress marker.

Revision ID: a00000000007
Revises: a00000000006

The hourly Discord digest (discord-simplification §8) may only report
*explicit* recorded progress. A task comment is not intrinsically progress: a
human question, a plan, a status request or arbitrary chatter all land in the
same table, and treating them as progress fabricates work that never happened.
``kind`` makes the distinction durable and unambiguous — ``note`` for ordinary
history, ``progress`` for an author asserting that recorded work advanced —
so the digest reads a label rather than guessing from prose.

Existing rows become ``note``: the server default backfills them, which is the
conservative reading (nothing historical is retroactively claimed as progress).

**The add is conditional**, for the same reason ``a00000000003``'s is: the
squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after this column was
declared already has it, and an unconditional ``add_column`` would fail on
every fresh database.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000007"
down_revision = "a00000000006"
branch_labels = None
depends_on = None

_TABLE = "task_comments"
_COLUMN = "kind"
_CHECK = "ck_task_comment_kind"


def _has_column(bind) -> bool:
    return _COLUMN in {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def _has_check(bind) -> bool:
    return _CHECK in {
        c["name"] for c in sa.inspect(bind).get_check_constraints(_TABLE)
    }


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Text(), nullable=False, server_default="note"),
        )
    if not _has_check(bind):
        op.create_check_constraint(_CHECK, _TABLE, "kind IN ('note','progress')")


def downgrade() -> None:
    bind = op.get_bind()
    if _has_check(bind):
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
