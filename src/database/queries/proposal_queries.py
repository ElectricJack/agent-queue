"""Persistence + cross-graph cycle detection for task_proposals.

Phase 6 Task 2 (design §8, spec ingestion).  Module-level helpers rather
than a Database mixin — the command layer calls these directly with the
adapter instance so tests can build them without inheritance gymnastics.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

from sqlalchemy import select, update

from src.database.tables import task_dependencies, task_proposals, tasks


# --- Persistence -----------------------------------------------------------


async def insert_proposal(
    db, *, project_id: str, source: str, payload: dict
) -> str:
    """Insert a fresh draft proposal and return its ``prop-``-prefixed id."""
    proposal_id = "prop-" + uuid.uuid4().hex[:12]
    now = time.time()
    async with db._engine.begin() as conn:
        await conn.execute(
            task_proposals.insert().values(
                id=proposal_id,
                project_id=project_id,
                source=source,
                payload=json.dumps(payload),
                status="draft",
                created_at=now,
                updated_at=now,
            )
        )
    return proposal_id


async def get_proposal(db, proposal_id: str) -> dict | None:
    """Return the proposal row as a dict (with JSON-decoded payload)."""
    async with db._engine.begin() as conn:
        row = (
            await conn.execute(
                select(task_proposals).where(task_proposals.c.id == proposal_id)
            )
        ).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["payload"] = json.loads(out["payload"])
    return out


async def update_proposal(
    db,
    proposal_id: str,
    *,
    payload: dict | None = None,
    status: str | None = None,
) -> bool:
    """Update payload / status on an existing proposal; returns True on hit."""
    values: dict[str, Any] = {"updated_at": time.time()}
    if payload is not None:
        values["payload"] = json.dumps(payload)
    if status is not None:
        values["status"] = status
    async with db._engine.begin() as conn:
        result = await conn.execute(
            update(task_proposals)
            .where(task_proposals.c.id == proposal_id)
            .values(**values)
        )
    return result.rowcount > 0


async def existing_graph_edges(
    db, project_id: str
) -> list[tuple[str, str, str]]:
    """Return ``(task_id, depends_on_task_id, dep_type)`` for a project."""
    async with db._engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    task_dependencies.c.task_id,
                    task_dependencies.c.depends_on_task_id,
                    task_dependencies.c.dep_type,
                )
                .select_from(
                    task_dependencies.join(
                        tasks, task_dependencies.c.task_id == tasks.c.id
                    )
                )
                .where(tasks.c.project_id == project_id)
            )
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


# --- Cycle detection -------------------------------------------------------
#
# Edge semantics: an edge (from, to, dep_type) means "from depends on to"
# (matches task_dependencies where task_id depends_on depends_on_task_id).
# We build the same graph across both existing edges and the proposal edges
# and run Tarjan-style SCC to find non-trivial strongly connected components.


def detect_cycles(
    existing_edges: Iterable[tuple[str, str, str]],
    proposal_tasks: list[dict],
    proposal_edges: list[dict],
) -> list[list[str]]:
    """Return each SCC (non-trivial) as a list of node ids; [] if acyclic."""
    graph: dict[str, set[str]] = {}
    nodes: set[str] = set()

    def add_edge(u: str, v: str) -> None:
        nodes.add(u)
        nodes.add(v)
        graph.setdefault(u, set()).add(v)

    for u, v, _ in existing_edges:
        add_edge(u, v)
    for t in proposal_tasks:
        nodes.add(t["tempId"])
    for e in proposal_edges:
        add_edge(e["from"], e["to"])

    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[list[str]] = []
    counter = [0]

    def strongconnect(v: str) -> None:
        index_of[v] = counter[0]
        lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in index_of:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index_of[w])
        if lowlink[v] == index_of[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or (
                len(comp) == 1 and comp[0] in graph.get(comp[0], ())
            ):
                sccs.append(comp)

    for v in list(nodes):
        if v not in index_of:
            strongconnect(v)

    return sccs
