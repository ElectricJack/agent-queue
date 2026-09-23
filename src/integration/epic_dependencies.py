"""Declared epic dependencies and deterministic integration-batch ordering."""

from __future__ import annotations

from heapq import heappop, heappush

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import epic_dependencies, tasks


async def declare(conn, *, dependent_task_id: str, dependency_task_id: str, now: float) -> None:
    """Record an epic dependency without changing an earlier declaration."""
    statement = pg_insert(epic_dependencies).values(
        dependent_task_id=dependent_task_id,
        dependency_task_id=dependency_task_id,
        declared_at=now,
    )
    await conn.execute(statement.on_conflict_do_nothing(constraint="pk_epic_dependencies"))


async def dependencies_for(conn, task_ids: list[str]) -> dict[str, set[str]]:
    """Return dependencies for the requested epics only."""
    if not task_ids:
        return {}
    rows = (
        await conn.execute(
            select(
                epic_dependencies.c.dependent_task_id,
                epic_dependencies.c.dependency_task_id,
            ).where(epic_dependencies.c.dependent_task_id.in_(task_ids))
        )
    ).all()
    edges: dict[str, set[str]] = {}
    for dependent, dependency in rows:
        edges.setdefault(dependent, set()).add(dependency)
    return edges


async def dependents_of(conn, dependency_task_id: str) -> set[str]:
    """Return live epics that declared a dependency on this epic."""
    rows = (
        await conn.execute(
            select(epic_dependencies.c.dependent_task_id)
            .join(tasks, tasks.c.id == epic_dependencies.c.dependent_task_id)
            .where(epic_dependencies.c.dependency_task_id == dependency_task_id)
        )
    ).scalars().all()
    return set(rows)


def order_members(
    members: list[dict], edges: dict[str, set[str]], delivered: set[str]
) -> tuple[list[dict], list[dict]]:
    """Place dependencies first; defer members with absent or cyclic blockers.

    The smallest ready task id is placed next, so unrelated members retain
    their previous lexicographic order even when a dependency becomes ready.
    """
    by_id = {member["task_id"]: member for member in members}
    if len(by_id) != len(members):
        raise ValueError("integration batch contains duplicate task ids")

    remaining = {task_id: set(edges.get(task_id, set())) - delivered for task_id in by_id}
    dependents: dict[str, set[str]] = {task_id: set() for task_id in by_id}
    for task_id, dependencies in remaining.items():
        for dependency in dependencies & by_id.keys():
            dependents[dependency].add(task_id)

    ready = [task_id for task_id, dependencies in remaining.items() if not dependencies]
    ready.sort()
    ordered: list[dict] = []
    placed: set[str] = set()
    while ready:
        task_id = heappop(ready)
        ordered.append(by_id[task_id])
        placed.add(task_id)
        for dependent in dependents[task_id]:
            remaining[dependent].remove(task_id)
            if not remaining[dependent]:
                heappush(ready, dependent)

    deferred = [by_id[task_id] for task_id in sorted(by_id.keys() - placed)]
    return ordered, deferred
