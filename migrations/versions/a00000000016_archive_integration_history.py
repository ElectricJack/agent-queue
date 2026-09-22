"""Keep integration audit ids after task archive.

Revision ID: a00000000016
Revises: a00000000015

Integration episode, verification, verifier, and candidate-resolution rows are
append-only history.  Their task ids remain meaningful after the task moves to
``archived_tasks``; they must not retain a foreign key to the live ``tasks``
table.  The application guard still refuses hard deletion of those ids.

The squashed baseline is built from current metadata, so this revision must be
a no-op for a fresh database.  It discovers constraints by table/column rather
than by their historic names: installations may have named them differently.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000016"
down_revision = "a00000000015"
branch_labels = None
depends_on = None

_HISTORY_COLUMNS = (
    ("integration_parent_episodes", "parent_task_id"),
    ("integration_parent_verifications", "parent_task_id"),
    ("integration_repair_operations", "verifier_task_id"),
    ("integration_candidate_resolutions", "repair_task_id"),
)
_INDEXES = (
    ("idx_integration_repair_operations_verifier_task", "integration_repair_operations", "verifier_task_id", "verifier_task_id IS NOT NULL"),
    ("idx_integration_candidate_resolutions_repair_task", "integration_candidate_resolutions", "repair_task_id", None),
    ("idx_integration_repair_stages_repair_task", "integration_repair_stages", "repair_task_id", "repair_task_id IS NOT NULL"),
)
_TASK_FKS = sa.text(
    """
    SELECT c.conname, array_length(c.conkey, 1) AS width
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.contype = 'f'
       AND c.conrelid = to_regclass(:table)
       AND c.confrelid = to_regclass('tasks')
       AND a.attname = :column
    """
)


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _foreign_keys(bind, table: str, column: str) -> list[tuple[str, int]]:
    return [(str(name), int(width)) for name, width in bind.execute(
        _TASK_FKS, {"table": table, "column": column}
    ).all()]


def upgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    for table, column in _HISTORY_COLUMNS:
        if table not in existing:
            continue
        for name, width in _foreign_keys(bind, table, column):
            if width != 1:
                raise RuntimeError(f"{table}.{column}: composite foreign key {name} onto tasks")
            op.drop_constraint(name, table, type_="foreignkey")
    left = [
        (table, column, name)
        for table, column in _HISTORY_COLUMNS
        if table in existing
        for name, _ in _foreign_keys(bind, table, column)
    ]
    if left:
        raise RuntimeError(f"foreign keys onto tasks remain: {left}")

    indexes = {row["name"] for table in existing for row in sa.inspect(bind).get_indexes(table)}
    for name, table, column, predicate in _INDEXES:
        if table in existing and name not in indexes:
            op.create_index(name, table, [column], postgresql_where=sa.text(predicate) if predicate else None)


def downgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    orphaned: list[tuple[str, str, int]] = []
    for table, column in _HISTORY_COLUMNS:
        if table not in existing:
            continue
        count = bind.execute(
            sa.text(
                f"SELECT count(*) FROM {table} h WHERE h.{column} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.id = h.{column})"
            )
        ).scalar_one()
        if count:
            orphaned.append((table, column, int(count)))
    if orphaned:
        raise RuntimeError(f"cannot restore task foreign keys; archived history remains: {orphaned}")

    for name, table, _, _ in _INDEXES:
        if table in existing and name in {row["name"] for row in sa.inspect(bind).get_indexes(table)}:
            op.drop_index(name, table_name=table)

    constraints = (
        ("fk_integration_parent_episodes_parent_task", "integration_parent_episodes", ["parent_task_id"], "RESTRICT"),
        ("fk_integration_parent_verifications_parent_task", "integration_parent_verifications", ["parent_task_id"], "RESTRICT"),
        ("fk_integration_repair_operations_verifier_task", "integration_repair_operations", ["verifier_task_id"], "RESTRICT"),
        ("fk_integration_candidate_resolutions_task", "integration_candidate_resolutions", ["repair_task_id"], None),
    )
    for name, table, columns, ondelete in constraints:
        if table not in existing or _foreign_keys(bind, table, columns[0]):
            continue
        op.create_foreign_key(name, table, "tasks", columns, ["id"], ondelete=ondelete)
