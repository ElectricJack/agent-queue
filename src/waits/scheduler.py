"""Guarded scheduler wait reconciliation and restart-safe branch admission deadlines.

The listener owns deadline discovery. It dispatches reservation and scheduler
escalation deadlines to this handler through wait_deadline_fire. No task/session
is woken here and no activation configuration is written.
"""

import logging
import time
import uuid

from sqlalchemy import exists, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.queries.wait_queries import register_wait
from src.database.tables import (
    admission_reservations, tasks, wait_deadlines, wait_escalations, wait_records,
)
from src.waits.admission import AdmissionReader, branch_holders
from src.waits.conditions import Condition, NodeKey, Scope, SleepDecision, WaitError, canonical
from src.waits.graph import add_edges, clear_edges, lock_graph, recompute_from
from src.waits.registry import build_registry

logger = logging.getLogger(__name__)


def enabled(config):
    policy = getattr(config, "wait_policy", None)
    return (getattr(policy, "v2_enabled", False) is True
            and getattr(policy, "scheduler_waits_enabled", False) is True)


class SchedulerPolicy:
    in_place_ceiling_seconds = 1800

    def escalation_seconds(self, kind):
        return None  # Only branch-holder reservations have scheduler deadlines.


async def close_row(conn, row, *, now, outcome="cancelled", result_ref=None):
    await conn.execute(update(wait_records).where(wait_records.c.id == row["id"]).values(
        outcome=outcome, resolved_at=now, cleared_at=now, blocking=False,
        result_ref=result_ref, version=row["version"] + 1,
    ))
    await conn.execute(update(wait_deadlines).where(
        wait_deadlines.c.wait_id == row["id"], wait_deadlines.c.fired_at.is_(None),
    ).values(cancelled_at=now))
    return await clear_edges(conn, row["id"], now=now)


class SchedulerWaitService:
    def __init__(self, orchestrator, *, reader=None, clock=time.time):
        self.orchestrator = orchestrator
        self.db = orchestrator.db
        self.reader = reader or AdmissionReader(orchestrator, clock=clock)
        self.registry = build_registry(resource_reader=self.reader)
        self.clock = clock

    def authorize(self, principal):
        if principal is None or principal.kind != PrincipalKind.SERVICE:
            raise WaitError("wait.out_of_scope", "scheduler waits require a service principal")
        if not enabled(self.orchestrator.config):
            raise WaitError("wait.disabled", "scheduler waits are disabled")

    async def sync(self, args, *, principal, scope=None, handler=None):
        self.authorize(principal)
        now = self.clock() if args.get("now") is None else args["now"]
        project_id = args["project_id"]
        # The contract carries a candidate list, never authoritative verdicts.
        # A fresh project scan also retracts stale rows after a missed cycle.
        watermark = await self.db.wait_source_watermark()
        changed = set()
        async with self.db._engine.begin() as conn:
            await lock_graph(conn)
            candidates = (await conn.execute(select(tasks).where(
                tasks.c.project_id == project_id,
                or_(tasks.c.status == "READY", exists(select(wait_records.c.id).where(
                    wait_records.c.origin == "scheduler", wait_records.c.owner_id == tasks.c.id,
                    wait_records.c.outcome == "pending",
                )), exists(select(admission_reservations.c.id).where(
                    admission_reservations.c.task_id == tasks.c.id,
                    admission_reservations.c.state.in_(("pending", "overdue", "granted")),
                ))),
            ).order_by(tasks.c.id).with_for_update())).mappings().all()
            previous = [dict(r) for r in (await conn.execute(select(wait_records).where(
                wait_records.c.project_id == project_id, wait_records.c.origin == "scheduler",
            ).order_by(wait_records.c.id).with_for_update())).mappings()]
            by_key = {(r["owner_id"], r["source_key"]): dict(r) for r in previous}
            reservations = {r["task_id"]: dict(r) for r in (await conn.execute(
                select(admission_reservations).where(
                    admission_reservations.c.project_id == project_id,
                    admission_reservations.c.state.in_(("pending", "overdue", "granted")),
                ).order_by(admission_reservations.c.id).with_for_update()
            )).mappings()}
            prior_deadlines = [dict(d) for d in (await conn.execute(select(wait_deadlines).where(
                wait_deadlines.c.wait_id.in_([r["id"] for r in previous]),
                wait_deadlines.c.deadline_type == "reservation",
            ))).mappings()]
            desired = set()
            for task in candidates:
                observation = await self.reader.observe_task(conn, task["id"])
                reservation = reservations.get(task["id"])
                if reservation and (observation is None
                        or reservation["scope"] != observation.binding["scope"]
                        or reservation["provenance"] == "branch_blocker" and not observation.owns_branch):
                    await conn.execute(update(admission_reservations).where(
                        admission_reservations.c.id == reservation["id"],
                    ).values(state="cancelled"))
                    reservation = None
                if observation is None:
                    continue
                service_scope = Scope(project_id, "scheduler", task["id"],
                                      claim_epoch=task["claim_epoch"], principal="service")
                for condition in observation.conditions():
                    key = (task["id"], self.registry.adapter("resource").source_key(condition).identity)
                    desired.add(key)
                    row = by_key.get(key)
                    if row is None:
                        try:
                            receipt = await register_wait(
                                conn, scope=service_scope, condition=condition.as_dict(),
                                idempotency_key=f"scheduler:{condition.parameters['identity']}",
                                registry=self.registry, policy=SchedulerPolicy(), now=now,
                                source_watermark=watermark,
                                decide_sleep=lambda c, e: SleepDecision("hibernate", "scheduler"),
                            )
                        except WaitError as exc:
                            if exc.code in {"wait.already_satisfied", "wait.source_unresolved"}:
                                continue
                            raise
                        row = dict((await conn.execute(select(wait_records).where(
                            wait_records.c.id == receipt["wait_id"],
                        ))).mappings().one())
                    else:
                        evidence = await self.registry.verify(conn, condition, service_scope)
                        if evidence.observation.state != "unsatisfied":
                            continue
                        # Scheduler identity is unique across its history. Reopen
                        # a later episode under a new version; never replace the
                        # original idempotent registration receipt.
                        reopening = row["outcome"] != "pending"
                        version = row["version"] + int(reopening)
                        await conn.execute(update(wait_records).where(
                            wait_records.c.id == row["id"],
                        ).values(
                            condition=evidence.condition.as_record(), outcome="pending",
                            verification_state="verified", evidence_ref=evidence.evidence_ref,
                            verified_at=now, resolved_at=None, cleared_at=None,
                            created_at=now if reopening else row["created_at"], version=version,
                            source_watermark_seq=watermark,
                        ))
                        changed.update(await clear_edges(conn, row["id"], now=now))
                        edges = self.registry.adapter("resource").edges(
                            evidence.condition, NodeKey(f"task:{task['id']}", project_id),
                        )
                        await add_edges(conn, row["id"], edges,
                                        source_revision=evidence.observation.revision, now=now)
                        row.update(version=version, outcome="pending")
                        observed = await self.registry.observe(conn, evidence.condition)
                        if observed.state != "unsatisfied":
                            changed.update(await close_row(conn, row, now=now,
                                            outcome="satisfied" if observed.state == "satisfied"
                                            else "source_unavailable", result_ref=observed.result_ref))
                            continue
                    changed.add(f"task:{task['id']}")
                    if (row["outcome"] == "pending" and observation.owns_branch
                            and condition.parameters["blocker"] == "slot"):
                        first_due = min([
                            d["due_at"] for d in prior_deadlines
                            if any(r["id"] == d["wait_id"] and r["owner_id"] == task["id"]
                                   and r["claim_epoch"] == task["claim_epoch"]
                                   and r["outcome"] == "pending" for r in previous)
                        ], default=now + self.orchestrator.config.wait_policy.branch_admission_deadline_seconds)
                        await conn.execute(pg_insert(wait_deadlines).values(
                            id=str(uuid.uuid4()), wait_id=row["id"], deadline_type="reservation",
                            generation=row["version"],
                            due_at=first_due,
                        ).on_conflict_do_nothing(constraint="uq_wait_deadlines_wait_type_gen"))
                        if reservation and reservation["provenance"] == "branch_blocker":
                            await conn.execute(pg_insert(wait_deadlines).values(
                                id=str(uuid.uuid4()), wait_id=row["id"], deadline_type="escalation",
                                generation=row["version"], due_at=reservation["deadline_at"],
                            ).on_conflict_do_nothing(constraint="uq_wait_deadlines_wait_type_gen"))
            for row in previous:
                if row["outcome"] == "pending" and (row["owner_id"], row["source_key"]) not in desired:
                    observed = await self.registry.observe(conn, Condition.from_record(row["condition"]))
                    changed.update(await close_row(conn, row, now=now,
                                    outcome="satisfied" if observed.state == "satisfied"
                                    else "source_unavailable", result_ref=observed.result_ref))
                    changed.add(f"task:{row['owner_id']}")
            if changed:
                await recompute_from(conn, changed, now=now)
        return {"success": True, "changed": len(changed), "cycle_id": args["cycle_id"]}

    async def deadline(self, args, *, principal, scope=None, handler=None):
        self.authorize(principal)
        now = self.clock() if args.get("now") is None else args["now"]
        async with self.db._engine.begin() as conn:
            initial = (await conn.execute(select(wait_records).where(
                wait_records.c.id == args["wait_id"], wait_records.c.origin == "scheduler",
            ))).mappings().first()
            if not initial:
                return {"success": False, "error_code": "wait.unavailable"}
            await lock_graph(conn)
            (await conn.execute(select(tasks).where(
                tasks.c.id == initial["owner_id"],
            ).with_for_update())).mappings().first()
            row = (await conn.execute(select(wait_records).where(
                wait_records.c.id == initial["id"],
            ).with_for_update())).mappings().one()
            (await conn.execute(select(admission_reservations).where(
                admission_reservations.c.task_id == row["owner_id"],
                admission_reservations.c.state.in_(("pending", "overdue", "granted")),
            ).with_for_update())).mappings().first()
            due = (await conn.execute(select(wait_deadlines).where(
                wait_deadlines.c.wait_id == row["id"],
                wait_deadlines.c.deadline_type == args["deadline_type"],
                wait_deadlines.c.generation == args["generation"],
                wait_deadlines.c.fired_at.is_(None), wait_deadlines.c.cancelled_at.is_(None),
                wait_deadlines.c.due_at <= now,
            ).with_for_update())).mappings().first()
            if not due:
                return {"success": True, "fired": False}
            result = await self.deadline_locked(conn, due, row, now=now, handler=handler)
        return result

    async def deadline_locked(self, conn, due, row, *, now, handler):
        """Compose with the listener's lease/task/wait/deadline transaction."""
        if not enabled(self.orchestrator.config):
            raise WaitError("wait.disabled", "scheduler waits are disabled")
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == row["owner_id"],
        ).with_for_update())).mappings().first()
        reservation = (await conn.execute(select(admission_reservations).where(
            admission_reservations.c.task_id == row["owner_id"],
            admission_reservations.c.state.in_(("pending", "overdue", "granted")),
        ).with_for_update())).mappings().first()
        event_payload = None
        owns = task and await conn.scalar(select(tasks.c.id).where(
            tasks.c.id == task["id"], tasks.c.id.in_(select(branch_holders().subquery().c.task_id)),
        ))
        if (not owns or task["status"] != "READY" or task["assigned_agent_id"] is not None
                or task["claim_epoch"] != row["claim_epoch"] or row["outcome"] != "pending"
                or row["version"] != due["generation"]):
            await conn.execute(update(wait_deadlines).where(
                wait_deadlines.c.id == due["id"],
            ).values(cancelled_at=now))
            return {"success": True, "fired": False}
        observation = await self.reader.observe_task(conn, task["id"])
        condition = Condition.from_record(row["condition"])
        if (not observation or observation.identity(condition.parameters["blocker"])
                != condition.parameters["identity"]):
            return {"success": False, "error_code": "wait.source_unresolved"}
        seconds = self.orchestrator.config.wait_policy.branch_admission_deadline_seconds
        if due["deadline_type"] == "reservation":
            if reservation is None:
                await conn.execute(insert(admission_reservations).values(
                    id=str(uuid.uuid4()), project_id=task["project_id"], task_id=task["id"],
                    scope=observation.binding["scope"], effective_priority=task["effective_priority"],
                    provenance="branch_blocker", deadline_at=now + seconds,
                    state="pending", created_at=now,
                ))
                await conn.execute(pg_insert(wait_deadlines).values(
                    id=str(uuid.uuid4()), wait_id=row["id"], deadline_type="escalation",
                    generation=due["generation"], due_at=now + seconds,
                ).on_conflict_do_nothing(constraint="uq_wait_deadlines_wait_type_gen"))
        elif due["deadline_type"] == "escalation" and reservation is not None:
            incident = f"branch-admission:{reservation['id']}"
            escalation, created = await self.db.create_escalation(
                conn=conn, id=str(uuid.uuid4()), project_id=task["project_id"],
                task_id=task["id"], task_title=task["title"], task_status=task["status"],
                source_kind="wait", source_identity=incident, incident_key=incident,
                supervisor_owner=f"supervisor-{task['project_id']}", severity="medium",
                summary="A branch holder has not received its reserved admission slot",
                investigation=f"Wait {row['id']}; reservation {reservation['id']}",
                decision_requested="Inspect admission constraints and unblock the branch holder",
                now=now,
            )
            if created:
                await conn.execute(insert(wait_escalations).values(
                    id=str(uuid.uuid4()), wait_id=row["id"], generation=due["generation"],
                    escalation_id=escalation["id"], raised_at=now,
                    original_age_seconds=now - row["created_at"],
                ))
                event_payload = {
                    "wait_id": row["id"], "project_id": task["project_id"],
                    "task_id": task["id"], "generation": due["generation"],
                    "escalation_id": escalation["id"],
                }
                await self.db.log_event("wait.escalated", task["project_id"], task["id"],
                                        payload=canonical(event_payload), conn=conn)
            await conn.execute(update(admission_reservations).where(
                admission_reservations.c.id == reservation["id"],
            ).values(state="overdue"))
        else:
            return {"success": False, "error_code": "wait.unavailable"}
        await conn.execute(update(wait_deadlines).where(
            wait_deadlines.c.id == due["id"],
        ).values(fired_at=now))
        return {"success": True, "fired": True}


async def scheduler_tick(handler):
    o = getattr(handler, "orchestrator", None)
    if o is None or not enabled(getattr(o, "config", None)):
        return
    service = getattr(o, "wait_scheduler_service", None) or SchedulerWaitService(o)
    handlers = getattr(o, "wait_v2_handlers", None)
    if handlers is None:
        handlers = o.wait_v2_handlers = {}
    handlers.setdefault("scheduler_wait_sync", service.sync)
    # The listener's general dispatcher calls this handler only for scheduler
    # reservation/escalation rows; do not overwrite another lifecycle owner.
    handlers.setdefault("wait_deadline_fire", service.deadline)
    with principal_context(ExecutionPrincipal.service("scheduler-waits")):
        cycle_id = str(uuid.uuid4())
        for project in await o.db.list_projects():
            result = await handler.execute("scheduler_wait_sync", {
                "project_id": project.id, "blockers": [], "cycle_id": cycle_id,
            })
            if not result.get("success"):
                raise WaitError("wait.scheduler_sync_failed", str(result))
