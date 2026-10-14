"""Retain the last failed pool preparation identity on its session."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000096"
down_revision = "a00000000095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("sessions"):
        return
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "failed_preparation" not in columns:
        op.add_column("sessions", sa.Column("failed_preparation", JSONB(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("sessions"):
        return
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "failed_preparation" in columns:
        op.drop_column("sessions", "failed_preparation")
