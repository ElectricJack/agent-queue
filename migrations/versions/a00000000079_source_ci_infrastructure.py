"""Record how long one exact source head has been infrastructure-only CI.

Source CI observation treats a run whose non-success required checks are all
cancelled as infrastructure: it waits, asks GitHub to re-run the exact head
under bounded backoff, and files no code repair.  Those three facts are
durable bookkeeping — the consecutive-observation counter that decides when to
stop asking and name a blocker, and the backoff deadline plus request count
that bound how often a re-run is asked for.  They are per exact source
identity, so a moved head or a new generation starts from zero, exactly as its
repair lineage does.

Nothing is backfilled: the new columns are nullable or default to ``0``, which
is the state of every row that predates this revision — no row has yet been
observed under the infra policy, so a back-filled counter would be a fabricated
observation.

Every step is guarded because the squashed baseline builds
``integration_source_ci`` from the live ``src.database.tables.metadata``: a
database created after the columns were declared already carries them.

Revision ID: a00000000079
Revises: a00000000078
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000079"
down_revision = "a00000000078"
branch_labels = None
depends_on = None

TABLE = "integration_source_ci"
COLUMNS = (
    ("infra_observations", sa.Integer()),
    ("infra_rerun_at", sa.Float()),
    ("infra_reruns", sa.Integer()),
)
CONSTRAINTS = (
    ("ck_integration_source_ci_infra_observations", "infra_observations >= 0"),
    ("ck_integration_source_ci_infra_reruns", "infra_reruns >= 0"),
)


def _normalise(sqltext: str) -> str:
    return "".join(str(sqltext or "").lower().split()).replace("'", "")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return
    existing = {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}
    for name, type_ in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))
    current = {
        item["name"]: item["sqltext"]
        for item in sa.inspect(bind).get_check_constraints(TABLE)
        if item["name"]
    }
    for name, text in CONSTRAINTS:
        recorded = current.get(name)
        if recorded is None:
            op.create_check_constraint(name, TABLE, text)
        elif _normalise(recorded) != _normalise(text):
            # A constraint of the same name with other content is this
            # revision's own, narrowed or widened by a later one.
            op.drop_constraint(name, TABLE, type_="check")
            op.create_check_constraint(name, TABLE, text)
    # The counters are NOT NULL with a zero default; a legacy NULL from a
    # column added above would otherwise read as None in the observation path.
    op.execute(
        sa.text(
            f"UPDATE {TABLE} SET infra_observations = 0, infra_reruns = 0 "
            "WHERE infra_observations IS NULL OR infra_reruns IS NULL"
        )
    )
    op.alter_column(TABLE, "infra_observations", nullable=False, server_default="0")
    op.alter_column(TABLE, "infra_reruns", nullable=False, server_default="0")


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
    for name, _text in CONSTRAINTS:
        if name in constraints:
            op.drop_constraint(name, TABLE, type_="check")
    existing = {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}
    for name, _type in reversed(COLUMNS):
        if name in existing:
            op.drop_column(TABLE, name)
