"""Immutable image evidence owned by document review revisions.

Revision ID: a00000000045
Revises: a00000000044
"""

from alembic import op
import sqlalchemy as sa

revision = "a00000000045"
down_revision = "a00000000044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("doc_review_attachments"):
        return
    from src.database.tables import doc_review_attachments

    doc_review_attachments.create(bind, checkfirst=True)


def downgrade() -> None:
    op.drop_table("doc_review_attachments")
