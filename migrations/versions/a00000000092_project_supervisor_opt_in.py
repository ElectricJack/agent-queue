"""Make project supervisors opt-in, including existing projects."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000092"
down_revision = "a00000000091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("projects") and "supervisor_enabled" not in {
        column["name"] for column in inspector.get_columns("projects")
    }:
        op.add_column(
            "projects",
            sa.Column("supervisor_enabled", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("projects") and "supervisor_enabled" in {
        column["name"] for column in inspector.get_columns("projects")
    }:
        op.drop_column("projects", "supervisor_enabled")
