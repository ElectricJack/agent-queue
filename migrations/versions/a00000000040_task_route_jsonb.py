"""Convert tasks.route from json to jsonb so task rows support DISTINCT.

Revision ID: a00000000040
Revises: a00000000039

The squashed baseline uses live metadata, and the operator database was
already converted out of band. Only databases whose route column is still
json need an ALTER; both paths preserve existing route records.
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
        type_=JSONB(),
        existing_nullable=True,
        postgresql_using="route::jsonb",
    )


def downgrade() -> None:
    current = _route_type(op.get_bind())
    if isinstance(current, JSONB):
        op.alter_column(
            "tasks",
            "route",
            type_=JSON(),
            existing_nullable=True,
            postgresql_using="route::json",
        )
    elif not isinstance(current, JSON):
        raise RuntimeError(f"tasks.route has unexpected type {current!r}; expected jsonb")
