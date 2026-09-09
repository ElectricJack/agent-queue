"""Record a branch-discard intent on ``task_branch_origins``.

Revision ID: a00000000004
Revises: a00000000003

Deleting a task in a hierarchy/train project may now also remove the task's
branch from the remote, on an explicit operator choice
(``2026-09-08-task-deletion-with-materialized-branches-design`` §3.2).  The
intent is recorded on the origin row rather than in a new table: origins carry
no foreign key to ``tasks`` and ``_delete_one`` leaves them alone, so a retired
origin already outlives its task as an audit record and is the natural place to
park work the drain still owes.

``discard_state`` is NULL for every existing row, which reads as "nothing was
asked for" — no backfill.

The revision also narrows ``task_branch_origin_materialized_immutable``.  That
guard froze a materialized origin against *every* UPDATE, which is what made
retiring one impossible and the removal block permanent.  Identity —
repository, parent, base SHA, creation generation, ``materialized`` itself —
stays frozen, and DELETE stays forbidden; only ``retired_at`` and the discard
bookkeeping may now move.  ``migrations/integration_guards.py`` is the
immutable pre-squash snapshot by contract, so the change belongs here as a
``CREATE OR REPLACE`` that runs after the baseline installs it.

**Every add is conditional**, for the reason ``a00000000003`` documents: the
squashed baseline builds its tables from the live ``src.database.tables``
metadata, so a database created after these columns were declared already has
them and only older databases need the ``ALTER``.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000004"
down_revision = "a00000000003"
branch_labels = None
depends_on = None

_TABLE = "task_branch_origins"
_INDEX = "ix_task_branch_origins_discard_due"
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, dict], ...] = (
    ("discard_state", sa.Text(), {"nullable": True}),
    ("discard_requested_at", sa.Float(), {"nullable": True}),
    (
        "discard_attempts",
        sa.Integer(),
        {"nullable": False, "server_default": "0"},
    ),
    ("discard_next_attempt_at", sa.Float(), {"nullable": True}),
    ("discard_last_error", sa.Text(), {"nullable": True}),
)
_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    (
        "ck_task_branch_origins_discard_state",
        (
            "discard_state IS NULL OR discard_state IN "
            "('pending', 'complete', 'conflict', 'failed')"
        ),
    ),
    ("ck_task_branch_origins_discard_attempts", "discard_attempts >= 0"),
    (
        "ck_task_branch_origins_discard_requires_retired",
        "discard_state IS NULL OR (materialized = true AND retired_at IS NOT NULL)",
    ),
)


def _column_names(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def _index_names(bind) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}


def _constraint_names(bind) -> set[str]:
    return {
        c["name"] for c in sa.inspect(bind).get_check_constraints(_TABLE) if c.get("name")
    }


#: Identity a materialized origin still cannot change.  Everything absent from
#: this comparison (``retired_at`` and the ``discard_*`` columns) is now
#: writable on a materialized row; everything present is not.
_FROZEN_IDENTITY = """
    CREATE OR REPLACE FUNCTION task_branch_origin_materialized_immutable()
    RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.materialized THEN
                    RAISE EXCEPTION 'materialized task branch origin is immutable';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.materialized AND ROW(
                NEW.task_id, NEW.repository_id, NEW.parent_task_id,
                NEW.parent_repository_id, NEW.parent_ref, NEW.base_sha,
                NEW.creation_generation, NEW.reserved, NEW.materialized,
                NEW.created_at, NEW.materialized_at
            ) IS DISTINCT FROM ROW(
                OLD.task_id, OLD.repository_id, OLD.parent_task_id,
                OLD.parent_repository_id, OLD.parent_ref, OLD.base_sha,
                OLD.creation_generation, OLD.reserved, OLD.materialized,
                OLD.created_at, OLD.materialized_at
            ) THEN
                RAISE EXCEPTION 'materialized task branch origin is immutable';
            END IF;
            RETURN NEW;
        END;
    $$ LANGUAGE plpgsql
"""

#: The pre-squash body, restored on downgrade.
_FROZEN_WHOLE_ROW = """
    CREATE OR REPLACE FUNCTION task_branch_origin_materialized_immutable()
    RETURNS trigger AS $$
        BEGIN
            IF OLD.materialized THEN
                RAISE EXCEPTION 'materialized task branch origin is immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
    $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    bind = op.get_bind()
    present = _column_names(bind)
    for name, type_, kwargs in _COLUMNS:
        if name not in present:
            op.add_column(_TABLE, sa.Column(name, type_, **kwargs))
    existing_constraints = _constraint_names(bind)
    for name, condition in _CONSTRAINTS:
        if name not in existing_constraints:
            op.create_check_constraint(name, _TABLE, sa.text(condition))
    if _INDEX not in _index_names(bind):
        op.create_index(
            _INDEX,
            _TABLE,
            ["discard_next_attempt_at"],
            postgresql_where=sa.text("discard_state = 'pending'"),
        )
    # Must follow the ALTERs: the new body names the discard columns only
    # indirectly, but the guard has to be in place before any caller retires a
    # materialized origin.
    op.execute(sa.text(_FROZEN_IDENTITY))


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(sa.text(_FROZEN_WHOLE_ROW))
    if _INDEX in _index_names(bind):
        op.drop_index(_INDEX, table_name=_TABLE)
    existing_constraints = _constraint_names(bind)
    for name, _ in _CONSTRAINTS:
        if name in existing_constraints:
            op.drop_constraint(name, _TABLE, type_="check")
    present = _column_names(bind)
    for name, _type, _kwargs in _COLUMNS:
        if name in present:
            op.drop_column(_TABLE, name)
