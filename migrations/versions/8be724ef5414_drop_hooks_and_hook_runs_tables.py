"""drop hooks and hook_runs tables

Playbooks spec §13 Phase 3: the hook engine and rule manager have been
removed.  All automation is now handled by playbooks.  The hooks and
hook_runs tables are no longer used.

Revision ID: 8be724ef5414
Revises: 6eb45198a32c
Create Date: 2026-04-10 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8be724ef5414"
down_revision: Union[str, None] = "6eb45198a32c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop hook_runs first (has FK to hooks)
    op.drop_table("hook_runs")
    # Then drop hooks
    _ = op.execute("DROP INDEX IF EXISTS idx_hooks_plugin_id")
    op.drop_table("hooks")


def downgrade() -> None:
    # Recreate hooks table
    op.create_table(
        "hooks",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("context_steps", sa.Text, nullable=False, server_default="'[]'"),
        sa.Column("prompt_template", sa.Text, nullable=False),
        sa.Column("llm_config", sa.Text, nullable=True),
        sa.Column("cooldown_seconds", sa.Integer, nullable=False, server_default="3600"),
        sa.Column("max_tokens_per_run", sa.Integer, nullable=True),
        sa.Column("last_triggered_at", sa.Float, nullable=True),
        sa.Column("plugin_id", sa.Text, nullable=True),
        sa.Column("source_hash", sa.Text, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("idx_hooks_plugin_id", "hooks", ["plugin_id"])

    # Recreate hook_runs table
    op.create_table(
        "hook_runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("hook_id", sa.Text, sa.ForeignKey("hooks.id"), nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("trigger_reason", sa.Text, nullable=False),
        sa.Column("event_data", sa.Text, nullable=True),
        sa.Column("context_results", sa.Text, nullable=True),
        sa.Column("prompt_sent", sa.Text, nullable=True),
        sa.Column("llm_response", sa.Text, nullable=True),
        sa.Column("actions_taken", sa.Text, nullable=True),
        sa.Column("skipped_reason", sa.Text, nullable=True),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.Text, nullable=False, server_default="'running'"),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("completed_at", sa.Float, nullable=True),
    )
