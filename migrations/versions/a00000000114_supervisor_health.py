"""Persist named supervisor turn health and bounded recovery state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000114"
down_revision = "a00000000113"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("sessions") and "supervisor_health" not in {
        column["name"] for column in inspector.get_columns("sessions")
    }:
        op.add_column("sessions", sa.Column("supervisor_health", JSONB, nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("sessions") and "supervisor_health" in {
        column["name"] for column in inspector.get_columns("sessions")
    }:
        op.drop_column("sessions", "supervisor_health")
