"""Order review comments by a per-review creation sequence.

Revision ID: a0000000001d
Revises: a0000000001c

list_review_comments sorted by ``created_at`` then the random comment ``id``.
``created_at`` is the caller's clock, and clocks hand out identical stamps — a
fixed test clock did exactly that once and left two comments tied, so the list
came back in random order and the focused test flapped. ``created_seq`` is the
comment's 1-based position among its review's comments, assigned at insert and
stable on any clock, and the listing now orders by it.

``created_seq`` is NOT NULL on new rows (the insert always supplies it). On an
existing database the column is added nullable, backfilled with a window over
the review's prior order (``created_at``, then ``id`` — the order the queries
already used), then made NOT NULL. The backfill is idempotent and conditional,
mirroring a00000000004 / a0000000000a, because the squashed baseline builds
fresh databases from the live ``src.database.tables`` metadata that already
declares the column; an unconditional ``add_column`` would fail on them.

Downgrade drops the column; the old ``created_at, id`` order remains the
fallback a comment that predated the change would otherwise lose.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a0000000001d"
down_revision = "a0000000001c"
branch_labels = None
depends_on = None

_TABLE = "doc_review_comments"
_COLUMN = "created_seq"
_CHECK = "ck_doc_review_comments_created_seq"

#: Backfill: rank each comment 1..N inside its review by the order the queries
#: already used (created_at, then id).  Windowed so it is correct per review
#: regardless of how many rows a review carries.
_BACKFILL = sa.text(
    """
    UPDATE doc_review_comments AS c
    SET created_seq = ranked.seq
    FROM (
        SELECT id,
               row_number() OVER (
                    PARTITION BY review_id
                    ORDER BY created_at ASC, id ASC
               ) AS seq
        FROM doc_review_comments
    ) AS ranked
    WHERE c.id = ranked.id AND c.created_seq IS NULL
    """
)


def _column_names(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def _check_names(bind) -> set[str]:
    return {
        c["name"]
        for c in sa.inspect(bind).get_check_constraints(_TABLE)
        if c.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    present = _column_names(bind)
    if _COLUMN not in present:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))
        bind.execute(_BACKFILL)
        op.alter_column(_TABLE, _COLUMN, nullable=False)
    existing = _check_names(bind)
    if _CHECK not in existing:
        op.create_check_constraint(
            _CHECK, _TABLE, "created_seq >= 1"
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = _check_names(bind)
    if _CHECK in existing:
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    if _COLUMN in _column_names(bind):
        op.drop_column(_TABLE, _COLUMN)
