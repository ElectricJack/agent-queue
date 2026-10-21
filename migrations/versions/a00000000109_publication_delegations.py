"""Publication delegation binding on the target branch's owner lease."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000109"
down_revision = "a00000000108"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(
        "integration_branch_owners")}
    if "delegation" not in columns:
        op.add_column("integration_branch_owners",
                      sa.Column("delegation", JSONB(none_as_null=True), nullable=True))


def downgrade():
    # Authority/audit evidence survives code rollback; old code cannot use grants.
    pass
