"""Human ask ledgers and additive review/correspondence fences (no new sends).

Definitions are frozen at revision 91, independent of later transport/cleanup.
The squashed baseline uses live metadata, so every addition is guarded.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Table,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000096"
down_revision = "a00000000095"
branch_labels = None
depends_on = None

metadata = sa.MetaData()
# Only FK targets; never created or used as live table definitions.
for name, columns in {
    "projects": ["id"],
    "doc_review_revisions": ["review_id", "revision"],
    "doc_review_comments": ["id"],
    "escalation_messages": ["id"],
    "escalation_actions": ["id"],
}.items():
    Table(name, metadata, *(Column(c, Text, primary_key=True) for c in columns))

# Human communication ledgers (implementation specification, 2026-10-11 §3.1).
# Source task/session ids remain soft references. Audit owners never cascade.
human_asks = Table(
    "human_asks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("source_key", Text, nullable=False),
    Column("source_identity", JSONB, nullable=False),
    Column("source_fingerprint", Text, nullable=False),
    Column("generation_fingerprint", Text, nullable=False),
    Column("feedback_version", Integer, nullable=False, server_default="0"),
    Column("decision_generation", Integer, nullable=False, server_default="1"),
    Column("display_version", Integer, nullable=False, server_default="1"),
    Column("context", JSONB, nullable=False),
    Column("audience", JSONB, nullable=False),
    Column("logical_owner", Text, nullable=False),
    Column("decider", JSONB, nullable=False),
    Column("next_actor", Text, nullable=False, server_default="human"),
    Column("decision_state", Text, nullable=False, server_default="pending"),
    Column("application_state", Text, nullable=False, server_default="not_started"),
    Column("context_state", Text, nullable=False, server_default="complete"),
    Column("severity", Text, nullable=False, server_default="medium"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "context_state <> 'complete' OR coalesce(("
        "length(context->>'title') BETWEEN 1 AND 200 AND "
        "length(context->>'why') BETWEEN 1 AND 2000 AND "
        "length(context->>'waiting_effect') BETWEEN 1 AND 2000 AND "
        "jsonb_typeof(context->'choices') = 'array' AND "
        "jsonb_array_length(context->'choices') BETWEEN 1 AND 20), false)",
        name="ck_human_asks_complete_context",
    ),
    UniqueConstraint("project_id", "source_kind", "source_key", name="uq_human_asks_source"),
    UniqueConstraint("project_id", "id", name="uq_human_asks_project_id"),
    CheckConstraint(
        "decision_generation >= 1 AND display_version >= 1 AND feedback_version >= 0",
        name="ck_human_asks_versions",
    ),
    CheckConstraint(
        "length(source_kind) BETWEEN 1 AND 128 AND length(source_key) BETWEEN 1 AND 512",
        name="ck_human_asks_identity",
    ),
    CheckConstraint("length(logical_owner) BETWEEN 1 AND 512", name="ck_human_asks_owner"),
    CheckConstraint("next_actor IN ('human','agent','none')", name="ck_human_asks_next_actor"),
    CheckConstraint(
        "context_state IN ('complete','context_needed','quarantined')",
        name="ck_human_asks_context_state",
    ),
    CheckConstraint(
        "decision_state IN ('pending','approved','changes_requested','declined',"
        "'withdrawn','superseded','answered')",
        name="ck_human_asks_decision_state",
    ),
    CheckConstraint(
        "application_state IN ('not_started','applying','applied','failed','unknown')",
        name="ck_human_asks_application_state",
    ),
    CheckConstraint(
        "severity IN ('critical','high','medium','low')", name="ck_human_asks_severity"
    ),
    CheckConstraint(
        "jsonb_typeof(context) = 'object' AND octet_length(context::text) <= 65536",
        name="ck_human_asks_context",
    ),
    Index(
        "idx_human_asks_pending", "project_id", "next_actor", "decision_state", "created_at", "id"
    ),
)

ask_source_aliases = Table(
    "ask_source_aliases",
    metadata,
    Column("project_id", Text, primary_key=True),
    Column("legacy_kind", Text, primary_key=True),
    Column("legacy_id", Text, primary_key=True),
    Column("ask_id", Text, nullable=False),
    Column("source_generation", Integer, nullable=False),
    Column("source_reference", JSONB, nullable=False),
    Column("provenance", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
    ForeignKeyConstraint(
        ["project_id", "ask_id"],
        ["human_asks.project_id", "human_asks.id"],
        name="fk_ask_source_aliases_ask",
        ondelete="RESTRICT",
    ),
    CheckConstraint("source_generation >= 1", name="ck_ask_source_aliases_generation"),
    Index("idx_ask_source_aliases_ask", "ask_id"),
)

ask_decision_previews = Table(
    "ask_decision_previews",
    metadata,
    Column("id", Text, primary_key=True),
    Column("ask_id", Text, ForeignKey("human_asks.id", ondelete="RESTRICT"), nullable=False),
    Column("decision_generation", Integer, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("input_id", Text, nullable=True),
    Column("source_fingerprint", Text, nullable=False),
    Column("feedback_version", Integer, nullable=False),
    Column("packet", JSONB, nullable=False),
    Column("packet_sha256", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("consumed_at", Float, nullable=True),
    CheckConstraint(
        "expires_at > created_at AND expires_at <= created_at + 600",
        name="ck_ask_decision_previews_expiry",
    ),
    CheckConstraint(
        "decision_generation >= 1 AND feedback_version >= 0",
        name="ck_ask_decision_previews_versions",
    ),
    Index("idx_ask_decision_previews_expiry", "expires_at"),
)

approved_document_snapshots = Table(
    "approved_document_snapshots",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
    Column("source_path", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", LargeBinary, nullable=False),
    Column("body_sha256", Text, nullable=False),
    Column("spec_kind", Text, nullable=False),
    Column("approval_provenance", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
    CheckConstraint(
        "spec_kind IN ('design','implementation')", name="ck_approved_document_snapshots_spec_kind"
    ),
    CheckConstraint(
        "length(title) BETWEEN 1 AND 200 AND octet_length(body) <= 262144",
        name="ck_approved_document_snapshots_content",
    ),
)

ask_decisions = Table(
    "ask_decisions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("ask_id", Text, ForeignKey("human_asks.id", ondelete="RESTRICT"), nullable=False),
    Column("decision_generation", Integer, nullable=False),
    Column("option_key", Text, nullable=False),
    Column("adapter", Text, nullable=False),
    Column("adapter_version", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("actor", JSONB, nullable=False),
    Column(
        "evidence_entry_id",
        Text,
        ForeignKey("escalation_messages.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("idempotency_key", Text, nullable=False),
    Column("request_sha256", Text, nullable=False),
    Column("source_fingerprint", Text, nullable=False),
    Column("review_id", Text, nullable=True),
    Column("review_revision", Integer, nullable=True),
    Column("content_sha256", Text, nullable=True),
    Column("attachment_manifest_sha256", Text, nullable=True),
    Column(
        "snapshot_id",
        Text,
        ForeignKey("approved_document_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("feedback_version", Integer, nullable=False),
    Column("overall_note", Text, nullable=False, server_default=""),
    Column("accepted_dispositions", JSONB, nullable=False, server_default="[]"),
    Column("packet", JSONB, nullable=False),
    Column("packet_sha256", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    ForeignKeyConstraint(
        ["review_id", "review_revision"],
        ["doc_review_revisions.review_id", "doc_review_revisions.revision"],
        name="fk_ask_decisions_revision",
        ondelete="RESTRICT",
    ),
    UniqueConstraint("ask_id", "decision_generation", name="uq_ask_decisions_generation"),
    UniqueConstraint("ask_id", "actor_id", "idempotency_key", name="uq_ask_decisions_request"),
    CheckConstraint(
        "decision_generation >= 1 AND feedback_version >= 0", name="ck_ask_decisions_versions"
    ),
    CheckConstraint(
        "length(overall_note) <= 16000 AND length(idempotency_key) BETWEEN 1 AND 512",
        name="ck_ask_decisions_text",
    ),
    CheckConstraint(
        "(review_id IS NULL) = (review_revision IS NULL) AND "
        "(review_id IS NULL OR snapshot_id IS NULL)",
        name="ck_ask_decisions_document",
    ),
    Index("idx_ask_decisions_history", "ask_id", "created_at", "id"),
)

review_feedback_obligations = Table(
    "review_feedback_obligations",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "decision_id", Text, ForeignKey("ask_decisions.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("feedback_key", Text, nullable=False),
    Column(
        "comment_id", Text, ForeignKey("doc_review_comments.id", ondelete="RESTRICT"), nullable=True
    ),
    Column("original_text", Text, nullable=False),
    Column("original_anchor", JSONB, nullable=False),
    Column("original_author", Text, nullable=False),
    Column("original_revision", Integer, nullable=True),
    Column("disposition", Text, nullable=False),
    Column("accepted_by", JSONB, nullable=False),
    Column("logical_owner", Text, nullable=False),
    Column("required_before_release", Boolean, nullable=False, server_default=false()),
    Column(
        "operation_id",
        Text,
        ForeignKey("escalation_actions.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("task_id", Text, nullable=True),
    Column("acceptance_text", Text, nullable=False),
    Column("progress", Text, nullable=False, server_default="pending"),
    Column("completion_evidence", JSONB, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("decision_id", "feedback_key", name="uq_review_feedback_obligations_item"),
    CheckConstraint(
        "disposition IN ('follow_up','fold_in','applied','no_change','withdrawn')",
        name="ck_review_feedback_obligations_disposition",
    ),
    CheckConstraint(
        "progress IN ('pending','filed','in_progress','completed','failed')",
        name="ck_review_feedback_obligations_progress",
    ),
    CheckConstraint(
        "progress <> 'completed' OR completion_evidence IS NOT NULL",
        name="ck_review_feedback_obligations_evidence",
    ),
    CheckConstraint(
        "length(original_text) BETWEEN 1 AND 16000 AND length(acceptance_text) BETWEEN 1 AND 16000",
        name="ck_review_feedback_obligations_text",
    ),
    Index("idx_review_feedback_obligations_owner", "logical_owner", "progress"),
)

ask_reconciliation_cases = Table(
    "ask_reconciliation_cases",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
    Column("conflict_key", Text, nullable=False),
    Column("source_evidence", JSONB, nullable=False),
    Column("affected_aliases", JSONB, nullable=False, server_default="[]"),
    Column("affected_deliveries", JSONB, nullable=False, server_default="[]"),
    Column("logical_owner", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="open"),
    Column("repair_receipt", JSONB, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("project_id", "conflict_key", name="uq_ask_reconciliation_cases_conflict"),
    CheckConstraint("status IN ('open','resolved')", name="ck_ask_reconciliation_cases_status"),
    CheckConstraint(
        "status <> 'resolved' OR repair_receipt IS NOT NULL",
        name="ck_ask_reconciliation_cases_receipt",
    ),
    Index("idx_ask_reconciliation_cases_owner", "project_id", "logical_owner", "status"),
)


NEW_TABLES = (
    human_asks,
    ask_source_aliases,
    ask_decision_previews,
    approved_document_snapshots,
    ask_decisions,
    review_feedback_obligations,
    ask_reconciliation_cases,
)

ADDITIONS = {
    "doc_reviews": [
        Column("feedback_version", Integer, nullable=False, server_default="0"),
        Column("decision_generation", Integer, nullable=False, server_default="1"),
        Column("pending_decision_id", Text, nullable=True),
    ],
    "doc_review_comments": [
        Column("source_discussion_id", Text, nullable=True),
        Column("responder_disposition", JSONB, nullable=True),
    ],
    "escalation_messages": [
        Column("ask_id", Text, nullable=True),
        Column("input_id", Text, nullable=True),
        Column("source_version", JSONB, nullable=True),
        Column("entry_kind", Text, nullable=False, server_default="discussion"),
        Column("reply_to_entry_id", Text, nullable=True),
    ],
    "escalation_actions": [
        Column("ask_id", Text, nullable=True),
        Column("decision_id", Text, nullable=True),
        Column("operation_kind", Text, nullable=True),
        Column("operation_key", Text, nullable=True),
        Column("dependency_keys", JSONB, nullable=False, server_default="[]"),
        Column("lease_owner", Text, nullable=True),
        Column("lease_expires_at", Float, nullable=True),
        Column("lease_generation", Integer, nullable=False, server_default="0"),
        Column("retry_at", Float, nullable=True),
        Column("receipt", JSONB, nullable=True),
    ],
}
CHECKS = {
    "doc_reviews": {
        "ck_doc_reviews_feedback_version": "feedback_version >= 0",
        "ck_doc_reviews_decision_generation": "decision_generation >= 1",
    },
    "escalation_messages": {
        "ck_escalation_messages_entry_kind": "entry_kind IN ('discussion','formal_comment',"
        "'decision_evidence','system_outcome','interpretation')",
    },
    "escalation_actions": {
        "ck_escalation_actions_kind": "action_kind IN ('question_answer','gate_resolve',"
        "'task_recover','ask_operation')",
        "ck_escalation_actions_status": "status IN ('pending','processing','succeeded',"
        "'failed','unknown','cancelled')",
        "ck_escalation_actions_completion": "(status IN ('pending','processing') AND "
        "completed_at IS NULL AND outcome IS NULL) OR (status IN "
        "('succeeded','failed','unknown','cancelled') AND completed_at IS NOT NULL "
        "AND outcome IS NOT NULL)",
        "ck_escalation_actions_ask_operation": "operation_kind IS NULL OR "
        "(ask_id IS NOT NULL AND operation_key IS NOT NULL)",
        "ck_escalation_actions_lease_generation": "lease_generation >= 0",
        "ck_escalation_actions_lease": "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
    },
}
FKS = (
    ("doc_reviews", "pending_decision_id", "ask_decisions", "fk_doc_reviews_pending_decision"),
    (
        "doc_review_comments",
        "source_discussion_id",
        "escalation_messages",
        "fk_doc_review_comments_discussion",
    ),
    ("escalation_messages", "ask_id", "human_asks", "fk_escalation_messages_ask"),
    (
        "escalation_messages",
        "reply_to_entry_id",
        "escalation_messages",
        "fk_escalation_messages_reply_to",
    ),
    ("escalation_actions", "ask_id", "human_asks", "fk_escalation_actions_ask"),
    ("escalation_actions", "decision_id", "ask_decisions", "fk_escalation_actions_decision"),
)


IMMUTABLE_SQL = """
CREATE OR REPLACE FUNCTION human_ask_immutable_v1() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'human ask audit records are immutable' USING ERRCODE = '23514';
END;
$$
"""


def install_ask_audit_guards(bind) -> None:
    bind.exec_driver_sql(IMMUTABLE_SQL)
    for table in ("ask_decisions", "approved_document_snapshots"):
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS tr_{table}_immutable ON {table}")
        bind.exec_driver_sql(
            f"CREATE TRIGGER tr_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION human_ask_immutable_v1()"
        )


def upgrade() -> None:
    bind = op.get_bind()
    for table in NEW_TABLES:
        table.create(bind, checkfirst=True)
    for table, columns in ADDITIONS.items():
        existing = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column._copy())
    for table, checks in CHECKS.items():
        existing = {c["name"] for c in sa.inspect(bind).get_check_constraints(table)}
        for name, expression in checks.items():
            if name in existing:
                op.drop_constraint(name, table, type_="check")
            op.create_check_constraint(name, table, expression)
    for table, column, target, name in FKS:
        existing = sa.inspect(bind).get_foreign_keys(table)
        if not any(fk["constrained_columns"] == [column] for fk in existing):
            op.create_foreign_key(name, table, target, [column], ["id"], ondelete="RESTRICT")
    for table in ("escalation_messages", "escalation_actions"):
        op.alter_column(table, "escalation_id", existing_type=Text(), nullable=True)
        for fk in sa.inspect(bind).get_foreign_keys(table):
            if fk["constrained_columns"] == ["escalation_id"]:
                op.drop_constraint(fk["name"], table, type_="foreignkey")
        op.create_foreign_key(
            f"fk_{table}_incident",
            table,
            "escalations",
            ["escalation_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.alter_column("escalation_actions", "reply_id", existing_type=Text(), nullable=True)
    if "uq_escalation_actions_operation" not in {
        c["name"] for c in sa.inspect(bind).get_unique_constraints("escalation_actions")
    }:
        op.create_unique_constraint(
            "uq_escalation_actions_operation", "escalation_actions", ["ask_id", "operation_key"]
        )
    for table, name, columns in (
        ("escalation_messages", "idx_escalation_messages_ask", ["ask_id", "created_at", "id"]),
        (
            "escalation_actions",
            "idx_escalation_actions_ready",
            ["status", "retry_at", "created_at"],
        ),
    ):
        if name not in {i["name"] for i in sa.inspect(bind).get_indexes(table)}:
            op.create_index(name, table, columns)

    install_ask_audit_guards(bind)


def downgrade() -> None:
    # A rollback must not erase human evidence or reactivate legacy publishers.
    # Deliberately no flag that a worker/caller can use as an export attestation.
    bind = op.get_bind()
    for table in NEW_TABLES:
        if (
            sa.inspect(bind).has_table(table.name)
            and bind.execute(sa.text(f'SELECT 1 FROM "{table.name}" LIMIT 1')).first()
        ):
            raise RuntimeError(
                "Unsafe human ask downgrade: retained ask/decision evidence; "
                "a verified forward-compatible reader/export is required"
            )
    for table in ("escalation_messages", "escalation_actions"):
        if bind.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE escalation_id IS NULL LIMIT 1")
        ).first():
            raise RuntimeError("Unsafe human ask downgrade: retained correspondence/operations")
    if bind.execute(
        sa.text(
            "SELECT 1 FROM escalation_actions WHERE reply_id IS NULL "
            "OR status NOT IN ('processing','succeeded','failed') LIMIT 1"
        )
    ).first():
        raise RuntimeError("Unsafe human ask downgrade: unreconciled operations")
    for table, column, _target, _name in reversed(FKS):
        for fk in sa.inspect(bind).get_foreign_keys(table):
            if fk["constrained_columns"] == [column]:
                op.drop_constraint(fk["name"], table, type_="foreignkey")
    for table in reversed(NEW_TABLES):
        table.drop(bind, checkfirst=True)
    for table, checks in CHECKS.items():
        for name in checks:
            op.drop_constraint(name, table, type_="check")
    op.drop_constraint("uq_escalation_actions_operation", "escalation_actions", type_="unique")
    op.drop_index("idx_escalation_messages_ask", table_name="escalation_messages")
    op.drop_index("idx_escalation_actions_ready", table_name="escalation_actions")
    for table, columns in ADDITIONS.items():
        for column in reversed(columns):
            op.drop_column(table, column.name)
    for table in ("escalation_messages", "escalation_actions"):
        op.drop_constraint(f"fk_{table}_incident", table, type_="foreignkey")
        op.create_foreign_key(
            f"{table}_escalation_id_fkey",
            table,
            "escalations",
            ["escalation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.alter_column(table, "escalation_id", existing_type=Text(), nullable=False)
    op.alter_column("escalation_actions", "reply_id", existing_type=Text(), nullable=False)
    op.create_check_constraint(
        "ck_escalation_actions_kind",
        "escalation_actions",
        "action_kind IN ('question_answer','gate_resolve','task_recover')",
    )
    op.create_check_constraint(
        "ck_escalation_actions_status",
        "escalation_actions",
        "status IN ('processing','succeeded','failed')",
    )
    op.create_check_constraint(
        "ck_escalation_actions_completion",
        "escalation_actions",
        "(status = 'processing' AND completed_at IS NULL AND outcome IS NULL) "
        "OR (status <> 'processing' AND completed_at IS NOT NULL "
        "AND outcome IS NOT NULL)",
    )

    op.execute("DROP FUNCTION IF EXISTS human_ask_immutable_v1()")
