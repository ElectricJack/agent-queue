"""Preserve exhausted stages while allocating bounded successor repair work.

Revision ID: a00000000048
Revises: a00000000047
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a00000000048"
down_revision = "a00000000047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # The squashed baseline is constructed from current live metadata.
    if not sa.inspect(bind).has_table("integration_source_ci"):
        op.create_table(
            "integration_source_ci",
            sa.Column("task_id", sa.Text(), primary_key=True),
            sa.Column("repository_id", sa.Text(), sa.ForeignKey("repos.id"), primary_key=True),
            sa.Column("source_base", sa.Text(), primary_key=True),
            sa.Column("source_head", sa.Text(), primary_key=True),
            sa.Column("generation", sa.Integer(), primary_key=True),
            sa.Column("policy_generation", sa.Integer(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("evidence", postgresql.JSONB(), nullable=False),
            sa.Column("repair_task_id", sa.Text()),
            sa.Column("repair_attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("repair_history", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("observed_at", sa.Float(), nullable=False),
            sa.CheckConstraint("generation >= 0", name="ck_integration_source_ci_generation"),
            sa.CheckConstraint("repair_attempt >= 0", name="ck_integration_source_ci_attempt"),
            sa.CheckConstraint(
                "state IN ('green', 'red', 'cancelled', 'pending')",
                name="ck_integration_source_ci_state",
            ),
        )
        op.create_index(
            "idx_integration_source_ci_repair", "integration_source_ci", ["repair_task_id"]
        )
    if not sa.inspect(bind).has_table("integration_repair_stages"):
        return
    constraints = sa.inspect(bind).get_check_constraints("integration_repair_stages")
    name = "ck_integration_repair_stages_ordinal"
    current = next((item for item in constraints if item["name"] == name), None)
    if current is not None and current["sqltext"].replace(" ", "").strip("()") == "ordinal>=0":
        return
    if current is not None:
        op.drop_constraint(name, "integration_repair_stages", type_="check")
    op.create_check_constraint(name, "integration_repair_stages", "ordinal >= 0")


def downgrade() -> None:
    # Retained successor history cannot fit the old two-stage schema.
    bind = op.get_bind()
    if bind.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM integration_repair_stages WHERE ordinal > 1)")
    ):
        raise RuntimeError("Cannot downgrade while successor repair stages exist")
    op.drop_constraint(
        "ck_integration_repair_stages_ordinal", "integration_repair_stages", type_="check"
    )
    op.create_check_constraint(
        "ck_integration_repair_stages_ordinal", "integration_repair_stages", "ordinal IN (0, 1)"
    )
    op.drop_table("integration_source_ci")
