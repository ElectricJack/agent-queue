"""Legacy import mapping schema (K06).

Adds ``record_import_runs``, ``record_legacy_mappings`` and
``record_import_items`` for sealed manifest tracking, permanent legacy
identity mapping and per-item import receipts.

Revision ID: a00000000060
Revises: a00000000059
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000060"
down_revision = "a00000000059"
branch_labels = None
depends_on = None

IMPORT_TABLE_NAMES = (
    "record_import_runs",
    "record_legacy_mappings",
    "record_import_items",
)


def upgrade():
    from src.database.tables import metadata

    bind = op.get_bind()
    metadata.create_all(bind, tables=[metadata.tables[name] for name in IMPORT_TABLE_NAMES])


def downgrade():
    from src.database.tables import metadata

    bind = op.get_bind()
    for name in IMPORT_TABLE_NAMES:
        if (
            sa.inspect(bind).has_table(name)
            and bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{name}")')).scalar_one()
        ):
            raise RuntimeError("import inventory contains data; use read-only rollback")
    metadata.drop_all(bind, tables=[metadata.tables[name] for name in IMPORT_TABLE_NAMES])
