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


async def retire_obsolete_ci_waits(conn, *, now, batch_id=None, project_id=None, target=None):
    """Cancel obsolete service subscriptions without observing remote CI.

    Called under GRAPH_LOCK by the source listener or train reconciliation.
    A target can reuse the same head in a replacement batch; that subscription
    still belongs to current work and must survive.
    """
    from src.database.queries.wait_queries import cancel_wait
    from src.waits.graph import lock_graph

    await lock_graph(conn)
    query = select(batches.c.project_id, batches.c.repository_id, batches.c.target_ref).distinct()
    if batch_id is not None:
        query = query.where(batches.c.id == batch_id)
    if project_id is not None:
        query = query.where(batches.c.project_id == project_id)
    if target is not None:
        query = query.where(batches.c.project_id == target.project_id,
                            batches.c.repository_id == target.repository_id,
                            batches.c.target_ref == target.target_ref)
    retired = []
    for batch in (await conn.execute(query)).mappings():
        prefix = "train:" + digest((batch["project_id"], batch["repository_id"],
                                    batch["target_ref"])) + ":"
        active = batches.alias("active_ci_batch")
        # A replacement can reuse a head. Promoted candidates also retain
        # their source-owned successful settlement, even after departure.
        still_used = select(active.c.id).where(
            active.c.project_id == batch["project_id"],
            active.c.repository_id == batch["repository_id"],
            active.c.target_ref == batch["target_ref"],
            active.c.intent != "aborted",
            active.c.tested_candidate_sha == wait_records.c.condition["head_sha"].astext,
        ).exists()
        ids = list(await conn.scalars(select(wait_records.c.id).where(
            wait_records.c.project_id == batch["project_id"],
            wait_records.c.owner_kind == "integration", wait_records.c.session_id.is_(None),
            wait_records.c.owner_id.startswith(prefix, autoescape=True),
            ~wait_records.c.owner_id.startswith(prefix + "speculative:", autoescape=True),
            wait_records.c.outcome == "pending",
            wait_records.c.condition_kind.in_(("ci_check", "ci_run")),
            wait_records.c.condition["repository_id"].astext == batch["repository_id"],
            ~still_used,
        ).order_by(wait_records.c.id)))
        for wait_id in ids:
            await cancel_wait(conn, wait_id, project_id=batch["project_id"], now=now)
        retired.extend(ids)
    return retired


class DispatchWaits:
    def __init__(self, orch, *, lanes=None, clock=time.time):
        self.orch, self.db, self.lanes, self.clock = orch, orch.db, lanes, clock
        self._speculative = {}
        self._candidate_checks = {}
        if lanes is not None:
            watcher = CheckWatcher(self.db, self.commit, orch.git,
                checks=self.checks, repository=self.repository,
                clock=clock)
            orch.wait_ci_watcher = watcher
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
                batches.c.intent != "aborted",
            ).order_by(batches.c.created_at.desc()).limit(1))).mappings().first()
        if row is None:
            return self._speculative.get((repo["id"], cond.parameters["head_sha"]))
        target = TrainTarget(row["project_id"], row["repository_id"], row["target_ref"])
        batch = Batch.from_row(row)
        key = (batch.id, cond.parameters["head_sha"])
        if key in self._candidate_checks:
            exact = self._candidate_checks[key]
        else:
            lane = await self.lanes(target)
            resolved = getattr(lane.checks, "resolved", lambda *_: None)
            exact = resolved(batch, key[1])
            if exact is None:
                exact = await lane.checks.for_candidate(batch, key[1])
            self._remember_checks(key, exact)
        exact = exact.hosted if isinstance(exact, HybridChecks) else exact
        return None if exact is not None and isinstance(exact.provider, LocalChecks) else exact

    def _remember_checks(self, key, exact):
        # Observation hints only: publication still resolves current policy in
        # its own lane. Production constructs a fresh lane on every call.
        self._candidate_checks.pop(key, None)
        self._candidate_checks[key] = exact
        while len(self._candidate_checks) > 128:
            self._candidate_checks.pop(next(iter(self._candidate_checks)))

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

    async def sync(self, target, batch, visit, queued, exact, *, speculative=None):
        # Recovery of subscriptions predates activation gates: a rollout
        # rollback must not strand waits that were already registered.
        async with self.db._engine.begin() as conn:
            await retire_obsolete_ci_waits(conn, target=target, now=self.clock())
        if not enabled(self.orch.config, target.project_id):
            return {"wait_ids": []}
        if batch is not None and visit.candidate_sha:
            self._remember_checks((batch.id, visit.candidate_sha), exact)
        prefix = "train:" + digest(target.key) + ":"
        wanted = await self.conditions(target, batch, visit, queued, exact, prefix)
        # Disposable exact-SHA readers are observation hints, never publication
        # authority. Restart safely loses them and rebuilds the next batch.
        for key in list(self._speculative):
            if key[0] == target.repository_id:
                self._speculative.pop(key)
        if speculative and speculative.sha and speculative.exact:
            self._speculative[(target.repository_id, speculative.sha)] = speculative.exact
            verdict = await speculative.exact.read(
                candidate_head(speculative.prepared.batch, speculative.sha))
            for run_id in {check.run_id for check in verdict.checks if check.run_id and
                           check.conclusion not in {Conclusion.SUCCESS, Conclusion.FAILURE,
                                                    Conclusion.CANCELLED}}:
                condition = {"kind": "ci_run", "repository_id": target.repository_id,
                             "head_sha": speculative.sha, "run_id": run_id}
                wanted[prefix + "speculative:" + digest(condition)] = (
                    condition, speculative.exact.required.model_dump(mode="json"))
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
                hosted_removed = (row["condition_kind"] in {"ci_check", "ci_run"}
                    and exact is not None and (isinstance(exact.provider, LocalChecks)
                        if not isinstance(exact, HybridChecks) else exact.hosted is None))
                if service is not None and not hosted_removed:
                    try:
                        async with self.db._engine.connect() as conn:
                            observed = await service.registry.observe(
                                conn, Condition.from_record(row["condition"]))
                    except WaitError as exc:
                        # Observation failure cannot prove this subscription is
                        # obsolete. Preserve it for the listener and keep the
                        # train visit moving, including other owners' waits.
                        ids.append(row["id"])
                        errors.append({"wait_id": row["id"], **exc.as_dict()})
                        continue
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
