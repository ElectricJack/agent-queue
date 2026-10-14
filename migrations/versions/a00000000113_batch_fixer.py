"""Retain task-free batch fixer identity across daemon restarts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000113"
down_revision = "a00000000112"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("integration_batches"):
        columns = {column["name"] for column in inspector.get_columns("integration_batches")}
        for name in ("fixer_state", "fixer_queue"):
            if name not in columns:
                op.add_column("integration_batches", sa.Column(name, JSONB(), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("integration_batches"):
        columns = {column["name"] for column in inspector.get_columns("integration_batches")}
        for name in ("fixer_queue", "fixer_state"):
            if name in columns:
                op.drop_column("integration_batches", name)
