"""How every foreign key onto ``tasks`` is disposed of when a task leaves.

Deleting or archiving a task removes its ``tasks`` row, so every table that
references ``tasks.id`` has to be dealt with first.  ``_delete_one``
(``src/database/queries/task_queries.py``) does that for the task-owned
tables — but it can only handle the tables somebody remembered to add, and a
table it has never heard of announces itself as a raw ``IntegrityError`` from
the final ``DELETE FROM tasks``.  That is exactly how the hourly auto-archive
sweep came to fail 225 times in a row on ``integration_parent_episodes``.

:data:`TASK_REFERENCE_DISPOSITIONS` is therefore the declared answer for
*every* such foreign key, and ``tests/test_hierarchy_archive_delete.py``
walks ``src.database.tables.metadata`` and fails when a new one is missing.
A future table with a RESTRICT foreign key onto ``tasks`` breaks that test
instead of silently breaking the sweep again.

Integration audit rows are durable history, not live foreign-key ownership.
They retain a task id after archive, but still refuse a hard delete so the
history never resolves to nothing. ``src.integration.removal_guard`` owns
that policy; this module remains the compact registry and reader shared by
the guard, doctor checks, and tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from src.database.tables import (
    integration_candidate_resolutions,
    integration_parent_episodes,
    integration_parent_verifications,
    integration_repair_operations,
)

#: ``(table, column)`` → how a task leaving disposes of that reference.
#:
#: * ``"deleted"``  — ``_delete_one`` deletes the rows.
#: * ``"nulled"``   — ``_delete_one`` clears the column; the row is history.
#: * ``"released"`` — ``_delete_one`` clears the whole resource claim.
#: * ``"db_cascade"`` — the foreign key is ``ON DELETE CASCADE``.
#: * ``"subtree"``  — the hierarchy edge itself; the subtree goes deepest-first.
#: * ``"history"``  — durable integration audit; archive retains it and a
#:   hard delete is refused by ``src.integration.removal_guard``.
TASK_REFERENCE_DISPOSITIONS: dict[tuple[str, str], str] = {
    ("agents", "current_task_id"): "nulled",
    ("integration_candidate_resolutions", "repair_task_id"): "history",
    ("integration_parent_episodes", "parent_task_id"): "history",
    ("integration_parent_verifications", "parent_task_id"): "history",
    ("integration_repair_operations", "verifier_task_id"): "history",
    ("sessions", "task_id"): "nulled",
    ("task_assignment_routes", "task_id"): "db_cascade",
    ("task_context", "task_id"): "deleted",
    ("task_criteria", "task_id"): "deleted",
    ("task_dependencies", "depends_on_task_id"): "deleted",
    ("task_dependencies", "task_id"): "deleted",
    ("task_gates", "task_id"): "deleted",
    ("task_labels", "task_id"): "deleted",
    ("task_layouts", "task_id"): "deleted",
    ("task_metadata", "task_id"): "deleted",
    ("task_results", "task_id"): "deleted",
    ("task_reroutes", "task_id"): "db_cascade",
    ("task_tools", "task_id"): "deleted",
    ("task_workspace_requirements", "task_id"): "deleted",
    ("tasks", "parent_task_id"): "subtree",
    ("workspaces", "locked_by_task_id"): "released",
}


@dataclass(frozen=True)
class IntegrationTaskReference:
    """One durable history reference, with what writes it."""

    table: str
    column: str
    written_by: str


#: History entries, in the order a hard-delete refusal reports them.
INTEGRATION_TASK_REFERENCES: tuple[IntegrationTaskReference, ...] = (
    IntegrationTaskReference(
        "integration_parent_episodes",
        "parent_task_id",
        "parent collection (src/integration/parent_completion.py)",
    ),
    IntegrationTaskReference(
        "integration_parent_verifications",
        "parent_task_id",
        "parent verification (src/integration/parent_completion.py)",
    ),
    IntegrationTaskReference(
        "integration_repair_operations",
        "verifier_task_id",
        "branchless-parent verifier (src/integration/parent_completion.py)",
    ),
    IntegrationTaskReference(
        "integration_candidate_resolutions",
        "repair_task_id",
        "candidate conflict repair (src/integration/candidates.py)",
    ),
)

_COLUMNS = {
    "integration_parent_episodes": integration_parent_episodes.c.parent_task_id,
    "integration_parent_verifications": integration_parent_verifications.c.parent_task_id,
    "integration_repair_operations": integration_repair_operations.c.verifier_task_id,
    "integration_candidate_resolutions": integration_candidate_resolutions.c.repair_task_id,
}


async def find_integration_task_references(conn, ids: Sequence[str]) -> list[dict]:
    """Which of *ids* durable integration bookkeeping still names.

    Read-only, one indexed statement per table.  Returns
    ``[{"task_id", "table", "column"}]`` sorted by task then table.
    """
    if not ids:
        return []
    found: list[dict] = []
    for ref in INTEGRATION_TASK_REFERENCES:
        column = _COLUMNS[ref.table]
        rows = (
            (await conn.execute(select(column).where(column.in_(list(ids))).distinct()))
            .scalars()
            .all()
        )
        found.extend(
            {"task_id": task_id, "table": ref.table, "column": ref.column} for task_id in rows
        )
    return sorted(found, key=lambda r: (r["task_id"], r["table"]))


async def find_integration_repository_references(conn, ids: Sequence[str]) -> list[dict]:
    """Return durable integration audit rows attached directly to repositories.

    Project deletion also removes repositories. These rows are not task-id
    history, but deleting their repository would orphan evidence exactly as
    surely as deleting the task an episode names.
    """
    if not ids:
        return []
    columns = (
        ("integration_parent_episodes", "repository_id", integration_parent_episodes.c.repository_id),
        (
            "integration_candidate_resolutions",
            "repository_id",
            integration_candidate_resolutions.c.repository_id,
        ),
    )
    found: list[dict] = []
    for table, column_name, column in columns:
        rows = (await conn.execute(select(column).where(column.in_(list(ids))).distinct())).scalars().all()
        found.extend(
            {"repository_id": repository_id, "table": table, "column": column_name}
            for repository_id in rows
        )
    return sorted(found, key=lambda r: (r["repository_id"], r["table"]))


def describe_integration_references(found: Sequence[dict]) -> str:
    """One line naming the tables (and tasks) that hold a subtree back."""
    parts = [f"{row['table']}({row.get('task_id') or row['repository_id']})" for row in found]
    return ", ".join(parts)
