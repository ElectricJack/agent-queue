"""Derived provider index receipts (K12).

Adds ``record_index_state``: per-provider, per-record checkpoints for an
optional semantic index of an exact revision. The rows are derived bookkeeping
for a rebuildable index; they never carry record content and never substitute
for the revision they describe.

Revision ID: a00000000065
Revises: a00000000064
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000065"
down_revision = "a00000000064"
branch_labels = None
depends_on = None

INDEX_RECEIPT_TABLE_NAMES = ("record_index_state",)


def upgrade():
    from src.database.tables import metadata

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("record_index_state"):
        metadata.create_all(bind, tables=[metadata.tables["record_index_state"]])


def downgrade():
    from src.database.tables import metadata

    bind = op.get_bind()
    for name in INDEX_RECEIPT_TABLE_NAMES:
        if (
            sa.inspect(bind).has_table(name)
            and bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{name}")')).scalar_one()
        ):
            raise RuntimeError("index receipts contain data; use read-only rollback")
    metadata.drop_all(bind, tables=[metadata.tables[name] for name in INDEX_RECEIPT_TABLE_NAMES])
