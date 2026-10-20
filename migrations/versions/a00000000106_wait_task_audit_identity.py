"""Retain continuation and admission audit identities after task removal."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000106"
down_revision = "a00000000105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("continuations", "admission_reservations"):
        if not inspector.has_table(table):
            continue
        for constraint in inspector.get_foreign_keys(table):
            if (constraint["constrained_columns"] == ["task_id"]
                    and constraint["referred_table"] == "tasks"):
                op.drop_constraint(constraint["name"], table, type_="foreignkey")


def downgrade() -> None:
    # Historical task identities may no longer exist in tasks. Restoring the
    # foreign keys would require destroying that audit, so retain this change.
    return
