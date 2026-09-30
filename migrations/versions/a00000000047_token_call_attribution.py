"""Capture token-ledger session, attempt and call attribution at ingest.

Revision ID: a00000000047
Revises: a00000000046
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000047"
down_revision = "a00000000046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("token_ledger")}
    for name in ("session_id", "attempt_id", "call_id", "model_source"):
        if name not in columns:
            op.add_column("token_ledger", sa.Column(name, sa.Text(), nullable=True))
    if "route" not in {column["name"] for column in inspector.get_columns("archived_tasks")}:
        op.add_column("archived_tasks", sa.Column("route", JSONB(), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("token_ledger")}
    if "idx_token_ledger_task_attempt" not in indexes:
        op.create_index("idx_token_ledger_task_attempt", "token_ledger", ["task_id", "attempt_id"])
    if "uq_token_ledger_call" not in indexes:
        op.create_index(
            "uq_token_ledger_call", "token_ledger", ["session_id", "call_id"], unique=True
        )
    if not sa.inspect(bind).has_table("benchmark_stage_spans"):
        op.create_table(
            "benchmark_stage_spans",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column(
                "project_id",
                sa.Text(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("task_id", sa.Text(), nullable=False),
            sa.Column("session_attempt_id", sa.Text(), nullable=True),
            sa.Column("stage", sa.Text(), nullable=False),
            sa.Column("started_monotonic_ns", sa.BigInteger(), nullable=False),
            sa.Column("ended_monotonic_ns", sa.BigInteger(), nullable=False),
            sa.Column("duration_ms", sa.Float(), nullable=False),
            sa.Column("recorded_at", sa.Float(), nullable=False),
        )
    span_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("benchmark_stage_spans")
    }
    if "idx_benchmark_stage_task" not in span_indexes:
        op.create_index(
            "idx_benchmark_stage_task", "benchmark_stage_spans", ["task_id", "session_attempt_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("benchmark_stage_spans"):
        op.drop_table("benchmark_stage_spans")
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("token_ledger")}
    for name in ("uq_token_ledger_call", "idx_token_ledger_task_attempt"):
        if name in indexes:
            op.drop_index(name, table_name="token_ledger")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("token_ledger")}
    for name in ("model_source", "call_id", "attempt_id", "session_id"):
        if name in columns:
            op.drop_column("token_ledger", name)
    if "route" in {column["name"] for column in sa.inspect(bind).get_columns("archived_tasks")}:
        op.drop_column("archived_tasks", "route")
