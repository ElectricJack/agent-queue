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

The ``"refused"`` disposition is the integration subsystem's append-only
bookkeeping: rows the archive path must not delete (a database trigger
forbids it) and must not orphan (the foreign key is RESTRICT and the column
is the audit row's own identity).  Those tasks are refused up front with a
:class:`~src.database.queries.hierarchy_queries.HierarchyError`
``integration_owned`` rather than being allowed to reach the ``DELETE``.
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
#: * ``"refused"``  — durable integration audit; archive/delete is refused.
TASK_REFERENCE_DISPOSITIONS: dict[tuple[str, str], str] = {
    ("agents", "current_task_id"): "nulled",
    ("integration_candidate_resolutions", "repair_task_id"): "refused",
    ("integration_parent_episodes", "parent_task_id"): "refused",
    ("integration_parent_verifications", "parent_task_id"): "refused",
    ("integration_repair_operations", "verifier_task_id"): "refused",
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
    ("task_tools", "task_id"): "deleted",
    ("task_workspace_requirements", "task_id"): "deleted",
    ("tasks", "parent_task_id"): "subtree",
    ("workspaces", "locked_by_task_id"): "released",
}


@dataclass(frozen=True)
class IntegrationTaskReference:
    """One ``"refused"`` foreign key, with what writes it."""

    table: str
    column: str
    written_by: str


#: The ``"refused"`` entries, in the order a refusal reports them.
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


def describe_integration_references(found: Sequence[dict]) -> str:
    """One line naming the tables (and tasks) that hold a subtree back."""
    parts = [f"{row['table']}({row['task_id']})" for row in found]
    return ", ".join(parts)


async def assert_no_integration_task_references(conn, ids: Sequence[str], mutation: str) -> None:
    """Refuse *mutation* while integration bookkeeping still names the subtree.

    Raised before anything is written, so the caller's transaction has not
    touched a row when the refusal lands.  ``integration_owned`` is the same
    code ``archive_task`` already uses for an active repair operation.
    """
    from src.database.queries.hierarchy_queries import HierarchyError

    found = await find_integration_task_references(conn, ids)
    if not found:
        return
    raise HierarchyError(
        "integration_owned",
        f"{mutation} would orphan {len(found)} integration record(s): "
        f"{describe_integration_references(found)}",
        {"references": found},
    )
