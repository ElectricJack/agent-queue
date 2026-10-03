"""Durable integration subjects and their append-only journal.

Revision ID: a00000000057
Revises: a00000000054

Additive only (rev-agile-ridge revision 2, phase 1): ``integration_subjects``
is the one row the level-triggered reconciler visits per root batch, parent
episode or source, and ``integration_subject_journal`` records what it
observed, decided and did.  No existing table changes and nothing is activated:
a subject defaults to the ``legacy`` engine.

The never-blocked rule of §3.6 is a CHECK (a live subject is held by a gate or
due within its pinned ``max_wait_seconds``).  Two triggers keep the rest of the
identity honest: a subject's kind, key, task, batch and pinned policy artifact
never change, its version and generation never decrease and a ``done`` subject
never reopens; the journal refuses UPDATE and DELETE.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a00000000057"
down_revision = "a00000000054"
branch_labels = None
depends_on = None

SUBJECTS = "integration_subjects"
JOURNAL = "integration_subject_journal"

KINDS = "('root_batch', 'parent_episode', 'source')"
PHASES = (
    "('admitting', 'building', 'testing', 'repairing', 'promotable', 'publishing', "
    "'published', 'cleaning', 'done')"
)
PRIMITIVES = (
    "('integration_observe_subject', 'integration_seal', 'git_materialize_ref', "
    "'git_merge_members', 'git_preserve', 'git_publish', 'git_ancestry', 'ci_request', "
    "'ci_observe', 'ci_attest', 'writer_file', 'writer_lease', 'writer_stop_proof', "
    "'record_receipt', 'record_attempt', 'record_decision', 'wait', 'gate', 'eject', "
    "'cleanup')"
)

FUNCTIONS = (
    """
    CREATE OR REPLACE FUNCTION integration_subject_identity_is_pinned() RETURNS trigger AS $$
    BEGIN
        IF NEW.id <> OLD.id OR NEW.project_id <> OLD.project_id
            OR NEW.repository_id <> OLD.repository_id OR NEW.kind <> OLD.kind
            OR NEW.subject_key <> OLD.subject_key
            OR NEW.task_id IS DISTINCT FROM OLD.task_id THEN
            RAISE EXCEPTION 'integration subject identity is immutable';
        END IF;
        IF NEW.policy_playbook_id <> OLD.policy_playbook_id
            OR NEW.policy_artifact_sha256 <> OLD.policy_artifact_sha256 THEN
            RAISE EXCEPTION 'integration subject policy artifact is pinned';
        END IF;
        IF OLD.batch_id IS NOT NULL AND NEW.batch_id IS DISTINCT FROM OLD.batch_id THEN
            RAISE EXCEPTION 'integration subject batch is immutable once bound';
        END IF;
        IF NEW.version < OLD.version OR NEW.generation < OLD.generation THEN
            RAISE EXCEPTION 'integration subject version and generation cannot decrease';
        END IF;
        IF OLD.phase = 'done' AND NEW.phase <> 'done' THEN
            RAISE EXCEPTION 'a done integration subject cannot reopen';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION integration_subject_journal_is_append_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'integration subject journal is append-only';
    END;
    $$ LANGUAGE plpgsql
    """,
)

TRIGGERS = (
    (
        "integration_subject_identity_pinned",
        SUBJECTS,
        (
            "CREATE TRIGGER integration_subject_identity_pinned BEFORE UPDATE ON "
            "integration_subjects FOR EACH ROW EXECUTE FUNCTION "
            "integration_subject_identity_is_pinned()"
        ),
    ),
    (
        "integration_subject_journal_append_only",
        JOURNAL,
        (
            "CREATE TRIGGER integration_subject_journal_append_only BEFORE UPDATE OR DELETE "
            "ON integration_subject_journal FOR EACH ROW EXECUTE FUNCTION "
            "integration_subject_journal_is_append_only()"
        ),
    ),
)


def _create_subjects() -> None:
    op.create_table(
        SUBJECTS,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("engine", sa.Text(), nullable=False, server_default="legacy"),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("policy_playbook_id", sa.Text(), nullable=False),
        sa.Column(
            "policy_artifact_sha256",
            sa.Text(),
            sa.ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("target_ref", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_due_at", sa.Float(), nullable=True),
        sa.Column("due_set_at", sa.Float(), nullable=False),
        sa.Column("max_wait_seconds", sa.Integer(), nullable=False),
        sa.Column("wait_reason", sa.Text(), nullable=True),
        sa.Column("gate_id", sa.Text(), nullable=True),
        sa.Column("refusal_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wake_requested_at", sa.Float(), nullable=True),
        sa.Column("last_visit_at", sa.Float(), nullable=True),
        sa.Column("last_journal_seq", sa.BigInteger(), nullable=True),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("writer_status", sa.Text(), nullable=False, server_default="none"),
        sa.Column("writer_task_id", sa.Text(), nullable=True),
        sa.Column("writer_fence_token", sa.Integer(), nullable=True),
        sa.Column("writer_session_id", sa.Text(), nullable=True),
        sa.Column("writer_claimed_at", sa.Float(), nullable=True),
        sa.Column("writer_last_push_at", sa.Float(), nullable=True),
        sa.Column("writer_stop_proof", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("budget_ordinal", sa.Integer(), nullable=True),
        sa.Column("budget_class", sa.Text(), nullable=True),
        sa.Column("budget_started_at", sa.Float(), nullable=True),
        sa.Column("budget_deadline_at", sa.Float(), nullable=True),
        sa.Column("budget_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("budget_attempt_limit", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(f"kind IN {KINDS}", name="ck_integration_subjects_kind"),
        sa.CheckConstraint(f"phase IN {PHASES}", name="ck_integration_subjects_phase"),
        sa.CheckConstraint(
            "engine IN ('legacy', 'reconciler')", name="ck_integration_subjects_engine"
        ),
        sa.CheckConstraint(
            "writer_status IN ('none', 'filed', 'claimed', 'working', 'stopped', 'unknown')",
            name="ck_integration_subjects_writer_status",
        ),
        sa.CheckConstraint(
            "(kind = 'root_batch' AND task_id IS NULL) OR "
            "(kind <> 'root_batch' AND task_id IS NOT NULL AND batch_id IS NULL)",
            name="ck_integration_subjects_identity",
        ),
        sa.CheckConstraint(
            "(head_sha IS NULL OR (target_ref IS NOT NULL AND head_sha ~ '^[0-9a-f]{40}$')) "
            "AND (base_sha IS NULL OR base_sha ~ '^[0-9a-f]{40}$')",
            name="ck_integration_subjects_head",
        ),
        sa.CheckConstraint("generation >= 0", name="ck_integration_subjects_generation"),
        sa.CheckConstraint("version >= 0", name="ck_integration_subjects_version"),
        sa.CheckConstraint("max_wait_seconds > 0", name="ck_integration_subjects_max_wait"),
        sa.CheckConstraint("refusal_streak >= 0", name="ck_integration_subjects_refusal_streak"),
        sa.CheckConstraint(
            "(phase = 'done' AND closed_reason IS NOT NULL AND next_due_at IS NULL "
            "AND gate_id IS NULL AND wait_reason IS NULL) OR "
            "(phase <> 'done' AND closed_reason IS NULL AND (gate_id IS NOT NULL OR "
            "(next_due_at IS NOT NULL AND next_due_at <= due_set_at + max_wait_seconds)))",
            name="ck_integration_subjects_never_blocked",
        ),
        sa.CheckConstraint(
            "wait_reason IS NULL OR next_due_at IS NOT NULL",
            name="ck_integration_subjects_wait_until",
        ),
        sa.CheckConstraint(
            "(writer_status = 'none' AND writer_task_id IS NULL AND writer_fence_token IS NULL "
            "AND writer_session_id IS NULL) OR "
            "(writer_status <> 'none' AND writer_task_id IS NOT NULL)",
            name="ck_integration_subjects_writer",
        ),
        sa.CheckConstraint(
            "writer_fence_token IS NULL OR writer_fence_token >= 0",
            name="ck_integration_subjects_writer_fence",
        ),
        sa.CheckConstraint(
            "(budget_ordinal IS NULL AND budget_class IS NULL AND budget_started_at IS NULL "
            "AND budget_deadline_at IS NULL AND budget_attempt_limit IS NULL "
            "AND budget_attempts = 0) OR "
            "(budget_ordinal IS NOT NULL AND budget_ordinal >= 0 AND budget_class IS NOT NULL "
            "AND budget_started_at IS NOT NULL AND budget_deadline_at IS NOT NULL "
            "AND budget_deadline_at >= budget_started_at)",
            name="ck_integration_subjects_budget",
        ),
        sa.CheckConstraint(
            "budget_attempts >= 0 AND (budget_attempt_limit IS NULL OR budget_attempt_limit > 0)",
            name="ck_integration_subjects_budget_attempts",
        ),
        sa.UniqueConstraint(
            "project_id", "kind", "subject_key", name="uq_integration_subjects_key"
        ),
    )
    op.create_index(
        "idx_integration_subjects_due",
        SUBJECTS,
        ["next_due_at", "id"],
        postgresql_where=sa.text("phase <> 'done' AND next_due_at IS NOT NULL"),
    )
    op.create_index(
        "uq_integration_subjects_admitting_root",
        SUBJECTS,
        ["project_id", "repository_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'root_batch' AND phase = 'admitting'"),
    )
    op.create_index(
        "uq_integration_subjects_batch",
        SUBJECTS,
        ["batch_id"],
        unique=True,
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )
    op.create_index("idx_integration_subjects_project_phase", SUBJECTS, ["project_id", "phase"])
    op.create_index(
        "idx_integration_subjects_task",
        SUBJECTS,
        ["task_id"],
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )
    op.create_index(
        "idx_integration_subjects_writer_task",
        SUBJECTS,
        ["writer_task_id"],
        postgresql_where=sa.text("writer_task_id IS NOT NULL"),
    )
    op.create_index(
        "idx_integration_subjects_gate",
        SUBJECTS,
        ["gate_id"],
        postgresql_where=sa.text("gate_id IS NOT NULL"),
    )
    op.create_index("idx_integration_subjects_artifact", SUBJECTS, ["policy_artifact_sha256"])


def _create_journal() -> None:
    op.create_table(
        JOURNAL,
        sa.Column("seq", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Text(),
            sa.ForeignKey("integration_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("entry_kind", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("visit_id", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column(
            "policy_artifact_sha256",
            sa.Text(),
            sa.ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("subject_version", sa.Integer(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=True),
        sa.Column("primitive", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("facts_digest", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "entry_kind IN ('decision', 'action', 'attempt', 'receipt')",
            name="ck_integration_subject_journal_kind",
        ),
        sa.CheckConstraint(
            "mode IN ('shadow', 'active')", name="ck_integration_subject_journal_mode"
        ),
        sa.CheckConstraint(f"phase IN {PHASES}", name="ck_integration_subject_journal_phase"),
        sa.CheckConstraint(
            f"primitive IS NULL OR primitive IN {PRIMITIVES}",
            name="ck_integration_subject_journal_primitive",
        ),
        sa.CheckConstraint(
            "subject_version >= 0 AND generation >= 0",
            name="ck_integration_subject_journal_identity",
        ),
        sa.CheckConstraint(
            "head_sha IS NULL OR head_sha ~ '^[0-9a-f]{40}$'",
            name="ck_integration_subject_journal_head",
        ),
        sa.CheckConstraint(
            "(entry_kind = 'decision' AND rule IS NOT NULL AND primitive IS NOT NULL "
            "AND facts_digest IS NOT NULL AND outcome IS NULL) OR "
            "(entry_kind = 'action' AND primitive IS NOT NULL AND outcome IS NOT NULL) OR "
            "(entry_kind IN ('attempt', 'receipt') AND head_sha IS NOT NULL "
            "AND outcome IS NOT NULL)",
            name="ck_integration_subject_journal_entry",
        ),
        sa.UniqueConstraint(
            "subject_id", "idempotency_key", name="uq_integration_subject_journal_idempotency"
        ),
    )
    op.create_index("idx_integration_subject_journal_subject", JOURNAL, ["subject_id", "seq"])
    op.create_index("idx_integration_subject_journal_artifact", JOURNAL, ["policy_artifact_sha256"])


def upgrade() -> None:
    bind = op.get_bind()
    # The squashed baseline is constructed from current live metadata, so a
    # fresh install already has both tables; only the triggers are installed.
    if not sa.inspect(bind).has_table(SUBJECTS):
        _create_subjects()
    if not sa.inspect(bind).has_table(JOURNAL):
        _create_journal()
    for statement in FUNCTIONS:
        op.execute(statement)
    for name, table, statement in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    for table in (JOURNAL, SUBJECTS):
        if sa.inspect(bind).has_table(table):
            op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS integration_subject_journal_is_append_only()")
    op.execute("DROP FUNCTION IF EXISTS integration_subject_identity_is_pinned()")
