"""Permanent exact anti-replay fences for archived transcript usage calls."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000122"
down_revision = "a00000000121"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    call_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("transcript_usage_calls")}
    if "idx_transcript_usage_calls_updated" not in call_indexes:
        op.create_index("idx_transcript_usage_calls_updated", "transcript_usage_calls",
                        ["updated_at", "usage_key"])
    if not sa.inspect(bind).has_table("transcript_usage_fences"):
        op.create_table(
            "transcript_usage_fences",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("usage_keys", JSONB(), nullable=False),
            sa.Column("archived_at", sa.Float(), nullable=False),
            sa.Column("archive_path", sa.Text(), nullable=False),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("transcript_usage_fences")}
    if "idx_transcript_usage_fences_keys" not in indexes:
        op.create_index(
            "idx_transcript_usage_fences_keys", "transcript_usage_fences", ["usage_keys"],
            postgresql_using="gin", postgresql_ops={"usage_keys": "jsonb_path_ops"},
        )


def downgrade():
    # Removing a populated fence would allow deleted calls to be charged again.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("transcript_usage_fences"):
        if bind.execute(sa.text("SELECT 1 FROM transcript_usage_fences LIMIT 1")).first():
            raise RuntimeError("Cannot discard transcript replay fences; restore archived calls first")
        op.drop_table("transcript_usage_fences")
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("transcript_usage_calls")}
    if "idx_transcript_usage_calls_updated" in indexes:
        op.drop_index("idx_transcript_usage_calls_updated", table_name="transcript_usage_calls")
