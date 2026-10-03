"""Prepared context, observed delivery and exact execution citations (K08)."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000062"
down_revision = "a00000000061"
branch_labels = None
depends_on = None

CONTEXT_TABLE_NAMES = (
    "knowledge_context_bundles", "knowledge_context_deliveries", "knowledge_citations",
)


def upgrade():
    from src.database.tables import metadata

    metadata.create_all(op.get_bind(), tables=[metadata.tables[n] for n in CONTEXT_TABLE_NAMES])


def downgrade():
    from src.database.tables import metadata

    bind = op.get_bind()
    for name in CONTEXT_TABLE_NAMES:
        if sa.inspect(bind).has_table(name) and bind.scalar(
            sa.text(f'SELECT EXISTS (SELECT 1 FROM "{name}")')
        ):
            raise RuntimeError("knowledge context contains data; use read-only rollback")
    metadata.drop_all(bind, tables=[metadata.tables[n] for n in CONTEXT_TABLE_NAMES])
