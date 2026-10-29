"""Persist wait source dispatch failure streaks and quarantine retry deadlines."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000124"
down_revision = "a00000000123"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("wait_source_events")}
    for column in (
        sa.Column("dispatch_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dispatch_retry_at", sa.Float),
        sa.Column("quarantined_at", sa.Float),
    ):
        if column.name not in columns:
            op.add_column("wait_source_events", column)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM wait_source_events WHERE quarantined_at IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Cannot discard quarantined wait source retry ownership")
    for name in ("quarantined_at", "dispatch_retry_at", "dispatch_failures"):
        op.drop_column("wait_source_events", name)
