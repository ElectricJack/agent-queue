"""Add epic dependency edges, settling windows, and GitHub reviewer identity.

Revision ID: a0000000001b
Revises: a0000000001a

The squashed baseline creates from live metadata, so a fresh database already
has these changes when this revision runs. An existing database may not.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a0000000001b"
down_revision = "a0000000001a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import epic_dependencies

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "epic_dependencies" not in inspector.get_table_names():
        epic_dependencies.create(bind, checkfirst=True)

    schedule_columns = {
        column["name"] for column in inspector.get_columns("project_integration_schedules")
    }
    for name in ("settling_first_approval_at", "settling_fires_at"):
        if name not in schedule_columns:
            op.add_column("project_integration_schedules", sa.Column(name, sa.Float(), nullable=True))

    review_columns = {
        column["name"]: column
        for column in inspector.get_columns("integration_review_evidence")
    }
    if "reviewer_identity" not in review_columns:
        op.add_column(
            "integration_review_evidence",
            sa.Column("reviewer_identity", sa.Text(), nullable=True),
        )
    if not review_columns["reviewer_task_id"]["nullable"]:
        op.alter_column(
            "integration_review_evidence", "reviewer_task_id", existing_type=sa.Text(), nullable=True
        )


def downgrade() -> None:
    op.drop_column("project_integration_schedules", "settling_fires_at")
    op.drop_column("project_integration_schedules", "settling_first_approval_at")
    op.drop_table("epic_dependencies")
