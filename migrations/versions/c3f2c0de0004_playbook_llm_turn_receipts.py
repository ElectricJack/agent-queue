"""Allow ordered per-turn receipts for Playbook V2 LLM attempts.

Revision ID: c3f2c0de0004
Revises: c52f1a4fb6ba
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c3f2c0de0004"
down_revision: str | Sequence[str] | None = "c52f1a4fb6ba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("playbook_step_receipts") as batch:
        batch.drop_constraint("uq_playbook_step_receipts_attempt", type_="unique")
        batch.add_column(
            sa.Column("receipt_kind", sa.Text(), nullable=False, server_default="step")
        )
        batch.add_column(
            sa.Column("turn_index", sa.Integer(), nullable=False, server_default="-1")
        )
        batch.add_column(sa.Column("operator_decision_id", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_playbook_step_receipts_kind",
            "receipt_kind IN ('step', 'tool_turn', 'interrupted', 'operator_decision')",
        )
        batch.create_check_constraint(
            "ck_playbook_step_receipts_turn_index",
            "(receipt_kind = 'step' AND turn_index = -1) OR "
            "(receipt_kind <> 'step' AND turn_index >= 0)",
        )
        batch.create_check_constraint(
            "ck_playbook_step_receipts_decision_ref",
            "(receipt_kind IN ('interrupted', 'operator_decision') AND "
            "operator_decision_id IS NOT NULL) OR "
            "(receipt_kind NOT IN ('interrupted', 'operator_decision') AND "
            "operator_decision_id IS NULL)",
        )
        batch.create_unique_constraint(
            "uq_playbook_step_receipts_boundary",
            [
                "run_id",
                "step_id",
                "iteration",
                "attempt",
                "turn_index",
                "receipt_kind",
            ],
        )
    op.create_index(
        "idx_playbook_step_receipts_turn",
        "playbook_step_receipts",
        ["run_id", "step_id", "iteration", "attempt", "turn_index"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    expanded = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM playbook_step_receipts "
            "WHERE receipt_kind <> 'step' OR turn_index <> -1"
        )
    ).scalar_one()
    if expanded:
        raise RuntimeError(
            "cannot downgrade while per-turn playbook receipts exist; "
            "retain or explicitly remove them first"
        )

    op.drop_index("idx_playbook_step_receipts_turn", table_name="playbook_step_receipts")
    with op.batch_alter_table("playbook_step_receipts") as batch:
        batch.drop_constraint("uq_playbook_step_receipts_boundary", type_="unique")
        batch.drop_constraint("ck_playbook_step_receipts_decision_ref", type_="check")
        batch.drop_constraint("ck_playbook_step_receipts_turn_index", type_="check")
        batch.drop_constraint("ck_playbook_step_receipts_kind", type_="check")
        batch.create_unique_constraint(
            "uq_playbook_step_receipts_attempt",
            ["run_id", "step_id", "iteration", "attempt"],
        )
        batch.drop_column("operator_decision_id")
        batch.drop_column("turn_index")
        batch.drop_column("receipt_kind")
