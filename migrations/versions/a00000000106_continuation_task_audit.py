"""Preserve continuation history when its task is removed."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000106"
down_revision = "a00000000105"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("continuations"):
        return
    for foreign_key in inspector.get_foreign_keys("continuations"):
        if foreign_key["constrained_columns"] == ["task_id"]:
            op.drop_constraint(foreign_key["name"], "continuations", type_="foreignkey")


def downgrade():
    # Historical task identities may no longer exist. Retain those receipts.
    pass
