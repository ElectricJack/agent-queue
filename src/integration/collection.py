"""Reconcile approved children into the durable parent promotion policy."""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select

from src.database.tables import (
    integration_promotion_intents,
    integration_repair_operations,
    projects,
    task_integration_checkpoints,
    tasks,
)
from src.integration.models import BranchKey, Fence
from src.integration.outbox import enqueue_integration_event

logger = logging.getLogger(__name__)


class CollectionService:
    """Submit one child per parent; promotion remains the mutation authority."""

    def __init__(self, db, *, hierarchy_service_factory, page_size=10):
        self.db = db
        self.hierarchy_service_factory = hierarchy_service_factory
        self.page_size = page_size
        self.after = ""

    async def tick(self, now):
        hierarchy = self.hierarchy_service_factory()
        if hierarchy is None:
            return
        checkpoint = task_integration_checkpoints
        operation = integration_repair_operations
        async with self.db._engine.connect() as conn:
            parents = (
                (
                    await conn.execute(
                        select(tasks.c.id)
                        .join(checkpoint, checkpoint.c.task_id == tasks.c.id)
                        .join(
                            operation,
                            (operation.c.parent_task_id == tasks.c.id)
                            & (operation.c.episode_id == checkpoint.c.episode_id),
                        )
                        .join(projects, projects.c.id == tasks.c.project_id)
                        .where(
                            tasks.c.id > self.after,
                            tasks.c.status == "PAUSED",
                            checkpoint.c.state == "awaiting_children",
                            operation.c.state == "active",
                            projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                        )
                        .order_by(tasks.c.id)
                        .limit(self.page_size)
                    )
                )
                .scalars()
                .all()
            )
        self.after = parents[-1] if len(parents) == self.page_size else ""
        for task_id in parents:
            try:
                await self.queue_next(hierarchy, task_id, now)
            except Exception:
                logger.warning("Child collection failed for %s", task_id, exc_info=True)

    async def queue_next(self, hierarchy, task_id, now):
        parent = await self.db.get_task(task_id)
        checkpoint = await self.db.get_integration_checkpoint(task_id)
        if parent is None or checkpoint is None or not checkpoint.get("episode_id"):
            return "waiting"
        target = BranchKey(repository_id=parent.repo_id, branch=parent.branch_name)
        owner = await hierarchy.ownership.get_owner(target)
        if (
            owner is None
            or owner["owner_role"] != "collector"
            or owner["handoff_state"] != "reserved"
        ):
            return "waiting"
        operation = await self.db.get_integration_operation(owner["owner_id"])
        if (
            operation is None
            or operation["state"] != "active"
            or operation["parent_task_id"] != task_id
            or operation["episode_id"] != checkpoint["episode_id"]
        ):
            return "waiting"
        fence = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
        children = await self.db.get_children(task_id)
        for child in sorted(children, key=lambda child: child.id):
            if child.status.value != "COMPLETED":
                continue
            origin = await self.db.get_task_branch_origin_for_promotion(child.id, parent.repo_id)
            source = await self.db.get_integration_checkpoint(child.id)
            if (
                origin is None
                or source is None
                or not origin["materialized"]
                or not origin["reserved"]
                or origin["parent_task_id"] != task_id
            ):
                continue
            source_head = source["checkpoint_sha"]
            review = await self.db.get_applicable_integration_review_evidence(
                source_task_id=child.id,
                repository_id=parent.repo_id,
                source_base=origin["base_sha"],
                reviewed_head_sha=source_head,
                current_generation=int(source["generation"]),
            )
            if review is None:
                continue
            receipts = await self.db.list_integration_delivery_receipts(
                source_task_id=child.id,
                repository_id=parent.repo_id,
                target_branch=parent.branch_name,
            )
            if any(receipt["reviewed_head_sha"] == source_head for receipt in receipts):
                continue
            # Observe Git only when an approved child needs delivery. Promotion
            # rechecks this exact lease and source evidence before any push.
            repo = await self.db.get_repo(parent.repo_id)
            head = await hierarchy._resolve_head(repo, parent.branch_name)
            identity = hashlib.sha256(
                f"{operation['id']}:{child.id}:{source_head}:{head}:{fence.token}".encode()
            ).hexdigest()
            async with hierarchy.ownership.mutation_exclusion(
                fence, expected_role="collector"
            ) as conn:
                current = (
                    await conn.execute(
                        select(integration_repair_operations.c.id)
                        .join(
                            task_integration_checkpoints,
                            task_integration_checkpoints.c.episode_id
                            == integration_repair_operations.c.episode_id,
                        )
                        .join(tasks, tasks.c.id == task_integration_checkpoints.c.task_id)
                        .join(projects, projects.c.id == tasks.c.project_id)
                        .where(
                            integration_repair_operations.c.id == operation["id"],
                            integration_repair_operations.c.state == "active",
                            task_integration_checkpoints.c.state == "awaiting_children",
                            tasks.c.id == task_id,
                            tasks.c.status == "PAUSED",
                            projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                        )
                    )
                ).first()
                if current is None:
                    return "waiting"
                pending = (
                    await conn.execute(
                        select(integration_promotion_intents.c.id)
                        .where(
                            integration_promotion_intents.c.repository_id == parent.repo_id,
                            integration_promotion_intents.c.target_branch == parent.branch_name,
                            integration_promotion_intents.c.state != "committed",
                        )
                        .limit(1)
                    )
                ).first()
                if pending:
                    return "waiting"
                await enqueue_integration_event(
                    conn,
                    event_id=f"delivery-ready-{identity}",
                    dedup_key=f"delivery.ready:{identity}",
                    project_id=parent.project_id,
                    event_type="delivery.ready",
                    available_at=now,
                    payload={
                        "operation_id": operation["id"],
                        "operation_key": operation["id"],
                        "source_task_id": child.id,
                        "source_head": source_head,
                        "source_base": origin["base_sha"],
                        "expected_target": head,
                        "fence": fence.model_dump(mode="json"),
                    },
                )
            return "queued"
        return "waiting"
