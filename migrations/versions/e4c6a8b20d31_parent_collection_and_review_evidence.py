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
    "integration_parent_verification_evidence",
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
        "task_delivery_receipts", sa.Column("disposition_revision", sa.Integer())
    )
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
    )
    op.create_table(
        "integration_child_dispositions",
        sa.Column("parent_task_id", sa.Text(), primary_key=True),
        sa.Column("child_task_id", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disposition", sa.Text()),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "revision >= 0", name="ck_integration_child_dispositions_revision"
        ),
        sa.CheckConstraint(
            "disposition IS NULL OR disposition IN ('noop', 'ineligible', 'skipped')",
            name="ck_integration_child_dispositions_value",
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
    _create_append_only_guards()


def downgrade() -> None:
    _drop_append_only_guards()
    op.drop_index(
        "idx_integration_operation_artifact_pins_sha",
        table_name="integration_operation_artifact_pins",
    )
    op.drop_table("integration_operation_artifact_pins")
    op.drop_table("integration_parent_verification_evidence")
    op.drop_table("integration_parent_verifications")
    op.drop_table("integration_child_dispositions")
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
    op.drop_column("task_delivery_receipts", "disposition_revision")
    op.drop_column("task_integration_checkpoints", "current_verification_id")
    op.drop_column("task_integration_checkpoints", "episode_id")
    op.drop_column("projects", "hierarchical_integration_policy")
