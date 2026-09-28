"""Record who wrote each task's route, the filer's class hint, and the router binding.

Revision ID: a00000000039
Revises: a00000000038

Revision 1 of the mandatory-task-routing spec (2026-09-28, §11):

* ``tasks.route_source`` (NOT NULL, default ``unrouted``), ``tasks.class_hint``
  and ``tasks.route`` (JSON);
* backfills: every class becomes the row's class hint; a role profile
  (``triage``, ``spec-ingest``, ``reviewer``, ``final-reviewer``) is ``role``;
  any other profile is ``legacy``; a row without a profile stays ``unrouted``;
* ``ck_tasks_route_source``, the value set;
* every project with no router binding is bound to ``default-assignment-routing``.

The squashed baseline builds from live metadata, so every step is guarded and
the backfills only touch rows that still hold their pre-revision value: running
this revision twice, or over a baseline that already has the columns, changes
nothing.  The ``design`` and ``art`` task kinds need no DDL (``task_type`` is
free text).  Downgrade drops the three columns and the check; project bindings
stay, because the column predates this revision.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000039"
down_revision = "a00000000038"
branch_labels = None
depends_on = None

_CHECK = "ck_tasks_route_source"
_SOURCES = "route_source IN ('unrouted','router','override','role','legacy')"
# Literal copies of src.routing.sources: a revision must not change meaning
# when the code it shipped with moves on.
_ROLE_PROFILES = ("triage", "spec-ingest", "reviewer", "final-reviewer")
_DEFAULT_ROUTER = "default-assignment-routing"


def _task_columns(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns("tasks")}


def _has_check(bind) -> bool:
    return any(c["name"] == _CHECK for c in sa.inspect(bind).get_check_constraints("tasks"))


def upgrade() -> None:
    bind = op.get_bind()
    columns = _task_columns(bind)
    if "route_source" not in columns:
        op.add_column(
            "tasks",
            sa.Column("route_source", sa.Text(), nullable=False, server_default="unrouted"),
        )
    if "class_hint" not in columns:
        op.add_column("tasks", sa.Column("class_hint", sa.Text(), nullable=True))
    if "route" not in columns:
        op.add_column("tasks", sa.Column("route", sa.JSON(), nullable=True))

    bind.execute(
        sa.text(
            "UPDATE tasks SET class_hint = intelligence_class "
            "WHERE class_hint IS NULL AND intelligence_class IS NOT NULL"
        )
    )
    roles = sa.bindparam("roles", _ROLE_PROFILES, expanding=True)
    bind.execute(
        sa.text(
            "UPDATE tasks SET route_source = 'role' "
            "WHERE route_source = 'unrouted' AND profile_id IN :roles"
        ).bindparams(roles)
    )
    bind.execute(
        sa.text(
            "UPDATE tasks SET route_source = 'legacy' "
            "WHERE route_source = 'unrouted' AND profile_id IS NOT NULL"
        )
    )
    if not _has_check(bind):
        op.create_check_constraint(_CHECK, "tasks", _SOURCES)

    bind.execute(
        sa.text(
            "UPDATE projects SET assignment_playbook_id = :router "
            "WHERE assignment_playbook_id IS NULL"
        ),
        {"router": _DEFAULT_ROUTER},
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_check(bind):
        op.drop_constraint(_CHECK, "tasks", type_="check")
    columns = _task_columns(bind)
    for name in ("route", "class_hint", "route_source"):
        if name in columns:
            op.drop_column("tasks", name)
