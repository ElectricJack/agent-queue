"""Explicit supervisor answers to human waits, atomic with the resume intent."""

import time

from sqlalchemy import insert, select

from src.database.tables import messages, sessions, tasks, wait_edges, wait_records
from src.waits.conditions import Condition, WaitError
from src.waits.decisions import WaitDecisions
from src.waits.explain import owner_key
from src.waits.graph import lock_component_tasks, lock_graph
from src.waits.ownership import fence_owner
from src.waits.sources import insert_message_with_source_event


class HumanWaits:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.clock = clock

    async def lift(self, args, *, principal, scope, handler):
        now = self.clock()
        async with self.service.orch.db._engine.begin() as conn:
            await lock_graph(conn)
            candidate = (await conn.execute(select(wait_records).where(
                wait_records.c.id == args["wait_id"],
            ))).mappings().first()
            if not candidate or (scope.get("project_id")
                                 and scope["project_id"] != candidate["project_id"]):
                raise WaitError("wait.out_of_scope", "wait is outside the supervisor scope")
            await WaitDecisions.authorize(conn, principal, candidate["project_id"])
            if candidate["condition_kind"] != "human":
                raise WaitError("wait.not_human", "only human waits can be lifted")
            cond = Condition.from_record(candidate["condition"])
            if cond.parameters['subject'] == 'legacy_escalation':
                raise WaitError('wait.not_human', 'legacy projections follow their owning service')
            endpoints = (await conn.execute(select(
                wait_edges.c.waiter_key, wait_edges.c.blocker_key,
            ).where(wait_edges.c.wait_id == candidate["id"],
                    wait_edges.c.active.is_(True)))).all()
            keys = {owner_key(candidate), *(key for edge in endpoints for key in edge)}
            if cond.parameters["subject"] == "task_comment":
                keys.add(f"task:{cond.parameters['task_id']}")
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
            if wait["outcome"] != "pending":
                raise WaitError("wait.already_satisfied", "wait already resolved",
                                outcome=wait["outcome"], result_ref=wait["result_ref"])
            source = self.service.registry.adapter("human").source_key(cond)
            if (wait["condition"] != candidate["condition"]
                    or wait["verification_state"] != "verified"
                    or (source.identity, source.generation)
                    != (wait["source_key"], wait["source_generation"])):
                raise WaitError("wait.out_of_scope", "verified human source changed")
            # A racing human answer belongs to source dispatch, never to a lift.
            observed = await self.service.registry.observe(conn, cond)
            if observed.state != "unsatisfied":
                raise WaitError("wait.already_satisfied", "source changed; dispatch its evidence",
                                result_ref=observed.result_ref)
            _, continuation = await fence_owner(conn, wait, task, parked_required=True)
            generation = (continuation["suspension_generation"] if continuation
                          else wait["source_generation"])
            if (args.get("expected_generation") is not None
                    and args["expected_generation"] != generation):
                raise WaitError("continuation.stale_generation", "human wait generation changed")
            if principal.session_id:
                await conn.execute(select(sessions.c.id).where(
                    sessions.c.id == principal.session_id,
                ).with_for_update(read=True))
                await WaitDecisions.authorize(conn, principal, wait["project_id"])
            if wait["owner_kind"] not in {"task", "session"}:
                raise WaitError("wait.out_of_scope", "human response owner has no message address")
            supervisor_id = principal.session_id or "local-operator"
            message_id = f"wait:{wait['id']}:lift"
            thread_id = cond.parameters.get("thread_id") or f"task:{wait['owner_id']}:{wait['id']}"
            await insert_message_with_source_event(conn, insert(messages).values(
                id=message_id, project_id=wait["project_id"],
                from_kind="session" if principal.session_id else "system", from_id=supervisor_id,
                to_kind=wait["owner_kind"], to_id=wait["owner_id"],
                thread_id=thread_id, body=args["response"], body_kind="wait_lift",
                created_at=now,
            ))
            detail = {"decision": "lift", "facts": {"supervisor_session_id": supervisor_id},
                      "supervisor_session_id": supervisor_id, "message_id": message_id, "at": now}
            result = await dispatcher.finish(
                conn, wait, outcome="lifted_by_supervisor", result_ref=f"message:{message_id}",
                event=detail, task=task, continuation=continuation, changed_keys=keys,
                decision=detail,
            )
            if not result.get("committed"):
                raise WaitError("wait.already_satisfied", "wait changed before lift commit")
            return {"success": True, "wait_id": wait["id"], "outcome": "lifted_by_supervisor",
                    "decision": "lift"}
