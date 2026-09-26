"""Add immutable test selections, appended observations and policy promotions.

Revision ID: a00000000028
Revises: a00000000027

Creates are conditional because the squashed baseline builds live metadata;
fresh databases already have these tables before reaching this revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000028"
down_revision = "a00000000027"
branch_labels = None
depends_on = None

_TABLES = ("test_selections", "test_selection_observations", "test_selection_promotions")


def upgrade() -> None:
    from src.database.tables import (
        test_selection_observations,
        test_selection_promotions,
        test_selections,
    )

    bind = op.get_bind()
    for table in (test_selections, test_selection_observations, test_selection_promotions):
        if not sa.inspect(bind).has_table(table.name):
            table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_TABLES):
        if sa.inspect(bind).has_table(name):
            op.drop_table(name)
