"""Keep notification attempts separate from recipient delivery receipts."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000091"
down_revision = "a00000000090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("messages"):
        return
    columns = {column["name"] for column in inspector.get_columns("messages")}
    if "last_notified_at" not in columns:
        op.add_column("messages", sa.Column("last_notified_at", sa.Float(), nullable=True))
    if "notification_attempts" not in columns:
        op.add_column("messages", sa.Column(
            "notification_attempts", sa.Integer(), nullable=False, server_default="0",
        ))
    # A legacy pointer is not proof that the body was read. Recover this
    # backlog without reopening injected, read or intentionally archived mail.
    op.execute(sa.text("""
        UPDATE messages SET last_notified_at = delivered_at,
            notification_attempts = 1, delivered_at = NULL
        WHERE via = 'nudge' AND delivered_at IS NOT NULL
            AND read_at IS NULL AND archived_at IS NULL
    """))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("messages"):
        columns = {column["name"] for column in inspector.get_columns("messages")}
        for name in ("notification_attempts", "last_notified_at"):
            if name in columns:
                op.drop_column("messages", name)
