"""Count verified re-registrations after wakes without owner progress.

Leases, status churn and routine mail are not progress. Branch heads, task edits
and task comments are. Missing Git evidence never proves an unchanged branch.
Supervisor wakes/lifts reset the window; registration receipts remain immutable.
"""

from sqlalchemy import and_, func, or_, select

from src.database.tables import events, sessions, task_comments, tasks, wait_records
from src.waits.conditions import Condition, SleepDecision, canonical
from src.waits.graph import graph_locked


async def prepare_progress(conn, owners):
    """Git is an observation, never work to perform while holding GRAPH_LOCK."""
    if graph_locked(conn):
        return
    from src.git.manager import GitManager

    heads = {}

    async def read_head(work_dir, branch):
        key = (work_dir, branch)
        if key not in heads:
            heads[key] = await GitManager().arev_parse(work_dir, branch)
        return heads[key]

    for owner in owners:
        await progress(conn, owner, read_head=read_head)
    conn.info["wait_progress_observations"] = (conn.sync_connection.get_transaction(), heads)


async def progress(conn, owner, *, task=None, continuation=None, read_head=None):
    if owner["owner_kind"] != "task":
        return None
    if task is None:
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == owner["owner_id"], tasks.c.project_id == owner["project_id"],
        ))).mappings().first()
    if not task:
        return None
    branch = task["branch_name"]
    head = continuation["saved_head"] if continuation and branch else None
    if branch and not head:
        session = (await conn.execute(select(sessions).where(
            sessions.c.id == owner["session_id"],
        ))).mappings().first()
        if session and session["work_dir"]:
            if graph_locked(conn):
                transaction, observations = conn.info.get("wait_progress_observations", (None, {}))
                if transaction is not conn.sync_connection.get_transaction():
                    return None
                head = observations.get((session["work_dir"], branch))
            else:
                if read_head is None:
                    from src.git.manager import GitManager
                    read_head = GitManager().arev_parse
                head = await read_head(session["work_dir"], branch)
        if not head:
            return None
    comment = (await conn.execute(select(func.count(task_comments.c.id),
                                         func.max(task_comments.c.created_at)).where(
        task_comments.c.task_id == task["id"],
    ))).one()
    edit = await conn.scalar(select(func.max(events.c.id)).where(
        events.c.project_id == task["project_id"], events.c.task_id == task["id"],
        or_(events.c.event_type == "task.set",
            and_(events.c.event_type == "task.updated",
                 events.c.payload.contains('"edited_fields"'))),
    ))
    return {"branch": branch, "head": head, "comment": list(comment),
            "edit": edit, "fields": {key: task[key] for key in (
                "title", "description", "priority", "class_hint", "task_type",
            )}}


async def detect(conn, scope, evidence, now, *, policy=None, read_head=None):
    if scope.owner_kind != "task" or evidence.condition.kind in {"human", "resource"}:
        return None
    window = getattr(policy, "loop_window_seconds", 3600)
    limit = getattr(policy, "loop_window_registrations", 3)
    current = await progress(conn, {"owner_kind": scope.owner_kind, "owner_id": scope.owner_id,
                                    "project_id": scope.project_id, "session_id": scope.session_id},
                             read_head=read_head)
    if current is None:
        return None
    history = (await conn.execute(select(wait_records).where(
        wait_records.c.project_id == scope.project_id, wait_records.c.owner_kind == scope.owner_kind,
        wait_records.c.owner_id == scope.owner_id, wait_records.c.created_at >= now - window,
    ).order_by(wait_records.c.created_at.desc(), wait_records.c.id.desc()))).mappings().all()
    matches = []
    for old in history:
        inputs = old["decision_inputs"] or {}
        if (old["outcome"] == "lifted_by_supervisor"
                or inputs.get("supervisor_decision", {}).get("decision") == "wake"):
            break
        if not old["cleared_at"] or not old["resolved_at"] or old["outcome"] != "satisfied":
            continue
        if inputs.get("wake_progress") != current:
            # Any intervening owner progress starts a fresh window.
            break
        if ((old["source_key"], old["source_generation"]) !=
                (evidence.source.identity, evidence.source.generation)
                or canonical(Condition.from_record(old["condition"]).as_dict()) !=
                canonical(evidence.condition.as_dict())):
            continue
        matches.append({"wait_id": old["id"], "registered_at": old["created_at"],
                        "resolved_at": old["resolved_at"]})
    if len(matches) + 1 < limit:
        return None
    return SleepDecision("hibernate", "wait.loop_detected", {
        "loop_registrations": list(reversed(matches[:limit - 1])),
        "loop_window_seconds": window, "loop_window_registrations": limit,
    })
