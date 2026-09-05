"""Status-leading task index and pattern-ops task_layouts path index.

Revision ID: 806ad9f97451
Revises: e6a1b2c3d4f5

A dashboard performance investigation found two index gaps:

* Status-only task lists (``list_tasks(status=...)`` in the monitoring
  cycle, ``aq task list --status``) had no index leading with ``status``,
  so the query seq-scanned ``tasks`` as completed history grew.
  ``idx_tasks_status_project`` fixes that.

* ``idx_task_layouts_path`` already existed, but on PostgreSQL a plain
  btree under a non-C collation cannot serve a ``LIKE 'prefix%'`` query,
  which is exactly how ``load_paths_by_prefixes`` / ``load_subtree_ids``
  filter ``path``. The measured run recorded zero scans of this index.
  Recreating it with ``text_pattern_ops`` makes the prefix scan
  index-driven on PostgreSQL; SQLite ignores the op class, so the index
  is functionally unchanged there.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "806ad9f97451"
down_revision: str | Sequence[str] | None = "e6a1b2c3d4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("idx_tasks_status_project", "tasks", ["status", "project_id"])
    op.drop_index("idx_task_layouts_path", table_name="task_layouts")
    op.create_index(
        "idx_task_layouts_path",
        "task_layouts",
        ["project_id", "variant", "path"],
        postgresql_ops={"path": "text_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index("idx_task_layouts_path", table_name="task_layouts")
    op.create_index("idx_task_layouts_path", "task_layouts", ["project_id", "variant", "path"])
    op.drop_index("idx_tasks_status_project", table_name="tasks")
