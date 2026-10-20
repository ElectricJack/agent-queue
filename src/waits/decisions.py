"""Transactional operational escalations and supervisor decisions for verified waits.

Expiry only notifies the supervisor. A wake needs an explicit decision, fresh
source evidence and the same process/claim fences as source dispatch. Cancellation
holds the task; rerouting separately releases a proved stopped writer to admission.
"""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import delete, func, insert, select, update

from src.waits.adapters.legacy_escalation import execute_mutation as execute_escalation_mutation
from src.database.queries.wait_queries import wait_explain
from src.database.tables import (
    admission_reservations, continuations, escalation_deliveries, escalations, events,
    integration_branch_owners, messages, sessions, task_context, tasks, wait_deadlines,
    wait_edges, wait_escalations, wait_records,
)
from src.waits.conditions import Condition, WaitError
from src.waits.explain import owner_key
from src.waits.graph import lock_component_tasks, lock_graph
from src.waits.listener import ListenerUnavailable
from src.waits.ownership import fence_owner
from src.waits.sources import execute_task_status_mutation, insert_message_with_source_event

CHOICES = ["extend", "wake", "cancel", "reroute", "escalate_to_human"]


class WaitDecisions:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.clock = clock

    async def raise_escalation(self, conn, deadline, wait, *, now, handler):
        """One operational incident per deadline generation, with no worker mail."""
        if wait["outcome"] != "pending" or wait["condition_kind"] in {"human", "resource"}:
            raise WaitError("wait.not_escalated", "wait has no operational escalation")
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == wait["owner_id"], tasks.c.project_id == wait["project_id"],
        ))).mappings().first() if wait["owner_kind"] == "task" else None
        await fence_owner(conn, wait, task)
        generation = deadline["generation"]
        existing = (await conn.execute(select(wait_escalations).where(
            wait_escalations.c.wait_id == wait["id"],
            wait_escalations.c.generation == generation,
        ))).mappings().first()
        if existing:
            return {"success": True, "escalation_id": existing["escalation_id"]}
        explanation = await wait_explain(conn, project_id=wait["project_id"],
                                         wait_id=wait["id"], now=now)
        branch = await conn.scalar(select(integration_branch_owners.c.id).where(
            integration_branch_owners.c.owner_id == wait["owner_id"],
            integration_branch_owners.c.handoff_state != "released",
        ).limit(1)) if task else None
        severity = "high" if task and (task["effective_priority"] <= 20 or branch) else "medium"
        # Source identity is generation-qualified because escalations enforce
        # permanent uniqueness on (project, source_kind, source_identity).
        result = await handler.wait_escalation_create(conn, {
            "project_id": wait["project_id"], "task_id": task["id"] if task else None,
            "task_title": task["title"] if task else f"Wait owner {wait['owner_id']}",
            "source_kind": "wait", "source_identity": f"{wait['id']}:{generation}",
            "incident_key": f"wait:{wait['id']}:{generation}",
            "summary": f"Wait {wait['id']} needs a supervisor decision",
            "investigation": json.dumps(explanation, default=str)[:8000],
            "decision_requested": "Decide this operational wait through wait_decide.",
            "choices": CHOICES, "severity": severity,
        })
        if not result.get("success"):
            raise ListenerUnavailable(f"escalation_create refused: {result}")
        prior = await conn.scalar(select(wait_escalations.c.id).where(
            wait_escalations.c.wait_id == wait["id"],
        ).order_by(wait_escalations.c.generation.desc()).limit(1))
        incident_id = result["escalation"]["id"]
        await conn.execute(insert(wait_escalations).values(
            id=str(uuid.uuid4()), wait_id=wait["id"], generation=generation,
            escalation_id=incident_id, raised_at=now,
            original_age_seconds=max(0, now - wait["created_at"]), prior_escalation_id=prior,
        ))
        await self.audit(conn, wait, "wait.escalated", now,
                         generation=generation, escalation_id=incident_id)
        return {"success": True, "escalation_id": incident_id}

    @staticmethod
    async def audit(conn, wait, kind, now, **payload):
        await conn.execute(insert(events).values(
            event_type=kind, project_id=wait["project_id"],
            task_id=wait["owner_id"] if wait["owner_kind"] == "task" else None,
            timestamp=now, payload=json.dumps({"wait_id": wait["id"],
                                              "project_id": wait["project_id"], **payload}),
        ))

    async def resolve_escalation(self, conn, escalation, event, *, outcome, now):
        """Withdraw an operational incident without inventing a human answer."""
        incident = (await conn.execute(select(escalations).where(
            escalations.c.id == escalation["escalation_id"],
        ).with_for_update())).mappings().first()
        if not incident or incident["source_kind"] != "wait":
            return {"success": False}
        if incident["state"] in {"resolved", "cancelled", "stale"}:
            return {"success": True}
        evidence = {"wait_id": escalation["wait_id"], "source_seq": event.get("seq"),
                    "decision": event.get("decision"), "note": event.get("note")}
        await execute_escalation_mutation(conn, update(escalations).where(
            escalations.c.id == incident["id"], escalations.c.revision == incident["revision"],
        ).values(state="cancelled", revision=incident["revision"] + 1,
                 terminal_outcome="source_satisfied" if outcome == "satisfied" else outcome,
                 terminal_evidence=evidence, terminal_at=now, updated_at=now))
        await conn.execute(insert(events).values(
            event_type="escalation.updated.v1", project_id=incident["project_id"],
            task_id=incident["task_id"], timestamp=now,
            payload=json.dumps({"version": 1, "escalation_id": incident["id"],
                                "project_id": incident["project_id"], "state": "cancelled",
                                "revision": incident["revision"] + 1,
                                "terminal_outcome": ("source_satisfied" if outcome == "satisfied"
                                                     else outcome)}),
        ))
        # Unattempted human deliveries can be withdrawn. Preserve receipts of
        # attempted/external deliveries and all conversation history.
        await conn.execute(delete(escalation_deliveries).where(
            escalation_deliveries.c.escalation_id == incident["id"],
            escalation_deliveries.c.status == "pending",
            escalation_deliveries.c.attempt_count == 0,
        ))
        return {"success": True}

    async def decide(self, args, *, principal, scope, handler):
        now = self.clock()
        async with self.service.orch.db._engine.begin() as conn:
            await lock_graph(conn)
            candidate = (await conn.execute(select(wait_records).where(
                wait_records.c.id == args["wait_id"],
            ))).mappings().first()
            if (not candidate or (scope.get("project_id")
                                  and scope["project_id"] != candidate["project_id"])):
                raise WaitError("wait.out_of_scope", "wait is outside the supervisor scope")
            await self.authorize(conn, principal, candidate["project_id"])
            cond = Condition.from_record(candidate["condition"])
            endpoints = (await conn.execute(select(wait_edges.c.waiter_key,
                                                   wait_edges.c.blocker_key).where(
                wait_edges.c.wait_id == candidate["id"], wait_edges.c.active.is_(True),
            ))).all()
            keys = {owner_key(candidate), *(key for edge in endpoints for key in edge)}
            if cond.kind in {"task", "dependency"}:
                keys.add(f"task:{cond.parameters['task_id']}")
            if cond.kind == "dependency":
                keys.add(f"task:{cond.parameters['depends_on_task_id']}")
            await lock_component_tasks(conn, keys)
            task = (await conn.execute(select(tasks).where(
                tasks.c.id == candidate["owner_id"],
            ).with_for_update())).mappings().first() if candidate["owner_kind"] == "task" else None
            dispatcher = self.service.dispatcher
            if dispatcher is None or dispatcher.lock_source is None:
                raise WaitError("wait.unavailable", "transactional dispatch owner is unavailable")
            await dispatcher.lock_source(conn, cond)
            wait = dict((await conn.execute(select(wait_records).where(
                wait_records.c.id == candidate["id"],
            ).with_for_update())).mappings().one())
            escalation = (await conn.execute(select(wait_escalations).where(
                wait_escalations.c.wait_id == wait["id"],
                wait_escalations.c.decided_at.is_(None),
            ).order_by(wait_escalations.c.generation.desc()).with_for_update())).mappings().first()
            if (wait["outcome"] != "pending" or not escalation
                    or (args.get("expected_generation") is not None
                        and args["expected_generation"] != escalation["generation"])):
                raise WaitError("wait.not_escalated", "wait is resolved or escalation changed",
                                outcome=wait["outcome"])
            incident = (await conn.execute(select(escalations).where(
                escalations.c.id == escalation["escalation_id"],
                escalations.c.project_id == wait["project_id"],
                escalations.c.source_kind == "wait",
            ).with_for_update())).mappings().first()
            if not incident or incident["state"] not in {"needs_human", "reply_received", "resolving"}:
                raise WaitError("wait.not_escalated", "operational incident is no longer open")
            if (wait["condition"] != candidate["condition"]
                    or wait["verification_state"] != "verified"):
                raise WaitError("wait.out_of_scope", "verified wait identity changed")
            source = self.service.registry.adapter(cond.kind).source_key(cond)
            if (source.identity, source.generation) != (wait["source_key"], wait["source_generation"]):
                raise WaitError("wait.out_of_scope", "verified source generation changed")
            observed = await self.service.registry.observe(conn, cond)
            if observed.state != "unsatisfied":
                # Let source dispatch record the real evidence; never supersede
                # a source result with a stale supervisor digest decision.
                raise WaitError("wait.not_escalated", "source changed; dispatch its evidence first")
            decision = args["decision"]
            owner_scope, continuation = await fence_owner(
                conn, wait, task, parked_required=decision in {"wake", "reroute"},
            )
            detail = {"decision": decision, "note": args["note"],
                      "supervisor_session_id": principal.session_id, "at": now}
            event = {"decision": decision, "note": args["note"]}
            if decision == "extend":
                await self.extend(conn, wait, escalation, args, now)
            elif decision == "escalate_to_human":
                await self.human(conn, wait, escalation, owner_scope, continuation, detail, now)
            else:
                if decision == "reroute":
                    await self.reroute(conn, wait, task, continuation, args, handler, now)
                elif decision == "cancel":
                    await self.hold(conn, wait, task, continuation, now)
                result = await dispatcher.finish(
                    conn, wait, outcome="cancelled", result_ref=f"wait-decision:{wait['id']}",
                    event=event, task=task, continuation=continuation, changed_keys=keys,
                    decision=detail, deliver=decision == "wake",
                )
                if not result.get("committed"):
                    raise WaitError("wait.not_escalated", "wait changed before decision commit")
            if decision != "escalate_to_human":
                result = await self.resolve_escalation(conn, escalation, event,
                                                       outcome=decision, now=now)
                if not result.get("success"):
                    raise ListenerUnavailable("escalation decision resolution refused")
            await conn.execute(update(wait_escalations).where(
                wait_escalations.c.id == escalation["id"],
                wait_escalations.c.decided_at.is_(None),
            ).values(decision=decision, decided_at=now, note=args["note"],
                     supervisor_session_id=principal.session_id))
            await self.audit(conn, wait, "wait.decided", now,
                             generation=escalation["generation"], **detail)
            return {"success": True, "wait_id": wait["id"], "decision": decision,
                    "outcome": "cancelled" if decision in {"wake", "cancel", "reroute"}
                    else "pending", "generation": escalation["generation"]}

    @staticmethod
    async def authorize(conn, principal, project_id):
        if principal.kind == "local":
            return
        if principal.kind != "session" or not principal.elevated:
            raise WaitError("wait.out_of_scope", "a live supervisor is required")
        session = (await conn.execute(select(sessions).where(
            sessions.c.id == principal.session_id,
        ))).mappings().first()
        if (not session or session["profile_id"] != "supervisor"
                or session["lifecycle"] != "named" or session["desired_state"] != "running"
                or session["state"] not in {"starting", "running"}
                or session["instance_token"] != principal.session_instance_token
                or session["project_id"] != principal.project_id
                or (principal.project_id and principal.project_id != project_id)):
            raise WaitError("wait.out_of_scope", "supervisor process or project changed")

    async def extend(self, conn, wait, escalation, args, now):
        seconds = args.get("extend_seconds")
        if seconds is None:
            policy = getattr(self.service.orch.config, "wait_policy", None)
            if not callable(getattr(policy, "escalation_seconds", None)):
                raise WaitError("wait.unavailable", "wait escalation policy is unavailable")
            seconds = self.service.registry.adapter(wait["condition_kind"]).deadline_policy(
                Condition.from_record(wait["condition"]), policy,
            ).escalate_after_seconds
        if seconds is None or not 60 <= seconds <= 86400:
            raise WaitError("wait.condition_required", "extension must be between 60 and 86400 seconds")
        deadline_id = str(uuid.uuid4())
        await conn.execute(update(wait_deadlines).where(
            wait_deadlines.c.wait_id == wait["id"],
            wait_deadlines.c.deadline_type == "escalation",
            wait_deadlines.c.cancelled_at.is_(None),
        ).values(cancelled_at=now))
        await conn.execute(insert(wait_deadlines).values(
            id=deadline_id, wait_id=wait["id"], deadline_type="escalation",
            generation=escalation["generation"] + 1, due_at=now + seconds,
        ))
        await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
            escalate_at=now + seconds, version=wait["version"] + 1,
        ))
        await conn.execute(select(func.pg_notify("aq_wait_deadline", deadline_id)))

    @staticmethod
    async def hold(conn, wait, task, continuation, now):
        if not task:
            raise WaitError("wait.out_of_scope", "cancellation requires a task to hold")
        await conn.execute(update(continuations).where(
            continuations.c.wait_id == wait["id"],
            continuations.c.resume_state.in_(("sleeping", "queued", "reserved", "held")),
        ).values(resume_state="cancelled", hold_reason="supervisor_cancel",
                 version=continuations.c.version + 1))
        await conn.execute(update(admission_reservations).where(
            admission_reservations.c.task_id == task["id"],
            admission_reservations.c.state.in_(("pending", "overdue", "granted")),
        ).values(state="cancelled"))
        # Request a stop; the reconciler/hibernation owner owns termination and
        # fenced claim release. Never make a live writer available to admission.
        await conn.execute(update(sessions).where(
            sessions.c.id == wait["session_id"],
            sessions.c.instance_token == (wait["decision_inputs"] or {}).get(
                "delivery", {}).get("instance_token"),
        ).values(desired_state="stopped"))
        await execute_task_status_mutation(conn, update(tasks).where(
            tasks.c.id == task["id"],
        ).values(status="PAUSED", updated_at=now))

    async def reroute(self, conn, wait, task, continuation, args, handler, now):
        if not task or not continuation or not continuation["summary"]:
            raise WaitError("wait.unavailable", "reroute requires a stopped continuation and summary")
        if task["route_source"] == "role":
            raise WaitError("wait.out_of_scope", "a role task cannot be rerouted")
        target = args["to"]
        routing, error = await handler.wait_reroute_target(wait["project_id"], target)
        if error:
            raise WaitError("wait.condition_required", error)
        await self.hold(conn, wait, task, continuation, now)
        await conn.execute(insert(task_context).values(
            id=str(uuid.uuid4()), task_id=task["id"], type="handoff",
            label="Supervisor reroute from verified wait", created_at=now,
            content=json.dumps({"agent": {"detail": continuation["summary"],
                                           "next_step": "Continue through normal admission"},
                                "facts": continuation["facts"], "wait_id": wait["id"]}),
            claim_epoch=task["claim_epoch"], idempotency_key=f"wait-reroute:{wait['id']}",
        ))
        if not await self.service.orch.db.reset_task_route(task["id"], **routing, conn=conn):
            raise WaitError("wait.out_of_scope", "writer is still live or routing fence changed")
        # READY is a scheduling candidate, not a launch. Normal project, source,
        # dependency, review and resource gates still arbitrate admission.
        await execute_task_status_mutation(conn, update(tasks).where(
            tasks.c.id == task["id"], tasks.c.status == "PAUSED",
        ).values(status="READY", updated_at=now))

    async def human(self, conn, wait, escalation, scope, continuation, detail, now):
        """Convert to the existing verified-human thread adapter, never lift it."""
        incident = (await conn.execute(select(escalations).where(
            escalations.c.id == escalation["escalation_id"],
        ).with_for_update())).mappings().one()
        if incident["state"] != "needs_human":
            raise WaitError("wait.not_escalated", "escalation conversation already changed")
        request_id = f"wait:{wait['id']}:human-request"
        await insert_message_with_source_event(conn, insert(messages).values(
            id=request_id, project_id=wait["project_id"],
            from_kind="session", from_id=wait["session_id"],
            to_kind="user", to_id="*", thread_id=incident["id"], body=detail["note"],
            body_kind="wait_human_request", created_at=now, archived_at=now,
        ))
        cursor = await conn.scalar(select(messages.c.created_seq).where(messages.c.id == request_id))
        cond = self.service.registry.normalise({"kind": "human", "subject": "message_thread",
                                               "thread_id": incident["id"], "after_seq": cursor})
        # The old task claim may already be parked; the authenticated supervisor
        # owns this conversion, while the adapter binds evidence to the old owner.
        evidence = await self.service.registry.verify(conn, cond, scope)
        from src.waits.graph import clear_edges, recompute_from
        keys = await clear_edges(conn, wait["id"], now=now)
        await recompute_from(conn, keys | {owner_key(wait)}, now=now)
        await conn.execute(update(wait_deadlines).where(
            wait_deadlines.c.wait_id == wait["id"], wait_deadlines.c.cancelled_at.is_(None),
        ).values(cancelled_at=now))
        inputs = {**(wait["decision_inputs"] or {}), "supervisor_decision": detail,
                  "human_escalation_id": incident["id"]}
        await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
            condition_kind="human", condition=evidence.condition.as_record(), origin="human",
            source_key=evidence.source.identity, source_generation=evidence.source.generation,
            evidence_ref=evidence.evidence_ref, verified_at=now, escalate_at=None,
            ceiling_anchor_at=None, sleep_decision="hibernate", decision_reason="human",
            decision_inputs=inputs, version=wait["version"] + 1,
        ))
        await execute_escalation_mutation(conn, update(escalations).where(escalations.c.id == incident["id"]).values(
            severity="high", decision_requested=detail["note"],
            revision=incident["revision"] + 1, updated_at=now,
        ))
        await conn.execute(insert(events).values(
            event_type="escalation.updated.v1", project_id=incident["project_id"],
            task_id=incident["task_id"], timestamp=now,
            payload=json.dumps({"version": 1, "escalation_id": incident["id"],
                                "project_id": incident["project_id"], "state": incident["state"],
                                "revision": incident["revision"] + 1}),
        ))
        await self.service.orch.db.enqueue_escalation_delivery(
            incident["id"], conn=conn, dedup_key=f"{incident['id']}:root:0", kind="root",
            payload={"replacement": False}, available_at=now, priority=0, now=now,
        )
        if not continuation:
            # This durable suspension request is consumed by the checkpoint /
            # hibernation owner. It grants no early release of a live writer.
            await insert_message_with_source_event(conn, insert(messages).values(
                id=f"wait:{wait['id']}:human-suspend", project_id=wait["project_id"],
                from_kind="system", from_id="wait-dispatcher", to_kind="session",
                to_id=wait["session_id"], body_kind="wait_suspend", created_at=now,
                body=json.dumps({"wait_id": wait["id"], "decision": detail,
                                 "next_step": "checkpoint_then_end_turn"}),
            ))
