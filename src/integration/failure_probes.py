"""Bounded, durable diagnostics for an exact red train candidate.

Only the train-tick command calls this mechanism. Plans live in the owner's
fixer_queue; probe refs have no batch row, target ownership or publication path.
A singleton failure may use the normal ejection control, which creates an
unbuilt survivor. Every other outcome retains the original candidate for repair.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

from sqlalchemy import select, update

from src.database.tables import integration_batches, task_dependencies, tasks
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError
from src.integration.batches import Batch, candidate_ref
from src.integration.candidate_baseline import candidate_head, target_head
from src.integration.checks import ChecksState, Conclusion, HybridChecks, evaluate
from src.integration.signatures import SignatureIdentity, read_signature_documents

KEY = "failure_probes"
VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verdict(result, head, required):
    """Missing, flaky, infrastructure and mismatched evidence never imply blame."""
    if (result.required != required or result.sha != head.sha
            or result.repository_id != head.repository_id
            or len(result.checks) != len(required.names)
            or {c.name for c in result.checks} != set(required.names)
            or any((c.sha, c.repository_id, c.producer_id, c.required_check_version) != (
                head.sha, head.repository_id, required.producer_id, required.version)
                for c in result.checks)
            or evaluate(required, head, result.checks, now=0).state != result.state):
        return "unavailable"
    if any(c.conclusion not in {Conclusion.SUCCESS, Conclusion.FAILURE, Conclusion.PENDING}
           or (c.conclusion == Conclusion.FAILURE and c.classification != "conclusive")
           for c in result.checks):
        return "unavailable"
    # A failed job alongside unfinished jobs is not a complete product verdict.
    if any(c.conclusion == Conclusion.PENDING for c in result.checks):
        return "pending"
    return "pass" if result.green else "fail" if result.state == ChecksState.RED else "unavailable"


def plan(identity, units, risks, limit):
    """Rank connected dependency units, then split the ordered units in half."""
    units = sorted(units, key=lambda unit: (
        *tuple(-max(risks[tid][i] for tid in unit) for i in range(3)), min(unit)))
    cut = (len(units) + 1) // 2
    groups = [[tid for unit in half for tid in unit] for half in (units[:cut], units[cut:])]
    state = {"version": VERSION, "incident": digest(identity), "identity": identity,
             "generation": 0, "limit": min(3, limit), "units": units,
             "risks": risks, "probes": [], "decisions": [], "suspects": [],
             "queue": groups, "status": "probing"}
    if len(units) < 2 or not all(groups):
        fallback(state, "empty_survivors")
    return state


def fallback(state, reason):
    state.update(status="repair", queue=[])
    state["decisions"].append({"reason": reason})


def advance(state):
    """Consume recorded outcomes only; duplicates cannot spend another probe."""
    probes = state["probes"]
    if not probes or probes[-1]["status"] in {"reserved", "testing"}:
        return
    if probes[-1]["status"] not in {"pass", "fail"}:
        fallback(state, "probe_unavailable")
        return
    state["suspects"] = [p["members"] for p in probes if p["status"] == "fail"]
    if len(probes) >= 2:
        first, second = probes[:2]
        outcomes = (first["status"], second["status"])
        if outcomes == ("pass", "pass"):
            fallback(state, "interaction")
        elif outcomes == ("fail", "fail"):
            fallback(state, "both_halves_fail")
        else:
            failed = next(p for p in reversed(probes) if p["status"] == "fail")
            if len(failed["members"]) == 1:
                state.update(status="isolated", queue=[], suspect=failed["members"][0])
                state["decisions"].append({"reason": "singleton_failure",
                                           "probe": failed["id"]})
            elif len(probes) == 2 and len(probes) < state["limit"]:
                units = [u for u in state["units"] if set(u) <= set(failed["members"])]
                if len(units) > 1:
                    state["queue"] = [[tid for u in units[:(len(units) + 1) // 2] for tid in u]]
                else:
                    fallback(state, "inseparable_dependencies")
            else:
                fallback(state, "budget_exhausted")
    if state["status"] == "probing" and len(probes) >= state["limit"]:
        fallback(state, "budget_exhausted")


async def dependency_edges(db, batch, members):
    """Read only prerequisites reachable from this frozen member window."""
    ids = {m.task_id for m in members}
    reachable = select(tasks.c.id).where(
        tasks.c.id.in_(ids), tasks.c.project_id == batch.project_id,
    ).cte("probe_prerequisites", recursive=True)
    reachable = reachable.union(select(task_dependencies.c.depends_on_task_id)
        .join(reachable, reachable.c.id == task_dependencies.c.task_id)
        .join(tasks, tasks.c.id == task_dependencies.c.depends_on_task_id)
        .where(tasks.c.project_id == batch.project_id, task_dependencies.c.dep_type == "blocks"))
    async with db._engine.connect() as conn:
        return sorted(tuple(row) for row in (await conn.execute(select(
            task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id,
        ).where(task_dependencies.c.task_id.in_(select(reachable.c.id)),
                task_dependencies.c.dep_type == "blocks"))).all())


async def dependency_units(members, edges, gitops, repo):
    """Connected closures include intervening tasks and stacked Git sources."""
    ids = {m.task_id for m in members}
    direct, neighbors = {}, {tid: set() for tid in ids}
    for tid, need in edges:
        direct.setdefault(tid, set()).add(need)
    for tid in ids:
        seen, pending = set(), list(direct.get(tid, ()))
        while pending:
            need = pending.pop()
            if need not in seen:
                seen.add(need)
                pending.extend(direct.get(need, ()))
        for need in seen & ids:
            neighbors[tid].add(need)
            neighbors[need].add(tid)
    for source in members:
        for other in members:
            if source != other and await gitops.is_ancestor(repo, source.source_sha, other.source_sha):
                neighbors[source.task_id].add(other.task_id)
                neighbors[other.task_id].add(source.task_id)
    units = []
    while neighbors:
        unit, pending = set(), [min(neighbors)]
        while pending:
            tid = pending.pop()
            if tid not in unit:
                unit.add(tid)
                pending.extend(neighbors.pop(tid, ()))
        units.append(sorted(unit))
    return units


class FailureProbes:
    def __init__(self, db, *, clock):
        self.db, self.clock = db, clock

    async def save(self, batch, before, state):
        """CAS this namespace without replacing repair/parallel-candidate state."""
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).with_for_update())).mappings().one_or_none()
            if (not row or Batch.from_row(row) != batch or row["intent"] != "open"
                    or row["lifecycle"] == "promoted" or row["fixer_state"]):
                return False
            queue = dict(row["fixer_queue"] or {})
            if queue.get(KEY) != before:
                return False
            state["generation"] = (before or {}).get("generation", 0) + 1
            queue[KEY] = state
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).values(fixer_queue=queue, updated_at=self.clock()))
            return True

    async def run(self, target, batch, members, observation, result, checks, lane):
        from src.integration.train_policy import load_train_policy
        from src.integration.train_packing_sources import history_on
        from src.playbooks.train_policy import TrainPackingPolicy

        view = await load_train_policy(self.db, batch.project_id)
        policy = view.policy.bisection if view.policy else None
        if (not view.enabled or not policy or not policy.enabled
                or view.policy.action("red") != "repair"
                or target.kind not in {"root", "development"}
                or batch.repair_attempt_count or batch.epic_sync or batch.epic_refresh
                or len(members) < 2 or isinstance(checks, HybridChecks)
                or observation.candidate_sha != result.sha or not observation.target_sha):
            return None
        required = result.required
        baseline_head = target_head(batch, observation.target_sha)
        if (verdict(result, candidate_head(batch, result.sha), required) != "fail"
                or verdict(await checks.read(baseline_head), baseline_head, required) != "pass"):
            return None
        row = await self.db.get_integration_batch(batch.id)
        if not row or row["fixer_state"] or (row["fixer_queue"] or {}).get("status"):
            return None
        document = row["artifact_snapshot"] or {}
        try:
            identities = tuple(SignatureIdentity(**r["identity"])
                               for r in document["train_signatures"]["sources"])
            signatures = read_signature_documents(document, document, current_identities=identities)
            if (not signatures or len(signatures) != len(members)
                    or document["train_packing"]["policy_revision"] != view.policy_revision):
                return None
            for sig, member in zip(signatures, members, strict=True):
                ident = sig.identity
                if (sig.universal or (ident.project_id, ident.repository_id, ident.task_id,
                    ident.source_head, ident.merge_base, ident.target_sha, ident.target_ref,
                    ident.policy_revision) != (batch.project_id, batch.repository_id, member.task_id,
                    member.source_sha, member.base_sha, observation.target_sha, batch.target_ref,
                    view.policy_revision)):
                    return None
        except (KeyError, ValueError, TypeError):
            return None
        edges = await dependency_edges(self.db, batch, members)
        identity = {"batch_id": batch.id, "revision": batch.repair_attempt_count,
                    "candidate_sha": result.sha, "target_sha": observation.target_sha,
                    "sources": {m.task_id: m.source_sha for m in members},
                    "source_bases": {m.task_id: m.base_sha for m in members},
                    "required": required.model_dump(mode="json"),
                    "policy_revision": view.policy_revision,
                    "signature_digest": document["train_signatures"]["digest"],
                    "dependencies": [list(edge) for edge in edges]}
        service = lane.service
        repo = await service.gitops.repository(batch)
        state = (row["fixer_queue"] or {}).get(KEY)
        if state is None:
            units = await dependency_units(members, edges, service.gitops, repo)
            async with self.db._engine.connect() as conn:
                hot, _, _ = await history_on(conn, target, view.policy.packing or TrainPackingPolicy())
            failed_targets = {t for check in result.checks for t in
                              check.detail.get("target_failures", {}).get("targets", [])
                              if isinstance(t, str)}
            risks = {sig.identity.task_id: [
                len(set(sig.paths) | set(sig.owning_targets) | set(sig.reverse_test_targets)),
                len(set(sig.paths) & hot),
                len(set((*sig.owning_targets, *sig.reverse_test_targets)) & failed_targets),
            ] for sig in signatures}
            state = plan(identity, units, risks, policy.max_probes)
            if not await self.save(batch, None, state):
                return {"status": "waiting", "reason": "probe_state_changed"}
        elif (state.get("version") != VERSION or state.get("identity") != identity
              or state.get("incident") != digest(identity)):
            return None  # Never reset the budget on unverifiable retained state.

        async def authorized():
            current = await self.db.get_integration_batch(batch.id)
            return bool(current and Batch.from_row(current) == batch
                and not current["fixer_state"]
                and (current["fixer_queue"] or {}).get(KEY) == state
                and await load_train_policy(self.db, batch.project_id) == view
                and await dependency_edges(self.db, batch, members) == edges
                and await service.repair_authorized(batch, members, result.sha)
                and await service.gitops.remote(repo, batch.target_ref) == observation.target_sha
                and verdict(await checks.read(baseline_head), baseline_head, required) == "pass"
                and verdict(await checks.read(candidate_head(batch, result.sha)),
                            candidate_head(batch, result.sha), required) == "fail")

        if state["status"] == "repair":
            return {"status": "repair", "probe_state": state}
        if not await authorized():
            return None
        if state["status"] == "isolated":
            return await self.eject(batch, members, result, checks, lane, state, authorized)
        before = deepcopy(state)
        if not state["probes"] or state["probes"][-1]["status"] in {"pass", "fail"}:
            if len(state["probes"]) >= state["limit"] or not state["queue"]:
                fallback(state, "budget_exhausted")
            else:
                group = state["queue"].pop(0)
                probe_id = "train-probe-" + digest([state["incident"], group])[:32]
                state["probes"].append({"id": probe_id, "members": group, "status": "reserved"})
            if not await self.save(batch, before, state):
                return {"status": "waiting", "reason": "probe_state_changed"}
        if state["status"] == "repair":
            return {"status": "repair", "probe_state": state}
        probe = state["probes"][-1]
        probe_batch = replace(batch, id=probe["id"])
        ref = candidate_ref(probe_batch.id)
        try:
            if not probe.get("sha"):
                selected = tuple(replace(m, order=i) for i, m in enumerate(
                    m for m in members if m.task_id in probe["members"]))
                merged = await service.gitops.merge_sources(
                    repo, observation.target_sha, selected, created_at=batch.created_at)
                before = deepcopy(state)
                if merged["outcome"] != "merged":
                    fallback(state, "probe_assembly_unavailable")
                else:
                    probe.update(sha=merged["head"], status="testing")
                if not await self.save(batch, before, state):
                    return {"status": "waiting", "reason": "probe_state_changed"}
                if state["status"] == "repair":
                    return {"status": "repair", "probe_state": state}
            remote = await service.gitops.remote(repo, ref)
            if remote is None:
                await service.publish(repo, ref, expected_old_oid="0" * 40,
                                      new_oid=probe["sha"], authorize=authorized)
                remote = await service.gitops.remote(repo, ref)
            if remote != probe["sha"] or not await authorized():
                raise ValueError("probe identity changed")
            exact = await lane.checks.for_candidate(probe_batch, probe["sha"])
            if exact is None or isinstance(exact, HybridChecks):
                raise ValueError("probe checks unavailable")
            head = candidate_head(probe_batch, probe["sha"])
            await exact.request(head)  # The existing exact-head request path is idempotent.
            checked = await exact.refresh(head)
            outcome = verdict(checked, head, required)
            if outcome == "pending":
                return {"status": "probing", "probe_state": state}
            if not await authorized():
                return None
            before = deepcopy(state)
            probe.update(status=outcome, evidence=checked.model_dump(mode="json"))
            advance(state)
        except (GitError, GitHubAccessError, OSError, ValueError):
            before = deepcopy(state)
            fallback(state, "probe_infrastructure")
        if not await self.save(batch, before, state):
            return {"status": "waiting", "reason": "probe_state_changed"}
        if state["status"] == "isolated":
            return await self.eject(batch, members, result, checks, lane, state, authorized)
        return {"status": state["status"], "probe_state": state}

    async def eject(self, batch, members, result, checks, lane, state, authorized):
        from src.integration.train_controls import TrainControls

        async def verify():
            if not await authorized():
                return False
            for probe in state["probes"]:
                if probe["status"] not in {"pass", "fail"}:
                    return False
                probe_batch = replace(batch, id=probe["id"])
                exact = await lane.checks.for_candidate(probe_batch, probe["sha"])
                if exact is None or isinstance(exact, HybridChecks):
                    return False
                head = candidate_head(probe_batch, probe["sha"])
                if verdict(await exact.read(head), head, result.required) != probe["status"]:
                    return False
            return True

        attribution = {**state["identity"], "task_id": state["suspect"],
                       "probe_incident": state["incident"], "probes": state["probes"]}
        reason = "empty_survivors"
        try:
            ejected = await TrainControls(self.db, clock=self.clock).eject(
                batch.id, state["suspect"], service=lane.service, dry_run=False,
                operator_id="service:integration-train", reason="isolated product failure probe",
                attribution=attribution, verify_failure=verify)
        except ValueError:
            ejected, reason = None, "evidence_changed"
        if ejected:
            return {"status": "ejected", "ejection": ejected}
        before = deepcopy(state)
        fallback(state, reason)
        await self.save(batch, before, state)
        return {"status": "repair", "probe_state": state}
