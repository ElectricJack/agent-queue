"""Add ``integration_legacy_deliveries`` for pre-train delivered children.

Revision ID: a00000000022
Revises: a00000000021

A terminal child of a terminal parent that reached the default branch outside
the train can never get a train delivery receipt, so observe-mode readiness
reported it as ``missing_receipt`` forever.  ``aq integration
adopt-legacy-deliveries`` records one row per child it proves delivered (or an
operator explicitly accepts); integration status accepts those children.

**The create is conditional**, as in ``a00000000012``: the squashed baseline
builds its tables from the live ``src.database.tables.metadata``, so a
database created after this table was declared already has it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000022"
down_revision = "a00000000021"
branch_labels = None
depends_on = None

_TABLE = "integration_legacy_deliveries"


def upgrade() -> None:
    from src.database.tables import integration_legacy_deliveries

    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        integration_legacy_deliveries.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
