"""Exact operator authorizations of individual train root sources.

Revision ID: a00000000054
Revises: a00000000053
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000054"
down_revision = "a00000000053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # The squashed baseline is constructed from current live metadata.
    if sa.inspect(bind).has_table("integration_root_authorizations"):
        return
    op.create_table(
        "integration_root_authorizations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("source_base", sa.Text(), nullable=False),
        sa.Column("source_head", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("review_kind", sa.Text(), nullable=False),
        sa.Column("pr_url", sa.Text(), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=True),
        sa.Column("policy_generation", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "generation >= 0", name="ck_integration_root_authorizations_generation"
        ),
        sa.CheckConstraint(
            "policy_generation >= 0",
            name="ck_integration_root_authorizations_policy_generation",
        ),
        sa.CheckConstraint(
            "review_kind IN ('leaf', 'parent')",
            name="ck_integration_root_authorizations_kind",
        ),
        sa.UniqueConstraint(
            "task_id",
            "repository_id",
            "source_base",
            "source_head",
            "generation",
            name="uq_integration_root_authorizations_source",
        ),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("integration_root_authorizations"):
        op.drop_table("integration_root_authorizations")
