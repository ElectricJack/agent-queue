"""Connection-scoped task change sets; every write belongs to the proposal transaction.

Validation executes this same path inside a rolled-back savepoint. PostgreSQL
table locks fence scheduler/claim/routing writes without relying on a process
lock that another daemon connection could bypass.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

from sqlalchemy import delete, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.hierarchy_queries import LIVE_SESSION_STATES
from src.database.queries.proposal_queries import detect_cycles
from src.database.tables import (
    gates,
    projects,
    sessions,
    task_dependencies,
    task_gates,
    task_integration_checkpoints,
    task_metadata,
    tasks,
)
from src.models import BLOCKING_DEP_TYPES, DepType, TaskStatus, TaskType
from src.routing.filing import refused_spec_keys
from src.state_machine import is_valid_status_transition, validate_waits_for
from src.task_names import child_task_id, fresh_root_id

EDIT_FIELDS = frozenset(
    {
        "task_id",
        "title",
        "description",
        "priority",
        "intelligence_class",
        "task_type",
        "parent_id",
        "action",
        "reason",
        "status",
    }
)
CREATE_FIELDS = frozenset(
    {
        "tempId",
        "title",
        "description",
        "priority",
        "intelligence_class",
        "task_type",
        "deliverables",
        "parent_id",
    }
)
OPERATIONS = ("tasks", "edges", "edits", "remove_edges", "comments")


class ChangeSetError(ValueError):
    def __init__(self, message: str, code: str = "change_set.invalid", *, refused=None):
        super().__init__(message)
        self.code = code
        self.refused = refused


def normalize(args: dict) -> dict:
    payload = {key: args.get(key, []) for key in OPERATIONS}
    if not all(isinstance(items, list) for items in payload.values()):
        raise ChangeSetError("change set operations must be lists")
    if not any(payload.values()):
        raise ChangeSetError("tasks must be a non-empty list or supply change set operations")
    specs = payload["tasks"] + payload["edits"]
    if any(not isinstance(spec, dict) for spec in specs):
        raise ChangeSetError("tasks and edits must contain objects")
    refused = refused_spec_keys(specs)
    if refused:
        raise ChangeSetError(
            f"routing.choice_forbidden: {', '.join(refused)}",
            "routing.choice_forbidden",
            refused=refused,
        )
    seen = set()
    for spec in payload["tasks"]:
        tid = spec.get("tempId")
        if not isinstance(tid, str) or not tid:
            raise ChangeSetError("tasks[].tempId is required (string)")
        if tid in seen:
            raise ChangeSetError(f"duplicate tempId '{tid}'")
        seen.add(tid)
        _fields(spec, CREATE_FIELDS)
        if not isinstance(spec.get("title"), str) or not spec["title"].strip():
            raise ChangeSetError("tasks[].title is required")
        if not isinstance(spec.get("description"), str):
            raise ChangeSetError("tasks[].description is required (string)")
    seen_edits = set()
    for edit in payload["edits"]:
        _fields(edit, EDIT_FIELDS)
        tid = edit.get("task_id")
        if not isinstance(tid, str) or not tid or tid in seen_edits or tid in seen:
            raise ChangeSetError("edits need unique existing task_id values")
        seen_edits.add(tid)
        if len(edit) == 1:
            raise ChangeSetError("edit has no changes")
        if edit.get("action") not in (None, "pause", "resume", "block", "archive"):
            raise ChangeSetError("action must be pause, resume, block or archive")
        if "action" in edit and "status" in edit:
            raise ChangeSetError("use either action or status")
        if edit.get("status") in ("PAUSED", "ASSIGNED", "IN_PROGRESS", "COMPLETED"):
            raise ChangeSetError("use pause/resume actions; execution and completion are owned")
        if "title" in edit and (not isinstance(edit["title"], str) or not edit["title"].strip()):
            raise ChangeSetError("title must be a non-empty string")
        if "description" in edit and not isinstance(edit["description"], str):
            raise ChangeSetError("description must be a string")
        if "reason" in edit and not isinstance(edit["reason"], str):
            raise ChangeSetError("reason must be a string")
    for spec in specs:
        if "priority" in spec and (
            isinstance(spec["priority"], bool) or not isinstance(spec["priority"], int)
        ):
            raise ChangeSetError("priority must be an integer")
        if spec.get("task_type") in {"promotion", "backmerge"}:
            raise ChangeSetError("Promotion and backmerge task types are daemon-owned.")
        if spec.get("task_type") is not None:
            TaskType(spec["task_type"])
        if (
            "parent_id" in spec
            and spec["parent_id"] is not None
            and (not isinstance(spec["parent_id"], str) or not spec["parent_id"])
        ):
            raise ChangeSetError("parent_id must be a task id or null")
    for key in ("edges", "remove_edges"):
        for edge in payload[key]:
            if not isinstance(edge, dict):
                raise ChangeSetError(f"{key} must contain objects")
            _fields(edge, {"from", "to", "dep_type", "reason"})
            if not all(isinstance(edge.get(k), str) and edge[k] for k in ("from", "to")):
                raise ChangeSetError(f"{key} requires 'from' and 'to'")
            if edge["from"] == edge["to"]:
                raise ChangeSetError("A task cannot depend on itself")
            DepType(edge.get("dep_type", "blocks"))
    for comment in payload["comments"]:
        if not isinstance(comment, dict):
            raise ChangeSetError("comments must contain objects")
        _fields(comment, {"task_id", "body", "kind"})
        if not isinstance(comment.get("task_id"), str) or not comment["task_id"]:
            raise ChangeSetError("comments require task_id")
        body = comment.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 16000:
            raise ChangeSetError("comment body must contain 1 to 16000 characters")
        if comment.get("kind", "note") not in ("note", "progress"):
            raise ChangeSetError("comment kind must be note or progress")
    return payload


def _fields(spec, allowed):
    if extra := set(spec) - allowed:
        raise ChangeSetError(f"unknown change set fields: {sorted(extra)}")


@asynccontextmanager
async def boundary(db, project_id):
    # Routing plans are re-selected under this fleet lock. Acquire it first
    # so their read/decision/write cannot straddle a hint change in this set.
    async with db.routing_apply_lock() as conn:
        # Hierarchy writers already take this lock before writing task rows.
        await db.lock_hierarchy_project(conn, project_id)
        # All scheduler, claim and routing UPDATEs acquire ROW EXCLUSIVE,
        # which conflicts with this mode. Plain readers remain unblocked.
        await conn.execute(
            text(
                "LOCK TABLE tasks, task_dependencies, task_metadata, gates, task_gates, "
                "task_integration_checkpoints IN SHARE ROW EXCLUSIVE MODE"
            )
        )
        yield conn


async def snapshot(conn, project_id, payload, *, previous=None):
    project = (
        (await conn.execute(select(projects).where(projects.c.id == project_id)))
        .mappings()
        .one_or_none()
    )
    if project is None:
        raise ChangeSetError(f"project '{project_id}' not found")
    rows = (
        (
            await conn.execute(
                select(tasks, literal_column("tasks.xmin::text").label("_version")).where(
                    tasks.c.project_id == project_id
                )
            )
        )
        .mappings()
        .all()
    )
    by_id = {row["id"]: dict(row) for row in rows}
    for edit in payload["edits"]:
        row = by_id.get(edit["task_id"])
        if row and "task_type" in edit and row["task_type"] in {"promotion", "backmerge"}:
            raise ChangeSetError("Promotion and backmerge task types are daemon-owned.")
    temp_ids = {t["tempId"] for t in payload["tasks"]}
    if temp_ids & by_id.keys():
        raise ChangeSetError("tempId collides with an existing task id")
    refs = {edit["task_id"] for edit in payload["edits"]}
    refs.update(c["task_id"] for c in payload["comments"])
    refs.update(s["parent_id"] for s in payload["tasks"] + payload["edits"] if s.get("parent_id"))
    for edge in payload["edges"] + payload["remove_edges"]:
        refs.update((edge["from"], edge["to"]))
    refs -= temp_ids
    if missing := refs - by_id.keys():
        raise ChangeSetError(
            f"unknown existing task ids: {sorted(missing)}",
            "change_set.conflict" if previous else "change_set.invalid",
        )
    # Ancestors and descendants participate in placement/archive guards and
    # settlement, so their versions are part of the proposal's read set too.
    scope = set(refs)
    while True:
        expanded = (
            scope
            | {
                row["parent_task_id"]
                for tid, row in by_id.items()
                if tid in scope and row["parent_task_id"]
            }
            | {tid for tid, row in by_id.items() if row["parent_task_id"] in scope}
        )
        if expanded == scope:
            break
        scope = expanded
    edges = (
        (
            await conn.execute(
                select(task_dependencies)
                .join(tasks, tasks.c.id == task_dependencies.c.task_id)
                .where(tasks.c.project_id == project_id)
                .order_by(
                    task_dependencies.c.task_id,
                    task_dependencies.c.depends_on_task_id,
                    task_dependencies.c.dep_type,
                )
            )
        )
        .mappings()
        .all()
    )
    metadata = (
        (
            await conn.execute(
                select(task_metadata)
                .where(task_metadata.c.task_id.in_(scope))
                .order_by(task_metadata.c.task_id, task_metadata.c.key)
            )
        )
        .mappings()
        .all()
    )
    checkpoints = (
        (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id.in_(scope))
                .order_by(task_integration_checkpoints.c.task_id)
            )
        )
        .mappings()
        .all()
    )
    gate_rows = (
        (
            await conn.execute(
                select(gates)
                .where(
                    gates.c.id.in_(
                        select(task_gates.c.gate_id).where(task_gates.c.task_id.in_(scope))
                    )
                )
                .order_by(gates.c.id)
            )
        )
        .mappings()
        .all()
    )
    expected = {
        "versions": {tid: by_id[tid]["_version"] for tid in sorted(scope)},
        "edges": [dict(row) for row in edges],
        "metadata": [dict(row) for row in metadata],
        "checkpoints": [dict(row) for row in checkpoints],
        "gates": [dict(row) for row in gate_rows],
        "integration_mode": project["hierarchical_integration_mode"],
    }
    return expected, by_id, project


def final_graph(payload, rows, expected):
    """Validate the resulting graph, including jointly removed/replaced edges."""
    graph = {(e["task_id"], e["depends_on_task_id"], e["dep_type"]) for e in expected["edges"]}
    for e in payload["remove_edges"]:
        edge = (e["from"], e["to"], e.get("dep_type", "blocks"))
        if edge not in graph:
            raise ChangeSetError(f"No dependency found: {edge}")
        graph.remove(edge)
    parent = {tid: row["parent_task_id"] for tid, row in rows.items()}
    for e in payload["remove_edges"]:
        if e.get("dep_type", "blocks") == "parent-child":
            parent[e["from"]] = None
    for spec in payload["tasks"]:
        parent[spec["tempId"]] = spec.get("parent_id")
    explicit = {
        spec.get("task_id", spec.get("tempId")): spec["parent_id"]
        for spec in payload["tasks"] + payload["edits"]
        if "parent_id" in spec
    }
    for tid, pid in explicit.items():
        graph = {edge for edge in graph if edge[0] != tid or edge[2] != "parent-child"}
        parent[tid] = pid
    for e in payload["edges"]:
        edge = (e["from"], e["to"], e.get("dep_type", "blocks"))
        if edge[2] == "parent-child":
            tid, pid, _ = edge
            if tid in explicit and explicit[tid] != pid:
                raise ChangeSetError("conflicting parent_id and parent-child edge")
            if parent.get(tid) not in (None, pid):
                raise ChangeSetError("multiple parents; use an edit with parent_id")
            parent[tid] = pid
        graph.add(edge)
    for tid, pid in parent.items():
        if pid is not None:
            graph.add((tid, pid, "parent-child"))
    blocking = [edge for edge in graph if edge[2] in BLOCKING_DEP_TYPES]
    if cycles := detect_cycles(blocking, payload["tasks"], []):
        raise ChangeSetError(f"proposal introduces cycle(s): {cycles}")
    containers = {pid for pid in parent.values() if pid} | {
        row["task_id"]
        for row in expected["metadata"]
        if row["key"] == "container" and json.loads(row["value"])
    }
    for tid, pid, kind in blocking:
        if kind in ("blocks", "conditional-blocks") and pid in containers:
            raise ChangeSetError(f"dependency_on_container: {tid} -> {pid}")
        if kind == "waits-for":
            validate_waits_for({child: {p} for child, p in parent.items() if p}, tid, pid)
    for tid in parent:
        depth, current = 1, tid
        while parent.get(current):
            current = parent[current]
            depth += 1
            if depth > 3:
                raise ChangeSetError("hierarchy.depth: maximum structural depth is 3")
    return parent


async def apply(handler, conn, project_id, payload, expected, rows, project, source):
    """Apply a fully validated set, returning a receipt and deferred notifications."""
    db = handler.db
    transitions = []
    parents = final_graph(payload, rows, expected)
    for spec in payload["tasks"] + payload["edits"]:
        if error := handler._validate_routing_class(spec.get("intelligence_class")):
            raise ChangeSetError(error)
    # Session shutdown and git delivery are external effects. Do not perform
    # them in a speculative validation or an atomic SQL transaction.
    controlled = {
        edit["task_id"]
        for edit in payload["edits"]
        if set(edit) - {"task_id", "title", "description", "priority"}
    }
    controlled.update(
        e["from"] for e in payload["edges"] + payload["remove_edges"] if e["from"] in rows
    )
    if controlled:
        live = (
            (
                await conn.execute(
                    select(sessions.c.task_id).where(
                        sessions.c.task_id.in_(controlled),
                        sessions.c.state.in_(LIVE_SESSION_STATES),
                    )
                )
            )
            .scalars()
            .all()
        )
        live = set(live) | {tid for tid in controlled if rows[tid]["assigned_agent_id"]}
        if live:
            raise ChangeSetError(
                f"live holders must be stopped before control changes: {sorted(live)}"
            )

    manager = getattr(handler.orchestrator, "playbook_manager", None)
    from src.playbooks.routing import requires_routing_gate

    def routing_policy(task):
        return requires_routing_gate(manager, task, {"parent_task_id": task.parent_task_id})

    hierarchical = project["hierarchical_integration_mode"] in {"hierarchy", "train"}
    service = handler._hierarchy_integration_service() if hierarchical else None
    mapping, routing_ids = {}, []
    specs = {spec["tempId"]: spec for spec in payload["tasks"]}
    pending = set(specs)
    while pending:
        ready = [
            tid
            for tid in specs
            if tid in pending and (parents[tid] not in specs or parents[tid] in mapping)
        ]
        if not ready:
            raise ChangeSetError("unresolved parent cycle")
        grouped = {}
        for tid in ready:
            pid = parents[tid]
            grouped.setdefault(mapping.get(pid, pid), []).append(tid)
        for pid, tids in grouped.items():
            models = [handler._proposal_task(project_id, specs[tid]) for tid in tids]
            for model, tid in zip(models, tids, strict=True):
                model.task_type = (
                    TaskType(specs[tid]["task_type"]) if specs[tid].get("task_type") else None
                )
            if hierarchical and pid:
                items = await service.file_prepared_children_on(
                    conn,
                    pid,
                    models,
                    routing_policy=routing_policy,
                    defer_projection=True,
                )
            else:
                items = []
                for model in models:
                    if hierarchical:
                        item = await service.file_root_on(
                            conn, model, routing_policy=routing_policy
                        )
                    else:
                        if pid:
                            model.id, capped = await child_task_id(conn, pid)
                            if capped:
                                raise ChangeSetError("hierarchy.depth: child naming cap reached")
                        else:
                            model.id = await fresh_root_id(conn)
                        model.parent_task_id = pid
                        await db.create_task(model, conn=conn, routing_policy=routing_policy)
                        if pid:
                            await db.set_parent(model.id, pid, conn=conn, defer_projection=True)
                        item = {"task_id": model.id, "gate_id": model.is_blocked}
                    items.append(item)
            for tid, item in zip(tids, items, strict=True):
                mapping[tid] = item["task_id"]
                if item.get("gate_id"):
                    routing_ids.append(item["task_id"])
                await db._upsert_meta(item["task_id"], "proposal_source", source, conn=conn)
                pending.remove(tid)

    def real(tid):
        return mapping.get(tid, tid)

    # Remove edges first, so a reversal/reparent is checked against the final
    # constraints, not the old graph. Structural membership has one writer.
    changed_parents = {
        tid: real(pid)
        for tid, pid in parents.items()
        if tid in rows and rows[tid]["parent_task_id"] != pid
    }
    for tid in changed_parents:
        await db.set_parent(tid, None, conn=conn, defer_projection=True)
    for e in payload["remove_edges"]:
        if e.get("dep_type", "blocks") != "parent-child":
            await conn.execute(
                delete(task_dependencies).where(
                    task_dependencies.c.task_id == real(e["from"]),
                    task_dependencies.c.depends_on_task_id == real(e["to"]),
                    task_dependencies.c.dep_type == e.get("dep_type", "blocks"),
                )
            )
    for tid, pid in changed_parents.items():
        if pid:
            await db.set_parent(tid, pid, conn=conn, reject_live_parent=True, defer_projection=True)
    for e in payload["edges"]:
        if e.get("dep_type", "blocks") == "parent-child":
            continue
        await conn.execute(
            pg_insert(task_dependencies)
            .values(
                task_id=real(e["from"]),
                depends_on_task_id=real(e["to"]),
                dep_type=e.get("dep_type", "blocks"),
                description=e.get("reason"),
            )
            .on_conflict_do_nothing()
        )
    affected = set(mapping.values()) | {
        real(e[k]) for e in payload["edges"] + payload["remove_edges"] for k in ("from", "to")
    }
    affected |= set(changed_parents) | {
        pid
        for tid in changed_parents
        for pid in (rows[tid]["parent_task_id"], changed_parents[tid])
        if pid
    }
    for edit in payload["edits"]:
        tid = edit["task_id"]
        affected.add(tid)
        values = {
            key: edit[key]
            for key in ("title", "description", "priority", "task_type")
            if key in edit
        }
        if "intelligence_class" in edit:
            values["class_hint"] = edit["intelligence_class"] or None
        hint_changed = bool({"class_hint", "task_type"} & values.keys())
        if (
            hint_changed
            and rows[tid]["route_source"] in ("unrouted", "router", "legacy")
            and rows[tid]["status"] in ("DEFINED", "READY", "BLOCKED", "PAUSED")
        ):
            hint = {key: values.pop(key) for key in ("class_hint", "task_type") if key in values}
            if not await db.reset_task_route(tid, **hint, conn=conn):
                raise ChangeSetError("Task is running or claimed; stop it before changing hints")
            if not (
                await conn.execute(
                    select(gates.c.id)
                    .join(task_gates, task_gates.c.gate_id == gates.c.id)
                    .where(
                        task_gates.c.task_id == tid,
                        gates.c.gate_type == "routing",
                        gates.c.status == "open",
                    )
                )
            ).first():
                await db.create_gate(
                    project_id, "routing", "Route task", waiter_task_ids=[tid], conn=conn
                )
                routing_ids.append(tid)
        if values:
            await conn.execute(
                update(tasks).where(tasks.c.id == tid).values(**values, updated_at=time.time())
            )
        action = edit.get("action")
        if action == "pause":
            _, result = await db._pause_task_on(tid, conn=conn, defer_projection=True)
            if result:
                transitions.append(result)
        elif action == "resume":
            if rows[tid]["status"] != "PAUSED":
                raise ChangeSetError("Task is not paused")
            transitions.append(
                await db._resume_locked(
                    conn,
                    tid,
                    await db._read_manual_pause(conn, tid),
                    defer_projection=True,
                )
            )
        elif action == "block" or "status" in edit:
            status = TaskStatus.BLOCKED if action == "block" else TaskStatus(edit["status"])
            administrative_block = action == "block" and rows[tid]["status"] in (
                "DEFINED",
                "READY",
                "FAILED",
                "BLOCKED",
            )
            if not administrative_block and not is_valid_status_transition(
                TaskStatus(rows[tid]["status"]), status
            ):
                raise ChangeSetError(f"invalid transition: {rows[tid]['status']} -> {status.value}")
            if await db._read_manual_pause(conn, tid):
                raise ChangeSetError("Task is manually paused; use resume")
            transitions.append(
                await db._apply_transition(
                    conn,
                    tid,
                    status,
                    context="change_set",
                    force=administrative_block,
                    _defer_projection=True,
                )
            )
            if status == TaskStatus.BLOCKED:
                await db._upsert_meta(tid, "blocked_terminal", "change_set", conn=conn)
                # Fence a scheduler decision read before this administrative
                # block even when the status was already BLOCKED.
                await conn.execute(
                    update(tasks).where(tasks.c.id == tid).values(updated_at=time.time())
                )
    scope = handler._current_scope or {}
    author_kind = "supervisor" if scope.get("kind") == "session" else "user"
    author_id = scope.get("session_id") or "operator"
    comments = []
    for comment in payload["comments"]:
        comments.append(
            await db.add_task_comment(
                real(comment["task_id"]),
                comment["body"],
                kind=comment.get("kind", "note"),
                author_kind=author_kind,
                author_id=author_id,
                conn=conn,
            )
        )
        affected.add(real(comment["task_id"]))
    archived = []
    archived_tasks = []
    archive_parents = set()
    for edit in payload["edits"]:
        if edit.get("action") == "archive":
            ids = await db.subtree_ids(edit["task_id"], conn=conn)
            archived_tasks.extend([await db._get_task_conn(tid, conn=conn) for tid in ids])
            affected |= await db._collect_affected(set(ids), conn)
            if parent := real(parents[edit["task_id"]]):
                archive_parents.add(parent)
                affected.add(parent)
            result = await db._archive_task_on(
                edit["task_id"],
                conn=conn,
                archive_reason=edit.get("reason"),
                abandoned_by=author_id,
                defer_projection=True,
            )
            if result is None:
                raise ChangeSetError("archive task not found")
            transitions.append(result[2])
            archived.extend(ids)
    for parent in archive_parents - set(archived):
        transitions.append(await db.release_stale_container_claim(parent, conn=conn))
    if affected:
        await db.mark_layout_dirty(
            project_id, sorted(affected - set(archived)), "dependency.changed", conn=conn
        )
    flipped = await db.recompute_blocked(affected - set(archived), conn=conn)
    settlement = await db.settle_containers(
        (
            affected
            | {real(parents[tid]) for tid in affected if parents.get(tid)}
            | {real(parents[tid]) for tid in mapping if parents.get(tid)}
            | {
                rows[tid]["parent_task_id"]
                for tid in changed_parents
                if rows[tid]["parent_task_id"]
            }
            | archive_parents
        )
        - set(archived),
        conn=conn,
    )
    flipped |= settlement.flipped
    newly_ready = {
        edit["task_id"] for edit in payload["edits"] if rows[edit["task_id"]]["status"] != "READY"
    }
    already_noted = {tid for tid, _ in settlement.ready}
    ready = await db._note_frontier_entry(
        conn,
        (flipped | newly_ready) - already_noted - set(archived),
        reason="unblocked",
    )
    for result in transitions:
        flipped |= result.flipped
        settlement.settled.extend(result.settled)
        settlement.ready.extend(result.ready)
    ready.extend(tid for tid, _ in settlement.ready)
    return {
        "task_ids": [mapping[spec["tempId"]] for spec in payload["tasks"]],
        "temp_ids": mapping,
        "edited_task_ids": [edit["task_id"] for edit in payload["edits"]],
        "archived_task_ids": archived,
        "comment_ids": [comment["id"] for comment in comments],
    }, {
        "routing": routing_ids,
        "flipped": flipped,
        "ready": sorted(set(ready)),
        "settled": settlement,
        "updated": sorted(affected - set(archived)),
        "comments": comments,
        "archived": archived_tasks,
        "reparented": {
            tid: (rows[tid]["parent_task_id"], pid)
            for tid, pid in changed_parents.items()
            if tid not in archived
        },
    }
