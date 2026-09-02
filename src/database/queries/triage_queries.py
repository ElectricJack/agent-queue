"""Atomic, history-preserving lifecycle for a project's routing task.

Only the bundled ``triage`` / ``triage-open`` contract opts into reuse.
Open routing gates are the durable inbox; a saved set of gate IDs prevents
unchanged, unrouteable work from restarting a completed task every tick.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import and_, exists, insert, select, update

from src.database.tables import (
    agent_profiles,
    gates,
    projects,
    sessions,
    task_context,
    task_gates,
    task_labels,
    task_metadata,
    tasks,
)
from src.models import Task, TaskStatus
from src.task_names import fresh_root_id

TRIAGE_PROFILE = "triage"
TRIAGE_KEY = "triage-open"
_SEEN_GATES = "triage.seen_routing_gates"
_TERMINAL = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
_LIVE = ("starting", "running", "draining")


def _triage_rows():
    return and_(tasks.c.profile_id == TRIAGE_PROFILE, tasks.c.dedup_key == TRIAGE_KEY)


def _pending_gates():
    return (
        select(gates.c.id, gates.c.project_id)
        .select_from(
            gates.join(task_gates, task_gates.c.gate_id == gates.c.id).join(
                tasks, tasks.c.id == task_gates.c.task_id
            )
        )
        .where(
            gates.c.gate_type == "routing",
            gates.c.status == "open",
            tasks.c.project_id == gates.c.project_id,
            tasks.c.status.notin_(_TERMINAL),
        )
        .distinct()
    )


async def triage_reconciliation_projects(db) -> tuple[set[str], set[str]]:
    """Return already opted-in projects and projects with pending routing work."""
    async with db._engine.begin() as conn:
        existing = set(
            (
                await conn.execute(select(tasks.c.project_id).where(_triage_rows()).distinct())
            ).scalars()
        )
        pending = {row.project_id for row in (await conn.execute(_pending_gates())).all()}
    return existing, pending


async def _write_seen(conn, task_id: str, pending: set[str]) -> None:
    predicate = and_(task_metadata.c.task_id == task_id, task_metadata.c.key == _SEEN_GATES)
    value = json.dumps(sorted(pending))
    changed = await conn.execute(update(task_metadata).where(predicate).values(value=value))
    if not changed.rowcount:
        await conn.execute(
            insert(task_metadata).values(task_id=task_id, key=_SEEN_GATES, value=value)
        )


async def _preserve_close_report(conn, canonical) -> None:
    # Session closes keep their latest summary in metadata; archive it before
    # a later close overwrites the same keys. Legacy task_results stay untouched.
    report_keys = (
        "summary",
        "outcome",
        "failure_class",
        "work_outcome",
        "work_commit",
        "work_branch",
        "close_notes",
        "verification",
        "close_session_id",
    )
    rows = (
        await conn.execute(
            select(task_metadata.c.key, task_metadata.c.value).where(
                task_metadata.c.task_id == canonical["id"], task_metadata.c.key.in_(report_keys)
            )
        )
    ).all()
    if not rows:
        return
    report = {row.key: json.loads(row.value) for row in rows}
    report["previous_status"] = canonical["status"]
    report["previous_updated_at"] = canonical["updated_at"]
    report["claim_epoch"] = canonical["claim_epoch"]
    await conn.execute(
        insert(task_context).values(
            id=uuid.uuid4().hex,
            task_id=canonical["id"],
            type="triage_run_report",
            label="Previous triage run",
            content=json.dumps(report, sort_keys=True),
        )
    )


async def ensure_triage_task(
    db,
    project_id: str,
    *,
    title: str,
    description: str,
    priority: int,
    intelligence_class: str | None = None,
) -> dict:
    """Wake one canonical triage task for unseen open routing gates.

    The earliest matching row remains canonical, including legacy terminal
    rows. Historical duplicates are retained; any other open triage run
    prevents starting the canonical row concurrently. Manual stops, holds,
    claims and live sessions are never overridden.

    ``intelligence_class`` is the caller's explicit route for the canonical
    task and applies only when this call creates it; waking an existing row
    leaves its route alone.
    """
    transition = None
    ready = []
    async with db.immediate() as conn:
        # Serialize find-or-create on both backends, even before a task exists.
        project_query = select(projects.c.id).where(projects.c.id == project_id)
        if conn.dialect.name == "postgresql":
            project_query = project_query.with_for_update()
        if (await conn.execute(project_query)).scalar() is None:
            return {"success": False, "error": f"Project '{project_id}' not found"}

        query = (
            select(tasks)
            .where(tasks.c.project_id == project_id, _triage_rows())
            .order_by(tasks.c.created_at.asc(), tasks.c.id.asc())
        )
        if conn.dialect.name == "postgresql":
            query = query.with_for_update()
        rows = (await conn.execute(query)).mappings().all()
        canonical = rows[0] if rows else None
        task_id = canonical["id"] if canonical else None
        response = {"success": True, "task_id": task_id, "created": False, "restarted": False}
        pending = set(
            (await conn.execute(_pending_gates().where(gates.c.project_id == project_id))).scalars()
        )
        if not pending:
            return {**response, "skipped": True, "reason": "No open routing work"}

        if canonical is not None:
            active_session = exists(
                select(sessions.c.id).where(
                    sessions.c.task_id.in_([row["id"] for row in rows]), sessions.c.state.in_(_LIVE)
                )
            )
            held = exists(
                select(task_labels.c.task_id).where(
                    task_labels.c.task_id == task_id, task_labels.c.label.like("hold:%")
                )
            )
            protected = (
                canonical["assigned_agent_id"] is not None
                or canonical["status"] not in (*_TERMINAL, TaskStatus.READY.value)
                or canonical["is_blocked"]
                or (await conn.execute(select(active_session | held))).scalar()
            )
            if protected:
                # In particular, do not mark newly arrived gates as seen while
                # the worker is running: it may already have scanned its inbox.
                return {**response, "skipped": True, "reason": "Triage is active or held"}
            if any(row["status"] not in _TERMINAL or row["assigned_agent_id"] for row in rows[1:]):
                return {
                    **response,
                    "skipped": True,
                    "reason": "Another existing triage task is open",
                }

            encoded = (
                await conn.execute(
                    select(task_metadata.c.value).where(
                        task_metadata.c.task_id == task_id, task_metadata.c.key == _SEEN_GATES
                    )
                )
            ).scalar()
            try:
                decoded = json.loads(encoded) if encoded is not None else []
                seen = set(decoded) if isinstance(decoded, list) else set()
            except (TypeError, ValueError):
                seen = set()
            if canonical["status"] == TaskStatus.READY.value:
                # A queued, unclaimed task can coalesce more work before its
                # session starts. The row lock fences this against assignment.
                await _write_seen(conn, task_id, pending)
                return response
            if not (pending - seen):
                return {**response, "skipped": True, "reason": "No new routing work"}

            transition = await db._apply_transition(
                conn,
                task_id,
                TaskStatus.READY,
                context="triage_wake",
                retry_count=0,
                resume_after=None,
                returning=True,
                extra_where=and_(
                    tasks.c.status.in_(_TERMINAL),
                    tasks.c.assigned_agent_id.is_(None),
                    ~active_session,
                    ~held,
                ),
            )
            if transition.row is None:
                return {**response, "skipped": True, "reason": "Triage changed while waking"}
            await _preserve_close_report(conn, canonical)
            response["restarted"] = True
        else:
            if (
                await conn.execute(
                    select(agent_profiles.c.id).where(agent_profiles.c.id == TRIAGE_PROFILE)
                )
            ).scalar() is None:
                return {"success": False, "error": "Profile 'triage' not found"}
            task_id = await fresh_root_id(conn)
            await db.create_task(
                Task(
                    id=task_id,
                    project_id=project_id,
                    title=title,
                    description=description,
                    priority=priority,
                    status=TaskStatus.READY,
                    profile_id=TRIAGE_PROFILE,
                    dedup_key=TRIAGE_KEY,
                    intelligence_class=intelligence_class,
                ),
                conn=conn,
            )
            ready = [
                (tid, "created")
                for tid in await db._note_frontier_entry(conn, {task_id}, reason="created")
            ]
            response.update(task_id=task_id, created=True)

        await _write_seen(conn, task_id, pending)

    # No events inside the write transaction; consumers can read committed work.
    if transition is not None:
        await db.log_blocked_flips(transition.flipped)
        await db._notify_settled(transition.settled)
        ready.extend(transition.ready)
    await db._notify_ready(ready)
    return response
