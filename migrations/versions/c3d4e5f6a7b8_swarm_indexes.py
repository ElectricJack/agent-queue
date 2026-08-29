"""swarm work model — long-poll and pool-supply indexes (revision C)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-29

"""

from typing import Sequence, Union

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_events_type_project_id", "events", ["event_type", "project_id", "id"])
    op.create_index(
        "idx_sessions_pool", "sessions", ["lifecycle", "project_id", "profile_id", "state"]
    )


def downgrade() -> None:
    op.drop_index("idx_sessions_pool", table_name="sessions")
    op.drop_index("idx_events_type_project_id", table_name="events")
