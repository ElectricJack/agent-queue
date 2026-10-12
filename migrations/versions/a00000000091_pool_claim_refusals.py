"""Persist the pool session's identical claim-preparation refusal budget."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000091"
down_revision = "a00000000090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("sessions"):
        return
    columns = {col["name"] for col in inspector.get_columns("sessions")}
    additions = (
        sa.Column("claim_failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_failure_reason", sa.Text(), nullable=True),
        sa.Column("claim_failure_at", sa.Float(), nullable=True),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("sessions", column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("sessions"):
        return
    columns = {col["name"] for col in inspector.get_columns("sessions")}
    for name in ("claim_failure_at", "claim_failure_reason", "claim_failure_count"):
        if name in columns:
            op.drop_column("sessions", name)
