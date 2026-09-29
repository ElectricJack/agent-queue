"""PR links recorded on tasks in active projects, including archived tasks."""

from __future__ import annotations

import time

from sqlalchemy import and_, func, select, union_all
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import (
    archived_tasks, integration_batch_members, integration_batches,
    projects, pull_request_inbox_snapshot, repos, tasks,
)


async def list_known_pull_requests(db) -> list[dict]:
    def candidates(task_table):
        return (
            select(
                task_table.c.id.label("task_id"),
                task_table.c.title.label("task_title"),
                task_table.c.pr_url,
                task_table.c.created_at.label("task_created_at"),
                projects.c.id.label("project_id"),
                projects.c.name.label("project_name"),
                projects.c.hierarchical_integration_mode.label("integration_mode"),
                func.coalesce(repos.c.url, projects.c.repo_url).label("repository_url"),
            )
            .select_from(
                task_table.join(projects, task_table.c.project_id == projects.c.id)
                .outerjoin(
                    repos,
                    and_(
                        task_table.c.repo_id == repos.c.id,
                        repos.c.project_id == projects.c.id,
                    ),
                )
            )
            .where(
                projects.c.status == "ACTIVE",
                task_table.c.pr_url.is_not(None),
                func.trim(task_table.c.pr_url) != "",
            )
        )

    query = union_all(candidates(tasks), candidates(archived_tasks))
    async with db._engine.begin() as conn:
        result = await conn.execute(query)
        return [dict(row) for row in result.mappings()]


async def read_pull_request_snapshot(db) -> tuple[list[dict], float | None]:
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(pull_request_inbox_snapshot))).mappings().first()
    return (row["payload"], row["updated_at"]) if row else ([], None)


async def write_pull_request_snapshot(db, items: list[dict], *, now: float | None = None) -> None:
    table = pull_request_inbox_snapshot
    statement = insert(table).values(id=1, payload=items, updated_at=now or time.time())
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.id],
        set_={"payload": statement.excluded.payload, "updated_at": statement.excluded.updated_at},
    )
    async with db._engine.begin() as conn:
        await conn.execute(statement)


async def mark_pull_request_approved(db, url: str, head_sha: str) -> None:
    """Show the operator's new approval immediately without changing snapshot age."""
    table = pull_request_inbox_snapshot
    async with db._engine.begin() as conn:
        row = (await conn.execute(select(table).with_for_update())).mappings().first()
        if row is None:
            return
        items = list(row["payload"])
        for item in items:
            if item.get("url") == url and item.get("head_sha") == head_sha:
                item["review_decision"] = "approved"
        await conn.execute(table.update().where(table.c.id == 1).values(payload=items))


async def replace_pull_request_snapshot_row(db, task_id: str, item: dict | None) -> None:
    table = pull_request_inbox_snapshot
    async with db._engine.begin() as conn:
        row = (await conn.execute(select(table).with_for_update())).mappings().first()
        if row is None:
            return
        items = [entry for entry in row["payload"] if entry.get("task_id") != task_id]
        if item is not None:
            items.append(item)
        items.sort(key=lambda entry: entry.get("opened_at") or 0, reverse=True)
        await conn.execute(table.update().where(table.c.id == 1).values(payload=items))


async def train_states_for_tasks(db, task_ids: set[str]) -> dict[str, str]:
    """Project train lifecycle for the currently linked tasks, newest batch wins."""
    if not task_ids:
        return {}
    statement = (
        select(
            integration_batch_members.c.task_id,
            integration_batches.c.lifecycle,
            integration_batches.c.updated_at,
        )
        .select_from(integration_batch_members.join(
            integration_batches,
            integration_batch_members.c.batch_id == integration_batches.c.id,
        ))
        .where(integration_batch_members.c.task_id.in_(task_ids))
        .order_by(integration_batches.c.updated_at.desc())
    )
    async with db._engine.connect() as conn:
        rows = (await conn.execute(statement)).mappings().all()
        root_rows = (await conn.execute(
            select(tasks.c.id.label("task_id"), integration_batches.c.lifecycle,
                   integration_batches.c.updated_at)
            .select_from(tasks.join(
                integration_batches, tasks.c.pr_url == integration_batches.c.pr_url,
            ))
            .where(tasks.c.id.in_(task_ids))
            .order_by(integration_batches.c.updated_at.desc())
        )).mappings().all()
    states = {}
    mapping = {
        "testing": "testing", "repairing": "repairing",
        "promoted": "landed", "cleanup_pending": "landed",
    }
    for row in sorted([*rows, *root_rows], key=lambda item: item["updated_at"], reverse=True):
        states.setdefault(row["task_id"], mapping.get(row["lifecycle"], "in batch"))
    return states
