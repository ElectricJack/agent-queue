"""Command callbacks composing registration and the continuation lifecycle."""

import time

from sqlalchemy import select, update

from src.database.queries.wait_queries import register_wait
from src.database.tables import continuations, tasks, wait_records
from src.waits.conditions import SleepDecision, WaitError
from src.waits.hibernate import HibernationTransaction, begin_suspension
from src.waits.resume import ResumeTransaction
from src.waits.sources import execute_task_status_mutation


class WaitLifecycle:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.orch = service.orch
        self.db = self.orch.db
        self.clock = clock
        self.hibernate = HibernationTransaction(service, clock=clock)
        self.resume = ResumeTransaction(service, clock=clock)

    async def register(self, args, *, principal, scope, handler):
        policy = self.orch.config.wait_policy
        if scope is None:
            raise WaitError("wait.out_of_scope", "registration needs a bound owner")
        if args["condition"]["kind"] == "human" and not policy.hibernation_enabled:
            raise WaitError("wait.disabled", "human waits require hibernation support")
        watermark = await self.db.wait_source_watermark()
        now = self.clock()
        async with self.db._engine.begin() as conn:
            receipt = await register_wait(
                conn, scope=scope, condition=args["condition"],
                idempotency_key=args["idempotency_key"], registry=self.service.registry,
                policy=policy, source_watermark=watermark, now=now,
                decide_sleep=lambda *_: SleepDecision("in_place", "flag", {}),
                claimed=args.get("claimed"),
                escalate_after_seconds=args.get("escalate_after_seconds"),
            )
            if not receipt["success"] or scope.owner_kind != "task":
                return receipt
            wait = (await conn.execute(select(wait_records).where(
                wait_records.c.id == receipt["wait_id"],
            ))).mappings().one()
            task = (await conn.execute(select(tasks).where(
                tasks.c.id == scope.owner_id,
            ))).mappings().one()
            if wait["sleep_decision"] == "hibernate":
                if not policy.hibernation_enabled:
                    raise WaitError("wait.disabled", "forced hibernation is not enabled")
                intent = await begin_suspension(
                    conn, wait, task, now=now, budget=policy.transition_budget_seconds,
                )
                receipt = {**receipt, "continuation_id": intent["id"],
                           "generation": intent["suspension_generation"]}
                await conn.execute(update(wait_records).where(
                    wait_records.c.id == wait["id"],
                ).values(decision_inputs={**wait["decision_inputs"],
                                          "registration_receipt": receipt}))
            else:
                await execute_task_status_mutation(conn, update(tasks).where(
                    tasks.c.id == task["id"], tasks.c.status == "IN_PROGRESS",
                ).values(status="WAITING", updated_at=now))
            return receipt

    async def sleep_decide(self, args, *, principal, scope, handler):
        return await self.hibernate.advance(args["wait_id"], handler=handler)

    async def acknowledge(self, args, *, principal, scope, handler):
        from src.waits.mail import acknowledge_in_place
        from src.waits.resume_ack import acknowledge

        async with self.db._engine.begin() as conn:
            decision = await conn.scalar(select(wait_records.c.sleep_decision).where(
                wait_records.c.id == args["wait_id"],
            ))
            if decision == "in_place":
                result = await acknowledge_in_place(conn, wait_id=args["wait_id"],
                                                    scope=scope, now=self.clock())
                return {**result, "acknowledged": True, "next_step": "continue"}
            return await acknowledge(conn, args, scope=scope, now=self.clock())

    async def tick(self):
        if not self.orch.config.wait_policy.hibernation_enabled:
            return
        async with self.db._engine.connect() as conn:
            ids = list((await conn.execute(select(continuations.c.wait_id).where(
                continuations.c.resume_state.in_(("sleeping", "queued")),
                continuations.c.facts["workspace_released_at"].astext.is_(None),
            ))).scalars())
        for wait_id in ids:
            await self.service.command("wait_sleep_decide", {"wait_id": wait_id})
        async with self.db._engine.connect() as conn:
            ready = [dict(row) for row in (await conn.execute(select(continuations).join(
                tasks, tasks.c.id == continuations.c.task_id,
            ).where(
                continuations.c.resume_state.in_(("queued", "reserved", "launching")),
                tasks.c.status == "RESUME_PENDING",
            ).order_by(tasks.c.effective_priority, continuations.c.created_at))).mappings()]
        for intent in ready:
            await self.service.command("wait_resume_dispatch", {
                "continuation_id": intent["id"], "generation": intent["suspension_generation"],
            })
