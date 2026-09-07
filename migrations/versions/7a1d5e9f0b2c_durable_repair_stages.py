"""durable repair stages

Revision ID: 7a1d5e9f0b2c
Revises: e4c6a8b20d31
Create Date: 2026-09-05
"""

import importlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1d5e9f0b2c"
down_revision: str | Sequence[str] | None = "e4c6a8b20d31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_guards():
    return importlib.import_module(
        "migrations.versions.3f30b34c7e7c_hierarchical_integration_state"
    )


def upgrade() -> None:
    with op.batch_alter_table("integration_repair_operations") as batch_op:
        batch_op.create_unique_constraint(
            "uq_integration_repair_operations_batch_episode", ["batch_id"]
        )
    with op.batch_alter_table("integration_repair_stages") as batch_op:
        batch_op.drop_constraint("ck_integration_repair_stages_state", type_="check")
        batch_op.add_column(sa.Column("writer_kind", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("trigger_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("current_subject", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("deadline_event_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("success_subject", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("success_evidence_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("retained_workspace_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("retained_handoff", sa.JSON(), nullable=True))
        batch_op.alter_column(
            "intelligence_class", existing_type=sa.Text(), nullable=True
        )
    op.execute(
        "UPDATE integration_repair_stages SET writer_kind = 'repair_delegate' "
        "WHERE repair_task_id IS NOT NULL"
    )
    with op.batch_alter_table("integration_repair_stages") as batch_op:
        batch_op.create_check_constraint(
            "ck_integration_repair_stages_writer_kind",
            "writer_kind IS NULL OR writer_kind IN ('repair_delegate', 'existing_verifier')",
        )
        batch_op.create_check_constraint(
            "ck_integration_repair_stages_writer_binding",
            "(repair_task_id IS NULL AND writer_kind IS NULL) OR "
            "(repair_task_id IS NOT NULL AND writer_kind IS NOT NULL)",
        )
        batch_op.create_unique_constraint(
            "uq_integration_repair_stages_deadline_event", ["deadline_event_id"]
        )
        batch_op.create_check_constraint(
            "ck_integration_repair_stages_state",
            "state IN ('pending', 'active', 'awaiting_completion', 'passed', "
            "'failed', 'expired', 'cancelled')",
        )
    _base_guards()._recreate_sqlite_guards(
        "integration_repair_operations", "integration_repair_stages"
    )
    op.create_table(
        "integration_repair_stage_evidence",
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("evidence_id", sa.Text(), nullable=False),
        sa.Column(
            "counted_attempt", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("result_outcome", sa.Text(), nullable=False),
        sa.Column("result_action", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["integration_check_evidence.id"],
            name="fk_integration_repair_stage_evidence_evidence",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "ordinal"],
            ["integration_repair_stages.operation_id", "integration_repair_stages.ordinal"],
            name="fk_integration_repair_stage_evidence_stage",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "operation_id",
            "ordinal",
            "evidence_id",
            name="pk_integration_repair_stage_evidence",
        ),
        sa.UniqueConstraint(
            "evidence_id", name="uq_integration_repair_stage_evidence_evidence"
        ),
    )


def downgrade() -> None:
    op.drop_table("integration_repair_stage_evidence")
    op.execute(
        "UPDATE integration_repair_stages SET intelligence_class = '' "
        "WHERE intelligence_class IS NULL"
    )
    with op.batch_alter_table("integration_repair_stages") as batch_op:
        batch_op.drop_constraint("ck_integration_repair_stages_state", type_="check")
        batch_op.drop_constraint(
            "uq_integration_repair_stages_deadline_event", type_="unique"
        )
        batch_op.drop_constraint(
            "ck_integration_repair_stages_writer_binding", type_="check"
        )
        batch_op.drop_constraint(
            "ck_integration_repair_stages_writer_kind", type_="check"
        )
        batch_op.alter_column(
            "intelligence_class", existing_type=sa.Text(), nullable=False
        )
        batch_op.drop_column("success_evidence_id")
        batch_op.drop_column("success_subject")
        batch_op.drop_column("retained_handoff")
        batch_op.drop_column("retained_workspace_id")
        batch_op.drop_column("deadline_event_id")
        batch_op.drop_column("current_subject")
        batch_op.drop_column("trigger_id")
        batch_op.drop_column("writer_kind")
        batch_op.create_check_constraint(
            "ck_integration_repair_stages_state",
            "state IN ('pending', 'active', 'passed', 'failed', 'expired', 'cancelled')",
        )
    with op.batch_alter_table("integration_repair_operations") as batch_op:
        batch_op.drop_constraint(
            "uq_integration_repair_operations_batch_episode", type_="unique"
        )
    _base_guards()._recreate_sqlite_guards(
        "integration_repair_operations", "integration_repair_stages"
    )
