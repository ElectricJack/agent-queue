"""Add persisted playbook assignment routes."""

from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa

revision = "a7c91e4d2b63"
down_revision = "f1d7a9c20b64"
branch_labels = None
depends_on = None


@contextmanager
def _sqlite_fk_suspended():
    """Allow SQLite to rebuild the referenced projects table on downgrade."""
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


def upgrade():
    op.add_column(
        "projects", sa.Column("assignment_playbook_id", sa.Text(), nullable=True)
    )
    op.create_table(
        "task_assignment_routes",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("task_updated_at", sa.Float(), nullable=False),
        sa.Column("options_hash", sa.Text(), nullable=False),
        sa.Column("intelligence_class", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("playbook_id", sa.Text(), nullable=False),
        sa.Column("playbook_version", sa.Integer(), nullable=False),
        sa.Column("playbook_run_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["playbook_run_id"], ["playbook_runs.run_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        "idx_task_assignment_routes_project",
        "task_assignment_routes",
        ["project_id"],
    )


def downgrade():
    with _sqlite_fk_suspended():
        op.drop_index("idx_task_assignment_routes_project", table_name="task_assignment_routes")
        op.drop_table("task_assignment_routes")
        with op.batch_alter_table("projects") as batch:
            batch.drop_column("assignment_playbook_id")
