"""Bind delivered, terminal legacy tasks to their designated repository.

The development publisher accepted null-repository tasks on the designated
repository.  Its latest-completion receipt is the evidence for this recovery;
for a container, delivered children can establish the same repository route.
"""

from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import insert, select, update

from src.database.queries.blocked_state import development_delivery_receipt
from src.database.tables import projects, repos, task_comments, tasks
from src.integration.legacy_deliveries import TERMINAL_TASK_STATES


class LegacyRepositoryBinding:
    def __init__(self, db):
        self.db = db

    async def run(
        self, project_id: str, *, principal: str, dry_run: bool = True,
        reason: str | None = None,
    ) -> dict:
        if not dry_run and not (reason or "").strip():
            return {"outcome": "invalid", "project_id": project_id,
                    "error": "--apply requires an audit reason"}
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (await conn.execute(
                select(projects).where(projects.c.id == project_id).with_for_update()
            )).mappings().one_or_none()
            if project is None:
                return {"outcome": "not_found", "project_id": project_id}
            repository_id = project["integration_repository_id"]
            repository = None if repository_id is None else (await conn.execute(
                select(repos.c.id).where(
                    repos.c.id == repository_id, repos.c.project_id == project_id
                )
            )).one_or_none()
            if repository is None:
                return {"outcome": "invalid", "project_id": project_id,
                        "error": "project has no designated repository"}
            if project["hierarchical_integration_mode"] not in {"observe", "hierarchy", "train"}:
                return {"outcome": "invalid", "project_id": project_id,
                        "error": "legacy binding requires observe, hierarchy, or train mode"}

            rows = (await conn.execute(
                select(tasks).where(tasks.c.project_id == project_id).order_by(tasks.c.id)
                .with_for_update()
            )).mappings().all()
            by_id = {row["id"]: row for row in rows}
            children: dict[str, list[str]] = {}
            for row in rows:
                if row["parent_task_id"] in by_id:
                    children.setdefault(row["parent_task_id"], []).append(row["id"])
            hierarchy_ids = set(children) | {
                row["id"] for row in rows if row["parent_task_id"] is not None
            }
            receipt_ids = set((await conn.execute(
                select(tasks.c.id).select_from(
                    tasks.join(projects, projects.c.id == tasks.c.project_id).join(
                        repos, repos.c.id == projects.c.integration_repository_id
                    )
                ).where(
                    tasks.c.project_id == project_id,
                    tasks.c.status == "COMPLETED",
                    development_delivery_receipt(tasks, projects, repos),
                )
            )).scalars())
            proven: dict[str, bool] = {}

            def delivered(task_id: str) -> bool:
                if task_id in proven:
                    return proven[task_id]
                row = by_id[task_id]
                # An open or assigned member cannot inherit a historical route.
                if (row["status"] not in TERMINAL_TASK_STATES
                    or row["assigned_agent_id"]
                    or row["repo_id"] not in {None, repository_id}):
                    proven[task_id] = False
                else:
                    proven[task_id] = (
                        task_id in receipt_ids
                        or bool(children.get(task_id)) and all(
                            delivered(child_id) for child_id in children[task_id]
                        )
                    )
                return proven[task_id]

            bound = []
            unproven = []
            for task_id in sorted(hierarchy_ids):
                row = by_id[task_id]
                if row["repo_id"] is not None or row["status"] not in TERMINAL_TASK_STATES:
                    continue
                if delivered(task_id):
                    bound.append({"task_id": task_id,
                                  "proof": "development_delivery" if task_id in receipt_ids
                                  else "delivered_children"})
                else:
                    unproven.append(task_id)
            if not dry_run and bound:
                now = time.time()
                for item in bound:
                    task_id = item["task_id"]
                    await conn.execute(update(tasks).where(
                        tasks.c.id == task_id,
                        tasks.c.project_id == project_id,
                        tasks.c.repo_id.is_(None),
                    ).values(repo_id=repository_id))
                    await conn.execute(insert(task_comments).values(
                        id="comment-" + uuid4().hex,
                        task_id=task_id, project_id=project_id,
                        body=(f"Legacy repository binding: {repository_id}; "
                              f"proof={item['proof']}; reason={reason}"),
                        author_kind=("user" if principal.startswith("human:") else "supervisor"),
                        author_id=principal,
                        kind="note", created_at=now,
                    ))
            return {"outcome": "bound" if bound else "nothing_to_bind",
                    "project_id": project_id, "repository_id": repository_id,
                    "dry_run": dry_run, "bound": bound, "unproven": unproven}
