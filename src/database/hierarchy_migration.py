# src/database/hierarchy_migration.py
"""Hierarchy canonicalisation — spec §17 (preflight + revision B).

Sync SQLAlchemy Core on purpose: Alembic hands us a sync connection, and
the preflight command runs the same code through ``run_sync``.  Steps:

1. snapshot column pointers and parent-child edges into memory;
2. choose one canonical parent per task (single edge; else the edge equal
   to the column; else the oldest edge; else the column alone);
3. validate the whole candidate graph (parent exists, same project, no
   cycle, structural depth ≤ 3) and drop offenders into ``rejects``;
4. apply: rewrite edges + pointers, flag containers, backfill ordinals.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import text

MAX_STRUCTURAL_DEPTH = 3
ALLOW_REJECTS_ENV = "AQ_MIGRATION_ALLOW_REJECTS"
_ORDINAL_RE = re.compile(r"^(?P<prefix>.+)\.(?P<n>\d+)$")


@dataclass
class Reject:
    task_id: str
    parent_id: str | None
    source: str  # duplicate_edge | column_only | edge
    reason: str  # duplicate | cross_project | cycle | depth | not_found
    detail: str = ""


@dataclass
class CanonicalPlan:
    parents: dict[str, str] = field(default_factory=dict)
    rejects: list[Reject] = field(default_factory=list)
    ordinals: dict[str, int] = field(default_factory=dict)


def _snapshot(conn):
    tasks = {
        r.id: (r.project_id, r.parent_task_id)
        for r in conn.execute(text("SELECT id, project_id, parent_task_id FROM tasks"))
    }
    edges: dict[str, list[tuple[str, float]]] = {}
    # task_dependencies has no created_at; rowid order is insertion order on
    # SQLite and ctid-ish on Postgres — "oldest" means "first inserted".
    for i, r in enumerate(
        conn.execute(
            text(
                "SELECT task_id, depends_on_task_id FROM task_dependencies "
                "WHERE dep_type = 'parent-child'"
            )
        )
    ):
        edges.setdefault(r.task_id, []).append((r.depends_on_task_id, float(i)))
    archived_ids = [r[0] for r in conn.execute(text("SELECT id FROM archived_tasks"))]
    return tasks, edges, archived_ids


def canonicalise(conn) -> CanonicalPlan:
    tasks, edges, archived_ids = _snapshot(conn)
    plan = CanonicalPlan()
    candidates: dict[str, tuple[str, str]] = {}  # child -> (parent, source)

    for tid, (_proj, col) in tasks.items():
        es = sorted(edges.get(tid, []), key=lambda e: e[1])
        if len(es) == 1:
            candidates[tid] = (es[0][0], "edge")
        elif len(es) > 1:
            chosen = col if col in {p for p, _ in es} else es[0][0]
            candidates[tid] = (chosen, "edge")
            for p, _ in es:
                if p != chosen:
                    plan.rejects.append(
                        Reject(tid, p, "duplicate_edge", "duplicate", f"kept {chosen}")
                    )
        elif col:
            candidates[tid] = (col, "column_only")

    # An edge whose ``task_id`` has no ``tasks`` row cannot become a
    # candidate at all (the loop above walks live tasks).  ``apply`` deletes
    # every parent-child edge and reinserts only the plan's, so without this
    # such an edge would vanish with no record of it ever having existed.
    for tid, es in edges.items():
        if tid not in tasks:
            for p, _ in es:
                plan.rejects.append(Reject(tid, p, "edge", "not_found", "task row missing"))

    # Validate: existence, project, cycle, depth.
    parents = {c: p for c, (p, _) in candidates.items()}

    def reject(child, reason, detail=""):
        p, src = candidates[child]
        plan.rejects.append(Reject(child, p, src, reason, detail))
        parents.pop(child, None)

    for child, (p, _src) in list(candidates.items()):
        if p not in tasks:
            reject(child, "not_found")
        elif tasks[p][0] != tasks[child][0]:
            reject(child, "cross_project", f"{tasks[child][0]} vs {tasks[p][0]}")

    # Cycles: walk up from each node; a revisit means a cycle — reject the
    # edge that closes it (every member of the cycle loses its parent).
    for start in list(parents):
        seen = []
        cur = start
        while cur in parents and cur not in seen:
            seen.append(cur)
            cur = parents[cur]
        if cur in seen:
            for member in seen[seen.index(cur) :]:
                if member in parents:
                    reject(member, "cycle", " -> ".join(seen[seen.index(cur) :] + [cur]))

    def depth(node):
        d = 1
        while node in parents:
            node = parents[node]
            d += 1
        return d

    # Shallowest violator first (minimal detachment): severing the node
    # closest to the root turns it into a root and carries its whole
    # subtree one level shallower with it, so on a straight chain a single
    # reject can clear every deeper node too instead of walking the chain
    # leaf-first.  Depths are recomputed after every reject since severing
    # one node changes everyone below it.
    while True:
        violators = [c for c in parents if depth(c) > MAX_STRUCTURAL_DEPTH]
        if not violators:
            break
        victim = min(violators, key=depth)
        reject(victim, "depth", f"structural depth {depth(victim)}")

    plan.parents = dict(parents)

    # Ordinals by id prefix across live and archived ids.
    for tid in list(tasks) + archived_ids:
        m = _ORDINAL_RE.match(tid)
        if m:
            prefix, n = m.group("prefix"), int(m.group("n"))
            plan.ordinals[prefix] = max(plan.ordinals.get(prefix, 1), n + 1)
    return plan


def apply(conn, plan: CanonicalPlan) -> None:
    conn.execute(text("DELETE FROM task_dependencies WHERE dep_type = 'parent-child'"))
    conn.execute(text("UPDATE tasks SET parent_task_id = NULL"))
    for child, parent in plan.parents.items():
        conn.execute(
            text(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id, dep_type) "
                "VALUES (:c, :p, 'parent-child')"
            ),
            {"c": child, "p": parent},
        )
        conn.execute(
            text("UPDATE tasks SET parent_task_id = :p WHERE id = :c"), {"c": child, "p": parent}
        )
    for parent in set(plan.parents.values()):
        exists = conn.execute(
            text("SELECT 1 FROM task_metadata WHERE task_id = :p AND key = 'container'"),
            {"p": parent},
        ).fetchone()
        if not exists:
            conn.execute(
                text(
                    "INSERT INTO task_metadata (task_id, key, value) "
                    "VALUES (:p, 'container', 'true')"
                ),
                {"p": parent},
            )
    for prefix, n in plan.ordinals.items():
        conn.execute(
            text(
                "UPDATE tasks SET next_child_ordinal = :n WHERE id = :p AND next_child_ordinal < :n"
            ),
            {"p": prefix, "n": n},
        )


def persist_rejects(conn, run_id: str, rejects: list[Reject]) -> None:
    now = time.time()
    for r in rejects:
        conn.execute(
            text(
                "INSERT INTO hierarchy_migration_rejects "
                "(run_id, task_id, parent_id, source, reason, detail, created_at) "
                "VALUES (:run, :t, :p, :s, :r, :d, :c)"
            ),
            {
                "run": run_id,
                "t": r.task_id,
                "p": r.parent_id,
                "s": r.source,
                "r": r.reason,
                "d": r.detail,
                "c": now,
            },
        )


def write_report(path: str, run_id: str, plan: CanonicalPlan) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "run_id": run_id,
                "parents_resolved": len(plan.parents),
                "rejects": [r.__dict__ for r in plan.rejects],
                "ordinals": plan.ordinals,
            },
            fh,
            indent=2,
        )


def allow_rejects() -> bool:
    return os.environ.get(ALLOW_REJECTS_ENV) == "1"
