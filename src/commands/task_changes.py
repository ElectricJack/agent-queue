"""Connection-scoped task change sets; every write belongs to the proposal transaction.

Validation executes this same path inside a rolled-back savepoint. PostgreSQL
table locks fence scheduler/claim/routing writes without relying on a process
lock that another daemon connection could bypass.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.hierarchy_queries import LIVE_SESSION_STATES
from src.database.queries import ingest_batch_queries as batches
from src.database.queries.proposal_queries import detect_cycles
from src.database.tables import (
    gates,
    ingest_batch_members,
    projects,
    sessions,
    task_dependencies,
    task_gates,
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
        "section_anchor",
        "primary_outcome",
        "acceptance",
        "constraints",
        "owned_modules",
        "size",
        "placement",
        "delivery_targets",
    }
)
OPERATIONS = ("tasks", "edges", "edits", "remove_edges", "comments")


class ChangeSetError(ValueError):
    def __init__(self, message: str, code: str = "change_set.invalid", *, refused=None):
        super().__init__(message)
        self.code = code
        self.refused = refused


# These fields are available only inside an authorized held-batch update.
HELD_EDIT_FIELDS = frozenset({
    "acceptance", "constraints", "section_anchor", "primary_outcome", "size", "owned_modules",
    "placement", "delivery_targets", "tags", "replacement_task_id", "satisfied_by", "rewire",
})


def normalize(args: dict, *, held: bool = False) -> dict:
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
        for key in ("section_anchor", "primary_outcome"):
            if key in spec and (not isinstance(spec[key], str) or not spec[key].strip()):
                raise ChangeSetError(f"{key} must be a non-empty string", f"{key}_missing")
        if "acceptance" in spec and not (
            isinstance(spec["acceptance"], str)
            or isinstance(spec["acceptance"], list)
            and all(isinstance(item, str) for item in spec["acceptance"])
        ):
            raise ChangeSetError("acceptance must be a string or list of strings")
        for key in ("constraints", "owned_modules", "delivery_targets"):
            if key in spec and (not isinstance(spec[key], list)
                               or any(not isinstance(v, str) or not v.strip() for v in spec[key])):
                raise ChangeSetError(f"{key} must be a list of non-empty strings")
        if "size" in spec and spec["size"] not in ("s", "m", "l"):
            raise ChangeSetError("size must be s, m or l")
        if "placement" in spec and spec["placement"] not in ("child", "operational_root"):
            raise ChangeSetError("placement must be child or operational_root")
        if "delivery_targets" in spec and spec.get("placement") != "operational_root":
            raise ChangeSetError("delivery_targets require placement operational_root")
        if spec.get("placement") == "operational_root" and not spec.get("delivery_targets"):
            raise ChangeSetError("operational_root requires delivery_targets")
    seen_edits = set()
    for edit in payload["edits"]:
        _fields(edit, EDIT_FIELDS | HELD_EDIT_FIELDS if held else EDIT_FIELDS)
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
    if held:
        for edit in payload["edits"]:
            for key in ("section_anchor", "primary_outcome"):
                if key in edit and (not isinstance(edit[key], str) or not edit[key].strip()):
                    raise ChangeSetError(f"{key} must be a non-empty string")
            if "acceptance" in edit and not (
                isinstance(edit["acceptance"], str) or isinstance(edit["acceptance"], list)
                and all(isinstance(v, str) for v in edit["acceptance"])
            ):
                raise ChangeSetError("acceptance must be a string or list of strings")
            for key in ("constraints", "owned_modules", "tags", "delivery_targets"):
                if key in edit and (not isinstance(edit[key], list)
                                    or any(not isinstance(v, str) or not v.strip()
                                           for v in edit[key])):
                    raise ChangeSetError(f"{key} must be a list of non-empty strings")
            if "size" in edit and edit["size"] not in ("s", "m", "l"):
                raise ChangeSetError("size must be s, m or l")
            if "placement" in edit and edit["placement"] != "operational_root":
                raise ChangeSetError("placement must be operational_root")
            if edit.get("placement") == "operational_root" and not edit.get("delivery_targets"):
                raise ChangeSetError("operational_root requires delivery_targets")
    return payload


def _fields(spec, allowed):
    if extra := set(spec) - allowed:
        raise ChangeSetError(f"unknown change set fields: {sorted(extra)}")


@asynccontextmanager
async def boundary(db, project_id, *, source_lock=None):
    # Routing plans are re-selected under this fleet lock. Acquire it first
    # so their read/decision/write cannot straddle a hint change in this set.
    async with db.routing_apply_lock() as conn:
        if source_lock is not None:
            await source_lock(conn)
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


def references(payload):
    refs = {edit["task_id"] for edit in payload["edits"]}
    refs.update(c["task_id"] for c in payload["comments"])
    refs.update(s["parent_id"] for s in payload["tasks"] + payload["edits"] if s.get("parent_id"))
    refs.update(target for spec in payload["tasks"] for target in spec.get("delivery_targets", []))
    for edge in payload["edges"] + payload["remove_edges"]:
        refs.update((edge["from"], edge["to"]))
    for edit in payload["edits"]:
        refs.update(edit.get("delivery_targets", []))
        if edit.get("replacement_task_id"):
            refs.add(edit["replacement_task_id"])
        for edges in (edit.get("rewire") or {}).values():
            for edge in edges:
                refs.update((edge["from"], edge["to"]))
    return refs - {spec["tempId"] for spec in payload["tasks"]}


def surviving_operations(payload, rows):
    """Keep approved operations with surviving targets; never change their content.

    A create whose structural parent disappeared cannot become a root instead.
    Propagate that omission through new descendants and references to them, while
    recording the original missing task ids rather than unmaterialised temp ids.
    """
    missing = references(payload) - rows.keys()
    unavailable = {tid: {tid} for tid in missing}
    parents = {spec["tempId"]: spec.get("parent_id") for spec in payload["tasks"]}
    for edge in payload["edges"]:
        if edge.get("dep_type", "blocks") == "parent-child" and edge["from"] in parents:
            parents[edge["from"]] = edge["to"]
    while True:
        skipped = {
            tid: unavailable[parent]
            for tid, parent in parents.items()
            if tid not in unavailable and parent in unavailable
        }
        if not skipped:
            break
        unavailable.update(skipped)
    remaining, omissions = {}, []
    for operation in OPERATIONS:
        remaining[operation] = []
        for index, item in enumerate(payload[operation]):
            if operation == "tasks":
                targets = [item["tempId"]]
            elif operation in ("edges", "remove_edges"):
                targets = [item["from"], item["to"]]
            else:
                targets = [item["task_id"], item.get("parent_id")]
            absent = set().union(*(unavailable.get(tid, set()) for tid in targets))
            if absent:
                omissions.append({
                    "operation": operation, "index": index, "missing_task_ids": sorted(absent),
                })
            else:
                remaining[operation].append(item)
    return remaining, {"missing_task_ids": sorted(missing), "skipped_operations": omissions}


async def snapshot(conn, project_id, payload, *, allow_missing=False):
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
                select(tasks).where(tasks.c.project_id == project_id)
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
    refs = references(payload)
    if not allow_missing and (missing := refs - by_id.keys()):
        raise ChangeSetError(
            f"unknown existing task ids: {sorted(missing)}",
        )
    # Ancestors and descendants participate in placement/archive guards and
    # settlement; read their container metadata for final graph validation.
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
    expected = {
        "edges": [dict(row) for row in edges],
        "metadata": [dict(row) for row in metadata],
    }
    return expected, by_id, project


def final_graph(payload, rows, expected, *, approved=False):
    """Validate the resulting graph, including jointly removed/replaced edges."""
    graph = {(e["task_id"], e["depends_on_task_id"], e["dep_type"]) for e in expected["edges"]}
    for e in payload["remove_edges"]:
        edge = (e["from"], e["to"], e.get("dep_type", "blocks"))
        if not approved and edge not in graph:
            raise ChangeSetError(f"No dependency found: {edge}")
        graph.discard(edge)
    parent = {tid: row["parent_task_id"] for tid, row in rows.items()}
    for e in payload["remove_edges"]:
        if e.get("dep_type", "blocks") == "parent-child" and parent.get(e["from"]) == e["to"]:
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
            if not approved and parent.get(tid) not in (None, pid):
                raise ChangeSetError("multiple parents; use an edit with parent_id")
            graph = {edge for edge in graph if edge[0] != tid or edge[2] != "parent-child"}
            parent[tid] = pid
        graph.add(edge)
    for tid, pid in parent.items():
        if pid is not None:
            graph.add((tid, pid, "parent-child"))
    for spec in payload["tasks"] + payload["edits"]:
        if spec.get("placement") == "operational_root":
            tid = spec.get("tempId", spec.get("task_id"))
            if parent[tid] is not None:
                raise ChangeSetError("operational_root cannot have a structural parent")
            graph.update((tid, target, "delivery-of") for target in spec["delivery_targets"])
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


async def apply(handler, conn, project_id, payload, expected, rows, project, source, *,
                approved=False, hold=None, hold_revision=1):
    """Apply a fully validated set, returning a receipt and deferred notifications."""
    db = handler.db
    transitions = []
    # Retirement proofs have their own writer, but participate in the same
    # final-graph proof before any writes. Their typed rewires run at archive.
    projected = {key: list(value) for key, value in payload.items()}
    for edit in payload["edits"]:
        for key, edges in (edit.get("rewire") or {}).items():
            projected[key].extend(edges)
    parents = final_graph(projected, rows, expected, approved=approved)
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
                spec = specs[tid]
                if hold:
                    role = ("operational_root" if spec.get("placement") == "operational_root"
                            else "container" if tid in parents.values() else "leaf")
                    joined = await batches.add_member(
                        conn, hold, task_id=item["task_id"], member_key=tid, role=role,
                        section_anchor=spec["section_anchor"],
                        primary_outcome=spec.get("primary_outcome", ""), created_revision=hold_revision,
                        acceptance=spec.get("acceptance"), constraints=spec.get("constraints", []),
                        size=spec.get("size"),
                    )
                    if not joined["success"]:
                        raise ChangeSetError(joined["error"])
                if "owned_modules" in spec:
                    await db._upsert_meta(item["task_id"], "owned_modules",
                                          spec["owned_modules"], conn=conn)
                pending.remove(tid)

    def real(tid):
        return mapping.get(tid, tid)

    for spec in payload["tasks"] + payload["edits"]:
        if spec.get("placement") == "operational_root":
            targets = [real(target) for target in spec["delivery_targets"]]
            await db._upsert_meta(real(spec.get("tempId", spec.get("task_id"))), "delivery_condition",
                                  {"targets": targets, "kind": "delivery-of"}, conn=conn)
            for target in targets:
                await conn.execute(pg_insert(task_dependencies).values(
                    task_id=real(spec.get("tempId", spec.get("task_id"))), depends_on_task_id=target,
                    dep_type="delivery-of",
                ).on_conflict_do_nothing())

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
        if hold:
            fidelity = {key: edit[key] for key in batches.FIDELITY_FIELDS if key in edit}
            if fidelity:
                await batches.set_member_fidelity(conn, tid, **fidelity)
            member_fields = {key: edit[key] for key in ("section_anchor", "primary_outcome")
                             if key in edit}
            if edit.get("placement") == "operational_root":
                member_fields["role"] = "operational_root"
            if member_fields:
                await conn.execute(update(ingest_batch_members).where(
                    ingest_batch_members.c.batch_id == hold,
                    ingest_batch_members.c.task_id == tid,
                ).values(**member_fields))
            for key in ("owned_modules", "tags"):
                if key in edit:
                    await db._upsert_meta(tid, key, edit[key], conn=conn)
        action = edit.get("action")
        if action == "pause":
            _, result = await db._pause_task_on(tid, conn=conn, defer_projection=True)
            if result:
                transitions.append(result)
        elif action == "resume":
            if rows[tid]["status"] != "PAUSED":
                if approved:
                    continue  # Already resumed since approval; the intent is satisfied.
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
            retirement = {}
            if hold:
                reason = edit["reason"]
                retirement = {
                    "disposition": "obsolete", "abandon_reason": reason,
                    "replacement_task_id": real(edit.get("replacement_task_id")),
                    "satisfied_by": edit.get("satisfied_by"),
                    "rewire": {key: [{**edge, "from": real(edge["from"]),
                                      "to": real(edge["to"])} for edge in edges]
                               for key, edges in (edit.get("rewire") or {}).items()} or None,
                }
                joined = await batches.retire_member(
                    conn, hold, edit["task_id"], reason=reason, retired_revision=hold_revision,
                )
                if not joined["success"]:
                    raise ChangeSetError(joined["error"], joined["error_code"])
            result = await db._archive_task_on(
                edit["task_id"],
                conn=conn,
                archive_reason=edit.get("reason"),
                abandoned_by=author_id,
                defer_projection=True,
                **retirement,
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


def held_operations(operations, members, edges):
    """Compile the reviewer's scoped operations to the ordinary atomic writer.

    Member keys are stable aliases; existing ids are also accepted. Every
    reference, including a retirement's implicit dependent rewrite, stays in
    this batch. New tasks are never imported from another batch.
    """
    if not isinstance(operations, list) or not operations:
        raise ChangeSetError("operations must be a non-empty list")
    by_id = {m["task_id"]: m for m in members}
    aliases = {m["member_key"]: m["task_id"] for m in members}
    payload = {key: [] for key in OPERATIONS}
    edits = {}
    retiring = set()

    def member(value):
        tid = aliases.get(value, value) if isinstance(value, str) else None
        if tid not in by_id:
            raise ChangeSetError(f"{value!r} is not an active batch member", "membership")
        return tid

    def reference(value):
        if value in new_keys:
            return value
        return member(value)

    # Discover aliases before resolving references, permitting forward edges.
    new_keys = set()
    for op in operations:
        if not isinstance(op, dict):
            raise ChangeSetError("operations must contain objects")
        specs = (op.get("tasks", []) if op.get("kind") == "split" else
                 [op["task"]] if op.get("kind") in ("add_leaf", "combine") and "task" in op
                 else [])
        if not isinstance(specs, list) or any(not isinstance(s, dict) for s in specs):
            raise ChangeSetError("new tasks must be objects")
        refused = refused_spec_keys(specs + [op.get("changes", {})])
        if refused:
            raise ChangeSetError("routing.choice_forbidden", "routing.choice_forbidden",
                                 refused=refused)
        for spec in specs:
            key = spec.get("tempId")
            if not isinstance(key, str) or not key or key in new_keys or key in aliases or key in by_id:
                raise ChangeSetError("new tasks need unique, unused tempId values", "membership")
            new_keys.add(key)

    def edit(tid, changes):
        target = edits.setdefault(tid, {"task_id": tid})
        for key, value in changes.items():
            if key in target and target[key] != value:
                raise ChangeSetError(f"conflicting {key} edits for {tid}")
            target[key] = value

    def add_task(spec, parent=None):
        spec = dict(spec)
        if "parent_id" not in spec:
            spec["parent_id"] = parent
        if not spec.get("parent_id") and spec.get("placement") != "operational_root":
            raise ChangeSetError("New scoped leaves require a parent or operational placement",
                                 "validation_failed")
        if spec.get("parent_id"):
            spec["parent_id"] = reference(spec["parent_id"])
        spec["delivery_targets"] = [reference(t) for t in spec["delivery_targets"]] if (
            "delivery_targets" in spec
        ) else []
        if not spec["delivery_targets"]:
            spec.pop("delivery_targets")
        payload["tasks"].append(spec)
        return spec["tempId"]

    def rewire(plan):
        if not isinstance(plan, dict):
            raise ChangeSetError("rewire must be an object")
        _fields(plan, {"edges", "remove_edges"})
        out = {}
        for key, values in plan.items():
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                raise ChangeSetError("rewire edges must be lists of objects")
            out[key] = []
            for edge in values:
                _fields(edge, {"from", "to", "dep_type", "reason"})
                out[key].append({**edge, "from": reference(edge.get("from")),
                                 "to": reference(edge.get("to"))})
        return out

    def retire(tid, op, *, replacement=None, plan=None):
        reason = op.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ChangeSetError("retire, split and combine require a reason")
        if tid in retiring:
            raise ChangeSetError(f"duplicate retirement for {tid}")
        retiring.add(tid)
        if any(m.get("parent_task_id") == tid for m in members):
            raise ChangeSetError("retirement requires a leaf", "membership")
        incoming = [e for e in edges if e["depends_on_task_id"] == tid
                    and e["dep_type"] != "parent-child"]
        # An implicit replacement must not reach or rewrite a nonmember.
        for edge in incoming:
            member(edge["task_id"])
        replacement = replacement or op.get("replacement_task_id")
        changes = {"action": "archive", "reason": reason}
        if replacement:
            replacement = reference(replacement)
            changes["replacement_task_id"] = replacement
            if plan is None and "rewire" not in op:
                plan = {"edges": [], "remove_edges": []}
                for edge in incoming:
                    original = {"from": edge["task_id"], "to": tid,
                                "dep_type": edge["dep_type"], "reason": edge.get("description")}
                    plan["remove_edges"].append(original)
                    plan["edges"].append({**original, "to": replacement})
        else:
            changes["satisfied_by"] = op.get("satisfied_by") or (
                "Scoped batch retirement decision: " + reason
            )
        if plan is not None or "rewire" in op:
            changes["rewire"] = rewire(op["rewire"] if "rewire" in op else plan)
        edit(tid, changes)

    for op in operations:
        kind = op.get("kind")
        if kind == "rewrite":
            _fields(op, {"kind", "task_id", "changes"})
            changes = op.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise ChangeSetError("rewrite requires changes")
            _fields(changes, {"title", "description", "priority", "tags", "acceptance",
                              "constraints", "primary_outcome", "section_anchor", "size",
                              "owned_modules"})
            edit(member(op.get("task_id")), changes)
        elif kind == "reparent":
            _fields(op, {"kind", "task_id", "parent_id"})
            if not op.get("parent_id"):
                raise ChangeSetError("use move_to_root for operational placement")
            edit(member(op.get("task_id")), {"parent_id": reference(op["parent_id"])})
        elif kind == "move_to_root":
            _fields(op, {"kind", "task_id", "delivery_targets"})
            targets = op.get("delivery_targets")
            if not isinstance(targets, list) or not targets:
                raise ChangeSetError("move_to_root requires delivery_targets")
            tid = member(op.get("task_id"))
            targets = [reference(t) for t in targets]
            payload["remove_edges"].extend({"from": tid, "to": edge["depends_on_task_id"],
                                          "dep_type": "delivery-of"} for edge in edges
                                         if edge["task_id"] == tid and edge["dep_type"] == "delivery-of"
                                         and edge["depends_on_task_id"] not in targets)
            edit(tid, {"parent_id": None,
                 "placement": "operational_root", "delivery_targets": targets})
        elif kind == "rewire":
            _fields(op, {"kind", "edges", "remove_edges"})
            for key, values in rewire({k: v for k, v in op.items() if k != "kind"}).items():
                if any(e.get("dep_type", "blocks") not in {"blocks", "conditional-blocks"}
                       for e in values):
                    raise ChangeSetError("rewire only changes leaf-to-leaf execution edges")
                payload[key].extend(values)
        elif kind == "add_leaf":
            _fields(op, {"kind", "task"})
            add_task(op["task"])
        elif kind == "retire":
            _fields(op, {"kind", "task_id", "reason", "replacement_task_id", "satisfied_by", "rewire"})
            retire(member(op.get("task_id")), op)
        elif kind in {"split", "combine"}:
            _fields(op, {"kind", "task_id", "tasks", "reason", "rewire"} if kind == "split"
                    else {"kind", "task_ids", "task", "reason", "rewire"})
            ids = [member(op.get("task_id"))] if kind == "split" else [
                member(tid) for tid in op.get("task_ids", [])
            ]
            if not ids or len(set(ids)) != len(ids) or (kind == "combine" and len(ids) < 2):
                raise ChangeSetError("combine requires at least two distinct members")
            specs = op.get("tasks") if kind == "split" else [op["task"]]
            if kind == "split" and (not isinstance(specs, list) or len(specs) < 2):
                raise ChangeSetError("split requires at least two new leaves")
            new = [add_task(spec, by_id[ids[0]]["parent_task_id"]) for spec in specs]
            # Each successor inherits all still-relevant typed prerequisites.
            for edge in edges:
                if edge["task_id"] in ids and edge["depends_on_task_id"] not in ids and (
                    edge["dep_type"] != "parent-child"
                ):
                    payload["edges"].extend({"from": key, "to": edge["depends_on_task_id"],
                        "dep_type": edge["dep_type"], "reason": edge.get("description")} for key in new)
                elif edge["task_id"] in ids and edge["depends_on_task_id"] in ids:
                    payload["remove_edges"].append({"from": edge["task_id"],
                        "to": edge["depends_on_task_id"], "dep_type": edge["dep_type"]})
            for tid in ids:
                plan = {"edges": [], "remove_edges": []}
                for edge in edges:
                    if edge["depends_on_task_id"] != tid or edge["task_id"] in ids or (
                        edge["dep_type"] == "parent-child"
                    ):
                        continue
                    original = {"from": edge["task_id"], "to": tid,
                                "dep_type": edge["dep_type"], "reason": edge.get("description")}
                    plan["remove_edges"].append(original)
                    plan["edges"].extend({**original, "to": key} for key in new)
                retire(tid, op, replacement=new[0] if kind == "combine" else None, plan=plan)
        else:
            raise ChangeSetError(f"unknown held-batch operation {kind!r}")
    # Validate nested retirement plans with the same normalizer, including enum,
    # endpoint and routing-pin checks; no implicit writes escape the membership proof.
    for edit_row in edits.values():
        if "rewire" in edit_row:
            normalize({**edit_row["rewire"], "comments": [
                {"task_id": edit_row["task_id"], "body": "Retirement plan"}
            ]})
    payload["edits"] = list(edits.values())
    return normalize(payload, held=True)
