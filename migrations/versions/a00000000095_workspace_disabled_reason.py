"""Record why and when a workspace was disabled."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000095"
down_revision = "a00000000094"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("disabled_reason", sa.Text()),
    ("disabled_at", sa.Float()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("workspaces"):
        return
    existing = {column["name"] for column in inspector.get_columns("workspaces")}
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column("workspaces", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("workspaces"):
        return
    existing = {column["name"] for column in inspector.get_columns("workspaces")}
    for name, _type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("workspaces", name)
