"""Keep adversarial review dispatches after their tasks archive.

Revision ID: a0000000001a
Revises: a00000000019
"""

from alembic import op

revision = "a0000000001a"
down_revision = "a00000000019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import doc_review_dispatches

    doc_review_dispatches.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Preserve the activity ledger across a code rollback.
    return
