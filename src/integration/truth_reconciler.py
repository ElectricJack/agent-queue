"""Bounded, repeatable repair of disposable integration projections from Git.

No new writer is authorized here. Live sessions, workspace claims, ref leases
and operations stay fenced. Unknown Git facts remain retryable; one bad row
does not prevent the next page from being inspected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from sqlalchemy import delete, exists, select, union, update
from sqlalchemy.dialects.postgresql import insert

from src.database import tables as t
from src.database.queries.integration_state_queries import session_attached_clause
from src.integration.delegate_release import live_integration_owner
from src.integration.delivery_observer import DeliveryObserver, DeliveryTarget, delivery_targets
from src.integration.delivery_truth import DeliveryState
from src.integration.git_truth import GitTruth, snapshot_scope
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.selection_metrics import SelectionMetrics, selection_metrics_scope
from src.integration.truth_retention import delivery_identity_key, retain_delivery, retain_objects
from src.logging_config import CorrelationContext
from src.metrics.operation_profile import operation_profile

logger = logging.getLogger(__name__)


class GitTruthReconciler:
    """One cursor per source, advanced even when a row cannot yet be repaired."""

    def __init__(self, db, git, *, data_dir, owner_recovery=None, obsolete_close=None, page_size=50):
        if page_size < 1:
            raise ValueError("page size must be positive")
        self.db, self.git = db, git
        self.observer = DeliveryObserver(
            db, git=git, data_dir=data_dir, store_name="repository",
            truth=GitTruth(git, share_fetches=True, skip_unchanged=True),
        )
        self.owner_recovery = owner_recovery
        self.obsolete_close = obsolete_close
        self.page_size = page_size
        self._cursor = ""
        self._slot_cursor = ""
        self._orphan_cursor = ""
        self._origin_cursor = ""
        self._owner_cursor = ""
        self._repair_cursor = ""
        self._stage = 0
        self._lock = asyncio.Lock()

    async def tick(self, now):
        with snapshot_scope(reuse=True):
            return await self._tick(now)

    async def _tick(self, now):
        if self._lock.locked():
            return {"outcome": "running"}
        async with self._lock:
            counts = {"examined": 0, "repaired": 0, "sentinels": 0}
            stages = [
                ("delivery", lambda: self._completed(now, counts)),
                ("owners", self._owners),
                ("sentinels", lambda: self._sentinels(now)),
                ("orphans", lambda: self._orphans(now)),
                ("origins", lambda: self._origins(now)),
                ("repairs", lambda: self._repairs(now)),
            ]
            ordered = stages[self._stage:] + stages[:self._stage]
            self._stage = (self._stage + 1) % len(stages)
            timing, started = {}, time.perf_counter()
            try:
                for name, callback in ordered:
                    phase_started, outcome = time.perf_counter(), "completed"
                    selection = SelectionMetrics()
                    with operation_profile() as profile, selection_metrics_scope(selection):
                        try:
                            async with asyncio.timeout(60):
                                counts[name] = await callback()
                        except asyncio.CancelledError:
                            outcome = "cancelled"
                            raise
                        except Exception as exc:
                            outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
                            logger.exception("Git truth %s pass remains retryable", name)
                        finally:
                            timing[name] = {
                                "elapsed_seconds": time.perf_counter() - phase_started,
                                "outcome": outcome, **profile.as_dict(),
                                "selection": selection.as_dict(),
                            }
            finally:
                with CorrelationContext(git_truth_timing={
                    "elapsed_seconds": time.perf_counter() - started, "phases": timing,
                }):
                    logger.info("git truth reconciliation timing")
            return counts

    async def _repairs(self, now):
        """A delivered batch must not leave an unclaimable repair blocking work."""
        if self.obsolete_close is None:
            return 0
        async with self.db._engine.connect() as conn:
            rows = list((await conn.execute(select(t.tasks).join(
                t.integration_batches,
                t.integration_batches.c.id == t.tasks.c.created_by_id,
            ).where(
                t.integration_batches.c.lifecycle == "promoted",
                t.tasks.c.created_by_kind == "system",
                t.tasks.c.status != "COMPLETED",
                t.tasks.c.id > self._repair_cursor,
                t.tasks.c.dedup_key.startswith("repair:", autoescape=True),
            ).order_by(t.tasks.c.id).limit(self.page_size))).mappings())
        self._repair_cursor = rows[-1]["id"] if len(rows) == self.page_size else ""
        changed = 0
        for row in rows:
            try:
                changed += await self._retire_repair(dict(row), now)
            except Exception:
                logger.exception("Git truth batch repair remains retryable for %s", row["id"])
        return changed

    async def _retire_repair(self, task, now):
        from src.integration.delivery_watchdog import _held_on
        from src.integration.repair import OrdinaryRepairService
        from src.operator_decisions import history_on, related_on
        from src.sessions.obsolete import ObsoleteCloseRefused

        original = await OrdinaryRepairService(self.db).input(task["id"])
        if not original or original["batch_id"] != task["created_by_id"]:
            return False
        batch = await self.db.get_integration_batch(original["batch_id"])
        repository = await self.db.get_repo(original["repository_id"])
        if (not batch or batch["lifecycle"] != "promoted" or batch["intent"] != "open"
                or not batch["final_main_sha"]
                or repository is None or repository.project_id != task["project_id"]
                or batch["project_id"] != task["project_id"]
                or batch["repository_id"] != repository.id or task["repo_id"] != repository.id
                or task["branch_name"] != original["target_ref"].removeprefix("refs/heads/")):
            return False
        snapshot = await self.observer.snapshot(DeliveryTarget(
            task["project_id"], repository.id, repository.url, batch["target_ref"],
        ))
        observed = snapshot.observation
        if observed.error not in {None, "target_ref_missing"}:
            return False
        # Epic batches can be settled by delivery straight to the project branch.
        if not observed.target_oid or await self.git.ais_ancestor(
            observed.store, batch["final_main_sha"], observed.target_oid, strict=True,
        ) is not True:
            snapshot = snapshot.for_target("refs/heads/" + repository.default_branch)
            observed = snapshot.observation
        if observed.error:
            return False
        async with self.db._engine.connect() as conn:
            members = list((await conn.execute(select(t.integration_batch_members).where(
                t.integration_batch_members.c.batch_id == batch["id"],
            ).order_by(t.integration_batch_members.c.ordinal))).mappings())
        repair_head = observed.source_heads.get(
            "refs/remotes/origin/" + original["target_ref"].removeprefix("refs/heads/"),
        )
        sources = {batch["final_main_sha"], *(member["source_sha"] for member in members)}
        # An untouched disposable merge need not have landed: its frozen inputs
        # can reach the target by another merge. Actual repair commits must land.
        if repair_head and repair_head != original["starting_sha"]:
            sources.add(repair_head)
        if not members or None in sources or not observed.target_oid or not all([
            await self.git.ais_ancestor(observed.store, sha, observed.target_oid, strict=True)
            is True for sha in sources
        ]):
            return False
        await retain_objects(observed, sources | ({repair_head} if repair_head else set()))

        async def prepare(conn):
            from src.git.manager import RemoteRefState

            await self.db.lock_hierarchy_project(conn, task["project_id"])
            current = (await conn.execute(select(t.tasks).where(
                t.tasks.c.id == task["id"],
            ).with_for_update())).mappings().one()
            current_batch = (await conn.execute(select(t.integration_batches).where(
                t.integration_batches.c.id == batch["id"],
            ).with_for_update())).mappings().one()
            refs = await related_on(conn, "batch", batch["id"])
            busy = await conn.scalar(select(exists().where(
                t.sessions.c.task_id == task["id"], session_attached_clause(),
            ))) or await conn.scalar(select(exists().where(
                t.workspaces.c.locked_by_task_id == task["id"],
            )))
            remote = await self.git.als_remote_ref(
                observed.store, original["target_ref"].removeprefix("refs/heads/"),
            )
            branch_fresh = (remote.state is RemoteRefState.PRESENT and remote.oid == repair_head
                            if repair_head else remote.state is RemoteRefState.ABSENT)
            if (dict(current) != task or dict(current_batch) != batch or busy
                    or current["assigned_agent_id"] or current["status"] in {"ASSIGNED", "IN_PROGRESS"}
                    or await _held_on(conn, task["project_id"], task["id"], current["status"])
                    or any(row["active"] for row in await history_on(conn, task["project_id"], refs))
                    or not branch_fresh or not await snapshot.is_fresh()):
                raise ObsoleteCloseRefused("obsolete.changed", "repair or delivery changed")

        try:
            await self.obsolete_close.close(
                task["id"], reason=f"Batch {batch['id']} already delivered at {observed.target_oid}",
                principal="git-truth-reconciler", _prepare_on=prepare,
            )
        except ObsoleteCloseRefused:
            return False
        return True

    async def _completed(self, now, counts):
        async with self.db._engine.connect() as conn:
            terminal = union(*[
                select(table.c.id).where(table.c.status == "COMPLETED")
                for table in (t.tasks, t.archived_tasks)
            ]).subquery()
            ids = list((await conn.execute(select(terminal.c.id).where(
                terminal.c.id > self._cursor,
            ).order_by(terminal.c.id).limit(self.page_size))).scalars())
        self._cursor = ids[-1] if len(ids) == self.page_size else ""
        if ids:
            # Final/chain target: the immediate parent still needs collection inputs.
            view = await self.observer.observe(ids, target_loader=delivery_targets)
            counts["examined"] = len(ids)
            for task_id in ids:
                try:
                    counts["repaired"] += await self._repair(view, task_id, now)
                except Exception:
                    logger.exception("Git truth repair remains retryable for %s", task_id)
        return counts["repaired"]

    async def _owners(self):
        from src.integration.owner_recovery import COLLECTOR_ROLE, RECOVERABLE_STATES

        if self.owner_recovery is None:
            return 0
        owners = t.integration_branch_owners
        async with self.db._engine.connect() as conn:
            ids = list((await conn.execute(select(owners.c.id).where(
                owners.c.id > self._owner_cursor,
                owners.c.handoff_state.in_(RECOVERABLE_STATES),
                owners.c.owner_role != COLLECTOR_ROLE,
                ~exists().where(
                    t.sessions.c.id == owners.c.session_id, session_attached_clause(),
                ),
            ).order_by(owners.c.id).limit(self.page_size))).scalars())
        self._owner_cursor = ids[-1] if len(ids) == self.page_size else ""
        # Recovery rechecks the writer/provider and retains unpublished work.
        # Advancing even on refusal prevents a blocked first page starving the rest.
        await self.owner_recovery.recover_many(ids, principal="git-truth-reconciler")
        return len(ids)

    async def _repair(self, view, task_id, now):
        evidence = view.get(task_id)
        if (evidence is None or evidence.state not in {
                DeliveryState.CONTAINED, DeliveryState.NO_CHANGE,
        } or evidence.source_oid is None):
            return False
        snapshot = next(s for s in view.snapshots if (
            s.project_id, s.repository_id, s.target_ref
        ) == (evidence.request.project_id, evidence.request.repository_id,
              evidence.request.target_ref))
        request = evidence.request
        key = delivery_identity_key(request, evidence.source_oid)
        domain = "git-truth:" + key
        async with self.db._engine.connect() as conn:
            recorded = (await conn.execute(select(
                t.task_delivery_receipts.c.id,
                exists().where(
                    t.task_branch_origins.c.task_id == task_id,
                    t.task_branch_origins.c.repository_id == request.repository_id,
                    t.task_branch_origins.c.retired_at.is_(None),
                ) | exists().where(
                    t.task_integration_checkpoints.c.task_id == task_id,
                    t.task_integration_checkpoints.c.repository_id == request.repository_id,
                ) | exists().where(
                    t.integration_branch_owners.c.owner_id == task_id,
                    t.integration_branch_owners.c.repository_id == request.repository_id,
                    t.integration_branch_owners.c.handoff_state != "released",
                ),
            ).where(
                t.task_delivery_receipts.c.domain_key == domain,
            ))).first()
        receipt = recorded[0] if recorded else None
        # This exact historical witness already exists and there is nothing to
        # mutate. A later projection is revisited next pass; receipts never
        # replace Git proof, freshness or writer checks for actual repairs.
        if recorded and not recorded[1]:
            return False
        witness = None if receipt else await retain_delivery(snapshot, evidence)
        if not await view.fresh():
            return False
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, request.project_id)
            task = (await conn.execute(select(t.tasks).where(
                t.tasks.c.id == task_id,
            ).with_for_update())).mappings().first()
            if task is None and request.archived:
                task = (await conn.execute(select(t.archived_tasks).where(
                    t.archived_tasks.c.id == task_id,
                ).with_for_update())).mappings().first()
            if task is None or task["status"] != "COMPLETED":
                return False
            if task_id not in await view.verified_on(conn, [task_id]):
                return False
            if await self._busy(conn, task_id):
                return False
            if witness is not None:
                await conn.execute(insert(t.task_delivery_receipts).values(
                    id=key, domain_key=domain, source_task_id=task_id,
                    repository_id=request.repository_id,
                    target_branch=request.target_ref.removeprefix("refs/heads/"),
                    reviewed_head_sha=evidence.source_oid, after_sha=evidence.target_oid,
                    disposition="noop" if evidence.state is DeliveryState.NO_CHANGE else "code",
                    resolution_evidence={
                        "git_truth": witness, "first_observed_at": now,
                    }, created_at=now,
                ).on_conflict_do_nothing(index_elements=["domain_key"]))
            # Keep origins as source history; only retire their live reservation.
            origins = list((await conn.execute(select(t.task_branch_origins).where(
                t.task_branch_origins.c.task_id == task_id,
                t.task_branch_origins.c.repository_id == request.repository_id,
                t.task_branch_origins.c.retired_at.is_(None),
            ).with_for_update())).mappings())
            checkpoint = (await conn.execute(select(t.task_integration_checkpoints).where(
                t.task_integration_checkpoints.c.task_id == task_id,
                t.task_integration_checkpoints.c.repository_id == request.repository_id,
            ).with_for_update())).mappings().first()
            # An active episode still consumes this checkpoint, even for a
            # terminal task. Its owning workflow, not this projection, ends it.
            if checkpoint and (checkpoint["episode_id"] or checkpoint["checkpoint_sha"] not in (
                None, evidence.source_oid, getattr(evidence, "source_base", None),
            )):
                return False
            released = await self._release_owners(conn, task_id, snapshot, evidence, now)
            if released is None:
                return False
            await conn.execute(update(t.task_branch_origins).where(
                t.task_branch_origins.c.id.in_([row["id"] for row in origins]),
            ).values(retired_at=now))
            if checkpoint:
                await conn.execute(delete(t.task_integration_checkpoints).where(
                    t.task_integration_checkpoints.c.task_id == task_id,
                    t.task_integration_checkpoints.c.version == checkpoint["version"],
                ))
            changed = bool(origins or checkpoint or released or witness)
            if changed:
                await self.db.log_event(
                    "integration.git_truth_reconciled", project_id=request.project_id,
                    task_id=task_id, payload=json.dumps({
                        "receipt": domain, "source_oid": evidence.source_oid,
                        "target_oid": evidence.target_oid,
                        "origins": [dict(row) for row in origins],
                        "checkpoint": dict(checkpoint) if checkpoint else None,
                        "released_owners": released,
                    }), conn=conn,
                )
            return changed

    async def _orphans(self, now):
        """A deleted task needs preservation, never invented delivery evidence."""
        checkpoint = t.task_integration_checkpoints
        async with self.db._engine.connect() as conn:
            rows = list((await conn.execute(select(checkpoint).where(
                checkpoint.c.task_id > self._orphan_cursor,
                ~exists().where(t.tasks.c.id == checkpoint.c.task_id),
                ~exists().where(t.archived_tasks.c.id == checkpoint.c.task_id),
            ).order_by(checkpoint.c.task_id).limit(self.page_size))).mappings())
        self._orphan_cursor = rows[-1]["task_id"] if len(rows) == self.page_size else ""
        repaired = 0
        for row in rows:
            try:
                repaired += await self._orphan(dict(row), now)
            except Exception:
                logger.exception("Git truth orphan remains retryable for %s", row["task_id"])
        return repaired

    async def _orphan(self, row, now):
        repository = await self.db.get_repo(row["repository_id"])
        if repository is None or row["episode_id"]:
            return False
        target = DeliveryTarget(repository.project_id, repository.id, repository.url,
                                "refs/heads/" + repository.default_branch)
        snapshot = await self.observer.snapshot(target)
        observed = snapshot.observation
        if observed.error:
            return False
        async with self.db._engine.connect() as conn:
            origins = list((await conn.execute(select(t.task_branch_origins).where(
                t.task_branch_origins.c.task_id == row["task_id"],
                t.task_branch_origins.c.repository_id == repository.id,
                t.task_branch_origins.c.retired_at.is_(None),
            ))).mappings())
        retained = await retain_objects(observed, [
            row["checkpoint_sha"], row["verified_sha"],
            *[origin["base_sha"] for origin in origins],
        ])
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, repository.project_id)
            if any([await conn.scalar(select(exists().where(
                table.c.id == row["task_id"],
            ))) for table in (t.tasks, t.archived_tasks)]):
                return False
            current = (await conn.execute(select(t.task_integration_checkpoints).where(
                t.task_integration_checkpoints.c.task_id == row["task_id"],
            ).with_for_update())).mappings().first()
            current_origins = list((await conn.execute(select(t.task_branch_origins).where(
                t.task_branch_origins.c.task_id == row["task_id"],
                t.task_branch_origins.c.repository_id == repository.id,
                t.task_branch_origins.c.retired_at.is_(None),
            ).with_for_update())).mappings())
            if (current is None or dict(current) != row or
                    {r["id"]: dict(r) for r in current_origins} !=
                    {r["id"]: dict(r) for r in origins} or
                    await self._busy(conn, row["task_id"])):
                return False
            await conn.execute(delete(t.task_integration_checkpoints).where(
                t.task_integration_checkpoints.c.task_id == row["task_id"],
            ))
            await conn.execute(update(t.task_branch_origins).where(
                t.task_branch_origins.c.id.in_([r["id"] for r in origins]),
            ).values(retired_at=now))
            await self.db.log_event(
                "integration.git_truth_orphan_retired", project_id=repository.project_id,
                payload=json.dumps({"checkpoint": row, "retained": retained,
                                    "origins": [dict(r) for r in origins]}), conn=conn,
            )
            return True

    async def _origins(self, now):
        """Heal materialization/name drift without rewriting the filing base."""
        async with self.db._engine.connect() as conn:
            rows = list((await conn.execute(select(t.task_branch_origins).where(
                t.task_branch_origins.c.retired_at.is_(None),
                t.task_branch_origins.c.id > self._origin_cursor,
            ).order_by(t.task_branch_origins.c.id).limit(self.page_size))).mappings())
        self._origin_cursor = rows[-1]["id"] if len(rows) == self.page_size else ""
        changed = 0
        for row in rows:
            try:
                changed += await self._origin(dict(row), now)
            except Exception:
                logger.exception("Git truth origin remains retryable for %s", row["id"])
        return changed

    async def _origin(self, row, now):
        task = await self.db.get_task(row["task_id"])
        if task is None:
            return await self._orphan_origin(row, now)
        if not task.branch_name:
            return False
        if row["materialized"] and row["branch_name"] == task.branch_name:
            return False
        repository = await self.db.get_repo(row["repository_id"])
        if repository is None:
            return False
        snapshot = await self.observer.snapshot(DeliveryTarget(
            repository.project_id, repository.id, repository.url,
            "refs/heads/" + repository.default_branch,
        ))
        observed = snapshot.observation
        head = observed.source_heads.get(
            "refs/remotes/origin/" + task.branch_name.removeprefix("refs/heads/"),
        )
        if observed.error or not head or not await self.git.ais_ancestor(
            observed.store, row["base_sha"], head, strict=True,
        ):
            return False
        from src.git.manager import RemoteRefState

        branch = task.branch_name.removeprefix("refs/heads/")
        remote = (await self.git.als_remote_refs(
            observed.store, [branch], repository_url=repository.url,
        ))[branch]
        if remote.state is not RemoteRefState.PRESENT or remote.oid != head:
            return False
        if row["materialized"]:
            old_head = observed.source_heads.get(
                "refs/remotes/origin/" + (row["branch_name"] or "").removeprefix("refs/heads/"),
            )
            await retain_objects(observed, [row["base_sha"], old_head, head])
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, task.project_id)
            current_task = (await conn.execute(select(t.tasks).where(
                t.tasks.c.id == task.id,
            ).with_for_update())).mappings().first()
            current = (await conn.execute(select(t.task_branch_origins).where(
                t.task_branch_origins.c.id == row["id"],
            ).with_for_update())).mappings().first()
            if (current is None or dict(current) != row or current_task is None or
                    current_task["branch_name"] != task.branch_name or
                    current_task["updated_at"] != task.updated_at or
                    await self._busy(conn, task.id)):
                return False
            values = dict(branch_name=task.branch_name, reserved=True, materialized=True,
                          materialized_at=row["materialized_at"] or now)
            if row["materialized"]:
                # Materialized identities are immutable. Preserve the old row
                # and replace only its live projection, with the same filing base.
                await conn.execute(update(t.task_branch_origins).where(
                    t.task_branch_origins.c.id == row["id"],
                ).values(retired_at=now))
                replacement = {**row, **values, "id": hashlib.sha256(
                    json.dumps([row["id"], task.branch_name]).encode(),
                ).hexdigest()}
                await conn.execute(insert(t.task_branch_origins).values(**replacement))
            else:
                await conn.execute(update(t.task_branch_origins).where(
                    t.task_branch_origins.c.id == row["id"],
                ).values(**values))
            await self.db.log_event(
                "integration.git_truth_origin_repaired", project_id=task.project_id,
                task_id=task.id, payload=json.dumps({
                    "origin_id": row["id"], "branch": task.branch_name, "head": head,
                    "previous_branch": row["branch_name"], "base_sha": row["base_sha"],
                }), conn=conn,
            )
            return True

    async def _orphan_origin(self, row, now):
        """Retain a deleted task's origin even when it has no checkpoint."""
        async with self.db._engine.connect() as conn:
            if await conn.scalar(select(exists().where(
                t.archived_tasks.c.id == row["task_id"],
            ))) or await conn.scalar(select(exists().where(
                t.task_integration_checkpoints.c.task_id == row["task_id"],
            ))):
                return False  # completion or checkpoint pass owns this history
        repository = await self.db.get_repo(row["repository_id"])
        if repository is None:
            return False
        snapshot = (await self.observer.snapshot(DeliveryTarget(
            repository.project_id, repository.id, repository.url,
            "refs/heads/" + repository.default_branch,
        ))).observation
        if snapshot.error:
            return False
        head = snapshot.source_heads.get(
            "refs/remotes/origin/" + (row["branch_name"] or "").removeprefix("refs/heads/"),
        )
        retained = await retain_objects(snapshot, [row["base_sha"], head])
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, repository.project_id)
            if any([await conn.scalar(select(exists().where(table.c.id == row["task_id"])))
                    for table in (t.tasks, t.archived_tasks)]) or await conn.scalar(
                select(exists().where(t.task_integration_checkpoints.c.task_id == row["task_id"])),
            ) or await self._busy(conn, row["task_id"]):
                return False
            current = (await conn.execute(select(t.task_branch_origins).where(
                t.task_branch_origins.c.id == row["id"],
            ).with_for_update())).mappings().first()
            if current is None or dict(current) != row:
                return False
            await conn.execute(update(t.task_branch_origins).where(
                t.task_branch_origins.c.id == row["id"],
            ).values(retired_at=now))
            await self.db.log_event(
                "integration.git_truth_orphan_origin_retired",
                project_id=repository.project_id,
                payload=json.dumps({"origin": row, "retained": retained}), conn=conn,
            )
            return True

    async def _busy(self, conn, task_id):
        return bool(
            await conn.scalar(select(exists().where(
                t.sessions.c.task_id == task_id, session_attached_clause(),
            )))
            or await conn.scalar(select(exists().where(
                t.workspaces.c.locked_by_task_id == task_id,
            )))
            or await conn.scalar(select(exists().where(
                t.integration_branch_owners.c.owner_id == task_id,
                t.integration_branch_owners.c.handoff_state != "released",
                (t.integration_branch_owners.c.holder.is_not(None)
                 | t.integration_branch_owners.c.session_id.is_not(None)
                 | t.integration_branch_owners.c.workspace_id.is_not(None)
                 | (t.integration_branch_owners.c.handoff_state != "reserved")),
            )))
            or await live_integration_owner(conn, [task_id])
        )

    async def _release_owners(self, conn, task_id, snapshot, evidence, now):
        from src.git.manager import RemoteRefState

        rows = list((await conn.execute(select(t.integration_branch_owners).where(
            t.integration_branch_owners.c.owner_id == task_id,
            t.integration_branch_owners.c.repository_id == snapshot.repository_id,
            t.integration_branch_owners.c.owner_role.in_(("worker", "repair")),
            t.integration_branch_owners.c.handoff_state == "reserved",
            t.integration_branch_owners.c.session_id.is_(None),
            t.integration_branch_owners.c.workspace_id.is_(None),
            t.integration_branch_owners.c.holder.is_(None),
        ))).mappings())
        branches = [row["ref"].removeprefix("refs/heads/") for row in rows]
        remote_refs = await snapshot.git.als_remote_refs(
            snapshot.store, branches, repository_url=snapshot.repository_url,
        ) if branches else {}
        if any(ref.state is RemoteRefState.ERROR or (
            ref.state is RemoteRefState.PRESENT and ref.oid != evidence.source_oid
        ) for ref in remote_refs.values()) or len(remote_refs) != len(set(branches)):
            return None  # later/unobservable work keeps its checkpoint and origin
        released = []
        for row in rows:
            branch = row["ref"].removeprefix("refs/heads/")
            current = await BranchLock(self.db).lock_on(
                conn, BranchKey(repository_id=row["repository_id"], branch=branch),
            )
            if current is None or dict(current) != dict(row):
                return None
            if await conn.scalar(select(exists().where(
                t.integration_candidate_ref_mutations.c.repository_id == row["repository_id"],
                t.integration_candidate_ref_mutations.c.branch.in_((branch, "refs/heads/" + branch)),
                t.integration_candidate_ref_mutations.c.state == "reserved",
            ))):
                return None
            await conn.execute(update(t.integration_branch_owners).where(
                t.integration_branch_owners.c.id == row["id"],
            ).values(handoff_state="released", fence_token=row["fence_token"] + 1,
                     updated_at=now))
            released.append(row["id"])
        return released

    async def _sentinels(self, now):
        from src.orchestrator.worktree_manager import WorktreeSlotManager

        async with self.db._engine.connect() as conn:
            slots = list((await conn.execute(select(t.workspaces).where(
                t.workspaces.c.base_workspace_id.is_not(None),
                t.workspaces.c.slot_index.is_not(None),
                t.workspaces.c.id > self._slot_cursor,
            ).order_by(t.workspaces.c.id).limit(self.page_size))).mappings())
        self._slot_cursor = slots[-1]["id"] if len(slots) == self.page_size else ""
        changed = 0
        for slot in slots:
            try:
                changed += await self._sentinel(slot, WorktreeSlotManager, now)
            except Exception:
                logger.exception("Git truth sentinel repair remains retryable for %s", slot["id"])
        return changed

    async def _sentinel(self, slot, manager, now):
        from src.claim_file import read_claim_file
        from src.models import WorktreeSentinel

        path = Path(slot["workspace_path"])
        if not path.exists() or not (path / ".git").is_file():
            return False  # never the operator clone or an unregistered directory
        if read_claim_file(path) is not None:
            return False
        async with self.db.immediate() as conn:
            current = (await conn.execute(select(t.workspaces).where(
                t.workspaces.c.id == slot["id"],
            ).with_for_update())).mappings().first()
            if (current is None or dict(current) != dict(slot) or
                    current["locked_by_task_id"] or current["locked_by_agent_id"] or
                    current["job_pin_count"]):
                return False
            live_dirs = (await conn.execute(select(t.sessions.c.work_dir).where(
                session_attached_clause(),
            ))).scalars()
            if any(work_dir and Path(work_dir).resolve() == path.resolve()
                   for work_dir in live_dirs):
                return False
            if await conn.scalar(select(exists().where(
                t.job_workspace_pins.c.workspace_id == slot["id"],
            ))) or await conn.scalar(select(exists().where(
                t.integration_branch_owners.c.workspace_id == slot["id"],
                t.integration_branch_owners.c.handoff_state != "released",
            ))):
                return False
            base = await conn.scalar(select(t.workspaces.c.workspace_path).where(
                t.workspaces.c.id == slot["base_workspace_id"],
            ))
            if not base or Path(await self.git.aworktree_base_path(str(path)) or "") != Path(base):
                return False
            attached = await self.git.aworktree_list(base)
            entry = next((row for row in attached if
                          Path(row["path"]).resolve() == path.resolve()), None)
            if entry is None:
                return False
            try:
                old = manager.read_sentinel(path)
            except (TypeError, ValueError):
                old = None
            actual_branch = (entry.get("branch") or "").removeprefix("refs/heads/") or None
            sentinel = WorktreeSentinel(
                slot=path.name, slot_index=slot["slot_index"],
                base_workspace_id=slot["base_workspace_id"], project_id=slot["project_id"],
                workspace_id=slot["id"], branch=actual_branch,
                created_at=old.created_at if old else slot["created_at"],
                setup_hash=old.setup_hash if old else "",
                daemon_epoch=old.daemon_epoch if old else "",
            )
            if old and old.to_dict() == sentinel.to_dict():
                return False
            # No reset, checkout, merge abort or index deletion: all user work
            # remains in place. The row lock excludes a concurrent acquisition.
            manager.write_sentinel(path, sentinel)
            await self.db.log_event(
                "integration.git_truth_sentinel_repaired", project_id=slot["project_id"],
                payload=json.dumps({"workspace_id": slot["id"], "branch": actual_branch,
                                    "observed_at": now}), conn=conn,
            )
            return True
