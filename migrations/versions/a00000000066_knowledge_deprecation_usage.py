"""Retain content-free compatibility attempt counters for K14.

Revision ID: a00000000066
Revises: a00000000065
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000066"
down_revision = "a00000000065"
branch_labels = None
depends_on = None


def upgrade():
    from src.database.tables import record_compatibility_usage

    record_compatibility_usage.create(op.get_bind(), checkfirst=True)


def downgrade():
    from src.database.tables import record_compatibility_usage

    bind = op.get_bind()
    if sa.inspect(bind).has_table(record_compatibility_usage.name):
        if bind.scalar(sa.select(sa.exists().select_from(record_compatibility_usage))):
            raise RuntimeError("compatibility evidence exists; use read-only rollback")
        record_compatibility_usage.drop(bind)
