"""Durable extraction jobs, exact input receipts and feature budgets (K13).

Adds ``knowledge_extraction_jobs``, ``knowledge_extraction_inputs``,
``knowledge_capture_checkpoints``, ``knowledge_feature_budgets`` and
``knowledge_budget_reservations``. The tables are defined additively in
``src.knowledge.extraction_schema`` and registered on the core metadata from
``src/database/tables.py``; this revision only creates and drops them so the
DDL order for the cross-referencing reservation pointer stays under Alembic.

Revision ID: a00000000064
Revises: a00000000063
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000064"
down_revision = "a00000000063"
branch_labels = None
depends_on = None


def upgrade():
    from src.database.tables import metadata
    from src.knowledge.extraction_schema import EXTRACTION_TABLE_NAMES

    bind = op.get_bind()
    metadata.create_all(bind, tables=[metadata.tables[name] for name in EXTRACTION_TABLE_NAMES])


def downgrade():
    from src.database.tables import metadata
    from src.knowledge.extraction_schema import EXTRACTION_TABLE_NAMES

    bind = op.get_bind()
    for name in EXTRACTION_TABLE_NAMES:
        if (
            sa.inspect(bind).has_table(name)
            and bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{name}")')).scalar_one()
        ):
            raise RuntimeError("extraction state contains data; use read-only rollback")
    metadata.drop_all(bind, tables=[metadata.tables[name] for name in EXTRACTION_TABLE_NAMES])