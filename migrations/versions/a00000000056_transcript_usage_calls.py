"""Durable Claude API-call usage maxima, preserving the original ledger.

Revision ID: a00000000056
Revises: a00000000055
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000056"
down_revision = "a00000000055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("transcript_usage_calls"):
        op.create_table(
            "transcript_usage_calls",
            sa.Column("usage_key", sa.Text(), primary_key=True),
            sa.Column("first_ledger_id", sa.Text(), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=False),
            sa.Column("output_tokens", sa.Integer(), nullable=False),
            sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.CheckConstraint(
                "input_tokens >= 0 AND output_tokens >= 0 "
                "AND cache_read_tokens >= 0 AND cache_write_tokens >= 0",
                name="ck_transcript_usage_calls_nonnegative",
            ),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("token_ledger")}
    if "idx_token_ledger_call_id" not in indexes:
        op.create_index("idx_token_ledger_call_id", "token_ledger", ["call_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("transcript_usage_calls"):
        op.drop_table("transcript_usage_calls")
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("token_ledger")}
    if "idx_token_ledger_call_id" in indexes:
        op.drop_index("idx_token_ledger_call_id", table_name="token_ledger")
