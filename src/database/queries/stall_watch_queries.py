"""Read-only task, hold and worker snapshot for get_stuck_tasks."""

from __future__ import annotations

import json

from sqlalchemy import select

from src.database import tables as t
from src.stall_watch import Snapshot, apply_holds

_META_KEYS = {
    "container", "declared_container", "phase", "standing_parent", "manual_pause",
    "manual_pause_withholds_children", "blocked_terminal", "integration_retirement",
}


async def active_projects(conn, project_id: str | None) -> dict[str, dict]:
    rows = (await conn.execute(select(t.projects).where(
        t.projects.c.status == "ACTIVE",
        t.projects.c.id == project_id if project_id else True,
        ~select(t.project_constraints.c.project_id).where(
            t.project_constraints.c.project_id == t.projects.c.id,
            t.project_constraints.c.pause_scheduling == 1,
        ).exists(),
    ))).mappings()
    return {row["id"]: dict(row) for row in rows}


async def read_snapshot(db, project_id: str | None = None) -> Snapshot:
    snapshot = Snapshot()
    async with db._engine.connect() as conn:
        projects = await active_projects(conn, project_id)
        if not projects:
            return snapshot
        rows = (await conn.execute(select(t.tasks).where(
            t.tasks.c.project_id.in_(projects),
        ))).mappings()
        snapshot.tasks = {r["id"]: dict(r) for r in rows}
        ids = set(snapshot.tasks)
        for table in (t.tasks, t.archived_tasks):
            snapshot.containers.update((await conn.execute(select(table.c.parent_task_id).where(
                table.c.parent_task_id.in_(ids),
            ))).scalars())
        meta = (await conn.execute(select(t.task_metadata).where(
            t.task_metadata.c.task_id.in_(ids), t.task_metadata.c.key.in_(_META_KEYS),
        ))).mappings()
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
        snapshot.held.update((await conn.execute(select(t.task_labels.c.task_id).where(
            t.task_labels.c.task_id.in_(ids), t.task_labels.c.label.like("hold:%"),
        ))).scalars())
        held_gates = set((await conn.execute(select(t.gates.c.id).where(
            t.gates.c.project_id.in_(projects), t.gates.c.status == "open",
            t.gates.c.gate_type == "human",
        ))).scalars())
        snapshot.held.update((await conn.execute(select(t.task_gates.c.task_id).where(
            t.task_gates.c.gate_id.in_(held_gates),
        ))).scalars())
        release = t.operator_decisions.alias("watch_release")
        decisions = (await conn.execute(select(t.operator_decisions).where(
            t.operator_decisions.c.project_id.in_(projects),
            t.operator_decisions.c.effect == "hold",
            t.operator_decisions.c.object_kind == "task",
            ~select(release.c.id).where(release.c.releases == t.operator_decisions.c.id).exists(),
        ))).mappings()
        snapshot.held.update(decision["object_id"] for decision in decisions)
        snapshot.sessions = [dict(r) for r in (await conn.execute(select(t.sessions).where(
            t.sessions.c.task_id.in_(ids), t.sessions.c.ended_at.is_(None),
        ))).mappings()]
        agent_ids = {task["assigned_agent_id"] for task in snapshot.tasks.values()
                     if task["assigned_agent_id"]}
        snapshot.agents = {r["id"]: dict(r) for r in (await conn.execute(select(t.agents).where(
            t.agents.c.id.in_(agent_ids),
        ))).mappings()}
    apply_holds(snapshot)
    return snapshot
