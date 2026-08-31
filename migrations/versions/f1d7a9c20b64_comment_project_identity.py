"""Isolate comments when legacy task IDs collide across projects."""
from alembic import op
import sqlalchemy as sa

revision = "f1d7a9c20b64"
down_revision = "e8b39a10c572"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_comments", sa.Column("project_id", sa.Text(), nullable=True))
    # Do not guess which project authored a comment for a colliding ID.
    # Preserve ambiguous/orphan rows as NULL; readers must not expose them.
    op.execute(sa.text("""
        UPDATE task_comments
        SET project_id = COALESCE(
            (SELECT project_id FROM tasks WHERE tasks.id = task_comments.task_id),
            (SELECT project_id FROM archived_tasks WHERE archived_tasks.id = task_comments.task_id)
        )
        WHERE NOT EXISTS (
            SELECT 1 FROM tasks JOIN archived_tasks ON tasks.id = archived_tasks.id
            WHERE tasks.id = task_comments.task_id
              AND tasks.project_id <> archived_tasks.project_id
        )
    """))
    op.create_index("idx_task_comments_project_created", "task_comments",
                    ["task_id", "project_id", "created_at", "id"])


def downgrade():
    op.drop_index("idx_task_comments_project_created", table_name="task_comments")
    with op.batch_alter_table("task_comments") as batch:
        batch.drop_column("project_id")
