"""Add parent collection episodes, evidence links, and frozen policy pins.

Revision ID: e4c6a8b20d31
Revises: c7a1e5d92f40
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "e4c6a8b20d31"
down_revision: str | Sequence[str] | None = "c7a1e5d92f40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_APPEND_ONLY_TABLES = (
    "integration_check_evidence",
    "integration_parent_episodes",
    "integration_parent_verifications",
    "integration_parent_operation_completions",
    "integration_parent_verification_evidence",
    "integration_episode_receipt_acceptances",
)


def _create_append_only_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION integration_parent_audit_append_only() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'integration parent evidence is append-only'; END; "
            "$$ LANGUAGE plpgsql"
        )
        for table in _APPEND_ONLY_TABLES:
            for event in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER trg_{table}_{event.lower()} BEFORE {event} ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()"
                )
        return
    for table in _APPEND_ONLY_TABLES:
        for event in ("update", "delete"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_{event} BEFORE {event.upper()} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'integration parent evidence is append-only'); END"
            )


def _drop_append_only_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in _APPEND_ONLY_TABLES:
            for event in ("update", "delete"):
                op.execute(f"DROP TRIGGER trg_{table}_{event} ON {table}")
        op.execute("DROP FUNCTION integration_parent_audit_append_only()")
        return
    for table in _APPEND_ONLY_TABLES:
        for event in ("update", "delete"):
            op.execute(f"DROP TRIGGER trg_{table}_{event}")


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("hierarchical_integration_policy", sa.JSON(), nullable=True)
    )
    op.add_column("task_integration_checkpoints", sa.Column("episode_id", sa.Text()))
    op.add_column(
        "task_integration_checkpoints", sa.Column("current_verification_id", sa.Text())
    )
    op.add_column(
        "task_integration_checkpoints",
        sa.Column("last_completed_operation_id", sa.Text()),
    )
    op.add_column(
        "task_integration_checkpoints",
        sa.Column("last_completed_verification_id", sa.Text()),
    )
    op.add_column(
        "task_delivery_receipts", sa.Column("disposition_revision", sa.Integer())
    )
    op.add_column("task_delivery_receipts", sa.Column("parent_operation_id", sa.Text()))
    op.add_column("task_delivery_receipts", sa.Column("parent_episode_id", sa.Text()))
    for name in (
        "verifier_task_id",
        "route_playbook_id",
        "route_scope",
        "route_scope_identifier",
        "route_activation_id",
    ):
        op.add_column("integration_repair_operations", sa.Column(name, sa.Text()))
    op.create_index(
        "uq_integration_repair_operations_parent_episode",
        "integration_repair_operations",
        ["parent_task_id", "episode_id"],
        unique=True,
    )

    op.create_table(
        "integration_parent_episodes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("parent_task_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("pre_collection_checkpoint_sha", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "generation >= 0", name="ck_integration_parent_episodes_generation"
        ),
        sa.UniqueConstraint(
            "parent_task_id", "id", name="uq_integration_parent_episodes_parent_id"
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"], ["tasks.id"],
            name="fk_integration_parent_episodes_parent_task", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["repos.id"],
            name="fk_integration_parent_episodes_repository", ondelete="RESTRICT"
        ),
    )
    with op.batch_alter_table("integration_repair_operations") as batch_op:
        batch_op.create_foreign_key(
            "fk_integration_repair_operations_parent_episode",
            "integration_parent_episodes", ["parent_task_id", "episode_id"],
            ["parent_task_id", "id"], ondelete="RESTRICT"
        )
        batch_op.create_foreign_key(
            "fk_integration_repair_operations_verifier_task",
            "tasks", ["verifier_task_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_table(
        "integration_child_dispositions",
        sa.Column("parent_task_id", sa.Text(), primary_key=True),
        sa.Column("child_task_id", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disposition", sa.Text()),
        sa.Column("parent_operation_id", sa.Text(), nullable=False),
        sa.Column("parent_episode_id", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "revision >= 0", name="ck_integration_child_dispositions_revision"
        ),
        sa.CheckConstraint(
            "disposition IS NULL OR disposition IN ('noop', 'ineligible', 'skipped')",
            name="ck_integration_child_dispositions_value",
        ),
        sa.ForeignKeyConstraint(
            ["parent_operation_id"], ["integration_repair_operations.id"],
            name="fk_integration_child_dispositions_parent_operation", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id", "parent_episode_id"],
            ["integration_parent_episodes.parent_task_id", "integration_parent_episodes.id"],
            name="fk_integration_child_dispositions_parent_episode", ondelete="RESTRICT"
        ),
    )
    op.create_table(
        "integration_parent_verifications",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("parent_task_id", sa.Text(), nullable=False),
        sa.Column("episode_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("required_check_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "generation >= 0", name="ck_integration_parent_verifications_generation"
        ),
        sa.UniqueConstraint(
            "operation_id",
            "generation",
            "head_sha",
            name="uq_integration_parent_verifications_tuple",
        ),
        sa.UniqueConstraint(
            "parent_task_id", "id", name="uq_integration_parent_verifications_parent_id"
        ),
        sa.UniqueConstraint(
            "operation_id",
            "id",
            "parent_task_id",
            "episode_id",
            name="uq_integration_parent_verifications_completion_identity",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["integration_repair_operations.id"],
            name="fk_integration_parent_verifications_operation", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"], ["tasks.id"],
            name="fk_integration_parent_verifications_parent_task", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id", "episode_id"],
            ["integration_parent_episodes.parent_task_id", "integration_parent_episodes.id"],
            name="fk_integration_parent_verifications_episode", ondelete="RESTRICT"
        ),
    )
    op.create_table(
        "integration_parent_operation_completions",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column("verification_id", sa.Text(), nullable=False),
        sa.Column("parent_task_id", sa.Text(), nullable=False),
        sa.Column("episode_id", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "verification_id", name="uq_parent_operation_completions_verification"
        ),
        sa.UniqueConstraint(
            "operation_id",
            "verification_id",
            "parent_task_id",
            name="uq_parent_operation_completions_checkpoint_identity",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["integration_repair_operations.id"],
            name="fk_parent_operation_completions_operation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "verification_id", "parent_task_id", "episode_id"],
            [
                "integration_parent_verifications.operation_id",
                "integration_parent_verifications.id",
                "integration_parent_verifications.parent_task_id",
                "integration_parent_verifications.episode_id",
            ],
            name="fk_parent_operation_completions_verification",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "integration_parent_verification_evidence",
        sa.Column(
            "verification_id",
            sa.Text(),
            sa.ForeignKey("integration_parent_verifications.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "evidence_id",
            sa.Text(),
            sa.ForeignKey("integration_check_evidence.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.UniqueConstraint(
            "evidence_id", name="uq_integration_parent_verification_evidence_evidence"
        ),
    )
    op.create_table(
        "integration_operation_artifact_pins",
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("integration_repair_operations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "artifact_sha256",
            sa.Text(),
            sa.ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
            primary_key=True,
        ),
    )
    op.create_index(
        "idx_integration_operation_artifact_pins_sha",
        "integration_operation_artifact_pins",
        ["artifact_sha256"],
    )
    op.create_table(
        "integration_episode_receipt_acceptances",
        sa.Column("episode_id", sa.Text(), primary_key=True),
        sa.Column("receipt_id", sa.Text(), primary_key=True),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("previous_episode_id", sa.Text(), nullable=False),
        sa.Column("previous_operation_id", sa.Text(), nullable=False),
        sa.Column("previous_verification_id", sa.Text(), nullable=False),
        sa.Column("ancestry_from_sha", sa.Text(), nullable=False),
        sa.Column("ancestry_to_sha", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["task_delivery_receipts.id"],
            name="fk_episode_receipt_acceptance_receipt", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["integration_parent_episodes.id"],
            name="fk_episode_receipt_acceptance_episode", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["previous_episode_id"], ["integration_parent_episodes.id"],
            name="fk_episode_receipt_acceptance_prev_episode",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["integration_repair_operations.id"],
            name="fk_episode_receipt_acceptance_operation", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["previous_operation_id"], ["integration_repair_operations.id"],
            name="fk_episode_receipt_acceptance_prev_operation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_verification_id"], ["integration_parent_verifications.id"],
            name="fk_episode_receipt_acceptance_prev_verification",
            ondelete="RESTRICT",
        ),
    )
    with op.batch_alter_table("task_delivery_receipts") as batch_op:
        batch_op.create_check_constraint(
            "ck_task_delivery_receipts_parent_binding",
            "(parent_operation_id IS NULL AND parent_episode_id IS NULL) OR "
            "(parent_operation_id IS NOT NULL AND parent_episode_id IS NOT NULL)",
        )
        batch_op.create_foreign_key(
            "fk_task_delivery_receipts_parent_operation",
            "integration_repair_operations", ["parent_operation_id"], ["id"],
            ondelete="RESTRICT"
        )
        batch_op.create_foreign_key(
            "fk_task_delivery_receipts_parent_episode",
            "integration_parent_episodes", ["target_task_id", "parent_episode_id"],
            ["parent_task_id", "id"], ondelete="RESTRICT"
        )
    with op.batch_alter_table("task_integration_checkpoints") as batch_op:
        batch_op.create_check_constraint(
            "ck_task_integration_checkpoints_completion_binding",
            "(last_completed_operation_id IS NULL AND "
            "last_completed_verification_id IS NULL) OR "
            "(last_completed_operation_id IS NOT NULL AND "
            "last_completed_verification_id IS NOT NULL)",
        )
        batch_op.create_foreign_key(
            "fk_task_integration_checkpoints_episode",
            "integration_parent_episodes", ["task_id", "episode_id"],
            ["parent_task_id", "id"], ondelete="RESTRICT"
        )
        batch_op.create_foreign_key(
            "fk_task_integration_checkpoints_completion",
            "integration_parent_operation_completions",
            [
                "last_completed_operation_id",
                "last_completed_verification_id",
                "task_id",
            ],
            ["operation_id", "verification_id", "parent_task_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_task_integration_checkpoints_verification",
            "integration_parent_verifications", ["task_id", "current_verification_id"],
            ["parent_task_id", "id"], ondelete="RESTRICT"
        )
    _create_append_only_guards()


def downgrade() -> None:
    _drop_append_only_guards()
    with op.batch_alter_table("task_integration_checkpoints") as batch_op:
        batch_op.drop_constraint(
            "fk_task_integration_checkpoints_completion", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_task_integration_checkpoints_verification", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_task_integration_checkpoints_episode", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "ck_task_integration_checkpoints_completion_binding", type_="check"
        )
    with op.batch_alter_table("task_delivery_receipts") as batch_op:
        batch_op.drop_constraint(
            "fk_task_delivery_receipts_parent_episode", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_task_delivery_receipts_parent_operation", type_="foreignkey"
        )
        batch_op.drop_constraint("ck_task_delivery_receipts_parent_binding", type_="check")
    op.drop_table("integration_episode_receipt_acceptances")
    op.drop_index(
        "idx_integration_operation_artifact_pins_sha",
        table_name="integration_operation_artifact_pins",
    )
    op.drop_table("integration_operation_artifact_pins")
    op.drop_table("integration_parent_verification_evidence")
    op.drop_table("integration_parent_operation_completions")
    op.drop_table("integration_parent_verifications")
    op.drop_table("integration_child_dispositions")
    with op.batch_alter_table("integration_repair_operations") as batch_op:
        batch_op.drop_constraint(
            "fk_integration_repair_operations_verifier_task", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_integration_repair_operations_parent_episode", type_="foreignkey"
        )
    op.drop_table("integration_parent_episodes")
    op.drop_index(
        "uq_integration_repair_operations_parent_episode",
        table_name="integration_repair_operations",
    )
    for name in (
        "route_activation_id",
        "route_scope_identifier",
        "route_scope",
        "route_playbook_id",
        "verifier_task_id",
    ):
        op.drop_column("integration_repair_operations", name)
    op.drop_column("task_delivery_receipts", "parent_episode_id")
    op.drop_column("task_delivery_receipts", "parent_operation_id")
    op.drop_column("task_delivery_receipts", "disposition_revision")
    op.drop_column("task_integration_checkpoints", "current_verification_id")
    op.drop_column("task_integration_checkpoints", "last_completed_verification_id")
    op.drop_column("task_integration_checkpoints", "last_completed_operation_id")
    op.drop_column("task_integration_checkpoints", "episode_id")
    op.drop_column("projects", "hierarchical_integration_policy")
