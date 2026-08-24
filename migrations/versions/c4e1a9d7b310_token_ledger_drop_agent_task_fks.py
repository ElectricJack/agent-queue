"""token_ledger: drop the agent_id / task_id foreign keys

Revision ID: c4e1a9d7b310
Revises: a1c7f3e08b42
Create Date: 2026-08-24 16:00:00.000000

``token_ledger`` is an append-only audit record of tokens actually spent, but
it carried real foreign keys to ``agents.id`` and ``tasks.id`` — two of the
most short-lived rows in the schema:

* **Tasks** are moved out of ``tasks`` into ``archived_tasks`` as soon as they
  reach a terminal state, so ``archive_task`` had to cascade-delete the
  matching ledger rows.  That erased the spend for *every task that ever
  finished* — i.e. essentially all of it.
* **Agents** are ephemeral.  The startup reconciler reaps any agent whose
  ``profile_id`` no longer resolves, plus idle agents over a project's
  ``max_concurrent_agents`` cap, and ``delete_agent`` cascaded into the ledger
  the same way.

Net effect: ``token_audit`` could only ever see spend belonging to tasks that
were still live and un-archived, which in a healthy queue is close to zero.
The observed symptom was a 24h audit reporting 0 tokens against 712 archived
tasks and an empty ``token_ledger``.

Both columns become plain best-effort attribution strings (still NOT NULL).
The readers were already written for this: ``get_cost_rollup`` outer-joins
``agents``, and this change is paired with outer joins in ``get_token_audit``
so unresolvable ids degrade to "(unknown)" instead of dropping the row.

``project_id`` keeps its FK on purpose — deleting a project is an explicit,
deliberate purge, and ``delete_project`` should still take the ledger with it.

SQLite cannot drop a constraint in place, so the upgrade goes through
``batch_alter_table`` with an explicit ``copy_from``: Alembic rebuilds the
table from that definition and copies the rows across.  PostgreSQL drops the
two named constraints directly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4e1a9d7b310"
down_revision = "a1c7f3e08b42"
branch_labels = None
depends_on = None


def _table(*, with_fks: bool) -> sa.Table:
    """The ``token_ledger`` definition, with or without the two FKs.

    ``batch_alter_table(copy_from=...)`` needs the table as it exists *now*
    so it can rebuild and repopulate it; the FK-free variant is the target
    shape.  ``project_id`` keeps its FK in both.
    """
    agent_args: list = [sa.Text]
    task_args: list = [sa.Text]
    if with_fks:
        agent_args.append(sa.ForeignKey("agents.id"))
        task_args.append(sa.ForeignKey("tasks.id"))

    return sa.Table(
        "token_ledger",
        sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("agent_id", *agent_args, nullable=False),
        sa.Column("task_id", *task_args, nullable=False),
        sa.Column("tokens_used", sa.Integer, nullable=False),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("timestamp", sa.Float, nullable=False),
    )


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # Named by SQLAlchemy's default convention; tolerate a DB where a
        # prior hand-edit already removed one.
        for name in ("token_ledger_agent_id_fkey", "token_ledger_task_id_fkey"):
            op.execute(f"ALTER TABLE token_ledger DROP CONSTRAINT IF EXISTS {name}")
        return

    # SQLite: rebuild the table without the two FKs.  ``copy_from`` is the
    # definition Alembic builds the *new* table from, so handing it the
    # FK-free variant with no batch operations is the whole migration.
    with op.batch_alter_table(
        "token_ledger",
        copy_from=_table(with_fks=False),
        recreate="always",
    ):
        pass


def downgrade() -> None:
    """Restore the foreign keys.

    This can fail on a database that has been running with the fixed code,
    because the whole point of the change is that the ledger now retains rows
    whose ``agent_id`` / ``task_id`` no longer exist.  Orphaned rows are
    deleted first so the constraints can be re-established — that is lossy,
    which is exactly the data loss this revision exists to stop.
    """
    op.execute("DELETE FROM token_ledger WHERE agent_id NOT IN (SELECT id FROM agents)")
    op.execute("DELETE FROM token_ledger WHERE task_id NOT IN (SELECT id FROM tasks)")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "token_ledger_agent_id_fkey", "token_ledger", "agents", ["agent_id"], ["id"]
        )
        op.create_foreign_key(
            "token_ledger_task_id_fkey", "token_ledger", "tasks", ["task_id"], ["id"]
        )
        return

    with op.batch_alter_table(
        "token_ledger",
        copy_from=_table(with_fks=True),
        recreate="always",
    ):
        pass
