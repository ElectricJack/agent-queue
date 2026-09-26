"""One bounded, read-only PostgreSQL snapshot for morning evidence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select, text, union_all

from src.database.tables import (
    archived_tasks,
    development_deliveries,
    digest_windows,
    escalations,
    projects,
    provider_availability_transitions,
    repos,
    task_completion_records,
    task_metadata,
    task_reroutes,
    task_session_attempts,
    tasks,
    workspaces,
)

SOURCE_LIMIT = 500
PROJECT_LIMIT = 100


async def read_morning_snapshot(
    engine: Any, *, since: float, until: float, project_ids: tuple[str, ...] | None
) -> dict[str, Any]:
    """Savepoints isolate a failed source without losing the shared snapshot.

    Fleet-only sources (providers and shared digests) are omitted for scoped
    destinations. Digest membership is context, never morning fact dedup.
    """
    output: dict[str, Any] = {"sources": {}, "gaps": [], "excluded_sources": []}
    wanted = projects.c.id.in_(project_ids) if project_ids is not None else True
    identities = union_all(
        select(tasks.c.id, tasks.c.project_id, tasks.c.title),
        select(archived_tasks.c.id, archived_tasks.c.project_id, archived_tasks.c.title),
    ).subquery()

    def bounded(stmt, at, identity):
        return stmt.order_by(at, identity).limit(SOURCE_LIMIT + 1)

    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="REPEATABLE READ")
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            selected = (
                (
                    await conn.execute(
                        select(projects)
                        .where(wanted)
                        .order_by(projects.c.id)
                        .limit(PROJECT_LIMIT + 1)
                    )
                )
                .mappings()
                .all()
            )
            output["projects"] = [dict(row) for row in selected[:PROJECT_LIMIT]]
            if len(selected) > PROJECT_LIMIT:
                output["gaps"].append({"source": "projects", "reason": "row_limit"})
            selected_ids = tuple(row["id"] for row in output["projects"])

            async def read(name, stmt):
                try:
                    async with conn.begin_nested():
                        rows = (await conn.execute(stmt)).mappings().all()
                    output["sources"][name] = [dict(row) for row in rows[:SOURCE_LIMIT]]
                    if len(rows) > SOURCE_LIMIT:
                        output["gaps"].append({"source": name, "reason": "row_limit"})
                except Exception:
                    # Diagnostics are stable and never echo SQL, paths or credentials.
                    output["sources"][name] = []
                    output["gaps"].append({"source": name, "reason": "read_failed"})

            completion_at = task_completion_records.c.completed_at
            await read(
                "completions",
                bounded(
                    select(task_completion_records, identities.c.project_id, identities.c.title)
                    .join(identities, identities.c.id == task_completion_records.c.task_id)
                    .where(
                        identities.c.project_id.in_(selected_ids),
                        completion_at >= since,
                        completion_at < until,
                    ),
                    completion_at,
                    task_completion_records.c.id,
                ),
            )
            for name, table, at in (
                ("deliveries", development_deliveries, development_deliveries.c.updated_at),
                ("escalations", escalations, escalations.c.updated_at),
                ("reroutes", task_reroutes, task_reroutes.c.at),
            ):
                conditions = [table.c.project_id.in_(selected_ids), at < until]
                if name == "escalations":
                    conditions.append(
                        or_(
                            at >= since,
                            table.c.state.in_(("needs_human", "reply_received", "resolving")),
                        )
                    )
                else:
                    conditions.append(at >= since)
                await read(name, bounded(select(table).where(*conditions), at, table.c.id))

            at = task_session_attempts.c.ended_at
            await read(
                "attempts",
                bounded(
                    select(task_session_attempts).where(
                        task_session_attempts.c.project_id.in_(selected_ids),
                        at >= since,
                        at < until,
                        or_(
                            task_session_attempts.c.outcome.in_(("fail", "failed")),
                            task_session_attempts.c.state == "quarantined",
                            task_session_attempts.c.end_reason.in_(
                                (
                                    "stuck_timeout",
                                    "session_exited_open",
                                    "session_not_live",
                                    "exited_holding_task",
                                    "prepare_timeout",
                                    "timeout",
                                )
                            ),
                        ),
                    ),
                    at,
                    task_session_attempts.c.id,
                ),
            )
            # Metadata is text, parsed in Python rather than cast unsafely to JSON.
            await read(
                "incidents",
                bounded(
                    select(
                        task_metadata.c.value,
                        tasks.c.id.label("task_id"),
                        tasks.c.project_id,
                        tasks.c.title,
                        tasks.c.status,
                        tasks.c.updated_at,
                    )
                    .join(tasks, tasks.c.id == task_metadata.c.task_id)
                    .where(
                        task_metadata.c.key == "supervisor_recovery_incident",
                        tasks.c.project_id.in_(selected_ids),
                        tasks.c.updated_at < until,
                        or_(tasks.c.updated_at >= since, tasks.c.status.in_(("FAILED", "BLOCKED"))),
                    ),
                    tasks.c.updated_at,
                    tasks.c.id,
                ),
            )
            if project_ids is None:
                at = provider_availability_transitions.c.at
                await read(
                    "providers",
                    bounded(
                        select(provider_availability_transitions).where(at >= since, at < until),
                        at,
                        provider_availability_transitions.c.id,
                    ),
                )
                at = digest_windows.c.window_end
                await read(
                    "digests",
                    bounded(
                        select(
                            digest_windows.c.id,
                            digest_windows.c.window_start,
                            digest_windows.c.window_end,
                            digest_windows.c.payload,
                            digest_windows.c.send_status,
                        ).where(at >= since, at < until),
                        at,
                        digest_windows.c.id,
                    ),
                )
            else:
                output["excluded_sources"] = ["providers", "digests"]

            await read(
                "repos",
                select(repos)
                .where(repos.c.project_id.in_(selected_ids))
                .order_by(repos.c.id)
                .limit(SOURCE_LIMIT + 1),
            )
            await read(
                "workspaces",
                select(workspaces)
                .where(
                    workspaces.c.project_id.in_(selected_ids),
                    workspaces.c.enabled.is_(True),
                    workspaces.c.slot_index.is_(None),
                    workspaces.c.base_workspace_id.is_(None),
                )
                .order_by(workspaces.c.id)
                .limit(SOURCE_LIMIT + 1),
            )
            output["snapshot_at"] = float(
                await conn.scalar(select(func.extract("epoch", func.transaction_timestamp())))
            )
    return output
