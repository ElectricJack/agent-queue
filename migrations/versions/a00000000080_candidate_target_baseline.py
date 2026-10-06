"""Count how long one exact red candidate has failed only where the target fails.

A red candidate whose failing required checks also fail on the target commit it
was built on is a pre-existing failure, and one the target has not decided is
nobody's: neither is repairable, so the train re-requests that candidate's own
checks under bounded backoff and files no repair.  Those three facts — the
consecutive-observation counter that decides when to stop asking and name a
blocker, and the backoff deadline plus request count that bound how often a
re-request is asked for — are durable bookkeeping, per exact candidate and repair
generation, exactly as the source-CI infrastructure counters are.

Nothing is backfilled: the counters default to ``0`` and the identity columns to
NULL, which is the state of every row that predates this revision — no train
candidate has yet been classified against its target, so a back-filled
observation would be a fabricated one.

Every step is guarded because the squashed baseline builds
``integration_batches`` from the live ``src.database.tables.metadata``: a database
created after the columns were declared already carries them.

Revision ID: a00000000080
Revises: a00000000079
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000080"
down_revision = "a00000000079"
branch_labels = None
depends_on = None

TABLE = "integration_batches"
COLUMNS = (
    ("baseline_candidate_sha", sa.Text()),
    ("baseline_generation", sa.Integer()),
    ("baseline_observations", sa.Integer()),
    ("baseline_reruns", sa.Integer()),
    ("baseline_rerun_at", sa.Float()),
)
NOT_NULL = ("baseline_generation", "baseline_observations", "baseline_reruns")
CONSTRAINTS = (
    ("ck_integration_batches_baseline_generation", "baseline_generation >= 0"),
    ("ck_integration_batches_baseline_observations", "baseline_observations >= 0"),
    ("ck_integration_batches_baseline_reruns", "baseline_reruns >= 0"),
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
            # A constraint of the same name with other content is this revision's
            # own, narrowed or widened by a later one.
            op.drop_constraint(name, TABLE, type_="check")
            op.create_check_constraint(name, TABLE, text)
    # The counters are NOT NULL with a zero default; a legacy NULL from a column
    # added above would otherwise read as None in the comparison path.
    op.execute(
        sa.text(
            f"UPDATE {TABLE} SET baseline_generation = 0, baseline_observations = 0, "
            "baseline_reruns = 0 WHERE baseline_generation IS NULL "
            "OR baseline_observations IS NULL OR baseline_reruns IS NULL"
        )
    )
    for name in NOT_NULL:
        op.alter_column(TABLE, name, nullable=False, server_default="0")


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