"""Allow a review rejection to remain open for a response revision.

Revision ID: a00000000024
Revises: a00000000023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000024"
down_revision = "a00000000023"
branch_labels = None
depends_on = None

_TABLE = "doc_reviews"
_CHECK = "ck_doc_reviews_state"
_OLD = "state IN ('in_review', 'changes_requested', 'approved', 'withdrawn')"
_NEW = "state IN ('in_review', 'changes_requested', 'rejected', 'approved', 'withdrawn')"


def _constraint_sql(bind) -> str | None:
    for item in sa.inspect(bind).get_check_constraints(_TABLE):
        if item.get("name") == _CHECK:
            return str(item.get("sqltext") or "")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    existing = _constraint_sql(bind)
    if existing is not None and "rejected" in existing.lower():
        return  # The squashed baseline uses the current metadata.
    if existing is not None:
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_CHECK, _TABLE, _NEW)


def downgrade() -> None:
    bind = op.get_bind()
    existing = _constraint_sql(bind)
    if existing is None or "rejected" not in existing.lower():
        return
    bind.execute(sa.text("UPDATE doc_reviews SET state = 'changes_requested' WHERE state = 'rejected'"))
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_CHECK, _TABLE, _OLD)
