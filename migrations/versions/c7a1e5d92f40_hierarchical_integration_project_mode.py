"""Add the hierarchical-integration rollout mode and designated repository.

Revision ID: c7a1e5d92f40
Revises: b91e4d7a2c10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "c7a1e5d92f40"
down_revision: str | Sequence[str] | None = "b91e4d7a2c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column(
                "hierarchical_integration_mode",
                sa.Text(),
                nullable=False,
                server_default="disabled",
            )
        )
        batch.add_column(sa.Column("integration_repository_id", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_projects_hierarchical_integration_mode",
            "hierarchical_integration_mode IN ('disabled', 'observe', 'hierarchy', 'train')",
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("ck_projects_hierarchical_integration_mode", type_="check")
        batch.drop_column("integration_repository_id")
        batch.drop_column("hierarchical_integration_mode")
