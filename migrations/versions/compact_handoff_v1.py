"""Durable handoff ordering and optional per-claim retry identity.

Revision ID: compact_handoff_v1
Revises: a00000000025

Applied only by daemon/operator upgrade, or disposable migration tests.
The baseline creates current metadata, so all operations tolerate existing columns.
"""

import json
import math

import sqlalchemy as sa
from alembic import op

revision = "compact_handoff_v1"
down_revision = "a00000000025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("task_context")}
    if "created_at" not in columns:
        op.add_column(
            "task_context", sa.Column("created_at", sa.Float(), nullable=False, server_default="0")
        )
        # Old handoffs have a server-written ts in JSON. Other legacy rows
        # have no provable time; zero + id is deterministic, never guessed order.
        rows = bind.execute(
            sa.text("SELECT id, content FROM task_context WHERE type = 'handoff'")
        ).fetchall()
        for ident, content in rows:
            try:
                stamp = float(json.loads(content).get("ts", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            if math.isfinite(stamp) and stamp >= 0:
                bind.execute(
                    sa.text("UPDATE task_context SET created_at=:ts WHERE id=:id"),
                    {"ts": stamp, "id": ident},
                )
        op.alter_column(
            "task_context",
            "created_at",
            server_default=sa.text("EXTRACT(EPOCH FROM clock_timestamp())"),
        )
    for name, datatype in (("claim_epoch", sa.BigInteger()), ("idempotency_key", sa.Text())):
        if name not in columns:
            op.add_column("task_context", sa.Column(name, datatype, nullable=True))
    if "idx_task_context_latest" not in {i["name"] for i in inspector.get_indexes("task_context")}:
        op.create_index(
            "idx_task_context_latest", "task_context", ["task_id", "type", "created_at", "id"]
        )
    if "uq_task_context_handoff_retry" not in {
        c["name"] for c in inspector.get_unique_constraints("task_context")
    }:
        op.create_unique_constraint(
            "uq_task_context_handoff_retry",
            "task_context",
            ["task_id", "claim_epoch", "idempotency_key"],
        )


def downgrade() -> None:
    op.drop_constraint("uq_task_context_handoff_retry", "task_context", type_="unique")
    op.drop_index("idx_task_context_latest", table_name="task_context")
    for name in ("idempotency_key", "claim_epoch", "created_at"):
        op.drop_column("task_context", name)
