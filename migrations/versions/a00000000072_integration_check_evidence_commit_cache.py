"""Let ``integration_check_evidence`` hold the exact-commit check cache.

Revision ID: a00000000072
Revises: a00000000071

Git-first integration spec, "Kept in the database" item 2: CI results are one
refreshable cache per ``(repository_id, sha, check_name, trust identity)``
holding the latest attempt, its conclusion, ``observed_at`` and ``run_url``.
Plan §2.2 reshapes the existing table rather than adding a new one, and keeps
stage-2 revisions additive: legacy run-attempt rows bound to a batch revision
or parent generation are untouched and stay readable by every legacy reader.

The trust identity is the existing ``producer_id`` and the latest attempt the
existing ``attempt``. This revision adds the nullable cache columns, admits a
third "commit" subject form (no batch or parent binding, an exact repository,
SHA and check), widens the conclusion vocabulary with ``missing`` and
``unavailable``, and narrows the run-attempt uniqueness to legacy rows: two
required checks of one workflow run share its run id and attempt, so a cache
row is unique by commit and check instead.

Every step is guarded because the squashed baseline builds this table from
the live ``src.database.tables.metadata``: a database created after this
revision already carries the reshaped table.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000072"
down_revision = "a00000000071"
branch_labels = None
depends_on = None

TABLE = "integration_check_evidence"
COLUMNS = (
    ("repository_id", sa.Text()),
    ("sha", sa.Text()),
    ("check_name", sa.Text()),
    ("run_url", sa.Text()),
    ("due_at", sa.Float()),
)
RUN_ATTEMPT = "uq_integration_check_evidence_producer_run_attempt_checks"
RUN_ATTEMPT_COLUMNS = ["producer_id", "run_id", "attempt", "required_check_version"]
COMMIT_CHECK = "uq_integration_check_evidence_commit_check"
COMMIT_CHECK_COLUMNS = ["repository_id", "sha", "check_name", "producer_id"]
SUBJECT = "ck_integration_check_evidence_subject"
CONCLUSION = "ck_integration_check_evidence_conclusion"

_LEGACY_SUBJECT = (
    "(batch_id IS NOT NULL AND candidate_revision IS NOT NULL AND parent_task_id IS NULL "
    "AND parent_generation IS NULL AND parent_head_sha IS NULL) OR "
    "(batch_id IS NULL AND candidate_revision IS NULL AND parent_task_id IS NOT NULL "
    "AND parent_generation IS NOT NULL AND parent_head_sha IS NOT NULL)"
)
_SUBJECT = (
    "(batch_id IS NOT NULL AND candidate_revision IS NOT NULL AND parent_task_id IS NULL "
    "AND parent_generation IS NULL AND parent_head_sha IS NULL AND sha IS NULL) OR "
    "(batch_id IS NULL AND candidate_revision IS NULL AND parent_task_id IS NOT NULL "
    "AND parent_generation IS NOT NULL AND parent_head_sha IS NOT NULL AND sha IS NULL) OR "
    "(batch_id IS NULL AND candidate_revision IS NULL AND parent_task_id IS NULL "
    "AND parent_generation IS NULL AND parent_head_sha IS NULL AND repository_id IS NOT NULL "
    "AND sha IS NOT NULL AND check_name IS NOT NULL)"
)
_LEGACY_CONCLUSION = "conclusion IN ('success', 'failure', 'pending', 'cancelled', 'inconclusive')"
_CONCLUSION = (
    "conclusion IN ('success', 'failure', 'pending', 'cancelled', 'inconclusive', "
    "'missing', 'unavailable')"
)


def _normalise(sqltext: str) -> str:
    return "".join(str(sqltext or "").lower().split()).replace("'", "").replace("(", "").replace(
        ")", ""
    )


def _replace_check(bind, name: str, wanted: str) -> None:
    current = {
        item["name"]: item["sqltext"]
        for item in sa.inspect(bind).get_check_constraints(TABLE)
        if item["name"]
    }.get(name)
    if current is not None and _normalise(current) == _normalise(wanted):
        return
    if current is not None:
        op.drop_constraint(name, TABLE, type_="check")
    op.create_check_constraint(name, TABLE, wanted)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return
    existing = {column["name"] for column in inspector.get_columns(TABLE)}
    for name, type_ in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))
    unique = {item["name"] for item in sa.inspect(bind).get_unique_constraints(TABLE)}
    if RUN_ATTEMPT in unique:
        op.drop_constraint(RUN_ATTEMPT, TABLE, type_="unique")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes(TABLE)}
    if RUN_ATTEMPT not in indexes:
        op.create_index(
            RUN_ATTEMPT,
            TABLE,
            RUN_ATTEMPT_COLUMNS,
            unique=True,
            postgresql_where=sa.text("sha IS NULL"),
        )
    if COMMIT_CHECK not in indexes:
        op.create_index(
            COMMIT_CHECK,
            TABLE,
            COMMIT_CHECK_COLUMNS,
            unique=True,
            postgresql_where=sa.text("sha IS NOT NULL"),
        )
    _replace_check(bind, SUBJECT, _SUBJECT)
    _replace_check(bind, CONCLUSION, _CONCLUSION)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return
    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    if "sha" in columns:
        cached = bind.execute(
            sa.text(f'SELECT count(*) FROM "{TABLE}" WHERE sha IS NOT NULL')
        ).scalar_one()
        if cached:
            # The cache is refreshable from the trusted provider; dropping it
            # loses nothing that the next visit cannot observe again.
            bind.execute(sa.text(f'DELETE FROM "{TABLE}" WHERE sha IS NOT NULL'))
    _replace_check(bind, CONCLUSION, _LEGACY_CONCLUSION)
    _replace_check(bind, SUBJECT, _LEGACY_SUBJECT)
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes(TABLE)}
    if COMMIT_CHECK in indexes:
        op.drop_index(COMMIT_CHECK, table_name=TABLE)
    if RUN_ATTEMPT in indexes:
        op.drop_index(RUN_ATTEMPT, table_name=TABLE)
    unique = {item["name"] for item in sa.inspect(bind).get_unique_constraints(TABLE)}
    if RUN_ATTEMPT not in unique:
        op.create_unique_constraint(RUN_ATTEMPT, TABLE, RUN_ATTEMPT_COLUMNS)
    for name, _ in reversed(COLUMNS):
        if name in columns:
            op.drop_column(TABLE, name)
