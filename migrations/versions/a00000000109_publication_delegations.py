"""Publication delegation binding on the target branch's owner lease."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000109"
down_revision = "a00000000108"
branch_labels = None
depends_on = None

TABLE = "integration_branch_owners"


def upgrade():
    # A schema without the owner table (a sparse restore) has no lease to bind;
    # creating the table from live metadata later includes the column.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(TABLE) and "delegation" not in {
        c["name"] for c in inspector.get_columns(TABLE)
    }:
        op.add_column(TABLE, sa.Column("delegation", JSONB(none_as_null=True), nullable=True))


def downgrade():
    # Authority/audit evidence survives code rollback; old code cannot use grants.
    pass
