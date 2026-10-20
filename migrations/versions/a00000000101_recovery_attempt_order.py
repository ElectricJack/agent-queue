"""Keep the claim frontier's latest recovery-attempt lookup index ordered."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000101"
down_revision = "a00000000100"
branch_labels = None
depends_on = None

TABLE = "task_session_attempts"
INDEX = "idx_task_session_attempts_recovery"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(TABLE) and INDEX not in {
        index["name"] for index in inspector.get_indexes(TABLE)
    }:
        op.create_index(INDEX, TABLE, ["task_id", "project_id", "started_at", "id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(TABLE) and INDEX in {
        index["name"] for index in inspector.get_indexes(TABLE)
    }:
        op.drop_index(INDEX, table_name=TABLE)
