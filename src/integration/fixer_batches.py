"""Dependency-closed splitting and bounded, lossless deferred batch inputs.

The original batch remains the companion source: its immutable membership and
candidate ref retain every repair commit. A reduced batch never inherits that
candidate's ancestry (which would falsely deliver the dropped members).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace

from sqlalchemy import select, update

from src.database.tables import (
    integration_batch_members, integration_batches, task_dependencies, tasks,
)
from src.integration.batches import Batch, BatchStore, candidate_ref
from src.integration.models import BranchKey
from src.waits.sources import execute_batch_mutation


async def defer_on(conn, batch, *, now, reason, head_sha, revisit_seconds=300, **details):
    """Release the lane, retaining source and candidate refs until re-observation."""
    queue = {**(batch["fixer_queue"] or {}), "status": "deferred", "reason": reason,
             "family_id": (batch["fixer_queue"] or {}).get("family_id", batch["id"]),
             "observed_head": head_sha, "revisit_at": now + revisit_seconds,
             "revisit_seconds": revisit_seconds, **details}
    await execute_batch_mutation(conn, update(integration_batches).where(
        integration_batches.c.id == batch["id"],
    ).values(intent="aborted", lifecycle="aborted", cleanup_state="retained",
             human_abort_reason=reason, fixer_queue=queue, updated_at=now))
    return queue


class FixerBatches:
    def __init__(self, db, *, clock=time.time):
        self.db, self.clock = db, clock
        self.store = BatchStore(db, clock=clock)

    async def split(self, runtime, batch_id, evidence, *, policy, authorize):
        from src.integration.fixers import split_members

        if policy is not None and not policy.split_attributed:
            return None
        groups = evidence.get("groups") or []
        if not groups or any(not group.get("attributed_member") for group in groups):
            return None
        batch, lane = await runtime.lane({"batch_id": batch_id})
        # Promotion requests and refresh sentinels have distinct delivery semantics.
        row = await self.db.get_integration_batch(batch_id)
        if batch.epic_refresh or batch.epic_sync or row["trigger"] == "promotion":
            return None
        members = await self.store.members(batch_id)
        ids = {m.task_id for m in members}
        async with self.db._engine.connect() as conn:
            edges = (await conn.execute(select(task_dependencies).where(
                task_dependencies.c.task_id.in_(ids),
                task_dependencies.c.depends_on_task_id.in_(ids),
                task_dependencies.c.dep_type == "blocks",
            ))).mappings().all()
        dependencies = {key: set() for key in ids}
        for edge in edges:
            dependencies[edge["task_id"]].add(edge["depends_on_task_id"])
        repo = await lane.service.gitops.repository(batch)
        # A stacked source is a dependency even if its task edge was later removed.
        for member in members:
            for other in members:
                if member != other and await lane.service.gitops.is_ancestor(
                    repo, other.source_sha, member.source_sha,
                ):
                    dependencies[member.task_id].add(other.task_id)
        plan = split_members(ids, [[g["attributed_member"]] for g in groups], dependencies)
        if plan is None:
            return None
        retained = tuple(replace(m, order=i) for i, m in enumerate(
            m for m in members if m.task_id in plan["retained"]
        ))
        key = hashlib.sha256(repr(retained).encode()).hexdigest()
        queue = row["fixer_queue"] or {}
        if key in queue.get("split_keys", []):
            return None
        head = evidence["head_sha"]
        delivery = await lane.service.gitops.remote(repo, batch.target_ref)
        if not delivery:
            return None
        replacement = replace(batch, id=batch.id + "-split-" + key[:12],
                              repair_attempt_count=0, created_at=self.clock())
        async with self.db.immediate() as conn:
            await self.store._target_lock(conn, batch)
            current = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).with_for_update())).mappings().one()
            if current["intent"] != "open" or current["lifecycle"] == "promoted":
                return None
            if key in (current["fixer_queue"] or {}).get("split_keys", []):
                return None
            await conn.execute(select(tasks.c.id).where(tasks.c.id.in_(ids))
                               .order_by(tasks.c.id).with_for_update())
            if authorize is None or not await authorize():
                return None
            # The same row lock and per-ref fence as fixer publication. No
            # writer can change the candidate while its split input is frozen.
            fence = await runtime.locks.acquire(BranchKey(
                repository_id=batch.repository_id,
                branch=candidate_ref(batch.id).removeprefix("refs/heads/"),
            ), "split:" + batch.id, role="repair")
            try:
                if (await lane.service.gitops.remote(repo, candidate_ref(batch.id)) != head
                        or await lane.service.gitops.remote(repo, batch.target_ref) != delivery):
                    return None
                frozen = (await conn.execute(select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == batch.id,
                ))).mappings().all()
                keys = [*(current["fixer_queue"] or {}).get("split_keys", []), key]
                deferred = await defer_on(conn, current, now=self.clock(), reason="split",
                    head_sha=head, replacement_batch_id=replacement.id,
                    deferred_members=plan["deferred"], delivery_sha=delivery, split_keys=keys,
                    revisit_seconds=policy.deferred_revisit_seconds if policy else 300)
                await conn.execute(update(integration_batches).where(
                    integration_batches.c.id == batch.id,
                ).values(ejection_record={
                    "batch_id": batch.id, "project_id": batch.project_id,
                    "task_id": plan["deferred"][0], "deferred_members": plan["deferred"],
                    "replacement_batch_id": replacement.id, "operator_id": "integration-train",
                    "reason": "Conclusive attribution and transitive dependent closure",
                }))
                await self.store._freeze_on(conn, replacement, retained,
                    trees={m["task_id"]: m["reviewed_tree_sha"] for m in frozen})
                await conn.execute(update(integration_batches).where(
                    integration_batches.c.id == replacement.id,
                ).values(fixer_queue={"family_id": deferred["family_id"],
                                     "split_keys": keys, "delivery_sha": delivery}))
            finally:
                await runtime.locks.release(fence)
        return {"success": True, "outcome": "split", "replacement_batch_id": replacement.id,
                **plan}

    async def resume(self, target, snapshot, service):
        """Due observation is finite; unchanged exhausted work never re-admits.

        Split obligations resume after their reduced candidate settles. Budget
        deferrals resume only for a changed saved candidate or exact current
        green, without launching a successor. All original sources ride with
        the saved candidate when the delivery tip has advanced.
        """
        table = integration_batches
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(select(table).where(
                table.c.project_id == target.project_id,
                table.c.repository_id == target.repository_id,
                table.c.target_ref == target.target_ref,
                table.c.intent == "aborted", table.c.fixer_queue["status"].astext == "deferred",
            ).order_by(table.c.created_at, table.c.id))).mappings().all()
        for row in rows:
            queue = dict(row["fixer_queue"])
            head = snapshot.observation.source_heads.get(
                "refs/remotes/origin/" + candidate_ref(row["id"]).removeprefix("refs/heads/"))
            companion_id = queue.get("companion_batch_id")
            companion_head = (snapshot.observation.source_heads.get(
                "refs/remotes/origin/" + candidate_ref(companion_id).removeprefix("refs/heads/"))
                if companion_id else None)
            replacement = (await self.db.get_integration_batch(queue["replacement_batch_id"])
                           if queue.get("replacement_batch_id") else None)
            released = bool(queue["reason"] == "split" and replacement
                            and replacement["lifecycle"] == "promoted")
            if queue["revisit_at"] > self.clock() and not released:
                continue
            changed = bool(head and (head != queue["observed_head"] or (
                companion_head and companion_head != queue.get("companion_sha"))))
            retry_capacity = queue["reason"] == "capacity"
            green = False
            if head and not (changed or released):
                if service.observe_deferred is not None:
                    green = await service.observe_deferred(Batch.from_row(row), head)
                else:
                    repo = await service.gitops.repository(Batch.from_row(row))
                    tree = await service.gitops.git.atree_sha(str(repo.store), head)
                    green = await service.gate(Batch.from_row(row), head, tree)
            async with self.db.immediate() as conn:
                await self.store._target_lock(conn, Batch.from_row(row))
                current = (await conn.execute(select(table).where(
                    table.c.id == row["id"],
                ).with_for_update())).mappings().one()
                if current["intent"] != "aborted" or current["fixer_queue"] != queue:
                    continue
                queue["revisit_at"] = self.clock() + queue["revisit_seconds"]
                if not (head and (released or changed or green or retry_capacity)):
                    await conn.execute(update(table).where(table.c.id == row["id"])
                                       .values(fixer_queue=queue))
                    continue
                # Abort history is immutable. Freeze a new batch with the same
                # source manifest and explicit companion, never reopen/reset it.
                key = hashlib.sha256(repr((head, companion_head, snapshot.target_oid))
                                     .encode()).hexdigest()[:12]
                successor = replace(Batch.from_row(row), id=row["id"] + "-resume-" + key,
                                    intent="open", repair_attempt_count=0, created_at=self.clock())
                await self.store._exclusive_target(conn, successor)
                frozen = (await conn.execute(select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == row["id"],
                ).order_by(integration_batch_members.c.ordinal))).mappings().all()
                from src.integration.batches import BatchMember

                members = tuple(BatchMember(m["task_id"], m["source_sha"],
                                             m["source_base_sha"], m["ordinal"]) for m in frozen)
                await self.store._freeze_on(conn, successor, members,
                    trees={m["task_id"]: m["reviewed_tree_sha"] for m in frozen})
                successor_queue = {**queue, "status": "resumed", "companion_sha": head,
                                   "companion_batch_id": row["id"]}
                if (companion_head and not current["fixer_state"]
                        and not current["repair_attempt_count"]):
                    # This intermediate batch never had a writer. Its assembly
                    # contains sources/delivery already retained by the next
                    # assembly; keep following the actual fixer branch so a
                    # late save is not hidden behind this immutable abort.
                    successor_queue.update(companion_sha=companion_head,
                                           companion_batch_id=companion_id)
                successor_queue.pop("replacement_batch_id", None)
                await conn.execute(update(table).where(table.c.id == successor.id)
                                   .values(fixer_queue=successor_queue))
                queue.update(status="companion", successor_batch_id=successor.id)
                values = {"fixer_queue": queue, "updated_at": self.clock()}
                if not row["ejection_record"]:
                    values["ejection_record"] = {
                        "batch_id": row["id"], "project_id": row["project_id"],
                        "task_id": members[0].task_id, "replacement_batch_id": successor.id,
                        "operator_id": "integration-train", "reason": "Deferred input resumed",
                    }
                # Capacity waits have no process/checkout to rebind. Transfer the
                # one unlaunched identity; all registered sessions rebind explicitly.
                state = current["fixer_state"] or {}
                if state.get("status") == "waiting_capacity":
                    await conn.execute(update(table).where(table.c.id == successor.id).values(
                        fixer_state={**state, "batch_id": successor.id,
                                     "target_ref": candidate_ref(successor.id),
                                     "starting_sha": head}))
                    values["fixer_state"] = None
                await execute_batch_mutation(conn, update(table).where(table.c.id == row["id"])
                                              .values(**values))
                return successor
        return None
