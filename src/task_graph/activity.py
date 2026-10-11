"""A container's displayed activity, rolled up from its descendants.

A container's stored lifecycle is bookkeeping: an epic or phase reads
``IN_PROGRESS`` while any child is unfinished, even when nothing under it is
running. This module derives what the dashboard shows instead -- computed on
read, never stored -- from the executable leaves of the subtree:

- ``active``: at least one descendant is claimed or running;
- ``blocked``: every unfinished descendant is blocked or failed;
- ``waiting``: the rest (queued, paused, gated or waiting on a dependency),
  with a short reason.

Graph, layout, list, progress and detail reads all call `load_activity`, so
every surface agrees. ``display_state`` is set only while the container's own
lifecycle is a working one (``_ROLLING``); a paused or blocked container keeps
its stored status, though counts and reason are still returned.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import exists, func, literal, select

from src.database.tables import gates, task_dependencies, task_gates, task_labels, tasks
from src.task_names import MAX_STRUCTURAL_DEPTH

FIELDS = ("display_state", "display_reason", "child_state_counts", "descendant_state_counts")
#: Gates only a person can open.
HUMAN_GATE_TYPES = ("human", "review")

_DONE = {"COMPLETED", "CANCELLED", "CANCELED", "SKIPPED"}
_WAIT_DEPENDENCIES = ("blocks", "conditional-blocks", "waits-for")
#: Container lifecycles a rollup may replace on display.
_ROLLING = {"DEFINED", "READY", "ASSIGNED", "IN_PROGRESS"}


def _leaf_state(row: Mapping[str, Any]) -> str:
    status = row.get("status")
    if status in _DONE:
        return "completed"
    if status in {"IN_PROGRESS", "ASSIGNED"} or (status == "READY" and row.get("assigned_agent_id")):
        return "active"
    if status in {"BLOCKED", "FAILED"}:
        return "blocked"
    return "waiting"


def _combine(states: Iterable[str]) -> str:
    states = list(states)
    if "active" in states:
        return "active"
    unfinished = [state for state in states if state != "completed"]
    if not unfinished:
        return "completed"
    return "blocked" if all(state == "blocked" for state in unfinished) else "waiting"


def _paused(row: Mapping[str, Any]) -> bool:
    return row.get("status") == "PAUSED" or bool(row.get("held"))


def _waiting_reason(context: list[Mapping[str, Any]], leaves: list[Mapping[str, Any]]) -> str:
    """Why nothing runs; ``context`` adds waiting containers, whose gate or
    hold withholds their leaves without being copied onto each one."""
    if any(row.get("human_gate") or row.get("status") == "WAITING_INPUT" for row in context):
        return "Needs you: waiting on a decision"
    if any(_paused(row) for row in context):
        return "Paused" if all(_paused(row) for row in leaves) else "Some work is paused"
    if blocker := next((row["blocker_id"] for row in context if row.get("blocker_id")), None):
        return f"Waiting on {blocker}"
    if any(row.get("gated") or row.get("is_blocked") for row in context):
        return "Waiting on dependencies or gates"
    if any(row.get("status") == "READY" for row in leaves):
        return "Waiting for a worker"
    return "Waiting for work to become ready"


def rollup(
    root_id: str, rows: Iterable[Mapping[str, Any]], own_status: str | None = None
) -> dict[str, Any]:
    """Roll ``rows`` (the subtree under ``root_id``) up into display fields.

    Direct-child counts use each child's computed state; descendant counts
    are the stored statuses of the leaves, so queued, paused and failed work
    stay distinct and intermediate containers are not double counted.
    Returns ``{}`` when ``root_id`` has no children.
    """
    rows = list(rows)
    by_parent: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_parent.setdefault(row.get("parent_task_id") or root_id, []).append(row)
    if root_id not in by_parent:
        return {}
    memo: dict[str, str] = {}

    def state_of(row: Mapping[str, Any]) -> str:
        if row["id"] not in memo:
            children = by_parent.get(row["id"])
            memo[row["id"]] = _combine(map(state_of, children)) if children else _leaf_state(row)
        return memo[row["id"]]

    leaves = [row for row in rows if row["id"] not in by_parent]
    direct = [state_of(row) for row in by_parent[root_id]]
    state = _combine(direct)
    if state == "active":
        running = sum(state_of(row) == "active" for row in leaves)
        reason = f"{running} descendant{'s' if running != 1 else ''} working"
    elif state == "blocked":
        reason = "All unfinished descendants are blocked or failed"
    elif state == "waiting":
        waiting = [row for row in leaves if state_of(row) == "waiting"]
        reason = _waiting_reason([row for row in rows if state_of(row) == "waiting"], waiting)
    else:
        reason = "All descendants finished"
    rolling = state != "completed" and (own_status is None or own_status in _ROLLING)
    return {
        "display_state": state if rolling else None,
        "display_reason": reason,
        "child_state_counts": dict(sorted(Counter(direct).items())),
        "descendant_state_counts": dict(
            sorted(Counter(row.get("status") or "DEFINED" for row in leaves).items())
        ),
    }


def descendants_query(ids: Iterable[str]):
    """Every descendant of ``ids`` (to the structural depth), one statement.

    Root identity travels with each row, so nested containers each receive
    their complete subtree without an N+1 query.
    """
    subtree = select(
        tasks.c.parent_task_id.label("root_id"), tasks.c.id, literal(1).label("depth"),
    ).where(tasks.c.parent_task_id.in_(list(ids))).cte("activity_subtree", recursive=True)
    subtree = subtree.union_all(
        select(subtree.c.root_id, tasks.c.id, subtree.c.depth + 1)
        .join(subtree, tasks.c.parent_task_id == subtree.c.id)
        .where(subtree.c.depth < MAX_STRUCTURAL_DEPTH)
    )
    root = tasks.alias("activity_root")
    predecessor = tasks.alias("activity_predecessor")
    blocker = select(func.min(task_dependencies.c.depends_on_task_id)).select_from(
        task_dependencies.join(predecessor, predecessor.c.id == task_dependencies.c.depends_on_task_id)
    ).where(
        task_dependencies.c.task_id == tasks.c.id,
        task_dependencies.c.dep_type.in_(_WAIT_DEPENDENCIES),
        predecessor.c.status.not_in(sorted(_DONE)),
    ).correlate(tasks).scalar_subquery()

    def open_gate(*types: str):
        query = select(literal(1)).select_from(
            task_gates.join(gates, gates.c.id == task_gates.c.gate_id)
        ).where(task_gates.c.task_id == tasks.c.id, gates.c.status == "open")
        if types:
            query = query.where(gates.c.gate_type.in_(types))
        return exists(query.correlate(tasks))

    held = exists(select(literal(1)).where(
        task_labels.c.task_id == tasks.c.id, task_labels.c.label.like("hold:%"),
    ).correlate(tasks))
    return (
        select(
            subtree.c.root_id, root.c.status.label("root_status"), tasks.c.id,
            tasks.c.parent_task_id, tasks.c.status, tasks.c.assigned_agent_id, tasks.c.is_blocked,
            open_gate().label("gated"), open_gate(*HUMAN_GATE_TYPES).label("human_gate"),
            blocker.label("blocker_id"), held.label("held"),
        )
        .select_from(subtree.join(tasks, tasks.c.id == subtree.c.id).join(root, root.c.id == subtree.c.root_id))
        .order_by(subtree.c.root_id, tasks.c.id)
    )


async def load_activity(db, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """``{task_id: display fields}`` for each of ``ids`` that has children."""
    ids = sorted(set(ids))
    if not ids:
        return {}
    async with db._engine.connect() as conn:
        result = await conn.execute(descendants_query(ids))
        subtrees: dict[str, list[Mapping[str, Any]]] = {}
        for row in result.mappings():
            subtrees.setdefault(row["root_id"], []).append(row)
    return {
        root_id: rollup(root_id, rows, own_status=rows[0]["root_status"])
        for root_id, rows in subtrees.items()
    }


async def attach_activity(db, rows: list[dict[str, Any]]) -> None:
    """Merge display fields into task dicts that carry an ``id``, in place."""
    activity = await load_activity(db, [row["id"] for row in rows])
    for row in rows:
        row.update(activity.get(row["id"], {}))


async def task_activity(db, task_id: str) -> dict[str, Any]:
    return (await load_activity(db, [task_id])).get(task_id, {})
