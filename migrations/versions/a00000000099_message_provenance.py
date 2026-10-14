"""Optional server-derived playbook message provenance."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000099"
down_revision = "a00000000098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("messages") and "provenance" not in {
        c["name"] for c in inspector.get_columns("messages")
    }:
        op.add_column("messages", sa.Column("provenance", JSONB, nullable=True))


def downgrade() -> None:
    # Code rollback retains provenance along with durable notification journals.
    pass
