"""Keep authorized batch ejection instructions outside worker task metadata.

Revision ID: a00000000082
Revises: a00000000081
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000082"
down_revision = "a00000000081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("integration_batches") and "ejection_record" not in {
        column["name"] for column in inspector.get_columns("integration_batches")
    }:
        # Never adopt worker-writable legacy markers as authorized instructions.
        op.add_column("integration_batches", sa.Column("ejection_record", JSONB(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("integration_batches") and "ejection_record" in {
        column["name"] for column in inspector.get_columns("integration_batches")
    }:
        op.drop_column("integration_batches", "ejection_record")
