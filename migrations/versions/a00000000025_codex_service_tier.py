"""Add optional per-profile Codex service tier override.

Revision ID: a00000000025
Revises: a00000000024
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000025"
down_revision = "a00000000024"
branch_labels = None
depends_on = None

_TABLE = "agent_profiles"
_COLUMN = "codex_service_tier"


def _has_column() -> bool:
    bind = op.get_bind()
    return _TABLE in sa.inspect(bind).get_table_names() and _COLUMN in {
        column["name"] for column in sa.inspect(bind).get_columns(_TABLE)
    }


def upgrade() -> None:
    if not _has_column():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
