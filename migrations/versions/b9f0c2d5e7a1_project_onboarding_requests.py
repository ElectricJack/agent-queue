"""Add durable project-onboarding idempotency records.

Revision ID: b9f0c2d5e7a1
Revises: e6a1b2c3d4f5
Create Date: 2026-09-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b9f0c2d5e7a1"
down_revision = "e6a1b2c3d4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_onboarding_requests",
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("input_fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("phase", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_resources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')",
            name="ck_project_onboarding_requests_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND finished_at IS NULL) "
            "OR (status IN ('succeeded', 'failed') AND finished_at IS NOT NULL)",
            name="ck_project_onboarding_requests_terminal_timestamp",
        ),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "idx_project_onboarding_requests_finished",
        "project_onboarding_requests",
        ["status", "finished_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_project_onboarding_requests_finished", table_name="project_onboarding_requests"
    )
    op.drop_table("project_onboarding_requests")
