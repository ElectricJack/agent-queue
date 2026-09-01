"""integration_mode policy replaces requires_approval / auto_approve_plan

Revision ID: c4d5e6f7a8b9
Revises: f1d7a9c20b64
Create Date: 2026-08-31

Replaces the misnamed ``requires_approval`` boolean with an explicit
integration policy and deletes the dead ``auto_approve_plan`` flag:

* ``tasks.integration_mode`` / ``archived_tasks.integration_mode`` —
  ``'direct'`` | ``'pull_request'`` | NULL (inherit project/system policy).
* ``projects.integration_mode`` — project-level policy, NULL = system
  default (config ``integration.default_mode``).

Compatibility backfill (explicit policy, both active and archived rows):
``requires_approval=1`` → ``'pull_request'``; ``requires_approval=0`` →
``'direct'``.  This preserves each existing row's behavior exactly — only
rows created *after* the upgrade inherit the new policy chain.

Preflight: the legacy AWAITING_APPROVAL / AWAITING_PLAN_APPROVAL task
statuses are retired with this revision (new code cannot load rows in
those states).  Any *active* task still in one of them aborts the upgrade
with per-row remediation commands — the migration never fabricates
approval or merge state.  Archived rows keep their historical status
strings (the archive reads statuses as plain text).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "f1d7a9c20b64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LEGACY_STATUSES = ("AWAITING_APPROVAL", "AWAITING_PLAN_APPROVAL")


class _sqlite_fk_suspended:
    """Suspend SQLite FK enforcement around a batch table rebuild.

    SQLite rebuilds the referenced ``tasks`` table to drop a column;
    with ``PRAGMA foreign_keys=ON`` the rebuild trips FK checks from
    referencing tables (task_comments, task_dependencies, …).  Same
    pattern as revision 882b77dc8495.
    """

    def __init__(self, bind):
        self.bind = bind
        self.was_on = (
            bind.dialect.name == "sqlite"
            and bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        )

    def __enter__(self):
        if self.was_on:
            with op.get_context().autocommit_block():
                self.bind.exec_driver_sql("PRAGMA foreign_keys=OFF")

    def __exit__(self, *exc):
        if self.was_on:
            with op.get_context().autocommit_block():
                self.bind.exec_driver_sql("PRAGMA foreign_keys=ON")
        return False


def _preflight(bind) -> None:
    """Abort with exact remediation if active rows sit in a retired status."""
    rows = bind.execute(
        sa.text(
            "SELECT id, status, pr_url FROM tasks "
            "WHERE status IN ('AWAITING_APPROVAL', 'AWAITING_PLAN_APPROVAL') "
            "ORDER BY id"
        )
    ).fetchall()
    if not rows:
        return

    lines = []
    for task_id, status, pr_url in rows:
        if status == "AWAITING_APPROVAL" and pr_url:
            hint = (
                f"PR {pr_url} — if it merged, complete the task; if not, "
                "reopen or park it"
            )
        elif status == "AWAITING_APPROVAL":
            hint = "was waiting for manual approval; approve by completing, or reopen"
        else:
            hint = "plan discovery was removed; re-run the task or park it"
        lines.append(f"  - {task_id} [{status}]: {hint}")

    raise RuntimeError(
        "integration_mode migration preflight: "
        f"{len(rows)} task(s) are in retired approval statuses and must be "
        "dispositioned by an operator before upgrading (this migration never "
        "fabricates approval or merge state):\n"
        + "\n".join(lines)
        + "\n\nRemediation — run ONE of these per task, then re-run "
        "`alembic upgrade head`:\n"
        "  * re-run the task:      "
        "UPDATE tasks SET status='READY', assigned_agent_id=NULL WHERE id='<id>';\n"
        "  * complete (PR merged / work accepted): "
        "UPDATE tasks SET status='COMPLETED' WHERE id='<id>';\n"
        "  * park for later review: "
        "UPDATE tasks SET status='BLOCKED' WHERE id='<id>';\n"
        "On a pre-upgrade daemon the equivalent commands are "
        "`aq task approve <id>` / `aq task restart <id>`."
    )


def upgrade() -> None:
    bind = op.get_bind()
    _preflight(bind)

    op.add_column("tasks", sa.Column("integration_mode", sa.Text(), nullable=True))
    op.add_column("archived_tasks", sa.Column("integration_mode", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("integration_mode", sa.Text(), nullable=True))

    for table in ("tasks", "archived_tasks"):
        bind.execute(
            sa.text(
                f"UPDATE {table} SET integration_mode = "
                "CASE WHEN requires_approval != 0 THEN 'pull_request' ELSE 'direct' END"
            )
        )

    with _sqlite_fk_suspended(bind):
        with op.batch_alter_table("tasks") as batch_op:
            batch_op.drop_column("requires_approval")
            batch_op.drop_column("auto_approve_plan")
        with op.batch_alter_table("archived_tasks") as batch_op:
            batch_op.drop_column("requires_approval")
            batch_op.drop_column("auto_approve_plan")


def downgrade() -> None:
    bind = op.get_bind()

    for table in ("tasks", "archived_tasks"):
        op.add_column(
            table,
            sa.Column("requires_approval", sa.Integer(), nullable=False, server_default="0"),
        )
        op.add_column(
            table,
            sa.Column("auto_approve_plan", sa.Integer(), nullable=False, server_default="0"),
        )
        bind.execute(
            sa.text(
                f"UPDATE {table} SET requires_approval = "
                "CASE WHEN integration_mode = 'pull_request' THEN 1 ELSE 0 END"
            )
        )

    with _sqlite_fk_suspended(bind):
        with op.batch_alter_table("tasks") as batch_op:
            batch_op.drop_column("integration_mode")
        with op.batch_alter_table("archived_tasks") as batch_op:
            batch_op.drop_column("integration_mode")
        with op.batch_alter_table("projects") as batch_op:
            batch_op.drop_column("integration_mode")
