"""Add verified waits, continuations, reservations and priority projection.

Revision ID: a00000000096
Revises: a00000000095

Definitions are frozen here rather than imported from live table metadata.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000096"
down_revision = "a00000000095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("wait_records"):
        op.create_table(
            "wait_records",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("owner_kind", sa.Text, nullable=False),
            sa.Column("owner_id", sa.Text, nullable=False),
            sa.Column("origin", sa.Text, nullable=False),
            sa.Column("session_id", sa.Text, nullable=True),
            sa.Column("claim_epoch", sa.Integer, nullable=True),
            sa.Column("condition_kind", sa.Text, nullable=False),
            sa.Column("condition", JSONB, nullable=False),
            sa.Column("source_key", sa.Text, nullable=False),
            sa.Column("source_generation", sa.Integer, nullable=False),
            sa.Column("source_watermark_seq", sa.BigInteger, nullable=False),
            sa.Column("verification_state", sa.Text, nullable=False),
            sa.Column("evidence_ref", sa.Text, nullable=False),
            sa.Column("verified_at", sa.Float, nullable=False),
            sa.Column("created_at", sa.Float, nullable=False),
            sa.Column("escalate_at", sa.Float, nullable=True),
            sa.Column("due_at", sa.Float, nullable=True),
            sa.Column("ceiling_anchor_at", sa.Float, nullable=True),
            sa.Column("sleep_decision", sa.Text, nullable=True),
            sa.Column("decision_reason", sa.Text, nullable=True),
            sa.Column("decision_inputs", JSONB, nullable=True),
            sa.Column("idempotency_key", sa.Text, nullable=True),
            sa.Column("request_hash", sa.Text, nullable=False),
            sa.Column("claimed", sa.Text, nullable=True),
            sa.Column("outcome", sa.Text, nullable=False, server_default="pending"),
            sa.Column("resolved_at", sa.Float, nullable=True),
            sa.Column("result_ref", sa.Text, nullable=True),
            sa.Column("blocking", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("cleared_at", sa.Float, nullable=True),
            sa.Column("legacy_kind", sa.Text, nullable=True),
            sa.Column("legacy_id", sa.Text, nullable=True),
            sa.Column("wake_delivered_at", sa.Float, nullable=True),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.CheckConstraint(
                "owner_kind IN ('task','session','scheduler','playbook','integration')",
                name="ck_wait_records_owner_kind",
            ),
            sa.CheckConstraint(
                "origin IN ('agent','scheduler','integration','playbook','human')",
                name="ck_wait_records_origin",
            ),
            sa.CheckConstraint(
                "condition_kind IN ('task','dependency','job','ci_check','ci_run','pull_request',"
                "'batch','message','human','timer','resource','playbook_condition')",
                name="ck_wait_records_condition_kind",
            ),
            sa.CheckConstraint(
                "verification_state IN ('verified','stale','unavailable')",
                name="ck_wait_records_verification_state",
            ),
            sa.CheckConstraint(
                "sleep_decision IN ('in_place','hibernate')", name="ck_wait_records_sleep_decision"
            ),
            sa.CheckConstraint(
                "outcome IN ('pending','satisfied','cancelled','lifted_by_supervisor','source_unavailable')",
                name="ck_wait_records_outcome",
            ),
        )
    if "uq_wait_records_active_owner" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_records")
    }:
        op.create_index(
            "uq_wait_records_active_owner",
            "wait_records",
            ["owner_kind", "owner_id"],
            unique=True,
            postgresql_where=sa.text("outcome = 'pending' AND origin <> 'scheduler'"),
        )
    if "uq_wait_records_idempotency" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_records")
    }:
        op.create_index(
            "uq_wait_records_idempotency",
            "wait_records",
            ["project_id", "owner_kind", "owner_id", "claim_epoch", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        )
    if "uq_wait_records_scheduler_blocker" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_records")
    }:
        op.create_index(
            "uq_wait_records_scheduler_blocker",
            "wait_records",
            ["owner_kind", "owner_id", "source_key", "source_generation"],
            unique=True,
            postgresql_where=sa.text("origin = 'scheduler'"),
        )
    if "uq_wait_records_legacy" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_records")
    }:
        op.create_index(
            "uq_wait_records_legacy",
            "wait_records",
            ["legacy_kind", "legacy_id"],
            unique=True,
            postgresql_where=sa.text("legacy_id IS NOT NULL"),
        )
    if "idx_wait_records_source" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_records")
    }:
        op.create_index(
            "idx_wait_records_source",
            "wait_records",
            ["source_key", "source_generation"],
            postgresql_where=sa.text("outcome = 'pending'"),
        )
    if "idx_wait_records_project_pending" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_records")
    }:
        op.create_index(
            "idx_wait_records_project_pending",
            "wait_records",
            ["project_id", "created_at"],
            postgresql_where=sa.text("outcome = 'pending'"),
        )

    if not sa.inspect(bind).has_table("wait_edges"):
        op.create_table(
            "wait_edges",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("wait_id", sa.Text, sa.ForeignKey("wait_records.id"), nullable=False),
            sa.Column("waiter_key", sa.Text, nullable=False),
            sa.Column("waiter_project_id", sa.Text, nullable=False),
            sa.Column("blocker_key", sa.Text, nullable=False),
            sa.Column("blocker_project_id", sa.Text, nullable=False),
            sa.Column("relation", sa.Text, nullable=False),
            sa.Column("source_revision", sa.Text, nullable=False),
            sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.Float, nullable=False),
            sa.Column("cleared_at", sa.Float, nullable=True),
            sa.CheckConstraint(
                "relation IN ('semantic','resource')", name="ck_wait_edges_relation"
            ),
        )
    if "idx_wait_edges_waiter" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_edges")
    }:
        op.create_index(
            "idx_wait_edges_waiter",
            "wait_edges",
            ["waiter_key"],
            postgresql_where=sa.text("active"),
        )
    if "idx_wait_edges_blocker" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_edges")
    }:
        op.create_index(
            "idx_wait_edges_blocker",
            "wait_edges",
            ["blocker_key"],
            postgresql_where=sa.text("active"),
        )

    if not sa.inspect(bind).has_table("wait_source_events"):
        op.create_table(
            "wait_source_events",
            sa.Column("seq", sa.BigInteger, sa.Identity(), primary_key=True),
            sa.Column("id", sa.Text, nullable=False),
            sa.Column("source_key", sa.Text, nullable=False),
            sa.Column("source_generation", sa.Integer, nullable=False),
            sa.Column("source_revision", sa.Text, nullable=False),
            sa.Column("occurred_at", sa.Float, nullable=False),
            sa.Column("committed_at", sa.Float, nullable=False),
            sa.Column("outcome_ref", sa.Text, nullable=False),
            sa.Column("payload_hash", sa.Text, nullable=False),
            sa.Column("payload", JSONB, nullable=False),
            sa.Column("dispatched_at", sa.Float, nullable=True),
            sa.UniqueConstraint("id", name="uq_wait_source_events_id"),
            sa.UniqueConstraint(
                "source_key",
                "source_generation",
                "source_revision",
                name="uq_wait_source_events_revision",
            ),
        )
    if "idx_wait_source_events_undispatched" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_source_events")
    }:
        op.create_index(
            "idx_wait_source_events_undispatched",
            "wait_source_events",
            ["seq"],
            postgresql_where=sa.text("dispatched_at IS NULL"),
        )

    if not sa.inspect(bind).has_table("wait_listener_cursors"):
        op.create_table(
            "wait_listener_cursors",
            sa.Column("listener_key", sa.Text, primary_key=True),
            sa.Column("last_seq", sa.BigInteger, nullable=False),
            sa.Column("lease_owner", sa.Text, nullable=True),
            sa.Column("lease_expires_at", sa.Float, nullable=True),
            sa.Column("updated_at", sa.Float, nullable=False),
            sa.CheckConstraint(
                "listener_key IN ('source','timer')", name="ck_wait_listener_cursors_key"
            ),
        )

    if not sa.inspect(bind).has_table("wait_deadlines"):
        op.create_table(
            "wait_deadlines",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("wait_id", sa.Text, sa.ForeignKey("wait_records.id"), nullable=False),
            sa.Column("deadline_type", sa.Text, nullable=False),
            sa.Column("due_at", sa.Float, nullable=False),
            sa.Column("generation", sa.Integer, nullable=False),
            sa.Column("fired_at", sa.Float, nullable=True),
            sa.Column("cancelled_at", sa.Float, nullable=True),
            sa.CheckConstraint(
                "deadline_type IN ('due','escalation','ceiling','transition','reservation')",
                name="ck_wait_deadlines_type",
            ),
            sa.UniqueConstraint(
                "wait_id", "deadline_type", "generation", name="uq_wait_deadlines_wait_type_gen"
            ),
        )
    if "idx_wait_deadlines_due" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_deadlines")
    }:
        op.create_index(
            "idx_wait_deadlines_due",
            "wait_deadlines",
            ["due_at"],
            postgresql_where=sa.text("fired_at IS NULL AND cancelled_at IS NULL"),
        )

    if not sa.inspect(bind).has_table("wait_escalations"):
        op.create_table(
            "wait_escalations",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("wait_id", sa.Text, sa.ForeignKey("wait_records.id"), nullable=False),
            sa.Column("generation", sa.Integer, nullable=False),
            sa.Column("escalation_id", sa.Text, sa.ForeignKey("escalations.id"), nullable=False),
            sa.Column("raised_at", sa.Float, nullable=False),
            sa.Column("original_age_seconds", sa.Float, nullable=False),
            sa.Column("supervisor_session_id", sa.Text, nullable=True),
            sa.Column("decision", sa.Text, nullable=True),
            sa.Column("decided_at", sa.Float, nullable=True),
            sa.Column("note", sa.Text, nullable=True),
            sa.Column("prior_escalation_id", sa.Text, nullable=True),
            sa.CheckConstraint(
                "decision IN ('extend','wake','cancel','reroute','escalate_to_human')",
                name="ck_wait_escalations_decision",
            ),
        )

    if not sa.inspect(bind).has_table("continuations"):
        op.create_table(
            "continuations",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("task_id", sa.Text, sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("wait_id", sa.Text, sa.ForeignKey("wait_records.id"), nullable=False),
            sa.Column("suspension_generation", sa.Integer, nullable=False),
            sa.Column("summary", sa.Text, nullable=True),
            sa.Column("summary_sha256", sa.Text, nullable=True),
            sa.Column("summary_degraded", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("facts", JSONB, nullable=False),
            sa.Column("branch", sa.Text, nullable=False),
            sa.Column("saved_head", sa.Text, nullable=True),
            sa.Column("prior_status", sa.Text, nullable=False),
            sa.Column("held_cursor_seq", sa.BigInteger, nullable=False),
            sa.Column("resume_state", sa.Text, nullable=False),
            sa.Column("hold_reason", sa.Text, nullable=True),
            sa.Column("launch_attempts", sa.Integer, nullable=False, server_default="0"),
            sa.Column("new_session_id", sa.Text, nullable=True),
            sa.Column("envelope_id", sa.Text, nullable=True),
            sa.Column("created_at", sa.Float, nullable=True),
            sa.Column("stopped_at", sa.Float, nullable=True),
            sa.Column("resumed_at", sa.Float, nullable=True),
            sa.Column("acknowledged_at", sa.Float, nullable=True),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.CheckConstraint(
                "resume_state IN ('sleeping','queued','reserved','launching','delivered',"
                "'acknowledged','held','failed','cancelled')",
                name="ck_continuations_resume_state",
            ),
            sa.CheckConstraint(
                "octet_length(summary) <= 8192", name="ck_continuations_summary_bytes"
            ),
            sa.UniqueConstraint("wait_id", "suspension_generation", name="uq_continuations_intent"),
        )
    if "uq_continuations_active_task" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("continuations")
    }:
        op.create_index(
            "uq_continuations_active_task",
            "continuations",
            ["task_id"],
            unique=True,
            postgresql_where=sa.text("resume_state NOT IN ('acknowledged','failed','cancelled')"),
        )

    if not sa.inspect(bind).has_table("admission_reservations"):
        op.create_table(
            "admission_reservations",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("task_id", sa.Text, sa.ForeignKey("tasks.id"), nullable=True),
            sa.Column("continuation_id", sa.Text, sa.ForeignKey("continuations.id"), nullable=True),
            sa.Column("scope", JSONB, nullable=False),
            sa.Column("effective_priority", sa.Integer, nullable=False),
            sa.Column("provenance", sa.Text, nullable=False),
            sa.Column("deadline_at", sa.Float, nullable=False),
            sa.Column("fifo_seq", sa.BigInteger, sa.Identity(), nullable=False),
            sa.Column("state", sa.Text, nullable=False),
            sa.Column("lease_owner", sa.Text, nullable=True),
            sa.Column("lease_expires_at", sa.Float, nullable=True),
            sa.Column("consumed_by_session_id", sa.Text, nullable=True),
            sa.Column("created_at", sa.Float, nullable=False),
            sa.Column("granted_at", sa.Float, nullable=True),
            sa.Column("consumed_at", sa.Float, nullable=True),
            sa.CheckConstraint(
                "provenance IN ('branch_blocker','resume','train_repair')",
                name="ck_admission_reservations_provenance",
            ),
            sa.CheckConstraint(
                "state IN ('pending','overdue','granted','consumed','cancelled')",
                name="ck_admission_reservations_state",
            ),
        )
    if "uq_admission_reservations_subject" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("admission_reservations")
    }:
        op.create_index(
            "uq_admission_reservations_subject",
            "admission_reservations",
            [sa.text("COALESCE(task_id, continuation_id)")],
            unique=True,
            postgresql_where=sa.text("state IN ('pending','overdue','granted')"),
        )

    if not sa.inspect(bind).has_table("wait_measurements"):
        op.create_table(
            "wait_measurements",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("condition_kind", sa.Text, nullable=False),
            sa.Column("source_class", sa.Text, nullable=False),
            sa.Column("wait_id", sa.Text, sa.ForeignKey("wait_records.id"), nullable=False),
            sa.Column("decision", sa.Text, nullable=False),
            sa.Column("waited_seconds", sa.Float, nullable=False),
            sa.Column("hibernated", sa.Boolean, nullable=False),
            sa.Column("evicted", sa.Boolean, nullable=False),
            sa.Column("summary_tokens", sa.Integer, nullable=True),
            sa.Column("resume_tokens", sa.Integer, nullable=True),
            sa.Column("teardown_seconds", sa.Float, nullable=True),
            sa.Column("setup_seconds", sa.Float, nullable=True),
            sa.Column("rebuild_seconds", sa.Float, nullable=True),
            sa.Column("recorded_at", sa.Float, nullable=False),
        )
    if "idx_wait_measurements_bucket" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("wait_measurements")
    }:
        op.create_index(
            "idx_wait_measurements_bucket",
            "wait_measurements",
            ["project_id", "condition_kind", "source_class", "recorded_at"],
        )

    if not sa.inspect(bind).has_table("wait_priority_projection"):
        op.create_table(
            "wait_priority_projection",
            sa.Column("task_id", sa.Text, sa.ForeignKey("tasks.id"), primary_key=True),
            sa.Column("donor_task_id", sa.Text, nullable=True),
            sa.Column("donor_path", JSONB, nullable=False),
            sa.Column("graph_version", sa.BigInteger, nullable=False),
            sa.Column("computed_at", sa.Float, nullable=False),
        )

    existing_columns = {column["name"] for column in sa.inspect(bind).get_columns("tasks")}
    if "effective_priority" not in existing_columns:
        op.add_column(
            "tasks",
            sa.Column("effective_priority", sa.Integer(), nullable=False, server_default="100"),
        )
        # Backfill once. A rerun must preserve priorities derived by the wait graph.
        op.execute(sa.text("UPDATE tasks SET effective_priority = priority"))

    for name, columns, predicate in (
        (
            "idx_tasks_claim_frontier",
            ["project_id", "status", "is_blocked", "effective_priority", "created_at"],
            None,
        ),
        (
            "idx_tasks_claim_frontier_by_profile",
            [
                "project_id",
                "profile_id",
                "status",
                "is_blocked",
                "effective_priority",
                "created_at",
            ],
            None,
        ),
        (
            "idx_tasks_claim_frontier_by_affinity",
            [
                "affinity_agent_id",
                "project_id",
                "status",
                "is_blocked",
                "effective_priority",
                "created_at",
            ],
            "affinity_agent_id IS NOT NULL",
        ),
    ):
        existing_indexes = {index["name"]: index for index in sa.inspect(bind).get_indexes("tasks")}
        if name in existing_indexes and existing_indexes[name]["column_names"] != columns:
            op.drop_index(name, table_name="tasks")
        if name not in {index["name"] for index in sa.inspect(bind).get_indexes("tasks")}:
            op.create_index(
                name,
                "tasks",
                columns,
                postgresql_where=sa.text(predicate) if predicate else None,
            )


def downgrade() -> None:
    # Design §12 forbids destructive downgrade: preserve waits and saved work.
    return
