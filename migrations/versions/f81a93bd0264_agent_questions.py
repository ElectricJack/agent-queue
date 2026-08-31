"""Durable agent questions and successful Discord message receipts.

Revision ID: f81a93bd0264
Revises: e7a2b9c41d05
"""

from alembic import op
import sqlalchemy as sa

revision = "f81a93bd0264"
down_revision = "e7a2b9c41d05"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_questions",
        sa.Column("id", sa.Text(), primary_key=True),
        *[
            sa.Column(n, sa.Text(), nullable=False)
            for n in (
                "session_id",
                "session_name",
                "instance_token",
                "task_id",
                "project_id",
                "agent_id",
                "turn_id",
                "question",
                "state",
            )
        ],
        sa.Column("claim_epoch", sa.Integer(), nullable=False),
        sa.Column("requires_human", sa.Boolean(), nullable=False),
        *[
            sa.Column(n, sa.Float(), nullable=False)
            for n in ("created_at", "updated_at", "source_ts")
        ],
        *[
            sa.Column(n, sa.Text())
            for n in (
                "answer",
                "answered_by",
                "discord_channel_id",
                "discord_message_id",
                "delivery_token",
                "reason",
            )
        ],
        *[
            sa.Column(n, sa.Float())
            for n in ("supervisor_routed_at", "delivery_lease_until", "delivered_at")
        ],
        sa.Column("notification_next_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("notification_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "session_id",
            "instance_token",
            "task_id",
            "claim_epoch",
            "turn_id",
            name="uq_agent_question_turn",
        ),
        sa.CheckConstraint(
            "state IN ('supervisor','human','answered','delivered','resolved','stale')",
            name="ck_agent_question_state",
        ),
    )
    op.create_index("idx_agent_questions_pending", "agent_questions", ["state", "created_at"])
    op.create_index(
        "idx_agent_questions_session", "agent_questions", ["session_id", "instance_token"]
    )
    op.create_table(
        "message_discord_receipts",
        sa.Column("message_id", sa.Text(), primary_key=True),
        sa.Column("discord_channel_id", sa.Text()),
        sa.Column("discord_message_id", sa.Text()),
    )


def downgrade():
    op.drop_table("message_discord_receipts")
    op.drop_table("agent_questions")
