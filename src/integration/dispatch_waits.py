"""Bind train candidates and queued inputs to the shared verified wait service."""

import hashlib
import time

from sqlalchemy import func, or_, select

from src.database.tables import integration_batches as batches, wait_records
from src.integration.batches import Batch
from src.integration.checks import Conclusion, HybridChecks, LocalChecks
from src.integration.train import TrainTarget, candidate_head
from src.waits.conditions import Condition, WaitError, canonical
from src.waits.policy import enabled
from src.waits.watchers.ci import CheckWatcher


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class DispatchWaits:
    def __init__(self, orch, *, lanes=None, clock=time.time):
        self.orch, self.db, self.lanes, self.clock = orch, orch.db, lanes, clock
        if lanes is not None:
            watcher = CheckWatcher(self.db, self.commit, orch.git,
                checks=self.checks, repository=self.repository,
                clock=clock)
            orch.wait_ci_reader = watcher.read
            orch.wait_ci_lock_source = watcher.lock_source
            service = getattr(orch, "wait_service", None)
            if service is not None:
                for kind in ("ci_check", "ci_run"):
                    service.registry.adapter(kind).reader = watcher.read

    async def repository(self, repo):
        resolver = getattr(self.orch, "github_repository_binding_resolver", None)
        if resolver is None:
            raise WaitError("wait.source_unresolved", "repository watcher is unavailable")
        return await resolver(repo)

    async def commit(self, event):
        return await self.orch._command_handler.wait_service_command("wait_source_commit", event)

    async def checks(self, repo, cond):
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(batches).where(
                batches.c.repository_id == repo["id"],
                batches.c.project_id == repo["project_id"],
                batches.c.tested_candidate_sha == cond.parameters["head_sha"],
            ).order_by(batches.c.created_at.desc()).limit(1))).mappings().first()
        if row is None:
            return None
        target = TrainTarget(row["project_id"], row["repository_id"], row["target_ref"])
        lane = await self.lanes(target)
        exact = await lane.checks.for_candidate(Batch.from_row(row), cond.parameters["head_sha"])
        return exact.hosted if isinstance(exact, HybridChecks) else exact

    async def conditions(self, target, batch, visit, queued, exact, prefix):
        wanted = {}
        if batch is None:
            return wanted
        for member in queued:
            wanted[member.task_id] = ({"kind": "batch", "batch_id": batch.id},
                                       [member.source_sha, member.source_base_sha])
        if not visit.candidate_sha or exact is None or visit.state != "testing":
            return wanted
        runners = (exact.local, exact.hosted) if isinstance(exact, HybridChecks) else (exact,)
        for runner in runners:
            if runner is None:
                continue
            binding = runner.required.model_dump(mode="json")
            provider = runner.provider
            head = candidate_head(batch, visit.candidate_sha)
            if isinstance(provider, LocalChecks):
                states = await provider.producer.job_states(provider._owner(head), head)
                for _, job, state in states:
                    if job and state[0].value == "pending":
                        wanted[prefix + "job:" + job["id"]] = (
                            {"kind": "job", "job_id": job["id"]}, binding)
            else:
                result = await runner.read(head)
                for check in result.checks:
                    if check.conclusion not in {Conclusion.SUCCESS, Conclusion.FAILURE,
                                                Conclusion.MISSING, Conclusion.CANCELLED}:
                        wanted[prefix + "check:" + digest([visit.candidate_sha, check.name])] = (
                            {"kind": "ci_check", "repository_id": target.repository_id,
                             "head_sha": visit.candidate_sha, "check_name": check.name}, binding)
        return wanted

    async def sync(self, target, batch, visit, queued, exact):
        if not enabled(self.orch.config, target.project_id):
            return {"wait_ids": []}
        prefix = "train:" + digest(target.key) + ":"
        wanted = await self.conditions(target, batch, visit, queued, exact, prefix)
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(select(wait_records).where(
                wait_records.c.project_id == target.project_id,
                wait_records.c.owner_kind == "integration",
                wait_records.c.outcome == "pending",
                or_(wait_records.c.owner_id.startswith(prefix),
                    wait_records.c.condition["batch_id"].astext.in_(select(batches.c.id).where(
                        batches.c.project_id == target.project_id,
                        batches.c.repository_id == target.repository_id,
                        batches.c.target_ref == target.target_ref,
                    ))),
            ))).mappings().all()
        pending = {row["owner_id"]: row for row in rows}
        ids, errors = [], []
        command = self.orch._command_handler.wait_service_command
        for owner, row in pending.items():
            desired = wanted.get(owner)
            if desired and row["idempotency_key"].startswith(digest(desired) + ":"):
                ids.append(row["id"])
                wanted.pop(owner)
                continue
            if desired is None:
                # A committed source result owns normal settlement. Preserve it
                # for the durable listener rather than replacing it with cancel.
                service = getattr(self.orch, "wait_service", None)
                if service is not None:
                    async with self.db._engine.connect() as conn:
                        observed = await service.registry.observe(
                            conn, Condition.from_record(row["condition"]))
                    if observed.state == "satisfied":
                        continue
            result = await command("wait_cancel", {
                "schema_version": 2, "wait_id": row["id"], "project_id": target.project_id,
                "owner_kind": "integration", "owner_id": owner,
            })
            if not result.get("success"):
                errors.append(result)
                wanted.pop(owner, None)
        async with self.db._engine.connect() as conn:
            counts = dict((await conn.execute(select(
                wait_records.c.owner_id, func.count(),
            ).where(wait_records.c.project_id == target.project_id,
                    wait_records.c.owner_kind == "integration",
                    wait_records.c.owner_id.in_(wanted)).group_by(wait_records.c.owner_id))).all())
        for owner, desired in wanted.items():
            result = await command("wait_register", {
                "schema_version": 2, "project_id": target.project_id,
                "owner_kind": "integration", "owner_id": owner,
                "condition": desired[0],
                "idempotency_key": digest(desired) + ":" + str(counts.get(owner, 0)),
            })
            if result.get("success"):
                ids.append(result["wait_id"])
            elif result.get("error_code") != "wait.already_satisfied":
                errors.append(result)
        return {"wait_ids": ids, **({"wait_errors": errors} if errors else {})}
