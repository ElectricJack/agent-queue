"""Bound phantom Actions job reconciliation across visits and restarts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000120"
down_revision = "a00000000119"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("integration_check_evidence")}
    if "check_reconciliation" not in columns:
        op.add_column("integration_check_evidence", sa.Column("check_reconciliation", JSONB))


def downgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("integration_check_evidence")}
    if "check_reconciliation" in columns:
        op.drop_column("integration_check_evidence", "check_reconciliation")
