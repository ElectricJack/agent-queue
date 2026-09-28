"""Review dispatches name a class hint, not a reviewer profile.

Revision ID: a00000000040
Revises: a00000000039

Mandatory-task-routing spec (2026-09-28) §5.3, Task 4: ``aq review dispatch``
files ``--count`` reviewer tasks with a ``--class`` hint and the constraint
``exclude_providers``, and the project's router picks each reviewer's profile.
A dispatch row therefore has no profile when it is written:

* ``doc_review_dispatches.profile_id`` becomes nullable (rows written before
  this revision keep the profile they named);
* ``doc_review_dispatches.intelligence_class`` records the class hint.

The squashed baseline builds from live metadata, so both steps are guarded:
running this revision twice, or over a baseline that already has the column
and the nullable profile, changes nothing.  Downgrade drops the class column
and restores NOT NULL only when no row lacks a profile.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000040"
down_revision = "a00000000039"
branch_labels = None
depends_on = None

_TABLE = "doc_review_dispatches"


def _columns(bind) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind)
    if "intelligence_class" not in columns:
        op.add_column(_TABLE, sa.Column("intelligence_class", sa.Text(), nullable=True))
    if not columns["profile_id"]["nullable"]:
        op.alter_column(_TABLE, "profile_id", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind)
    if "intelligence_class" in columns:
        op.drop_column(_TABLE, "intelligence_class")
    unprofiled = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE profile_id IS NULL")
    ).scalar()
    if columns["profile_id"]["nullable"] and not unprofiled:
        op.alter_column(_TABLE, "profile_id", existing_type=sa.Text(), nullable=False)
