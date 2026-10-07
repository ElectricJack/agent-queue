"""Add session-owned recurring prompts.

Revision ID: a00000000086
Revises: a00000000085
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000086"
down_revision = "a00000000085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("agent_cron"):
        op.create_table(
            "agent_cron",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("session_id", sa.Text(), nullable=False),
            sa.Column("session_instance_token", sa.Text(), nullable=False),
            sa.Column("owner_task_id", sa.Text(), nullable=True),
            sa.Column("claim_epoch", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("idempotency_key", sa.Text(), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("recurrence", JSONB(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False, server_default="active"),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("next_fire_at", sa.Float(), nullable=False),
            sa.Column("checked_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("stopped_at", sa.Float(), nullable=True),
            sa.Column("tick_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("coalesced_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pending_message_id", sa.Text(), nullable=True),
            sa.Column("pending_until", sa.Float(), nullable=True),
            sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("last_delivery_at", sa.Float(), nullable=True),
            sa.Column("last_delivery_status", sa.Text(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "state IN ('active','cancelled','expired')", name="ck_agent_cron_state"
            ),
            sa.CheckConstraint(
                "delivery_attempts >= 0 AND delivery_attempts <= 5 AND claim_epoch >= 0",
                name="ck_agent_cron_attempts_epoch",
            ),
            sa.UniqueConstraint(
                "session_id",
                "session_instance_token",
                "idempotency_key",
                name="uq_agent_cron_idempotency",
            ),
        )
        op.create_index(
            "idx_agent_cron_scan", "agent_cron", ["state", "checked_at", "next_fire_at"]
        )


def downgrade() -> None:
    # Keep history and pending messages; cancel schedules before code rollback.
    return
