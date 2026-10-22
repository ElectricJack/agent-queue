"""Atomic project-scoped storage relocation generations."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000116"
down_revision = "a00000000115"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("projects") and "storage_binding" not in {
        c["name"] for c in inspector.get_columns("projects")
    }:
        op.add_column("projects", sa.Column("storage_binding", JSONB(), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("projects") and "storage_binding" in {
        c["name"] for c in inspector.get_columns("projects")
    }:
        op.drop_column("projects", "storage_binding")
