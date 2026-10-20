"""Commit verified outcomes and durable delivery intents in the listener transaction.

No terminal I/O, detached transaction or task launch happens here. Missing graph,
transition or escalation owners refuse the batch, preserving its leased cursor.
Source outcomes and supervisor wakes share the same terminal transaction.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

from sqlalchemy import insert, or_, select, update

from src.database.queries.continuation_queries import wait_outcome_envelope
from src.database.tables import (
    continuations, events, messages, tasks, wait_deadlines, wait_edges,
    wait_escalations, wait_measurements, wait_records, wait_source_events,
)
from src.waits.conditions import Condition, SourceEvent, WaitError
from src.waits.explain import owner_key
from src.waits.graph import clear_edges, lock_component_tasks, lock_graph, recompute_from
from src.waits.listener import ListenerUnavailable
from src.waits.loops import progress
from src.waits.ownership import fence_owner
from src.waits.sources import execute_task_status_mutation, insert_message_with_source_event


class WakeDispatcher:
    def __init__(self, registry, *, clock=time.time, recompute_priority=recompute_from,
                 resolve_escalation=None, lock_source=None):
        self.registry = registry
        self.clock = clock
        self.recompute_priority = recompute_priority
        self.resolve_escalation = resolve_escalation
        self.lock_source = lock_source

    async def commit(self, conn, candidate, event, *, evidence=None):
        """Source events and bridge evidence are hints, never satisfaction proofs."""
        if await conn.get_isolation_level() != "READ COMMITTED":
            raise ValueError("wait dispatch requires READ COMMITTED observations")
        durable = (await conn.execute(select(wait_source_events).where(
            wait_source_events.c.seq == event["seq"],
        ))).mappings().first()
        if durable is None or dict(durable) != event:
            return {"success": False, "error_code": "wait.source_unresolved"}
        await lock_graph(conn)
        # Lock all task nodes in lexical order, before any wait row. Source
        # writers need not hold the graph lock, so retain their locks through
        # observation and commit as well.
        cond = Condition.from_record(candidate["condition"])
        task_ids = {candidate["owner_id"]} if candidate["owner_kind"] == "task" else set()
        if cond.kind in {"task", "dependency"} or (
            cond.kind == "human" and cond.parameters.get("subject") == "task_comment"
        ):
            task_ids.add(cond.parameters["task_id"])
        if cond.kind == "dependency":
            task_ids.add(cond.parameters["depends_on_task_id"])
        endpoints = (await conn.execute(select(
            wait_edges.c.waiter_key, wait_edges.c.blocker_key,
        ).where(wait_edges.c.wait_id == candidate["id"], wait_edges.c.active.is_(True)))).all()
        changed_keys = {owner_key(candidate), *[f"task:{task_id}" for task_id in task_ids]}
        changed_keys.update(key for edge in endpoints for key in edge)
        await lock_component_tasks(conn, changed_keys)
        locked_tasks = (await conn.execute(select(tasks).where(
            tasks.c.id.in_(sorted(task_ids)),
        ).order_by(tasks.c.id).with_for_update())).mappings().all()
        task = None
        if candidate["owner_kind"] == "task":
            task = next((r for r in locked_tasks if r["id"] == candidate["owner_id"]), None)
        if self.lock_source is None:
            raise ListenerUnavailable("transactional source lock owner is required")
        await self.lock_source(conn, cond)
        row = (await conn.execute(select(wait_records).where(
            wait_records.c.id == candidate["id"],
        ).with_for_update())).mappings().first()
        if row is None or row["outcome"] != "pending":
            return {"success": True, "committed": False}
        row = dict(row)
        if any(row[key] != candidate[key] for key in
               ("owner_kind", "owner_id", "project_id", "session_id", "claim_epoch",
                "condition")):
            return {"success": True, "committed": False}
        if row["source_watermark_seq"] >= event["seq"]:
            return {"success": True, "committed": False}
        invalidation = event["source_key"].startswith("resource_invalidation:")
        if not invalidation and (row["source_key"], row["source_generation"]) != (
                event["source_key"], event["source_generation"]):
            return {"success": True, "committed": False}
        if invalidation and row["condition_kind"] != "resource":
            return {"success": True, "committed": False}
        cond = Condition.from_record(row["condition"])
        adapter = self.registry.adapter(cond.kind)
        source = adapter.source_key(cond)
        if (source.identity, source.generation) != (row["source_key"], row["source_generation"]):
            return {"success": True, "committed": False}
        try:
            scope, continuation = await fence_owner(conn, row, task, parked_required=True)
        except WaitError as exc:
            if exc.code == "wait.unavailable":
                # Retain the cursor until the hibernation transition has parked.
                raise ListenerUnavailable(str(exc)) from exc
            return {"success": True, "committed": False}
        if row["verification_state"] != "verified":
            return {"success": True, "committed": False}
        if cond.kind == "resource":
            try:
                fresh = await self.registry.verify(conn, cond, replace(scope, principal="service"))
            except WaitError:
                return {"success": True, "committed": False}
            if fresh.source != source:
                return {"success": True, "committed": False}
            observed = fresh.observation
        else:
            observed = await self.registry.observe(conn, cond)
        if observed.state == "unsatisfied":
            return {"success": True, "committed": False}
        outcome = "satisfied" if observed.state == "satisfied" else "source_unavailable"
        result_ref = adapter.project_result(cond, SourceEvent(
            source, observed.revision, observed.result_ref or f"wait:{row['id']}:unavailable",
            dict(observed.facts),
        ))
        return await self.finish(
            conn, row, outcome=outcome, result_ref=result_ref, event=event,
            task=task, continuation=continuation, changed_keys=changed_keys,
        )

    async def finish(self, conn, row, *, outcome, result_ref, event, task,
                     continuation, changed_keys, decision=None, deliver=True):
        """Finish an already fenced wait on the caller's transaction.

        The source listener and wait_decide hold the same graph/task/source/wait
        locks and prove the current process or stopped continuation before entry.
        ``deliver=False`` is the supervisor's explicit cancellation, never a wake.
        """
        cond = Condition.from_record(row["condition"])
        adapter = self.registry.adapter(cond.kind)
        now = self.clock()
        inputs = dict(row["decision_inputs"] or {})
        inputs["wake_progress"] = await progress(conn, row, task=task, continuation=continuation)
        if decision:
            inputs["supervisor_decision"] = decision
        changed = await conn.scalar(update(wait_records).where(
            wait_records.c.id == row["id"], wait_records.c.version == row["version"],
            wait_records.c.outcome == "pending",
        ).values(outcome=outcome, result_ref=result_ref, resolved_at=now,
                 blocking=False, decision_inputs=inputs,
                 version=row["version"] + 1).returning(wait_records.c.id))
        if not changed:
            return {"success": True, "committed": False}
        await conn.execute(update(wait_deadlines).where(
            wait_deadlines.c.wait_id == row["id"], wait_deadlines.c.fired_at.is_(None),
            wait_deadlines.c.cancelled_at.is_(None),
        ).values(cancelled_at=now))
        old_endpoints = await clear_edges(conn, row["id"], now=now)
        if self.recompute_priority is None:
            raise ListenerUnavailable("transactional priority recomputation is required")
        await self.recompute_priority(conn, changed_keys | old_endpoints, now=now)
        escalated = (await conn.execute(select(wait_escalations).where(
            wait_escalations.c.wait_id == row["id"],
            or_(wait_escalations.c.decided_at.is_(None),
                wait_escalations.c.decision == "escalate_to_human"),
        ))).mappings().all()
        if escalated:
            if self.resolve_escalation is None:
                raise ListenerUnavailable("transactional wait escalation resolution is required")
            for escalation in escalated:
                result = await self.resolve_escalation(
                    conn, escalation, event, outcome=outcome, now=now,
                )
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise ListenerUnavailable("transactional wait escalation resolution refused")
        from src.waits.measurements import cost_values

        await conn.execute(insert(wait_measurements).values(
            id=f"outcome:{row['id']}", project_id=row["project_id"], wait_id=row["id"],
            condition_kind=cond.kind, source_class=adapter.source_class(cond),
            decision=row["sleep_decision"], waited_seconds=max(0, now - row["created_at"]),
            hibernated=bool(continuation and continuation['stopped_at'] is not None),
            evicted=row['decision_reason'] == 'evicted', recorded_at=now,
            **await cost_values(conn, row, continuation),
        ))
        event_type = "wait.lifted" if outcome == "lifted_by_supervisor" else f"wait.{outcome}"
        payload = {"wait_id": row["id"], "project_id": row["project_id"],
                   "result_ref": result_ref, "source_seq": event.get("seq"), "decision": decision}
        if outcome == "lifted_by_supervisor":
            payload.update(supervisor_session_id=decision["supervisor_session_id"],
                           message_id=decision["message_id"])
        await conn.execute(insert(events).values(
            event_type=event_type, project_id=row["project_id"],
            task_id=row["owner_id"] if row["owner_kind"] == "task" else None,
            timestamp=now, payload=json.dumps(payload),
        ))
        if not deliver:
            await conn.execute(update(wait_records).where(wait_records.c.id == row["id"]).values(
                cleared_at=now,
            ))
        elif continuation and continuation["stopped_at"] is None:
            # The outcome commits once while the retiring writer still owns the
            # slot. Park observes it and queues exactly this generation. Never
            # deliver into a process which the stop transaction may be killing.
            pass
        elif continuation:
            await conn.execute(update(continuations).where(
                continuations.c.id == continuation["id"],
                continuations.c.version == continuation["version"],
                continuations.c.resume_state == "sleeping",
            ).values(resume_state="queued", version=continuation["version"] + 1))
            await execute_task_status_mutation(conn, update(tasks).where(
                tasks.c.id == task["id"], tasks.c.status == "HIBERNATED",
            ).values(status="RESUME_PENDING", updated_at=now))
        elif row["session_id"]:
            from src.waits.mail import held_messages

            held = await held_messages(conn, row)
            envelope = wait_outcome_envelope(
                {**row, "outcome": outcome, "result_ref": result_ref},
                held_messages=[message["id"] for message in held],
                decision=decision,
            )
            await insert_message_with_source_event(conn, insert(messages).values(
                id=f"wait:{row['id']}:outcome", project_id=row["project_id"],
                from_kind="system", from_id="wait-dispatcher", to_kind="session",
                to_id=row["session_id"],
                body_kind="wait_decision" if decision else "wait_outcome",
                body=json.dumps(envelope),
                created_at=now,
            ))
        else:
            # Service-owned explanatory waits have no agent envelope to acknowledge.
            await conn.execute(update(wait_records).where(wait_records.c.id == row["id"]).values(
                cleared_at=now,
            ))
        return {"success": True, "committed": True, "outcome": outcome}
