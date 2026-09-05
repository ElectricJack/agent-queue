"""add blocked playbook run lifecycle

Revision ID: c7d8e9f0a1b2
Revises: 8b4d2f7c1a90
Create Date: 2026-09-05

``blocked`` is a terminal Playbook V2 run outcome authored by a terminal
step.  Widening the named lifecycle CHECK preserves every row unchanged.
Downgrade refuses while blocked history exists because the prior constraint
cannot represent those rows without deleting or rewriting their outcome.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "8b4d2f7c1a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "playbook_v2_runs"
_CONSTRAINT = "ck_playbook_v2_runs_lifecycle"
_BEFORE = (
    "lifecycle IN ('running', 'paused', 'cancelling', 'completed', "
    "'failed', 'timed_out', 'cancelled')"
)
_AFTER = (
    "lifecycle IN ('running', 'paused', 'cancelling', 'completed', "
    "'failed', 'blocked', 'timed_out', 'cancelled')"
)


def _rewrite_constraint(predicate: str) -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, predicate)


def upgrade() -> None:
    _rewrite_constraint(_AFTER)


def downgrade() -> None:
    blocked = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM playbook_v2_runs WHERE lifecycle = 'blocked' LIMIT 1"))
        .first()
    )
    if blocked is not None:
        raise RuntimeError("cannot downgrade while blocked playbook runs exist")
    _rewrite_constraint(_BEFORE)
