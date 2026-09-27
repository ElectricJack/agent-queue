"""Bind delivered, terminal legacy tasks to their designated repository.

The development publisher accepted null-repository tasks on the designated
repository.  Git is the evidence for this recovery: the task's latest
completion is contained in the designated repository's default branch
(:mod:`src.integration.delivery_truth`, observed before the binding's locked
transaction and rechecked inside it).  So is an ``integration_legacy_deliveries``
row for the designated repository: ``aq integration adopt-legacy-deliveries``
records one for a child it proved on the default branch or that an operator
superseded, retired or accepted.  For a container, children proven either way
establish the same repository route.
"""

from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_legacy_deliveries,
    projects,
    repos,
    task_comments,
    tasks,
)
from src.integration.delivery_truth import DeliveryState
from src.integration.legacy_deliveries import TERMINAL_TASK_STATES


class LegacyRepositoryBinding:
    def __init__(self, db, *, delivery=None):
        self.db = db
        # Git delivery truth; the daemon registers one observer on its database.
        self.delivery = delivery if delivery is not None else getattr(
            db, "_delivery_observer", None
        )

    async def _observe(self, project_id: str):
        """Git evidence for the project's unbound completed tasks, before any lock."""
        if self.delivery is None:
            return None
        async with self.db._engine.connect() as conn:
            ids = set((await conn.execute(
                select(tasks.c.id).where(
                    tasks.c.project_id == project_id,
                    tasks.c.status == "COMPLETED",
                    tasks.c.repo_id.is_(None),
                )
            )).scalars())
        return await self.delivery.observe(ids) if ids else None

    async def run(
        self, project_id: str, *, principal: str, dry_run: bool = True,
        reason: str | None = None,
    ) -> dict:
        if not dry_run and not (reason or "").strip():
            return {"outcome": "invalid", "project_id": project_id,
                    "error": "--apply requires an audit reason"}
        view = await self._observe(project_id)
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
            # Git, not a receipt: the latest completion on the default branch,
            # for exactly the identity observed before this transaction.
            completed = [row["id"] for row in rows if row["status"] == "COMPLETED"]
            verified = await view.verified_on(conn, completed) if view is not None else {}
            receipt_ids = {
                task_id for task_id, evidence in verified.items()
                if evidence.state is DeliveryState.CONTAINED
            }
            # How adoption proved, or an operator decided, each recorded child.
            legacy_proofs = dict((await conn.execute(
                select(integration_legacy_deliveries.c.task_id,
                       integration_legacy_deliveries.c.proof).where(
                    integration_legacy_deliveries.c.project_id == project_id,
                    integration_legacy_deliveries.c.repository_id == repository_id,
                )
            )).all())
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
                        or task_id in legacy_proofs
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
                if not delivered(task_id):
                    unproven.append(task_id)
                elif task_id in receipt_ids:
                    bound.append({"task_id": task_id, "proof": "development_delivery"})
                elif task_id in legacy_proofs:
                    bound.append({"task_id": task_id, "proof": "legacy_delivery",
                                  "legacy_proof": legacy_proofs[task_id]})
                else:
                    bound.append({"task_id": task_id, "proof": "delivered_children"})
            if not dry_run and bound:
                now = time.time()
                for item in bound:
                    task_id = item["task_id"]
                    proof = item["proof"] + (
                        f" ({item['legacy_proof']})" if "legacy_proof" in item else ""
                    )
                    await conn.execute(update(tasks).where(
                        tasks.c.id == task_id,
                        tasks.c.project_id == project_id,
                        tasks.c.repo_id.is_(None),
                    ).values(repo_id=repository_id))
                    await conn.execute(insert(task_comments).values(
                        id="comment-" + uuid4().hex,
                        task_id=task_id, project_id=project_id,
                        body=(f"Legacy repository binding: {repository_id}; "
                              f"proof={proof}; reason={reason}"),
                        author_kind=("user" if principal.startswith("human:") else "supervisor"),
                        author_id=principal,
                        kind="note", created_at=now,
                    ))
            return {"outcome": "bound" if bound else "nothing_to_bind",
                    "project_id": project_id, "repository_id": repository_id,
                    "dry_run": dry_run, "bound": bound, "unproven": unproven}
