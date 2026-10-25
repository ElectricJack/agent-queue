"""Authoritative resource observations for READY, unclaimed tasks.

Identities bind a typed constraint to its task, route, repository and current
configuration. Occupancy and time are observations, never cached verdicts.
"""

from dataclasses import asdict, dataclass, replace
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import time
from types import SimpleNamespace

from sqlalchemy import func, select

from src.database.tables import (
    continuations, integration_batch_members, integration_batches, integration_branch_owners,
    integration_repair_stages, projects, sessions, tasks, token_ledger, wait_records, workspaces,
)
from src.waits.conditions import Condition, NodeKey, Observation, Snapshot
from src.waits.graph import graph_locked

_prepared = ContextVar("wait_admission_observation", default=None)


def admission_pass(reader_path):
    """Observe service inputs before a command takes any graph/owner locks."""
    def decorate(method):
        @wraps(method)
        async def run(self, *args, **kwargs):
            reader = self
            for name in reader_path.split("."):
                reader = getattr(reader, name, None)
            if not isinstance(reader, AdmissionReader) or _prepared.get() is not None:
                return await method(self, *args, **kwargs)
            request = args[0] if args and isinstance(args[0], dict) else {}
            async with reader.prepared(project_id=request.get("project_id")):
                return await method(self, *args, **kwargs)
        return run
    return decorate


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False,
    ).encode()).hexdigest()


def branch_holders():
    """The three durable forms of a retained branch writer."""
    return select(integration_branch_owners.c.owner_id.label("task_id")).where(
        integration_branch_owners.c.handoff_state != "released",
    ).union(
        select(integration_repair_stages.c.repair_task_id.label("task_id")).where(
            integration_repair_stages.c.state.in_(("active", "awaiting_completion")),
        ),
        select(integration_batch_members.c.task_id).join(
            integration_batches, integration_batches.c.id == integration_batch_members.c.batch_id,
        ).where(integration_batches.c.lifecycle.not_in(("delivered", "cancelled"))),
    )


def reservation_scope(task, project):
    return {
        "profile_id": task["profile_id"], "intelligence_class": task["intelligence_class"],
        "route_source": task["route_source"], "repository_id": project["integration_repository_id"],
        "claim_epoch": task["claim_epoch"],
    }


@dataclass(frozen=True)
class AdmissionObservation:
    task: dict
    binding: dict
    constraints: dict[str, dict]
    holders: tuple[str, ...]
    owns_branch: bool

    def identity(self, blocker):
        return digest({**self.binding, "blocker": blocker})

    def snapshot(self, blocker):
        value = self.constraints[blocker]
        identity = self.identity(blocker)
        revision = digest({"binding": self.binding, "constraint": value})
        facts = {
            "blocker": blocker, "identity": identity, "owner_kind": "scheduler",
            "owner_id": self.task["id"], "constraint": value,
        }
        return Snapshot(
            self.task["project_id"], 0,
            Observation(revision, "unsatisfied" if value["blocked"] else "satisfied",
                        f"admission:{identity}:{revision}", facts),
            f"resource:{blocker}",
            tuple(NodeKey(f"task:{tid}", self.task["project_id"])
                  for tid in self.holders) if blocker == "branch_owner" else (),
        )

    def conditions(self):
        return [Condition("resource", {
            "blocker": blocker, "identity": self.identity(blocker),
            "source_revision": self.snapshot(blocker).observation.revision,
        }, 0) for blocker, value in self.constraints.items() if value["blocked"]]


class AdmissionReader:
    def __init__(self, orchestrator, *, clock=time.time):
        self.orchestrator = orchestrator
        self.clock = clock

    @asynccontextmanager
    async def prepared(self, *, project_id=None, task_ids=None, cached_only=True):
        """Capture external evidence before locks; resumes request one fresh task view."""
        from src.integration.admission import observe_admission
        from src.integration.development import DevelopmentPrimitives
        from src.routing.readiness import orchestrator_ready_projects

        o = self.orchestrator
        ready = await orchestrator_ready_projects(o)
        async with o.db._engine.connect() as conn:
            query = select(tasks.c.id, tasks.c.project_id).where(
                tasks.c.status.in_(("READY", "DEFINED", "PAUSED", "RESUME_PENDING")),
            )
            if project_id is not None:
                query = query.where(tasks.c.project_id == project_id)
            if task_ids is not None:
                query = query.where(tasks.c.id.in_(task_ids))
            candidates = (await conn.execute(query)).all()
        service = getattr(o, "development_integration", None) or DevelopmentPrimitives(
            o.db, data_dir=o.config.data_dir, git=getattr(o, "git", None),
        )
        admission = await observe_admission(
            o.db, [r.id for r in candidates], service, cached_only=cached_only,
            default_mode=o.config.integration.default_mode, router_ready_projects=ready,
        )
        token = _prepared.set((self, admission, ready))
        try:
            yield
        finally:
            _prepared.reset(token)

    async def observe_task(self, conn, task_id, *, continuation_id=None, migration_audit=False,
                           blocker=None):
        """Read current inputs for admission, or just one subscribed constraint.

        A resource verification is not an admission grant. Re-reading unrelated
        constraints for each subscription multiplies expensive frontier queries
        while holding the graph fence. Never cache a verdict across observations.
        """
        from src.database.queries.claim_queries import (
            _pool_profiles_predicate,
            claim_frontier_predicates,
            route_claimable,
        )
        from src.database.queries.hierarchy_queries import ProjectIntegrationMode

        prepared = _prepared.get()
        if prepared is None or prepared[0] is not self:
            if graph_locked(conn):
                from src.waits.conditions import WaitError
                raise WaitError("wait.source_unresolved", "admission observation must precede graph lock")
            async with self.prepared():
                return await self.observe_task(conn, task_id, continuation_id=continuation_id,
                                               migration_audit=migration_audit, blocker=blocker)
        _, admission, ready_projects = prepared
        o = self.orchestrator
        config = o.config
        config_revision = digest(asdict(config))
        task = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
        expected_statuses = ({"RESUME_PENDING"} if continuation_id else
                             {"READY", "DEFINED", "PAUSED"} if migration_audit else {"READY"})
        if not task or task["status"] not in expected_statuses or task["assigned_agent_id"] is not None:
            return None
        if continuation_id:
            intent = (await conn.execute(select(continuations).where(
                continuations.c.id == continuation_id, continuations.c.task_id == task_id,
                continuations.c.resume_state == 'queued', continuations.c.hold_reason.is_(None),
            ))).mappings().first()
            if not intent:
                return None
            if intent['stopped_at'] is None:
                from src.wait_runtime.legacy_parking import legacy_parked

                wait = (await conn.execute(select(wait_records).where(
                    wait_records.c.id == intent['wait_id'],
                ))).mappings().one()
                if not await legacy_parked(conn, task, wait, intent):
                    return None
        task = dict(task)
        project = (await conn.execute(select(projects).where(
            projects.c.id == task["project_id"],
        ))).mappings().one()
        profile = await o.db.get_profile(task["profile_id"], conn=conn) if task["profile_id"] else None
        ready = task["project_id"] in ready_projects
        # Prepared stack views check cache expiry without Git I/O under the lock.
        project_model = await o.db.get_project(task["project_id"], conn=conn)
        batch_names = {"sibling_prerequisite_not_delivered", "prerequisite_not_on_default_branch",
                       "epic_refresh_pending", "origin_not_materialized", "retired_repair_delegate"}
        checks = {}
        if blocker is None or blocker in {"policy_hold", "batch_hold", "workspace", "backoff"}:
            mode = admission.hierarchy_modes.get(task["project_id"], ProjectIntegrationMode.of(project_model))
            _, stack = admission.hierarchy_evidence(task["project_id"])
            # A fresh writer pass may supply a network-capable StackView.
            # Under the graph fence, only its bounded ref cache may be read.
            if stack is not None and not await replace(
                stack, cached_only=True, fresh_max_age=stack.fresh_max_age or 2,
            ).fresh():
                mode = replace(mode, stackable_prerequisite_ids=frozenset())
            predicates = claim_frontier_predicates(hierarchy_mode=mode)
            predicates["route"] = route_claimable(ready)
            if profile:
                predicates["compatible_profile"] = _pool_profiles_predicate({profile.id: profile})
            if blocker == "batch_hold":
                predicates = {key: value for key, value in predicates.items() if key in batch_names}
            elif blocker == "policy_hold":
                predicates = {key: value for key, value in predicates.items()
                              if key not in batch_names | {"claim_prepare_backoff", "workspace_requirement"}}
            elif blocker in {"workspace", "backoff"}:
                key = "workspace_requirement" if blocker == "workspace" else "claim_prepare_backoff"
                predicates = {key: predicates[key]}
            checks = dict((await conn.execute(select(*(
                expr.label(name) for name, expr in predicates.items()
            )).where(tasks.c.id == task_id))).mappings().one())
        from src.pool_claims import idle_pool_claim_loop_stalled, pool_claim_loop_stall_seconds
        from src.waits.occupancy import continuation_occupancy

        idle, pending = [], []
        fleet_count = project_count = 0
        slot = False
        if blocker is None or blocker in {"slot", "workspace"}:
            resuming, reserved = await continuation_occupancy(o.db, config, conn=conn)
            live = [dict(row) for row in (await conn.execute(select(sessions).where(
                (sessions.c.lifecycle == "pool") | sessions.c.id.in_(resuming),
                sessions.c.state.in_(("starting", "running", "draining")),
            ))).mappings()]
            current = [s for s in live if s["project_id"] == task["project_id"]]
            idle = [s for s in current if s["profile_id"] == task["profile_id"]
                    and s['desired_state'] == 'running'
                    and s["state"] == "running" and not s["task_id"] and not s["claim_phase"]
                    and not idle_pool_claim_loop_stalled(SimpleNamespace(**s), now=self.clock(),
                        stall_seconds=pool_claim_loop_stall_seconds(config.swarm))]
            # Resumes start a new task session; an idle pool process cannot be
            # reused as that fresh process and still occupies its configured seat.
            if continuation_id:
                idle = []
            compatible = [s for s in live if s["profile_id"] == task["profile_id"]]
            pending = [launch for launch in getattr(o, "_pool_launches", {}).values()
                       if launch.session_id not in {s["id"] for s in live}]
            fleet_count = len(live) + len(pending) + sum(reserved.values())
            project_count = len(current) + sum(launch.project_id == task["project_id"] for launch in pending)
            project_count += sum(n for (pid, _), n in reserved.items() if pid == task["project_id"])
            profile_count = len(compatible) + sum(launch.profile_id == task["profile_id"] for launch in pending)
            profile_count += sum(n for (_, pid), n in reserved.items() if pid == task["profile_id"])
            cap = config.swarm.global_max_active
            if cap is None:
                cap = config.resources.max_concurrent_agents
            profile_cap = profile.max_active if profile else 0
            slot = not idle and (cap is not None and fleet_count >= cap
                                or project_count >= project["max_concurrent_agents"]
                                or profile_cap is not None and profile_count >= profile_cap)
        capacity = 0
        if blocker is None or blocker == "workspace":
            worktrees = o._worktrees_enabled()
            capacity = await o.db.count_available_workspaces(
                task["project_id"], kind_id="project-repo",
                worktree_slot_cap=o._project_slot_cap(project_model) if worktrees else None,
                workspace_snapshot=[o.db._row_to_workspace(r) for r in (await conn.execute(
                    select(workspaces).where(workspaces.c.project_id == task["project_id"],
                        func.coalesce(workspaces.c.kind_id, "project-repo") == "project-repo"),
                )).mappings()],
                kind_snapshot={"project-repo": await o.db.resolve_workspace_kind(
                    task["project_id"], "project-repo", conn=conn)},
            )
            capacity = max(0, capacity - sum(
                launch.project_id == task["project_id"] and not launch.workspace_acquired
                for launch in pending
            ))
        availability = getattr(o, "provider_availability", None)
        provider = availability.provider_for_profile(
            profile, project_id=task["project_id"],
        ) if availability and profile else None
        provider_blocked = bool(availability and availability.suppresses(provider))
        quarantine, _ = o._pool_quarantine_state(task["project_id"], task["profile_id"], self.clock())
        usage = {}
        global_budget = config.global_token_budget_daily
        if (blocker is None or blocker == "budget") and (
                global_budget is not None or project["budget_limit"] is not None):
            since = self.clock() - config.scheduling.rolling_window_hours * 3600
            usage = dict((await conn.execute(select(
                token_ledger.c.project_id, func.sum(token_ledger.c.tokens_used),
            ).where(token_ledger.c.timestamp >= since).group_by(token_ledger.c.project_id))).all())
        budget = (global_budget is not None and sum(usage.values()) >= global_budget
                  or project["budget_limit"] is not None
                  and usage.get(task["project_id"], 0) >= project["budget_limit"])
        owners = (await conn.execute(select(integration_branch_owners).where(
            integration_branch_owners.c.repository_id == project["integration_repository_id"],
            integration_branch_owners.c.ref == task["branch_name"],
            integration_branch_owners.c.handoff_state != "released",
            integration_branch_owners.c.owner_id != task_id,
        ))).mappings().all() if task["branch_name"] and blocker in {None, "branch_owner"} else []
        holders = tuple((await conn.execute(select(tasks.c.id).where(
            tasks.c.id.in_([r["owner_id"] for r in owners]), tasks.c.project_id == task["project_id"],
        ).order_by(tasks.c.id))).scalars()) if owners else ()
        owns = bool(await conn.scalar(select(tasks.c.id).where(
            tasks.c.id == task_id, tasks.c.id.in_(select(branch_holders().subquery().c.task_id)),
        ))) if blocker in {None, "slot"} else False
        batch_reasons = []
        if blocker in {None, "batch_hold"} and (task_id not in admission.allowed
                or not await admission.matches(conn=conn, task_id=task_id)):
            batch_reasons.append("delivery_not_verified")
        batch_reasons.extend(name for name in batch_names if checks.get(name) is False)
        policy_reasons = [name for name, ok in checks.items() if not ok and name not in batch_names
                          and name not in {"claim_prepare_backoff", "workspace_requirement"}]
        if not continuation_id and task["status"] != "READY":
            # Historical audit rows describe holds; they never admit paused or
            # undefined work. The ordinary claim path still requires READY.
            policy_reasons.append(f"status:{task['status']}")
        if not profile or not profile.enabled or project["status"] != "ACTIVE":
            policy_reasons.append("disabled_project_or_profile")
        constraints = {
            "slot": {"blocked": bool(slot or owns and not continuation_id),
                     "awaiting_branch_admission": owns and not continuation_id,
                     "fleet": fleet_count, "project": project_count, "idle": len(idle)},
            "workspace": {"blocked": not idle and capacity <= 0
                          or not checks.get("workspace_requirement", True), "capacity": capacity},
            "provider": {"blocked": provider_blocked, "provider": provider},
            "backoff": {"blocked": not checks.get("claim_prepare_backoff", True)
                        or quarantine is not None, "quarantine_until": quarantine},
            "budget": {"blocked": bool(budget), "usage": usage.get(task["project_id"], 0),
                       "fleet_usage": sum(usage.values())},
            "policy_hold": {"blocked": bool(policy_reasons), "reasons": sorted(policy_reasons)},
            "branch_owner": {"blocked": bool(owners), "holders": list(holders)},
            "batch_hold": {"blocked": bool(batch_reasons), "reasons": sorted(batch_reasons)},
        }
        if blocker is not None:
            constraints = {blocker: constraints[blocker]}
        binding = {
            "project_id": task["project_id"], "task_id": task_id,
            "scope": reservation_scope(task, project), "branch": task["branch_name"],
            "config_revision": config_revision, "profile_revision": digest(asdict(profile)) if profile else None,
            "router_ready": ready, "provider": provider,
            "owner_kind": "scheduler", "owner_id": task_id,
            "session_id": None, "claim_epoch": task["claim_epoch"],
            "project_revision": digest({key: project[key] for key in (
                "status", "integration_repository_id", "hierarchical_integration_mode",
                "hierarchical_integration_policy", "max_concurrent_agents", "budget_limit",
                "preferred_provider", "assignment_playbook_id", "repo_default_branch",
            )}),
        }
        if config_revision != digest(asdict(o.config)):
            return None
        return AdmissionObservation(task, binding, constraints, holders, owns)

    async def __call__(self, conn, condition):
        context = condition.context
        if context.get("owner_kind") != "scheduler" or context.get("session_id") is not None:
            return None
        # Resource snapshots describe constraints, never grant admission. This
        # path can re-observe migration projections of held tasks; direct
        # admission callers retain the default READY-only observation.
        blocker = condition.parameters["blocker"]
        observed = await self.observe_task(conn, context.get("owner_id"), migration_audit=True,
                                           blocker=blocker)
        if (not observed or observed.task["project_id"] != context.get("project_id")
                or observed.task["claim_epoch"] != context.get("claim_epoch")
                or observed.identity(blocker) != condition.parameters["identity"]):
            return None
        return observed.snapshot(blocker)
