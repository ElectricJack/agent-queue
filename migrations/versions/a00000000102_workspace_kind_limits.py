"""Bound special workspace kinds without changing project concurrency."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000102"
down_revision = "a00000000101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("workspace_kinds"):
        return
    if "max_slots" not in {c["name"] for c in inspector.get_columns("workspace_kinds")}:
        op.add_column("workspace_kinds", sa.Column("max_slots", sa.Integer(), nullable=True))
    if "ck_workspace_kinds_max_slots" not in {
        c["name"] for c in inspector.get_check_constraints("workspace_kinds")
    }:
        op.create_check_constraint(
            "ck_workspace_kinds_max_slots", "workspace_kinds", "max_slots > 0"
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("workspace_kinds"):
        return
    if "max_slots" in {c["name"] for c in inspector.get_columns("workspace_kinds")}:
        op.drop_column("workspace_kinds", "max_slots")
