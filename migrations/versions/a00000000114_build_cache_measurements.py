"""Retain compiler cache and workspace attribution in common wait samples."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000114"
down_revision = "a00000000113"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("wait_measurements"):
        columns = {column["name"] for column in inspector.get_columns("wait_measurements")}
        for name, kind in (("cache_hit_rate", sa.Float()), ("workspace_path", sa.Text())):
            if name not in columns:
                op.add_column("wait_measurements", sa.Column(name, kind, nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("wait_measurements"):
        columns = {column["name"] for column in inspector.get_columns("wait_measurements")}
        for name in ("cache_hit_rate", "workspace_path"):
            if name in columns:
                op.drop_column("wait_measurements", name)
