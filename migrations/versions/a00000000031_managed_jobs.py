"""Managed jobs, non-expiring workspace pins and terminal outbox (disabled).

Revision ID: a00000000031
Revises: a00000000030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000031"
down_revision = "a00000000030"
branch_labels = None
depends_on = None


def upgrade():
    from src.database.tables import jobs, job_workspace_pins, job_outbox

    bind = op.get_bind()
    if "generation" not in {c["name"] for c in sa.inspect(bind).get_columns("workspaces")}:
        op.add_column(
            "workspaces", sa.Column("generation", sa.Integer(), nullable=False, server_default="0")
        )
    if "job_pin_count" not in {c["name"] for c in sa.inspect(bind).get_columns("workspaces")}:
        op.add_column(
            "workspaces",
            sa.Column("job_pin_count", sa.Integer(), nullable=False, server_default="0"),
        )
    for table in (jobs, job_workspace_pins, job_outbox):
        if not sa.inspect(bind).has_table(table.name):
            table.create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in ("job_outbox", "job_workspace_pins", "jobs"):
        if sa.inspect(bind).has_table(name):
            op.drop_table(name)
    if "job_pin_count" in {c["name"] for c in sa.inspect(bind).get_columns("workspaces")}:
        op.drop_column("workspaces", "job_pin_count")
    if "generation" in {c["name"] for c in sa.inspect(bind).get_columns("workspaces")}:
        op.drop_column("workspaces", "generation")
