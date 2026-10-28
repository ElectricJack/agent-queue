"""Durable integration-train lane history, so restarts keep visit order."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000124"
down_revision = "a00000000123"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("integration_train_lanes"):
        op.create_table(
            "integration_train_lanes",
            sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("repository_id", sa.Text, nullable=False),
            sa.Column("target_ref", sa.Text, nullable=False),
            sa.Column("kind", sa.Text, nullable=False),
            sa.Column("visits", sa.Integer, nullable=False, server_default="0"),
            sa.Column("errors", sa.Integer, nullable=False, server_default="0"),
            sa.Column("state", sa.Text),
            sa.Column("observed_at", sa.Float),
            sa.Column("started_at", sa.Float, nullable=False),
            sa.Column("updated_at", sa.Float, nullable=False),
            sa.PrimaryKeyConstraint("project_id", "repository_id", "target_ref",
                                    name="pk_integration_train_lanes"),
        )


def downgrade():
    # Diagnostic history only; the train re-derives every lane from Git.
    if sa.inspect(op.get_bind()).has_table("integration_train_lanes"):
        op.drop_table("integration_train_lanes")
