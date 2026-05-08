"""add suppressed_by column to chat_analyzer_suggestions

Revision ID: df84af990b86
Revises: e4f2a8b1d6c9
Create Date: 2026-05-08 10:59:56.775884

OPERATIONAL NOTE
================

Phase 8 of the chat-analyzer suggestion-quality overhaul: every gate that
suppresses a suggestion (confidence, dedup, in-flight, dismiss-cooldown)
should now insert a row with ``status="suppressed"`` and
``suppressed_by=<gate>`` instead of returning silently. That makes
"how often did each gate fire?" a single SQL aggregation rather than a
log-grep.

The column is nullable — existing rows (and every non-suppressed row
going forward) carry ``NULL``. The accompanying index speeds up the
``COUNT(*) GROUP BY suppressed_by`` query that backs the
``get_chat_analyzer_metrics`` admin command.

Idempotent: inspects the schema first so re-running on a database that
already has the column (e.g. one re-stamped to head from a partial
state) is a no-op rather than an error.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'df84af990b86'
down_revision: Union[str, Sequence[str], None] = 'e4f2a8b1d6c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, col: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return col in {c["name"] for c in inspector.get_columns(table)}


def _has_index(table: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    """Upgrade schema."""
    if not _has_column("chat_analyzer_suggestions", "suppressed_by"):
        with op.batch_alter_table("chat_analyzer_suggestions", schema=None) as batch_op:
            batch_op.add_column(sa.Column("suppressed_by", sa.Text(), nullable=True))
    if not _has_index("chat_analyzer_suggestions", "idx_chat_analyzer_suppressed_by"):
        with op.batch_alter_table("chat_analyzer_suggestions", schema=None) as batch_op:
            batch_op.create_index(
                "idx_chat_analyzer_suppressed_by", ["suppressed_by"], unique=False
            )


def downgrade() -> None:
    """Downgrade schema."""
    if _has_index("chat_analyzer_suggestions", "idx_chat_analyzer_suppressed_by"):
        with op.batch_alter_table("chat_analyzer_suggestions", schema=None) as batch_op:
            batch_op.drop_index("idx_chat_analyzer_suppressed_by")
    if _has_column("chat_analyzer_suggestions", "suppressed_by"):
        with op.batch_alter_table("chat_analyzer_suggestions", schema=None) as batch_op:
            batch_op.drop_column("suppressed_by")
