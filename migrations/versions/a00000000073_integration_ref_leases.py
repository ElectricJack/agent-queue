"""Add git-first ref leases without retiring shadow readers.

Revision ID: a00000000073
Revises: a00000000072
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000073"
down_revision = "a00000000072"
branch_labels = None
depends_on = None

TABLE = "integration_branch_owners"
CONSTRAINTS = {
    "ck_integration_branch_owners_lease_fence": "fence IS NULL OR fence >= 0",
    "ck_integration_branch_owners_lease_binding": (
        "holder IS NULL OR (fence IS NOT NULL AND expires_at IS NOT NULL)"
    ),
}


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        from src.database.tables import integration_branch_owners

        integration_branch_owners.create(bind, checkfirst=True)
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}
    for column in (sa.Column("holder", sa.Text()), sa.Column("fence", sa.BigInteger())):
        if column.name not in columns:
            op.add_column(TABLE, column)
    constraints = {item["name"] for item in sa.inspect(bind).get_check_constraints(TABLE)}
    for name, expression in CONSTRAINTS.items():
        if name not in constraints:
            op.create_check_constraint(name, TABLE, expression)
    # Do not backfill: legacy execution remains authoritative under shadow.
    # First managed acquisition adopts the row and advances its old fence.


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}
    if "fence" in columns and bind.scalar(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE fence IS NOT NULL")
    ):
        raise RuntimeError("managed ref leases exist; use read-only rollback")
    constraints = {item["name"] for item in sa.inspect(bind).get_check_constraints(TABLE)}
    for name in CONSTRAINTS:
        if name in constraints:
            op.drop_constraint(name, TABLE, type_="check")
    for name in ("holder", "fence"):
        if name in columns:
            op.drop_column(TABLE, name)
