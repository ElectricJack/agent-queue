"""Retype tasks.route from json to jsonb so whole-row DISTINCT works.

Revision ID: a00000000040
Revises: a00000000039

Revision a00000000039 added tasks.route as plain json, which has no equality
operator. The scheduler's SELECT DISTINCT tasks.* then failed every cycle.
The squashed baseline uses live JSONB metadata and production was patched out
of band, so only databases still using json need an ALTER.

Downgrade leaves the column as jsonb: restoring json would restore the outage,
and the a00000000039 code can read and write jsonb unchanged.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON, JSONB

revision = "a00000000040"
down_revision = "a00000000039"
branch_labels = None
depends_on = None


def _route_type(bind):
    for column in sa.inspect(bind).get_columns("tasks"):
        if column["name"] == "route":
            return column["type"]
    raise RuntimeError("tasks.route is missing; apply a00000000039 first")


def upgrade() -> None:
    current = _route_type(op.get_bind())
    if isinstance(current, JSONB):
        return
    if not isinstance(current, JSON):
        raise RuntimeError(f"tasks.route has unexpected type {current!r}; expected json")
    op.alter_column(
        "tasks",
        "route",
        type_=JSONB(none_as_null=True),
        existing_nullable=True,
        postgresql_using="route::jsonb",
    )


def downgrade() -> None:
    pass
