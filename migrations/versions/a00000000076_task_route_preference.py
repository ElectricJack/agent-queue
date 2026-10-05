"""Add the per-task routing preference (``tasks.prefer_target``/``prefer_mode``).

Mandatory routing §4 gained a third filer input beside the two hints: a
preference naming a harness or a profile the router weighs before scoring.
``soft`` takes it when it has headroom and routes normally when it does not;
``strict`` allows only that target and waits for it rather than falling back.
Neither writes a route — the project's bound router still writes every route,
which is what keeps routing mandatory.

Nothing is backfilled: both columns are nullable and ``NULL`` means "no
preference", which is exactly the state and the routing behaviour of every row
that predates this revision. The one row that ever could have wanted a
back-fill does not exist — a preference is only meaningful against a live
profile, and a profile's ids and harnesses are read at plan time, so a stored
target never has to be reconciled here.

Every step is guarded because the squashed baseline builds ``tasks`` from the
live ``src.database.tables.metadata``: a database created after the columns
were declared already carries them.

Revision ID: a00000000076
Revises: a00000000075
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000076"
down_revision = "a00000000075"
branch_labels = None
depends_on = None

TABLE = "tasks"
CONSTRAINT = "ck_tasks_prefer_mode"
COLUMNS = (("prefer_target", sa.Text()), ("prefer_mode", sa.Text()))
CONSTRAINT_TEXT = "prefer_mode IS NULL OR prefer_mode IN ('soft','strict')"


def _normalise(sqltext: str) -> str:
    return "".join(str(sqltext or "").lower().split()).replace("'", "")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return
    existing = {column["name"] for column in inspector.get_columns(TABLE)}
    for name, type_ in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))
    current = {
        item["name"]: item["sqltext"]
        for item in sa.inspect(bind).get_check_constraints(TABLE)
        if item["name"]
    }.get(CONSTRAINT)
    if current is None:
        op.create_check_constraint(CONSTRAINT, TABLE, CONSTRAINT_TEXT)
    elif _normalise(current) != _normalise(CONSTRAINT_TEXT):
        # A constraint of the same name with other content is this revision's
        # own, narrowed or widened by a later one; replace it rather than
        # leave two rules under one name.
        op.drop_constraint(CONSTRAINT, TABLE, type_="check")
        op.create_check_constraint(CONSTRAINT, TABLE, CONSTRAINT_TEXT)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return
    constraints = {
        item["name"]
        for item in sa.inspect(bind).get_check_constraints(TABLE)
        if item["name"]
    }
    if CONSTRAINT in constraints:
        op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    existing = {column["name"] for column in inspector.get_columns(TABLE)}
    for name, _type in reversed(COLUMNS):
        if name in existing:
            op.drop_column(TABLE, name)