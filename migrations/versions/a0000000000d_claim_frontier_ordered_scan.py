"""Index-ordered scan for the §10 pool work query.

``select_ready_for_profile`` orders by ``priority, created_at`` and takes one
row.  Give that order to an index and PostgreSQL stops at the first admissible
row instead of materialising the whole frontier and top-N sorting it; measured
on PostgreSQL 18 at the §15.2 scale (5,000 tasks, 2,499 on the frontier) that
is ~10,100 shared buffers per claim down to 10-14.

Two of the three new indexes are strict supersets of indexes they replace --
``idx_tasks_project_status_blocked`` and ``idx_tasks_ready_by_profile`` are
each a leading-column prefix of one of them -- so every other query that used
them keeps its access path and the two are dropped rather than kept alongside.
The third is partial and holds only tasks pinned to an agent.

Idempotent by inspection: the squashed baseline builds a fresh database from
``src.database.tables.metadata``, which already carries these definitions, so
this revision must be a no-op there.

Revision ID: a0000000000d
Revises: a0000000000c
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text

revision = "a0000000000d"
down_revision = "a0000000000c"
branch_labels = None
depends_on = None

_TABLE = "tasks"

#: ``(name, columns, postgresql_where)`` — mirrors ``src.database.tables``.
_NEW = (
    (
        "idx_tasks_claim_frontier",
        ["project_id", "status", "is_blocked", "priority", "created_at"],
        None,
    ),
    (
        "idx_tasks_claim_frontier_by_profile",
        ["project_id", "profile_id", "status", "is_blocked", "priority", "created_at"],
        None,
    ),
    (
        "idx_tasks_claim_frontier_by_affinity",
        ["affinity_agent_id", "project_id", "status", "is_blocked", "priority", "created_at"],
        "affinity_agent_id IS NOT NULL",
    ),
)

#: ``(name, columns)`` — the prefixes the new indexes subsume.
_REPLACED = (
    ("idx_tasks_project_status_blocked", ["project_id", "status", "is_blocked"]),
    ("idx_tasks_ready_by_profile", ["project_id", "profile_id", "status", "is_blocked"]),
)


def _index_names(bind) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    present = _index_names(bind)
    for name, columns, where in _NEW:
        if name in present:
            continue
        kwargs = {"postgresql_where": text(where)} if where else {}
        op.create_index(name, _TABLE, columns, **kwargs)
    # Only after the replacements exist: dropping first would leave the
    # scheduler filter and `aq project ready` without an index for the length
    # of the build.
    present = _index_names(bind)
    for name, _columns in _REPLACED:
        if name in present:
            op.drop_index(name, table_name=_TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    present = _index_names(bind)
    for name, columns in _REPLACED:
        if name not in present:
            op.create_index(name, _TABLE, columns)
    present = _index_names(bind)
    for name, _columns, _where in _NEW:
        if name in present:
            op.drop_index(name, table_name=_TABLE)
