"""swarm work model — DDL (revision A)

Revision ID: a1b2c3d4e5f6
Revises: 4e925610d7a6
Create Date: 2026-08-28

DDL only (spec §17).  The hierarchy data step and the single-parent partial
unique index are revision B, so that a rejected canonicalisation never rolls
back the columns the preflight report lives in.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "4e925610d7a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as b:
        b.add_column(
            sa.Column("next_child_ordinal", sa.Integer(), server_default="1", nullable=False)
        )
        b.add_column(sa.Column("created_by_kind", sa.Text(), nullable=True))
        b.add_column(sa.Column("created_by_id", sa.Text(), nullable=True))
        b.add_column(sa.Column("claim_epoch", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("filed_count", sa.Integer(), server_default="0", nullable=False))
        b.create_index(
            "idx_tasks_ready_by_profile",
            ["project_id", "profile_id", "status", "is_blocked"],
        )
    with op.batch_alter_table("archived_tasks", schema=None) as b:
        b.add_column(sa.Column("created_by_kind", sa.Text(), nullable=True))
        b.add_column(sa.Column("created_by_id", sa.Text(), nullable=True))
    with op.batch_alter_table("sessions", schema=None) as b:
        b.add_column(sa.Column("claims", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("agent_id", sa.Text(), nullable=True))
        b.add_column(sa.Column("claim_phase", sa.Text(), nullable=True))
        b.add_column(sa.Column("claim_phase_at", sa.Float(), nullable=True))
        b.add_column(sa.Column("last_claim_epoch", sa.Integer(), nullable=True))
        b.add_column(sa.Column("last_claim_result", sa.Text(), nullable=True))
    with op.batch_alter_table("agent_profiles", schema=None) as b:
        b.add_column(sa.Column("min_active", sa.Integer(), nullable=True))
        b.add_column(sa.Column("max_active", sa.Integer(), nullable=True))
        b.add_column(sa.Column("max_claims_per_session", sa.Integer(), nullable=True))
    op.create_table(
        "hierarchy_migration_rejects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("idx_hier_rejects_run", "hierarchy_migration_rejects", ["run_id"])


def downgrade() -> None:
    op.drop_index("idx_hier_rejects_run", table_name="hierarchy_migration_rejects")
    op.drop_table("hierarchy_migration_rejects")
    with op.batch_alter_table("agent_profiles", schema=None) as b:
        b.drop_column("max_claims_per_session")
        b.drop_column("max_active")
        b.drop_column("min_active")
    with op.batch_alter_table("sessions", schema=None) as b:
        for col in (
            "last_claim_result",
            "last_claim_epoch",
            "claim_phase_at",
            "claim_phase",
            "agent_id",
            "claims",
        ):
            b.drop_column(col)
    with op.batch_alter_table("archived_tasks", schema=None) as b:
        b.drop_column("created_by_id")
        b.drop_column("created_by_kind")
    with op.batch_alter_table("tasks", schema=None) as b:
        b.drop_index("idx_tasks_ready_by_profile")
        for col in (
            "filed_count",
            "claim_epoch",
            "created_by_id",
            "created_by_kind",
            "next_child_ordinal",
        ):
            b.drop_column(col)
