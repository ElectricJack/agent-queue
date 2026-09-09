"""Recent-work reads: which tasks were worked on in a time window, and by what.

The Tasks tab's "last 24 hours" view needs one question answered cheaply:
*what moved, when, and which model did it*.  That spans three tables --
``tasks`` (status and timestamps), ``task_completion_records`` (outcome) and
``task_session_attempts`` (the durable per-attempt model snapshot) -- so it
gets its own read rather than N per-task round trips from the dashboard.

Attribution is reported, never inferred: an attempt whose ``model`` column is
NULL (legacy rows, or a harness that never reported one) is counted as
unattributed instead of being filled in from the profile's configured model.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select

from src.database.tables import (
    archived_tasks,
    task_completion_records,
    task_session_attempts,
    tasks,
)

#: Attempt columns the activity view exposes.  The full row carries work_dir
#: and session_key, which the caller does not need to render a summary line.
_ATTEMPT_FIELDS = (
    "id",
    "session_id",
    "task_id",
    "agent_id",
    "agent_name",
    "profile_id",
    "model",
    "intelligence_class",
    "llm_provider",
    "harness",
    "provider",
    "state",
    "started_at",
    "ended_at",
    "end_reason",
    "outcome",
)


def _overlaps_window(since: float, until: float):
    """Attempt-row predicate: any part of the attempt falls inside the window.

    An attempt that started before the window and is still running counts --
    that is exactly the in-progress work the view must not drop.
    """
    return (
        task_session_attempts.c.started_at <= until,
        or_(
            task_session_attempts.c.ended_at.is_(None),
            task_session_attempts.c.ended_at >= since,
        ),
    )


class ActivityQueryMixin:
    async def list_recent_task_activity(
        self,
        *,
        since: float,
        until: float,
        project_id: str | None = None,
        limit: int = 200,
    ) -> tuple[list[dict], int]:
        """Tasks touched between ``since`` and ``until`` (inclusive), newest first.

        A task counts as touched when a session attempt overlapped the window,
        when it was completed inside it, or when its own row was updated inside
        it -- so both finished and still-running work is included.

        Returns ``(items, total)`` where ``total`` is the number of matching
        tasks before ``limit`` is applied.
        """
        async with self._engine.connect() as conn:
            attempt_activity = select(
                task_session_attempts.c.task_id,
                func.max(
                    func.coalesce(
                        task_session_attempts.c.ended_at,
                        task_session_attempts.c.started_at,
                    )
                ).label("last_at"),
            ).where(*_overlaps_window(since, until))
            if project_id is not None:
                attempt_activity = attempt_activity.where(
                    task_session_attempts.c.project_id == project_id
                )
            attempt_activity = attempt_activity.group_by(task_session_attempts.c.task_id)

            last_at: dict[str, float] = {
                row.task_id: float(row.last_at)
                for row in (await conn.execute(attempt_activity)).all()
                if row.last_at is not None
            }

            completions = select(
                task_completion_records.c.task_id,
                func.max(task_completion_records.c.completed_at).label("last_at"),
            ).where(
                task_completion_records.c.completed_at >= since,
                task_completion_records.c.completed_at <= until,
            )
            for row in (
                await conn.execute(completions.group_by(task_completion_records.c.task_id))
            ).all():
                last_at[row.task_id] = max(last_at.get(row.task_id, 0.0), float(row.last_at))

            for table in (tasks, archived_tasks):
                touched = select(table.c.id, table.c.updated_at).where(
                    table.c.updated_at >= since, table.c.updated_at <= until
                )
                if project_id is not None:
                    touched = touched.where(table.c.project_id == project_id)
                for row in (await conn.execute(touched)).all():
                    last_at[row.id] = max(last_at.get(row.id, 0.0), float(row.updated_at))

            if not last_at:
                return [], 0

            candidates = list(last_at)
            rows: dict[str, dict] = {}
            for table, archived in ((tasks, False), (archived_tasks, True)):
                found = await conn.execute(table.select().where(table.c.id.in_(candidates)))
                for row in found.mappings().all():
                    # The active row wins: a restored task exists in both.
                    if archived and row["id"] in rows:
                        continue
                    rows[row["id"]] = {**dict(row), "archived": archived}

            # A completion record with no surviving task row (hard-deleted) and
            # a cross-project attempt both land here; neither is renderable.
            ordered = sorted(
                (tid for tid in candidates if tid in rows),
                key=lambda tid: (-last_at[tid], tid),
            )
            if project_id is not None:
                ordered = [tid for tid in ordered if rows[tid].get("project_id") == project_id]
            total = len(ordered)
            ordered = ordered[: max(0, limit)]
            if not ordered:
                return [], total

            attempts_by_task: dict[str, list[dict]] = {tid: [] for tid in ordered}
            attempt_rows = await conn.execute(
                select(task_session_attempts)
                .where(
                    task_session_attempts.c.task_id.in_(ordered),
                    *_overlaps_window(since, until),
                )
                .order_by(
                    task_session_attempts.c.started_at.desc(),
                    task_session_attempts.c.id.desc(),
                )
            )
            for row in attempt_rows.mappings().all():
                attempts_by_task[row["task_id"]].append(
                    {field: row[field] for field in _ATTEMPT_FIELDS}
                )

            completion_by_task: dict[str, dict] = {}
            completion_rows = await conn.execute(
                select(task_completion_records)
                .where(task_completion_records.c.task_id.in_(ordered))
                .order_by(
                    task_completion_records.c.completed_at.desc(),
                    task_completion_records.c.id.desc(),
                )
            )
            for row in completion_rows.mappings().all():
                completion_by_task.setdefault(row["task_id"], dict(row))

            items = []
            for task_id in ordered:
                row = rows[task_id]
                attempts = attempts_by_task[task_id]
                models: list[str] = []
                for attempt in attempts:  # newest first, deduplicated in order
                    model = attempt["model"]
                    if model and model not in models:
                        models.append(model)
                completion = completion_by_task.get(task_id)
                items.append(
                    {
                        "task_id": task_id,
                        "project_id": row.get("project_id"),
                        "title": row.get("title") or "",
                        "status": row.get("status") or "",
                        "priority": row.get("priority"),
                        "parent_task_id": row.get("parent_task_id"),
                        "archived": row["archived"],
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                        "last_activity_at": last_at[task_id],
                        "attempts": attempts,
                        "attempt_count": len(attempts),
                        "models": models,
                        "unattributed_attempts": sum(1 for a in attempts if not a["model"]),
                        "outcome": (completion or {}).get("outcome"),
                        "work_outcome": (completion or {}).get("work_outcome"),
                        "failure_class": (completion or {}).get("failure_class"),
                        "completed_at": (completion or {}).get("completed_at"),
                        "summary": (completion or {}).get("summary") or "",
                        "pr_url": (completion or {}).get("pr_url") or row.get("pr_url"),
                    }
                )
            return items, total
