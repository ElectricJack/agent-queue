"""playbook v2: durable waits and retained pending events

Revision ID: b3f2c0de0003
Revises: b3f2c0de0002
Create Date: 2026-09-01

Additive: two new tables.  Third and last in the Package 3 chain (child plan
§4.3).  playbook_waits references playbook_v2_runs; playbook_pending_events
deliberately has no foreign key at all — a pending event exists precisely
because no run does.

Both partial unique indexes are hand-written with matching sqlite_where /
postgresql_where predicates, including on the downgrade's drop_index, which
SQLAlchemy needs to render the drop on SQLite's batch path.
"""

import sqlalchemy as sa
from alembic import op

revision = "b3f2c0de0003"
down_revision = "b3f2c0de0002"
branch_labels = None
depends_on = None

_ACTIVE_WAIT_WHERE = "state = 'active'"
_PENDING_DEDUP_WHERE = "resolved_at IS NULL AND dedup_key <> ''"


def upgrade() -> None:
    op.create_table(
        "playbook_waits",
        sa.Column("wait_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("iteration", sa.Integer(), server_default="-1", nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), server_default="", nullable=False),
        sa.Column("correlation_key", sa.Text(), server_default="", nullable=False),
        sa.Column("match", sa.Text(), server_default="{}", nullable=False),
        sa.Column("deadline_at", sa.Float(), nullable=True),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), server_default="active", nullable=False),
        sa.Column("claimed_event_id", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["playbook_v2_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("wait_id"),
        sa.CheckConstraint(
            "kind IN ('event', 'timer', 'human', 'agent_task')",
            name="ck_playbook_waits_kind",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'claimed', 'expired', 'cleared')",
            name="ck_playbook_waits_state",
        ),
    )
    op.create_index(
        "uq_playbook_waits_active_step",
        "playbook_waits",
        ["run_id", "step_id", "iteration"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_WAIT_WHERE),
        postgresql_where=sa.text(_ACTIVE_WAIT_WHERE),
    )
    op.create_index("idx_playbook_waits_match", "playbook_waits", ["state", "event_type"])
    op.create_index("idx_playbook_waits_deadline", "playbook_waits", ["state", "deadline_at"])

    op.create_table(
        "playbook_pending_events",
        sa.Column("pending_event_id", sa.Text(), nullable=False),
        sa.Column("playbook_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), server_default="system", nullable=False),
        sa.Column("scope_identifier", sa.Text(), server_default="", nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), server_default="{}", nullable=False),
        sa.Column("event_id", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.Text(), server_default="", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("resolved_at", sa.Float(), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("pending_event_id"),
        sa.CheckConstraint(
            "reason IN ('stale_contract', 'invalid_artifact', 'disabled', "
            "'unavailable', 'question_required')",
            name="ck_playbook_pending_events_reason",
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('dispatched', 'discarded', 'expired')",
            name="ck_playbook_pending_events_resolution",
        ),
    )
    op.create_index(
        "uq_playbook_pending_events_dedup",
        "playbook_pending_events",
        ["playbook_id", "dedup_key"],
        unique=True,
        sqlite_where=sa.text(_PENDING_DEDUP_WHERE),
        postgresql_where=sa.text(_PENDING_DEDUP_WHERE),
    )
    op.create_index(
        "idx_playbook_pending_events_playbook",
        "playbook_pending_events",
        ["playbook_id", "received_at"],
    )
    op.create_index(
        "idx_playbook_pending_events_expiry", "playbook_pending_events", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_playbook_pending_events_expiry", table_name="playbook_pending_events")
    op.drop_index("idx_playbook_pending_events_playbook", table_name="playbook_pending_events")
    op.drop_index(
        "uq_playbook_pending_events_dedup",
        table_name="playbook_pending_events",
        sqlite_where=sa.text(_PENDING_DEDUP_WHERE),
        postgresql_where=sa.text(_PENDING_DEDUP_WHERE),
    )
    op.drop_table("playbook_pending_events")
    op.drop_index("idx_playbook_waits_deadline", table_name="playbook_waits")
    op.drop_index("idx_playbook_waits_match", table_name="playbook_waits")
    op.drop_index(
        "uq_playbook_waits_active_step",
        table_name="playbook_waits",
        sqlite_where=sa.text(_ACTIVE_WAIT_WHERE),
        postgresql_where=sa.text(_ACTIVE_WAIT_WHERE),
    )
    op.drop_table("playbook_waits")
