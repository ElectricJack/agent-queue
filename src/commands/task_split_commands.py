"""Post-release acceptance partitioning without transferring a code owner."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter

from pydantic import ValidationError
from sqlalchemy import insert, select, update

from src.commands import task_changes
from src.commands.contracts.builtin import TaskSplitArgs
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    ingest_batches,
    projects,
    task_criteria,
    task_integration_checkpoints,
    task_metadata,
    tasks,
)
from src.models import DepType, Task, TaskStatus
from src.task_names import child_task_id, fresh_root_id

logger = logging.getLogger(__name__)


def _refuse(code, detail):
    raise HierarchyError(code, detail)


def _acceptance_partition(description, criteria, retained, moved):
    """Remove exact criteria, preserving every other byte of the brief.

    Graph creation double-writes acceptance into rows and the trailing
    Acceptance Criteria section. Legacy descriptions with that same section
    are supported too; prose outside it is never heuristically rewritten.
    """
    marker = "\n\n## Acceptance Criteria\n"
    body, sep, rendered = description.rpartition(marker)
    described = rendered.splitlines() if sep else []
    if sep and any(not line.startswith("- ") for line in described):
        _refuse(
            "split.acceptance_unmapped", "acceptance section must contain exact bullet criteria"
        )
    visible = [line[2:] for line in described]
    original = [row["content"] for row in criteria] or visible
    if not original:
        _refuse(
            "split.acceptance_unmapped",
            "source needs explicit acceptance criteria before splitting",
        )
    if len(set(original)) != len(original):
        _refuse("split.acceptance_unmapped", "source acceptance criteria must be unique")
    if Counter(original) != Counter(retained + moved):
        _refuse(
            "split.scope_change", "retained and moved acceptance must exactly partition the source"
        )
    if sep and Counter(visible) != Counter(original):
        _refuse("split.acceptance_unmapped", "description and stored acceptance disagree")
    remaining = [criterion for criterion in original if criterion in retained]
    if sep:
        description = body + marker + "\n".join(f"- {criterion}" for criterion in remaining)
    else:
        description += marker + "\n".join(f"- {criterion}" for criterion in remaining)
    return description


class TaskSplitCommandsMixin:
    async def _cmd_task_split(self, args: dict) -> dict:
        try:
            request = TaskSplitArgs.model_validate(args)
        except ValidationError as exc:
            return {"success": False, "code": "split.invalid", "error": str(exc)}
        source = await self.db.get_task(request.task_id)
        if source is None:
            return {"success": False, "code": "split.not_found", "error": "source task not found"}
        # Reuse integration's authenticated operator/live-supervisor/policy
        # authority even for projects without hierarchical integration.
        if not await self._integration_delivery_authorized(
            source.project_id,
            "integration_mutate_hierarchy",
        ):
            return {
                "success": False,
                "code": "unauthorized",
                "error": "caller cannot split this task",
            }
        try:
            async with task_changes.boundary(self.db, source.project_id) as conn:
                result, flipped, routing = await self._split_task_on(conn, request)
        except HierarchyError as exc:
            return {"success": False, "code": exc.code, "error": exc.detail, **exc.context}
        await self.db.log_blocked_flips(flipped)
        if routing:
            await self._emit_gate_created(routing)
        try:
            moved = await self.db.get_task(result["moved_task_id"])
            await self.orchestrator._emit_task_event(
                "task.created",
                moved,
                parent_task_id=moved.parent_task_id,
                profile_id=moved.profile_id,
                created_by_kind=None,
                created_by_id=None,
            )
            await self._emit_task_graph_change(
                "task.updated", await self.db.get_task(request.task_id)
            )
        except Exception:
            logger.exception("task_split: notification failed after commit for %s", request.task_id)
        return {"success": True, **result}

    async def _split_task_on(self, conn, request):
        db = self.db
        source = (
            (
                await conn.execute(
                    select(tasks).where(tasks.c.id == request.task_id).with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            _refuse("split.not_found", "source task disappeared")
        if await self._split_is_container(conn, request.task_id):
            _refuse("split.not_leaf", "split requires a concrete source leaf")
        metadata = {
            row.key: json.loads(row.value)
            for row in (
                await conn.execute(
                    select(task_metadata).where(
                        task_metadata.c.task_id == request.task_id,
                    )
                )
            ).all()
        }
        if metadata.get("is_container") or metadata.get("container"):
            _refuse("split.not_leaf", "split requires a concrete source leaf")
        if source["held_batch_id"]:
            state = await conn.scalar(
                select(ingest_batches.c.state).where(
                    ingest_batches.c.batch_id == source["held_batch_id"],
                )
            )
            if state != "released":
                _refuse("split.batch_held", "use the held-batch edit path before release")
        project = (
            (
                await conn.execute(
                    select(projects).where(
                        projects.c.id == source["project_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        hierarchical = project["hierarchical_integration_mode"] in {"hierarchy", "train"}
        generation_task = source["parent_task_id"]
        if hierarchical and generation_task:
            generation = await conn.scalar(
                select(task_integration_checkpoints.c.generation)
                .where(
                    task_integration_checkpoints.c.task_id == generation_task,
                )
                .with_for_update()
            )
            if generation is None:
                _refuse("split.missing_generation", "source path has no integration checkpoint")
        else:
            generation = metadata.get("split", {}).get("generation", 0)
        if generation != request.expected_generation:
            _refuse(
                "stale_generation",
                f"expected generation {request.expected_generation}, found {generation}",
            )
        criteria = (
            (
                await conn.execute(
                    select(task_criteria)
                    .where(
                        task_criteria.c.task_id == request.task_id,
                        task_criteria.c.type == "acceptance",
                    )
                    .order_by(task_criteria.c.sort_order, task_criteria.c.id)
                )
            )
            .mappings()
            .all()
        )
        description = _acceptance_partition(
            source["description"],
            criteria,
            request.retained_acceptance,
            request.moved.acceptance,
        )
        targets = list(dict.fromkeys(request.moved.delivery_targets))
        for target in targets:
            row = (
                (await conn.execute(select(tasks).where(tasks.c.id == target)))
                .mappings()
                .one_or_none()
            )
            if row is None or row["project_id"] != source["project_id"]:
                _refuse(
                    "split.delivery_target",
                    f"delivery target must be a live same-project leaf: {target}",
                )
            if await self._split_is_container(conn, target):
                _refuse(
                    "split.delivery_target", f"delivery target must be a concrete leaf: {target}"
                )
        operational = request.moved.placement == "operational_root"
        if operational and source["id"] not in targets:
            _refuse(
                "split.delivery_target",
                "operational remainder must wait for delivery of its retained source",
            )
        parent_id = None if operational else source["parent_task_id"]
        # Keep the inherited brief/constraints on both sides. Only the
        # acceptance section is partitioned; source context is never dropped.
        context = source["description"].partition("\n\n## Acceptance Criteria\n")[0]
        moved = Task(
            id="",
            project_id=source["project_id"],
            repo_id=source["repo_id"],
            title=request.moved.title,
            priority=source["priority"],
            description=(
                f"Remaining work split from {source['id']}.\n\n{context}\n\n"
                "## Acceptance Criteria\n"
                + "\n".join(f"- {item}" for item in request.moved.acceptance)
            ),
            status=TaskStatus.DEFINED,
        )
        service = self._hierarchy_integration_service() if hierarchical else None
        if hierarchical:
            owner = await service._active_batch_owner(
                conn,
                [tid for tid in (source["id"], source["parent_task_id"]) if tid],
            )
            if owner:
                raise HierarchyError(
                    "needs_quiescence",
                    "split would change acceptance owned by an active integration batch",
                    context={
                        "owner": owner,
                        "safe_continuation": (
                            "Wait for the integration batch to finish or ask its owner for a guarded "
                            "exclusion, then retry. Preserve the source claim, branch and checks."
                        ),
                    },
                )
        if hierarchical and parent_id:
            filed = (await service.file_prepared_children_on(conn, parent_id, [moved]))[0]
        elif hierarchical:
            filed = await service.file_root_on(conn, moved)
        else:
            if parent_id:
                moved.id, capped = await child_task_id(conn, parent_id)
                if capped:
                    _refuse("hierarchy.depth", "child naming cap reached")
            else:
                moved.id = await fresh_root_id(conn)
            await db.create_task(moved, conn=conn)
            if parent_id:
                await db.set_parent(moved.id, parent_id, conn=conn)
            filed = {"task_id": moved.id}
        moved_id = filed["task_id"]
        gate_id, created, flipped = await db._create_unrouted_routing_gate_on(
            conn,
            source["project_id"],
            f"Route: {moved.title}",
            question="",
            await_id=None,
            timeout_at=None,
            waiter_task_ids=[moved_id],
        )
        for target in targets:
            flipped |= (
                await db.add_dependency(
                    moved_id,
                    target,
                    DepType.DELIVERY_OF.value,
                    description=request.reason,
                    conn=conn,
                )
                or set()
            )
        await db.add_dependency(
            moved_id,
            source["id"],
            DepType.DISCOVERED_FROM.value,
            description=request.reason,
            conn=conn,
        )
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == source["id"])
            .values(
                description=description,
                updated_at=time.time(),
            )
        )
        # Move criterion identities instead of deleting/recreating them. Other
        # criteria (constraints, tests, etc.) stay with their original source.
        for criterion in criteria:
            if criterion["content"] in request.moved.acceptance:
                await conn.execute(
                    update(task_criteria)
                    .where(
                        task_criteria.c.id == criterion["id"],
                    )
                    .values(task_id=moved_id)
                )
        if not criteria:
            for tid, acceptance in (
                (source["id"], request.retained_acceptance),
                (moved_id, request.moved.acceptance),
            ):
                await conn.execute(
                    insert(task_criteria),
                    [
                        {
                            "id": uuid.uuid4().hex[:12],
                            "task_id": tid,
                            "type": "acceptance",
                            "content": item,
                            "sort_order": index,
                        }
                        for index, item in enumerate(acceptance)
                    ],
                )
        constraints = (
            (
                await conn.execute(
                    select(task_criteria).where(
                        task_criteria.c.task_id == source["id"],
                        task_criteria.c.type == "constraint",
                    )
                )
            )
            .mappings()
            .all()
        )
        if constraints:
            await conn.execute(
                insert(task_criteria),
                [
                    {**dict(row), "id": uuid.uuid4().hex[:12], "task_id": moved_id}
                    for row in constraints
                ],
            )
        for key in ("spec_ingest", "proposal_source", "spec_map", "constraints"):
            if key in metadata:
                await db._upsert_meta(moved_id, key, metadata[key], conn=conn)
        if operational:
            await db._upsert_meta(
                moved_id,
                "delivery_condition",
                {
                    "kind": "delivery-of",
                    "targets": targets,
                },
                conn=conn,
            )
        impact = {
            "source_task_id": source["id"],
            "moved_task_id": moved_id,
            "old_parent": source["parent_task_id"],
            "new_parent": parent_id,
            "retained_acceptance": request.retained_acceptance,
            "moved_acceptance": request.moved.acceptance,
            "delivery_targets": targets,
            "reason": request.reason,
            "generation": generation + 1,
            "previous_split": metadata.get("split"),
            "source_owner": source["assigned_agent_id"],
            "source_branch": source["branch_name"],
        }
        await db._upsert_meta(source["id"], "split", impact, conn=conn)
        await db._upsert_meta(moved_id, "split", impact, conn=conn)
        if hierarchical and generation_task and operational:
            await service._bump_checkpoint(conn, generation_task, generation, generation + 1)
        routing = (
            {
                "gate_id": gate_id,
                "gate_type": "routing",
                "project_id": source["project_id"],
                "title": f"Route: {moved.title}",
                "question": "",
                "await_id": None,
                "timeout_at": None,
                "waiter_task_ids": [moved_id],
            }
            if created
            else None
        )
        return (
            {
                "task_id": source["id"],
                "moved_task_id": moved_id,
                "generation": generation + 1,
                "impact": impact,
            },
            flipped,
            routing,
        )

    async def _split_is_container(self, conn, task_id):
        return await self.db.is_container(task_id, conn=conn) or bool(
            await conn.scalar(
                select(tasks.c.id).where(tasks.c.parent_task_id == task_id).limit(1),
            )
        )
