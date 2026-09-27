"""Add bounded collaboration threads, members and ordered message links.

Revision ID: a00000000034
Revises: a00000000033
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000034"
down_revision = "a00000000033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import (
        collaboration_members,
        collaboration_messages,
        collaboration_threads,
    )

    bind = op.get_bind()
    for table in (collaboration_threads, collaboration_members, collaboration_messages):
        if not sa.inspect(bind).has_table(table.name):
            table.create(bind, checkfirst=True)


def downgrade() -> None:
    # Preserve membership, history and idempotency tombstones on rollback.
    return
