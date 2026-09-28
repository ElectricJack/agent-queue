"""Retype ``tasks.route`` from ``json`` to ``jsonb``.

Revision ID: a00000000040
Revises: a00000000039

Revision a00000000039 added ``tasks.route`` as plain ``json``.  ``json`` has no
equality operator, so a query that compares whole ``tasks`` rows fails at plan
time whatever the data: the scheduler's ``SELECT DISTINCT tasks.*`` over
failed dependencies raised ``could not identify an equality operator for type
json`` every cycle and no pool started a session (outage 2026-09-28).
Production was patched by hand with this ALTER; this revision brings every
other install to the same type.

Idempotent: the column is converted only while it is ``json``.  The
hand-patched production database, a fresh install (the squashed baseline builds
from live metadata, which now declares ``JSONB``) and a second run change
nothing.

Downgrade keeps ``jsonb``.  ``json`` is the type that broke the scheduler, and
the code at a00000000039 reads and writes ``jsonb`` unchanged (production ran
it on the patched column), so restoring ``json`` would only restore the outage.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a00000000040"
down_revision = "a00000000039"
branch_labels = None
depends_on = None


def _route_is_plain_json(bind) -> bool:
    for column in sa.inspect(bind).get_columns("tasks"):
        if column["name"] == "route":
            return isinstance(column["type"], sa.JSON) and not isinstance(
                column["type"], postgresql.JSONB
            )
    return False


def upgrade() -> None:
    if _route_is_plain_json(op.get_bind()):
        op.alter_column(
            "tasks",
            "route",
            type_=postgresql.JSONB(none_as_null=True),
            existing_nullable=True,
            postgresql_using="route::jsonb",
        )


def downgrade() -> None:
    pass
