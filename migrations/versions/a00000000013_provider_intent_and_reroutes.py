"""Add task provider intent, the re-route marker and ``task_reroutes`` (provider-failover D8-D10, D17).

Revision ID: a00000000013
Revises: a00000000012

* ``tasks.provider_intent`` / ``archived_tasks.provider_intent`` —
  ``pinned | preferred | class_only``, default ``class_only``, named CHECKs.
* ``tasks.rerouted_from`` / ``archived_tasks.rerouted_from`` — the profile a
  task was on before its first automatic re-route that has not been undone,
  plus the partial index ``idx_tasks_rerouted`` the re-route trickle counts.
* ``task_reroutes`` — the append-only re-route history.

**Existing explicit routes become ``preferred``, never ``pinned``** (D10): no
existing row can prove a human meant the provider, because the routing
playbook writes ``profile_id`` on every task it routes.  ``preferred`` keeps
today's behaviour exactly -- the existing ``profile_id`` still narrows the
routing catalog -- so the upgrade changes nothing on a healthy box.  NULL
``profile_id`` rows keep the default ``class_only``.

**Every step is conditional**, for the same reason ``a0000000000a``'s is: the
squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after these columns
were declared already has them.  The backfill runs only in the run that adds
the column, so it can never re-mark a ``class_only`` row that new code wrote.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000013"
down_revision = "a00000000012"
branch_labels = None
depends_on = None

_INTENTS = "provider_intent IN ('pinned','preferred','class_only')"
_CHECKS = {
    "tasks": "ck_tasks_provider_intent",
    "archived_tasks": "ck_archived_tasks_provider_intent",
}
_INDEX = "idx_tasks_rerouted"
_REROUTES = "task_reroutes"


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _checks(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_check_constraints(table)}


def upgrade() -> None:
    from src.database.tables import task_reroutes

    bind = op.get_bind()
    for table, check in _CHECKS.items():
        columns = _columns(bind, table)
        if "provider_intent" not in columns:
            op.add_column(
                table,
                sa.Column(
                    "provider_intent", sa.Text(), nullable=False, server_default="class_only"
                ),
            )
            op.execute(
                sa.text(
                    f"UPDATE {table} SET provider_intent = 'preferred' "
                    "WHERE profile_id IS NOT NULL AND provider_intent = 'class_only'"
                )
            )
        if "rerouted_from" not in columns:
            op.add_column(table, sa.Column("rerouted_from", sa.Text(), nullable=True))
        if check not in _checks(bind, table):
            op.create_check_constraint(check, table, _INTENTS)

    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("tasks")}
    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            "tasks",
            ["profile_id"],
            postgresql_where=sa.text("rerouted_from IS NOT NULL"),
        )

    if _REROUTES not in set(sa.inspect(bind).get_table_names()):
        task_reroutes.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _REROUTES in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_REROUTES)
    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("tasks")}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name="tasks")
    for table, check in _CHECKS.items():
        if check in _checks(bind, table):
            op.drop_constraint(check, table, type_="check")
        columns = _columns(bind, table)
        for column in ("rerouted_from", "provider_intent"):
            if column in columns:
                op.drop_column(table, column)
