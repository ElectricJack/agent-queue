"""Persistent generation failure circuit, additive to the inert K13 store."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000064"
down_revision = "a00000000063"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "consecutive_failures" not in {
        column["name"] for column in sa.inspect(bind).get_columns("knowledge_feature_budgets")
    }:
        op.add_column(
            "knowledge_feature_budgets",
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_check_constraint(
            "ck_knowledge_feature_budgets_failures",
            "knowledge_feature_budgets",
            "consecutive_failures >= 0",
        )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM knowledge_feature_budgets)")).scalar():
        raise RuntimeError("feature budgets contain data; use read-only rollback")
    op.drop_constraint("ck_knowledge_feature_budgets_failures", "knowledge_feature_budgets")
    op.drop_column("knowledge_feature_budgets", "consecutive_failures")
