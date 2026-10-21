"""Read-only task, hold and worker snapshot for get_stuck_tasks."""

from __future__ import annotations

import json

from sqlalchemy import select

from src.database import tables as t
from src.stall_watch import Snapshot, apply_holds

from .observation_snapshot_queries import bounded_rows, consistent_read

_META_KEYS = {
    "container", "declared_container", "phase", "standing_parent", "manual_pause",
    "manual_pause_withholds_children", "blocked_terminal", "integration_retirement",
}


async def _rows(conn, query, limit):
    if limit is not None:
        return await bounded_rows(conn, query, limit=limit)
    return (await conn.execute(query)).mappings().all()


async def active_projects(conn, project_id: str | None, limit=None) -> dict[str, dict]:
    rows = await _rows(conn, select(t.projects.c.id).where(
        t.projects.c.status == "ACTIVE",
        t.projects.c.id == project_id if project_id else True,
        ~select(t.project_constraints.c.project_id).where(
            t.project_constraints.c.project_id == t.projects.c.id,
            t.project_constraints.c.pause_scheduling == 1,
        ).exists(),
    ), limit)
    return {row["id"]: dict(row) for row in rows}


async def read_snapshot(db, project_id: str | None = None, *, max_tasks=None) -> Snapshot:
    snapshot = Snapshot()
    async with consistent_read(db._engine) as conn:
        projects = await active_projects(conn, project_id, max_tasks)
        if not projects:
            return snapshot
        query = select(*(t.tasks.c[key] for key in (
            "id", "project_id", "status", "updated_at", "assigned_agent_id",
            "parent_task_id", "claim_epoch",
        ))).where(
            t.tasks.c.project_id.in_(projects),
        )
        rows = await _rows(conn, query.order_by(t.tasks.c.id), max_tasks)
        snapshot.tasks = {r["id"]: dict(r) for r in rows}
        ids = set(snapshot.tasks)
        for table in (t.tasks, t.archived_tasks):
            snapshot.containers.update(r["parent_task_id"] for r in await _rows(
                conn, select(table.c.parent_task_id).where(
                    table.c.parent_task_id.in_(ids),
                ).distinct(), max_tasks))
        meta = await _rows(conn, select(t.task_metadata).where(
            t.task_metadata.c.task_id.in_(ids), t.task_metadata.c.key.in_(_META_KEYS),
        ), max_tasks * len(_META_KEYS) if max_tasks is not None else None)
        for row in meta:
            try:
                value = json.loads(row["value"])
            except ValueError:
                value = row["value"]
            tid, key = row["task_id"], row["key"]
            if key in {"container", "declared_container", "phase", "standing_parent"} and value:
                snapshot.containers.add(tid)
            elif key == "blocked_terminal":
                snapshot.tasks[tid][key] = value
                if value == "stop_task":
                    snapshot.held.add(tid)
            elif value:
                snapshot.held.add(tid)
        snapshot.held.update(r["task_id"] for r in await _rows(
            conn, select(t.task_labels.c.task_id).where(
                t.task_labels.c.task_id.in_(ids), t.task_labels.c.label.like("hold:%"),
            ).distinct(), max_tasks))
        snapshot.held.update(r["task_id"] for r in await _rows(
            conn, select(t.task_gates.c.task_id).join(t.gates).where(
                t.gates.c.project_id.in_(projects), t.gates.c.status == "open",
                t.gates.c.gate_type == "human", t.task_gates.c.task_id.in_(ids),
            ).distinct(), max_tasks))
        release = t.operator_decisions.alias("watch_release")
        decisions = await _rows(conn, select(t.operator_decisions.c.object_id).where(
            t.operator_decisions.c.project_id.in_(projects),
            t.operator_decisions.c.effect == "hold",
            t.operator_decisions.c.object_kind == "task",
            ~select(release.c.id).where(release.c.releases == t.operator_decisions.c.id).exists(),
        ).distinct(), max_tasks)
        snapshot.held.update(decision["object_id"] for decision in decisions)
        snapshot.sessions = [dict(r) for r in await _rows(conn, select(*(
            t.sessions.c[key] for key in ("task_id", "agent_id", "lifecycle", "ended_at",
                                         "last_claim_epoch", "state", "last_activity", "started_at")
        )).where(t.sessions.c.task_id.in_(ids), t.sessions.c.ended_at.is_(None)), max_tasks)]
        agent_ids = {task["assigned_agent_id"] for task in snapshot.tasks.values()
                     if task["assigned_agent_id"]}
        snapshot.agents = {r["id"]: dict(r) for r in await _rows(conn, select(*(
            t.agents.c[key] for key in ("id", "state", "current_task_id", "last_heartbeat")
        )).where(t.agents.c.id.in_(agent_ids)), max_tasks)}
    apply_holds(snapshot)
    return snapshot
