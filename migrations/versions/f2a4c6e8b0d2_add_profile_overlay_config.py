"""persist authored config for thin project pool profile overrides.
Revision ID: f2a4c6e8b0d2
Revises: e1b7c2a94d38
"""
from alembic import op
import sqlalchemy as sa
revision = "f2a4c6e8b0d2"
down_revision = "e1b7c2a94d38"
branch_labels = None
depends_on = None
def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("agent_profiles")}
    if "overlay_config" not in columns:
        with op.batch_alter_table("agent_profiles") as batch_op:
            batch_op.add_column(sa.Column("overlay_config", sa.Text(), nullable=True))
def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("agent_profiles")}
    if "overlay_config" in columns:
        with op.batch_alter_table("agent_profiles") as batch_op:
            batch_op.drop_column("overlay_config")
