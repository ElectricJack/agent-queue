"""Retain an explicit receipt for cancelled unsent deliveries.

Revision ID: a00000000033
Revises: a00000000032
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000033"
down_revision = "a00000000032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("outbound_deliveries"):
        return
    checks = inspector.get_check_constraints("outbound_deliveries")
    check = next((c for c in checks if c["name"] == "ck_outbound_deliveries_state"), None)
    if check and "cancelled" in check["sqltext"]:
        return
    if check:
        op.drop_constraint("ck_outbound_deliveries_state", "outbound_deliveries", type_="check")
    op.create_check_constraint(
        "ck_outbound_deliveries_state",
        "outbound_deliveries",
        "state IN ('pending','sending','sent','retry','unknown','cancelled')",
    )


def downgrade() -> None:
    # Cancellation history must survive a rollback without re-enabling sends.
    return
