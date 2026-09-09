"""Keep an audited remote observation for legacy resolution recovery.

Revision ID: a00000000006
Revises: a00000000005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a00000000006"
down_revision = "a00000000005"
branch_labels = None
depends_on = None


_CONSTRAINT = "ck_integration_promotion_intents_resolution_binding"
_CURRENT_BINDING = "(resolution_head_sha IS NULL AND resolution_tree_sha IS NULL AND resolution_commit_shas IS NULL AND resolution_operation_id IS NULL AND resolution_stage_ordinal IS NULL AND resolution_task_id IS NULL AND resolution_session_id IS NULL AND resolution_session_instance_token IS NULL AND resolution_workspace_id IS NULL AND resolution_fence_owner_id IS NULL AND resolution_fence_token IS NULL AND resolution_push_started_at IS NULL AND resolution_recovery_evidence IS NULL AND resolution_push_evidence IS NULL) OR (resolution_head_sha IS NOT NULL AND resolution_tree_sha IS NOT NULL AND resolution_commit_shas IS NOT NULL AND resolution_operation_id IS NOT NULL AND resolution_stage_ordinal IS NOT NULL AND resolution_task_id IS NOT NULL AND resolution_session_id IS NOT NULL AND resolution_session_instance_token IS NOT NULL AND resolution_workspace_id IS NOT NULL AND resolution_fence_owner_id IS NOT NULL AND resolution_fence_token IS NOT NULL AND state IN ('resolution_reserved', 'committed'))"
_PREVIOUS_BINDING = "(resolution_head_sha IS NULL AND resolution_tree_sha IS NULL AND resolution_commit_shas IS NULL AND resolution_operation_id IS NULL AND resolution_stage_ordinal IS NULL AND resolution_task_id IS NULL AND resolution_session_id IS NULL AND resolution_session_instance_token IS NULL AND resolution_workspace_id IS NULL AND resolution_fence_owner_id IS NULL AND resolution_fence_token IS NULL AND resolution_push_evidence IS NULL) OR (resolution_head_sha IS NOT NULL AND resolution_tree_sha IS NOT NULL AND resolution_commit_shas IS NOT NULL AND resolution_operation_id IS NOT NULL AND resolution_stage_ordinal IS NOT NULL AND resolution_task_id IS NOT NULL AND resolution_session_id IS NOT NULL AND resolution_session_instance_token IS NOT NULL AND resolution_workspace_id IS NOT NULL AND resolution_fence_owner_id IS NOT NULL AND resolution_fence_token IS NOT NULL AND state IN ('resolution_reserved', 'committed'))"


def _replace_binding(expression: str) -> None:
    op.drop_constraint(_CONSTRAINT, "integration_promotion_intents", type_="check")
    op.create_check_constraint(_CONSTRAINT, "integration_promotion_intents", expression)


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("integration_promotion_intents")
    }
    if "resolution_recovery_evidence" not in columns:
        op.add_column(
            "integration_promotion_intents",
            sa.Column("resolution_recovery_evidence", sa.JSON(), nullable=True),
        )

    _replace_binding(_CURRENT_BINDING)


def downgrade() -> None:
    _replace_binding(_PREVIOUS_BINDING)
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("integration_promotion_intents")
    }
    if "resolution_recovery_evidence" in columns:
        op.drop_column("integration_promotion_intents", "resolution_recovery_evidence")
