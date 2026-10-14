"""Retain one immutable legacy delivery obligation cutover per target binding.

Revision ID: a00000000096
Revises: a00000000095
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000096"
down_revision = "a00000000095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("integration_delivery_cutovers"):
        return
    op.create_table(
        "integration_delivery_cutovers",
        sa.Column("project_id", sa.Text(), primary_key=True),
        sa.Column("repository_id", sa.Text(), primary_key=True),
        sa.Column("target_ref", sa.Text(), primary_key=True),
        sa.Column("plan_digest", sa.Text(), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("operator_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("length(plan_digest) = 64", name="ck_integration_delivery_cutovers_digest"),
        sa.CheckConstraint("length(operator_id) > 0", name="ck_integration_delivery_cutovers_operator"),
    )


def downgrade() -> None:
    # Audit history and retained-original identities survive rollback.
    return
