"""Preserve continuation and admission audit identities when their task is removed."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000112"
down_revision = "a00000000111"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for table in ("continuations", "admission_reservations"):
        if not inspector.has_table(table):
            continue
        for foreign_key in inspector.get_foreign_keys(table):
            if (foreign_key["constrained_columns"] == ["task_id"]
                    and foreign_key["referred_table"] == "tasks"):
                op.drop_constraint(foreign_key["name"], table, type_="foreignkey")


def downgrade():
    # Historical task identities may no longer exist. Retain those receipts.
    pass
