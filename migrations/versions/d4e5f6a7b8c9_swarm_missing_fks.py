"""swarm work model — the two missing use_alter foreign keys (revision D)

`tables.py` declares ``agents.current_task_id -> tasks.id`` and
``tasks.preferred_workspace_id -> workspaces.id`` with ``use_alter=True``.
The baseline migration ``311e98c39ffa`` embedded them inside ``op.create_table``,
which Alembic never emits for ``use_alter`` constraints, so on PostgreSQL the
two foreign keys were silently missing (autogenerate against a fresh PG
database reported them as additions).

This migration nullifies any dangling references first, then creates the two
named constraints with ``ON DELETE SET NULL``.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-29

"""

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

AGENTS_FK = "fk_agents_current_task"
TASKS_FK = "fk_tasks_preferred_workspace"


def _nullify_orphans(bind: sa.engine.Connection) -> None:
    """Clear references that point at rows which no longer exist.

    Must run before the constraints are created or the ALTER would fail.
    """
    for table, column, target in (
        ("agents", "current_task_id", "tasks"),
        ("tasks", "preferred_workspace_id", "workspaces"),
    ):
        result = bind.execute(
            sa.text(
                f"UPDATE {table} SET {column} = NULL "
                f"WHERE {column} IS NOT NULL "
                f"AND {column} NOT IN (SELECT id FROM {target})"
            )
        )
        count = result.rowcount or 0
        if count:
            logger.info(
                "d4e5f6a7b8c9: nullified %d orphan %s.%s reference(s)", count, table, column
            )


def _existing_fk_names(bind: sa.engine.Connection, table: str) -> set:
    inspector = sa.inspect(bind)
    names = set()
    for fk in inspector.get_foreign_keys(table):
        if fk.get("name"):
            names.add(fk["name"])
    return names


def _has_fk_to(bind: sa.engine.Connection, table: str, column: str, target: str) -> bool:
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys(table):
        if fk.get("referred_table") == target and list(fk.get("constrained_columns") or []) == [
            column
        ]:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    _nullify_orphans(bind)

    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            AGENTS_FK, "agents", "tasks", ["current_task_id"], ["id"], ondelete="SET NULL"
        )
        op.create_foreign_key(
            TASKS_FK,
            "tasks",
            "workspaces",
            ["preferred_workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
        return

    # SQLite: the baseline's inline declaration may already have created these
    # (unnamed). Adding them again would require a full table rebuild for no
    # gain, so only act when the reference is genuinely absent.
    if not _has_fk_to(bind, "agents", "current_task_id", "tasks"):
        with op.batch_alter_table("agents") as batch_op:
            batch_op.create_foreign_key(
                AGENTS_FK, "tasks", ["current_task_id"], ["id"], ondelete="SET NULL"
            )
    if not _has_fk_to(bind, "tasks", "preferred_workspace_id", "workspaces"):
        with op.batch_alter_table("tasks") as batch_op:
            batch_op.create_foreign_key(
                TASKS_FK, "workspaces", ["preferred_workspace_id"], ["id"], ondelete="SET NULL"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite constraint removal means a table rebuild; the baseline shipped
        # these inline anyway, so downgrade is a no-op there.
        return
    if TASKS_FK in _existing_fk_names(bind, "tasks"):
        op.drop_constraint(TASKS_FK, "tasks", type_="foreignkey")
    if AGENTS_FK in _existing_fk_names(bind, "agents"):
        op.drop_constraint(AGENTS_FK, "agents", type_="foreignkey")
