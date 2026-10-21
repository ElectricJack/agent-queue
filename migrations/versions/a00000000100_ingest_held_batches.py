"""Held spec-ingestion batches, review receipts and delivery conditions.

Frozen definitions: the live-metadata baseline already includes these objects,
so additions are inspect-guarded. No operator database is touched by workers.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000100"
down_revision = "a00000000099"
branch_labels = None
depends_on = None

metadata = sa.MetaData()
for name in ("projects", "task_proposals"):
    Table(name, metadata, Column("id", Text, primary_key=True))

ingest_batches = Table(
    "ingest_batches",
    metadata,
    Column("batch_id", Text, ForeignKey("task_proposals.id"), primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("source_review_id", Text, nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("source_content_sha256", Text, nullable=False),
    Column("decision_id", Text, nullable=False),
    Column("design_review_id", Text, nullable=True),
    Column("design_revision", Integer, nullable=True),
    Column("design_content_sha256", Text, nullable=True),
    Column("lineage_kind", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("structure_revision", Integer, nullable=False, server_default="1"),
    Column("structure_digest", Text, nullable=False),
    Column("state", Text, nullable=False, server_default="held"),
    Column("check_results", JSONB(none_as_null=True), nullable=True),
    Column("reviewer_task_id", Text, nullable=True),
    Column("reviewer_session_id", Text, nullable=True),
    Column("reviewer_claim_epoch", Integer, nullable=True),
    Column("release_actor", Text, nullable=True),
    Column("release_revision", Integer, nullable=True),
    Column("release_digest", Text, nullable=True),
    Column("released_at", Float, nullable=True),
    Column("release_receipt", JSONB(none_as_null=True), nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "state IN ('held','reviewing','needs_scope','released','retired')",
        name="ck_ingest_batches_state",
    ),
    CheckConstraint(
        "lineage_kind IN ('resolved','none')",
        name="ck_ingest_batches_lineage_kind",
    ),
    CheckConstraint(
        "(design_review_id IS NULL AND design_revision IS NULL "
        "AND design_content_sha256 IS NULL) OR "
        "(design_review_id IS NOT NULL AND design_revision IS NOT NULL "
        "AND design_content_sha256 IS NOT NULL)",
        name="ck_ingest_batches_design_lineage",
    ),
    UniqueConstraint(
        "project_id", "source_review_id", "source_revision", "source_content_sha256",
        name="uq_ingest_batches_source",
    ),
)

ingest_batch_members = Table(
    "ingest_batch_members",
    metadata,
    Column("batch_id", Text, ForeignKey("ingest_batches.batch_id"), primary_key=True),
    Column("task_id", Text, primary_key=True),
    Column("member_key", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("section_anchor", Text, nullable=False),
    Column("primary_outcome", Text, nullable=False),
    Column("state", Text, nullable=False, server_default="active"),
    Column("retired_reason", Text, nullable=True),
    Column("created_revision", Integer, nullable=False),
    Column("retired_revision", Integer, nullable=True),
    CheckConstraint(
        "role IN ('container','leaf','operational_root')",
        name="ck_ingest_batch_members_role",
    ),
    CheckConstraint("state IN ('active','retired')", name="ck_ingest_batch_members_state"),
    UniqueConstraint("batch_id", "member_key", name="uq_ingest_batch_members_key"),
)

ingest_batch_receipts = Table(
    "ingest_batch_receipts",
    metadata,
    Column("receipt_id", Text, primary_key=True),
    Column("batch_id", Text, ForeignKey("ingest_batches.batch_id"), nullable=False),
    Column("structure_revision", Integer, nullable=False),
    Column("structure_digest", Text, nullable=False),
    Column("reviewer_task_id", Text, nullable=False),
    Column("reviewer_session_id", Text, nullable=False),
    Column("passing", Boolean, nullable=False, server_default=false()),
    Column("section_verdicts", JSONB(none_as_null=True), nullable=False),
    Column("reverse_coverage_verdict", Text, nullable=False),
    Column("walkthrough_verdict", Text, nullable=False),
    Column("structural_findings", JSONB(none_as_null=True), nullable=False),
    Column("created_at", Float, nullable=False),
    Index(
        "uq_ingest_batch_receipts_passing", "batch_id", "structure_revision",
        unique=True, postgresql_where=text("passing"),
    ),
)

NEW_TABLES = (ingest_batches, ingest_batch_members, ingest_batch_receipts)
PREVIOUS_PROPOSAL_CHECK = "status IN ('draft', 'ready', 'committed', 'discarded')"
PROPOSAL_CHECK = "status IN ('draft', 'ready', 'held', 'committed', 'discarded')"
PREVIOUS_DEP_CHECK = (
    "dep_type IN ('blocks', 'parent-child', 'waits-for', 'conditional-blocks', "
    "'discovered-from', 'related', 'duplicates', 'supersedes')"
)
DEP_CHECK = (
    "dep_type IN ('blocks', 'parent-child', 'waits-for', 'conditional-blocks', "
    "'delivery-of', 'discovered-from', 'related', 'duplicates', 'supersedes')"
)


def _replace_check(table: str, name: str, expression: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return
    if name in {check["name"] for check in inspector.get_check_constraints(table)}:
        op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expression)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("tasks"):
        if "held_batch_id" not in {c["name"] for c in inspector.get_columns("tasks")}:
            op.add_column("tasks", sa.Column("held_batch_id", sa.Text(), nullable=True))
        if "ix_tasks_held_batch_id" not in {
            index["name"] for index in sa.inspect(bind).get_indexes("tasks")
        }:
            op.create_index("ix_tasks_held_batch_id", "tasks", ["held_batch_id"])
    for table in NEW_TABLES:
        table.create(bind, checkfirst=True)
    _replace_check("task_proposals", "ck_task_proposals_status", PROPOSAL_CHECK)
    _replace_check("task_dependencies", "ck_task_deps_dep_type", DEP_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Check all refusal conditions before any DDL: downgrading a binary must
    # never make a pending fidelity review's graph executable.
    if inspector.has_table("ingest_batches") and bind.execute(
        sa.text("SELECT 1 FROM ingest_batches "
                "WHERE state IN ('held','reviewing','needs_scope') LIMIT 1")
    ).first():
        raise RuntimeError(
            "Unsafe held-ingestion downgrade: held/reviewing/needs_scope batches remain; "
            "release or explicitly retire them before downgrading"
        )
    if inspector.has_table("task_dependencies") and bind.execute(
        sa.text("SELECT 1 FROM task_dependencies WHERE dep_type = 'delivery-of' LIMIT 1")
    ).first():
        raise RuntimeError(
            "Unsafe held-ingestion downgrade: delivery-of edges remain; "
            "resolve their delivery obligations before downgrading"
        )
    if inspector.has_table("task_proposals") and bind.execute(
        sa.text("SELECT 1 FROM task_proposals WHERE status = 'held' LIMIT 1")
    ).first():
        raise RuntimeError("Unsafe held-ingestion downgrade: held proposals remain")
    if inspector.has_table("tasks"):
        columns = {c["name"] for c in inspector.get_columns("tasks")}
        if "held_batch_id" in columns and bind.execute(
            sa.text("SELECT 1 FROM tasks WHERE held_batch_id IS NOT NULL LIMIT 1")
        ).first():
            raise RuntimeError("Unsafe held-ingestion downgrade: tasks retain ingestion holds")
    _replace_check("task_dependencies", "ck_task_deps_dep_type", PREVIOUS_DEP_CHECK)
    _replace_check("task_proposals", "ck_task_proposals_status", PREVIOUS_PROPOSAL_CHECK)
    for table in reversed(NEW_TABLES):
        table.drop(bind, checkfirst=True)
    if inspector.has_table("tasks"):
        if "ix_tasks_held_batch_id" in {
            index["name"] for index in sa.inspect(bind).get_indexes("tasks")
        }:
            op.drop_index("ix_tasks_held_batch_id", table_name="tasks")
        if "held_batch_id" in {c["name"] for c in sa.inspect(bind).get_columns("tasks")}:
            op.drop_column("tasks", "held_batch_id")
