"""Store response routing on the review revision that received feedback.

Revision ID: a0000000001c
Revises: a0000000001b

The squashed baseline creates new databases from live metadata, so the columns
are already present there. Existing databases receive them here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a0000000001c"
down_revision = "a0000000001b"
branch_labels = None
depends_on = None

_COLUMNS = ("responder_class", "responder_profile", "responder_profile_source")


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("doc_review_revisions")}
    for name in _COLUMNS:
        if name not in existing:
            op.add_column("doc_review_revisions", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("doc_review_revisions", name)
