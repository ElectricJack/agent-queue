"""Project-owned long-horizon supervisor notifications."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000123"
down_revision = "a00000000122"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("project_schedules"):
        op.create_table(
            "project_schedules",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("idempotency_key", sa.Text, nullable=False),
            sa.Column("due_at", sa.Float, nullable=False),
            sa.Column("body", sa.Text, nullable=False),
            sa.Column("state", sa.Text, nullable=False),
            sa.Column("created_at", sa.Float, nullable=False),
            sa.Column("cancelled_at", sa.Float),
            sa.Column("fired_at", sa.Float),
            sa.Column("message_id", sa.Text),
            sa.UniqueConstraint("project_id", "idempotency_key", name="uq_project_schedules_key"),
            sa.CheckConstraint(
                "(state = 'pending' AND cancelled_at IS NULL AND fired_at IS NULL AND message_id IS NULL) "
                "OR (state = 'cancelled' AND cancelled_at IS NOT NULL AND fired_at IS NULL "
                "AND message_id IS NULL) OR (state = 'fired' AND cancelled_at IS NULL "
                "AND fired_at IS NOT NULL AND message_id IS NOT NULL)",
                name="ck_project_schedules_receipt",
            ),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("project_schedules")}
    if "idx_project_schedules_due" not in indexes:
        op.create_index("idx_project_schedules_due", "project_schedules", ["due_at", "id"],
                        postgresql_where=sa.text("state = 'pending'"))
    if "idx_project_schedules_project" not in indexes:
        op.create_index("idx_project_schedules_project", "project_schedules",
                        ["project_id", "due_at", "id"])


def downgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("project_schedules"):
        if bind.execute(sa.text("SELECT 1 FROM project_schedules LIMIT 1")).first():
            raise RuntimeError("Cannot discard durable schedules or firing receipts")
        op.drop_table("project_schedules")
