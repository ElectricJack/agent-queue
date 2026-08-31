"""add mandatory triage persistence

Revision ID: 72671cc2b86c
Revises: f1d7a9c20b64
Create Date: 2026-08-31
"""

from contextlib import contextmanager
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "72671cc2b86c"
down_revision: Union[str, Sequence[str], None] = "f1d7a9c20b64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


@contextmanager
def _sqlite_rebuild():
    """Allow cyclic FK tables to be rebuilt, restoring enforcement afterward."""
    bind = op.get_bind()
    foreign_keys = (
        bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    if foreign_keys:
        with op.get_context().autocommit_block():
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        yield
    finally:
        if foreign_keys:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    with _sqlite_rebuild():
        _upgrade()


def _upgrade() -> None:
    op.create_table(
        "task_routing_decisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("routing_revision", sa.Integer(), nullable=False),
        sa.Column("playbook_run_id", sa.Text(), nullable=False),
        sa.Column("playbook_id", sa.Text(), nullable=False),
        sa.Column("playbook_version", sa.Integer(), nullable=False),
        sa.Column("execution_type_key", sa.Text(), nullable=False),
        sa.Column("execution_snapshot", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("intelligence_class", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.UniqueConstraint("task_id", "routing_revision", name="uq_task_routing_revision"),
    )
    op.create_index(
        "idx_task_routing_decisions_project",
        "task_routing_decisions",
        ["project_id", "decided_at"],
        unique=False,
    )
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("last_assigned_at", sa.Float(), nullable=True))
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("triage_playbook_id", sa.Text(), nullable=True))
    with op.batch_alter_table("playbook_runs") as batch:
        batch.add_column(sa.Column("project_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("role", sa.Text(), nullable=True))
        batch.add_column(sa.Column("owner_session_id", sa.Text(), nullable=True))
        batch.create_foreign_key("fk_playbook_runs_project", "projects", ["project_id"], ["id"])
        batch.create_foreign_key(
            "fk_playbook_runs_owner_session",
            "sessions",
            ["owner_session_id"],
            ["id"],
            ondelete="SET NULL",
            use_alter=True,
        )
        batch.create_index(
            "uq_active_triage_run_project",
            ["project_id"],
            unique=True,
            sqlite_where=sa.text("role = 'triage' AND status IN ('running', 'paused')"),
            postgresql_where=sa.text("role = 'triage' AND status IN ('running', 'paused')"),
        )
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("playbook_run_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("playbook_node_id", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_sessions_playbook_run",
            "playbook_runs",
            ["playbook_run_id"],
            ["run_id"],
            ondelete="SET NULL",
            use_alter=True,
        )
    for table in ("tasks", "archived_tasks"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("routing_revision", sa.Integer(), server_default="1", nullable=False)
            )
            batch.add_column(
                sa.Column("routing_request", sa.Text(), server_default="{}", nullable=False)
            )
            batch.add_column(sa.Column("routing_decision_id", sa.Text(), nullable=True))
            batch.add_column(sa.Column("control_origin", sa.Text(), nullable=True))
            if table == "tasks":
                batch.create_foreign_key(
                    "fk_tasks_routing_decision",
                    "task_routing_decisions",
                    ["routing_decision_id"],
                    ["id"],
                    ondelete="SET NULL",
                    use_alter=True,
                )


def downgrade() -> None:
    bind = op.get_bind()
    decisions = bind.execute(sa.text("SELECT COUNT(*) FROM task_routing_decisions")).scalar_one()
    live_sessions = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM sessions s JOIN playbook_runs r "
            "ON r.run_id = s.playbook_run_id WHERE r.role = 'triage' "
            "AND s.state IN ('starting', 'running', 'draining')"
        )
    ).scalar_one()
    if decisions or live_sessions:
        raise RuntimeError(
            "Refusing mandatory-triage downgrade: audit data or live triage sessions exist. "
            "Export task_routing_decisions, stop triage sessions, verify the export, then "
            "delete the exported decisions before retrying the rollback."
        )
    with _sqlite_rebuild():
        _downgrade()


def _downgrade() -> None:
    for table in ("tasks", "archived_tasks"):
        with op.batch_alter_table(table) as batch:
            if table == "tasks":
                batch.drop_constraint("fk_tasks_routing_decision", type_="foreignkey")
            batch.drop_column("control_origin")
            batch.drop_column("routing_decision_id")
            batch.drop_column("routing_request")
            batch.drop_column("routing_revision")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_constraint("fk_sessions_playbook_run", type_="foreignkey")
        batch.drop_column("playbook_node_id")
        batch.drop_column("playbook_run_id")
    with op.batch_alter_table("playbook_runs") as batch:
        batch.drop_constraint("fk_playbook_runs_owner_session", type_="foreignkey")
        batch.drop_constraint("fk_playbook_runs_project", type_="foreignkey")
        batch.drop_index(
            "uq_active_triage_run_project",
            sqlite_where=sa.text("role = 'triage' AND status IN ('running', 'paused')"),
            postgresql_where=sa.text("role = 'triage' AND status IN ('running', 'paused')"),
        )
        batch.drop_column("owner_session_id")
        batch.drop_column("role")
        batch.drop_column("project_id")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("triage_playbook_id")
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("last_assigned_at")
    op.drop_index("idx_task_routing_decisions_project", table_name="task_routing_decisions")
    op.drop_table("task_routing_decisions")
