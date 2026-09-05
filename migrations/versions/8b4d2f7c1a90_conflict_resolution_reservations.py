"""conflict resolution reservations

Revision ID: 8b4d2f7c1a90
Revises: 7a1d5e9f0b2c
Create Date: 2026-09-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "8b4d2f7c1a90"
down_revision: str | Sequence[str] | None = "7a1d5e9f0b2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RESOLUTION_COLUMNS = (
    "resolution_head_sha",
    "resolution_tree_sha",
    "resolution_commit_shas",
    "resolution_operation_id",
    "resolution_stage_ordinal",
    "resolution_task_id",
    "resolution_session_id",
    "resolution_session_instance_token",
    "resolution_workspace_id",
    "resolution_fence_owner_id",
    "resolution_fence_token",
    "resolution_push_evidence",
)


def upgrade() -> None:
    with op.batch_alter_table("integration_promotion_intents") as batch_op:
        batch_op.drop_constraint("ck_integration_promotion_intents_state", type_="check")
        batch_op.add_column(sa.Column("resolution_head_sha", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolution_tree_sha", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolution_commit_shas", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("resolution_operation_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolution_stage_ordinal", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("resolution_task_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolution_session_id", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("resolution_session_instance_token", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("resolution_workspace_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolution_fence_owner_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolution_fence_token", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("resolution_push_evidence", sa.JSON(), nullable=True))
        batch_op.create_check_constraint(
            "ck_integration_promotion_intents_state",
            "state IN ('reserved', 'prepared', 'pushed', 'reconciled', 'committed', "
            "'conflict', 'resolution_reserved')",
        )
        batch_op.create_check_constraint(
            "ck_integration_promotion_intents_resolution_binding",
            "(resolution_head_sha IS NULL AND resolution_tree_sha IS NULL AND "
            "resolution_commit_shas IS NULL AND resolution_operation_id IS NULL AND "
            "resolution_stage_ordinal IS NULL AND resolution_task_id IS NULL AND "
            "resolution_session_id IS NULL AND resolution_session_instance_token IS NULL AND "
            "resolution_workspace_id IS NULL AND resolution_fence_owner_id IS NULL AND "
            "resolution_fence_token IS NULL AND resolution_push_evidence IS NULL) OR "
            "(resolution_head_sha IS NOT NULL AND resolution_tree_sha IS NOT NULL AND "
            "resolution_commit_shas IS NOT NULL AND resolution_operation_id IS NOT NULL AND "
            "resolution_stage_ordinal IS NOT NULL AND resolution_task_id IS NOT NULL AND "
            "resolution_session_id IS NOT NULL AND resolution_session_instance_token IS NOT NULL "
            "AND resolution_workspace_id IS NOT NULL AND resolution_fence_owner_id IS NOT NULL "
            "AND resolution_fence_token IS NOT NULL AND state IN "
            "('resolution_reserved', 'committed'))",
        )
        batch_op.create_check_constraint(
            "ck_integration_promotion_intents_resolution_stage",
            "resolution_stage_ordinal IS NULL OR resolution_stage_ordinal >= 0",
        )
        batch_op.create_check_constraint(
            "ck_integration_promotion_intents_resolution_fence",
            "resolution_fence_token IS NULL OR resolution_fence_token >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("integration_promotion_intents") as batch_op:
        batch_op.drop_constraint(
            "ck_integration_promotion_intents_resolution_fence", type_="check"
        )
        batch_op.drop_constraint(
            "ck_integration_promotion_intents_resolution_stage", type_="check"
        )
        batch_op.drop_constraint(
            "ck_integration_promotion_intents_resolution_binding", type_="check"
        )
        batch_op.drop_constraint("ck_integration_promotion_intents_state", type_="check")
        for column in reversed(_RESOLUTION_COLUMNS):
            batch_op.drop_column(column)
        batch_op.create_check_constraint(
            "ck_integration_promotion_intents_state",
            "state IN ('reserved', 'prepared', 'pushed', 'reconciled', 'committed', 'conflict')",
        )
