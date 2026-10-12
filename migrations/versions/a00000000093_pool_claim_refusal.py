"""Persist pool session claim refusal backoff across daemon restarts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000093"
down_revision = "a00000000092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("sessions") and "claim_refusal" not in {
        column["name"] for column in inspector.get_columns("sessions")
    }:
        op.add_column("sessions", sa.Column("claim_refusal", JSONB(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("sessions") and "claim_refusal" in {
        column["name"] for column in inspector.get_columns("sessions")
    }:
        op.drop_column("sessions", "claim_refusal")
