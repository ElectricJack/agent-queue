"""Keep latest-attempt recovery reads in index order.

Revision ID: a00000000095
Revises: a00000000094
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000095"
down_revision = "a00000000094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("task_session_attempts") and "idx_task_session_attempts_latest" not in {
        index["name"] for index in inspector.get_indexes("task_session_attempts")
    }:
        op.create_index(
            "idx_task_session_attempts_latest", "task_session_attempts", ["task_id", "started_at", "id"]
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("task_session_attempts") and "idx_task_session_attempts_latest" in {
        index["name"] for index in inspector.get_indexes("task_session_attempts")
    }:
        op.drop_index("idx_task_session_attempts_latest", table_name="task_session_attempts")
