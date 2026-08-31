"""Retain task execution attempts across retries, pool reuse and archival."""

from alembic import op
import sqlalchemy as sa

revision = "e8b39a10c572"
down_revision = "d37b821a6f04"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("ended_at", sa.Float(), nullable=True))
    op.add_column("sessions", sa.Column("end_reason", sa.Text(), nullable=True))
    op.create_table(
        "task_session_attempts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("task_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=True),
        sa.Column("agent_id", sa.Text, nullable=True),
        sa.Column("agent_name", sa.Text, nullable=True),
        sa.Column("profile_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("lifecycle", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("intelligence_class", sa.Text, nullable=True),
        sa.Column("llm_provider", sa.Text, nullable=True),
        sa.Column("harness", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("work_dir", sa.Text, nullable=False),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("session_started_at", sa.Float, nullable=False),
        sa.Column("ended_at", sa.Float, nullable=True),
        sa.Column("end_reason", sa.Text, nullable=True),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("session_key", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_task_session_attempts_task", "task_session_attempts", ["task_id", "started_at"]
    )
    op.create_index(
        "idx_task_session_attempts_session", "task_session_attempts", ["session_id", "started_at"]
    )
    # A saved terminal reason is evidence; wall-clock end times are not.
    op.execute(
        sa.text("""
        UPDATE sessions SET end_reason = sleep_reason
        WHERE state IN ('stopped', 'sleeping', 'quarantined')
          AND sleep_reason IS NOT NULL AND TRIM(sleep_reason) <> ''
    """)
    )
    # No end time/outcome can be inferred from current task state or activity.
    # Existing associations are all we know; lost earlier pool claims stay unknown.
    op.execute(
        sa.text("""
        INSERT INTO task_session_attempts
        (id, session_id, task_id, project_id, agent_id, agent_name, profile_id,
         name, lifecycle, model, intelligence_class, llm_provider, harness,
         provider, state, work_dir, started_at, session_started_at, session_key, end_reason)
        SELECT 'legacy-' || s.id, s.id, s.task_id, s.project_id, s.agent_id,
               a.name, s.profile_id, s.name, s.lifecycle, s.model,
               s.intelligence_class, s.llm_provider, s.harness, s.provider,
               s.state, s.work_dir,
               CASE WHEN s.lifecycle = 'pool' THEN COALESCE(s.claim_phase_at, s.started_at)
                    ELSE s.started_at END,
               s.started_at, s.session_key, s.end_reason
        FROM sessions s LEFT JOIN agents a ON a.id = s.agent_id
        LEFT JOIN tasks t ON t.id = s.task_id
        LEFT JOIN archived_tasks ar ON ar.id = s.task_id AND t.id IS NULL
        WHERE s.task_id IS NOT NULL
          AND s.project_id = COALESCE(t.project_id, ar.project_id)
          AND (CASE WHEN s.lifecycle = 'pool' THEN COALESCE(s.claim_phase_at, s.started_at)
                    ELSE s.started_at END) >= COALESCE(t.created_at, ar.created_at)
          AND NOT EXISTS (SELECT 1 FROM task_session_attempts h
                          WHERE h.session_id = s.id AND h.task_id = s.task_id)
    """)
    )


def downgrade():
    op.drop_table("task_session_attempts")
    op.drop_column("sessions", "end_reason")
    op.drop_column("sessions", "ended_at")
