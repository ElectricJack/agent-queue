"""Pin a compiled Playbook V2 artifact to a document-review revision.

Revision ID: a00000000036
Revises: a00000000034

* ``doc_review_revisions.playbook`` — JSON metadata of the artifact a
  playbook review's revision asks approval for (playbook id, artifact hash,
  source hash, scope, ``activate_on_approval``); NULL for other reviews.
* ``doc_review_revisions.playbook_artifact`` — that artifact's exact
  canonical bytes, which approval stores in the artifact store.

**Every step is conditional**, for the same reason ``a00000000013``'s is: the
squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after these columns
were declared already has them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000036"
down_revision = "a00000000034"
branch_labels = None
depends_on = None

_TABLE = "doc_review_revisions"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "playbook" not in columns:
        op.add_column(_TABLE, sa.Column("playbook", sa.JSON(), nullable=True))
    if "playbook_artifact" not in columns:
        op.add_column(_TABLE, sa.Column("playbook_artifact", sa.Text(), nullable=True))


def downgrade() -> None:
    # An approved review's pinned artifact is its audit record; keep it.
    return
