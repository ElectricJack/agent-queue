"""add metrics_samples plus the two indexes the sampler range-scans

Revision ID: d3e7b1c9a204
Revises: 3b560dbd527c
Create Date: 2026-09-01

``metrics_samples`` backs the dashboard Metrics tab: one row per
(resolution, bucket), payload carrying the JSON sample body.

The two extra indexes are not incidental.  The sampler reads a trailing
window from ``token_ledger`` (tokens/minute) and ``task_completion_records``
(completions and PRs per hour) on every slow tick.  Both tables are
append-only and unbounded, and neither had an index on the timestamp column
those windows filter on — without them the "cheap sampler" requirement turns
into a full scan of the ledger every few seconds.
"""

from alembic import op
import sqlalchemy as sa


revision = "d3e7b1c9a204"
down_revision = "3b560dbd527c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metrics_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("bucket_ts", sa.Float(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resolution", "bucket_ts", name="uq_metrics_samples_bucket"),
    )
    op.create_index(
        "idx_metrics_samples_res_ts", "metrics_samples", ["resolution", "bucket_ts"]
    )
    op.create_index("idx_token_ledger_timestamp", "token_ledger", ["timestamp"])
    op.create_index(
        "idx_task_completion_records_completed_at",
        "task_completion_records",
        ["completed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_task_completion_records_completed_at", table_name="task_completion_records"
    )
    op.drop_index("idx_token_ledger_timestamp", table_name="token_ledger")
    op.drop_index("idx_metrics_samples_res_ts", table_name="metrics_samples")
    op.drop_table("metrics_samples")
