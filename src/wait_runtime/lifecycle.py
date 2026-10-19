"""Command callbacks composing registration and the continuation lifecycle."""

import time
import json

from sqlalchemy import Float, cast, func, insert, select, update

from src.database.queries.wait_queries import register_wait
from src.database.tables import continuations, events, messages, tasks, wait_records
from src.waits.conditions import WaitError
from src.waits.decision import ceiling_anchor, decide_sleep, measured_inputs, project_policy
from src.waits.graph import lock_graph
from src.database.queries.continuation_queries import measurement_medians
from src.wait_runtime.hibernate import HibernationTransaction, begin_suspension
from src.wait_runtime.resume import ResumeTransaction
from src.waits.sources import execute_task_status_mutation, insert_message_with_source_event


class WaitLifecycle:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.orch = service.orch
        self.db = self.orch.db
        self.clock = clock
        self.hibernate = HibernationTransaction(service, clock=clock)
        self.resume = ResumeTransaction(service, clock=clock)
        self.medians = {}

    async def register(self, args, *, principal, scope, handler):
        policy = project_policy(self.orch.config.wait_policy, scope.project_id) if scope else None
        if scope is None:
            raise WaitError("wait.out_of_scope", "registration needs a bound owner")
        if args["condition"]["kind"] == "human" and not policy.hibernation_enabled:
            raise WaitError("wait.disabled", "human waits require hibernation support")
        watermark = await self.db.wait_source_watermark()
        now = self.clock()
        async with self.db._engine.begin() as conn:
            await lock_graph(conn)
            anchor = await ceiling_anchor(conn, scope, now=now, policy=policy)
            if scope.project_id not in self.medians:
                self.medians[scope.project_id] = await measurement_medians(
                    conn, scope.project_id, now=now,
                )
            task_profile = await conn.scalar(select(tasks.c.profile_id).where(
                tasks.c.id == scope.owner_id,
            )) if scope.owner_kind == 'task' else None
            pressure = getattr(self.orch, '_wait_slot_pressure', {}).get(
                (scope.project_id, task_profile), 0,
            )
            def measured_decision(cond, evidence):
                return decide_sleep(measured_inputs(
                    cond, evidence, policy=policy, medians=self.medians[scope.project_id],
                    pressure=pressure, anchor=anchor, now=now,
                ))

            receipt = await register_wait(
                conn, scope=scope, condition=args["condition"],
                idempotency_key=args["idempotency_key"], registry=self.service.registry,
                policy=policy, source_watermark=watermark, now=now,
                ceiling_anchor_at=anchor,
                decide_sleep=measured_decision,
                observe_sleep_inputs=lambda cond, evidence: measured_decision(cond, evidence).inputs,
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
        from src.waits.conditions import Condition
        from src.wait_runtime.measurements import capture_costs

        await capture_costs(self, args['wait_id'])

        async with self.db._engine.begin() as conn:
            await lock_graph(conn)
            wait = (await conn.execute(select(wait_records).where(
                wait_records.c.id == args['wait_id'],
            ))).mappings().first()
            if wait is None:
                raise WaitError('wait.source_unresolved', 'wait disappeared')
            if wait['cleared_at'] is not None:
                return {'success': True, 'measurements_updated': True}
            evicted = wait['decision_reason'] == 'evicted'
            if wait['sleep_decision'] == 'in_place':
                policy = project_policy(self.orch.config.wait_policy, wait['project_id'])
                task = (await conn.execute(select(tasks).where(
                    tasks.c.id == wait['owner_id'],
                ).with_for_update())).mappings().first()
                if not task:
                    return {'success': True, 'decision': 'in_place'}
                wait = (await conn.execute(select(wait_records).where(
                    wait_records.c.id == args['wait_id'],
                ).with_for_update())).mappings().one()
                medians = await measurement_medians(conn, wait['project_id'], now=self.clock())
                decision = decide_sleep(measured_inputs(
                    Condition.from_record(wait['condition']), None, policy=policy, medians=medians,
                    pressure=getattr(self.orch, '_wait_slot_pressure', {}).get(
                        (task['project_id'], task['profile_id']), 0),
                    anchor=wait['ceiling_anchor_at'] or wait['created_at'], now=self.clock(),
                ))
                if decision.mode == 'in_place':
                    return {'success': True, 'decision': 'in_place', 'inputs': dict(decision.inputs)}
                await self.suspend(conn, wait, reason=decision.reason, now=self.clock())
        result = await self.hibernate.advance(args["wait_id"], handler=handler)
        if result.get('parked'):
            await capture_costs(self, args['wait_id'])
        if result.get('parked') and evicted:
            # The suspension is now proved. Complete the waiting arbitration
            # immediately from fresh occupancy, rather than waiting a pool tick.
            await self.orch._reconcile_pools()
        return result

    async def evict(self, args, *, principal, scope, handler):
        from src.wait_runtime.eviction import evict

        return await evict(self, args)

    async def suspend(self, conn, wait, *, reason, now):
        """One checkpoint request; retain the registered source and its edges."""
        from src.waits.ownership import fence_owner

        if not getattr(self.orch.config.wait_policy, 'hibernation_enabled', False):
            return {'success': True, 'suspended': False, 'reason': 'flag'}
        if wait['sleep_decision'] != 'in_place' or wait['outcome'] != 'pending':
            return {'success': True, 'suspended': False}
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == wait['owner_id'],
        ).with_for_update())).mappings().first()
        if wait['owner_kind'] != 'task' or not task:
            return {'success': True, 'suspended': False}
        await fence_owner(conn, wait, task, parked_required=False)
        await conn.execute(update(wait_records).where(wait_records.c.id == wait['id']).values(
            sleep_decision='hibernate', decision_reason=reason, version=wait['version'] + 1,
            decision_inputs={**wait['decision_inputs'], 'suspension_reason': reason,
                             'suspension_requested_at': now},
        ))
        intent = await begin_suspension(
            conn, wait, task, now=now, budget=self.orch.config.wait_policy.transition_budget_seconds,
        )
        await insert_message_with_source_event(conn, insert(messages).values(
            id=f"wait:{wait['id']}:suspend:{intent['suspension_generation']}",
            project_id=wait['project_id'], from_kind='system', from_id='wait-service',
            to_kind='session', to_id=wait['session_id'], body_kind='wait_suspend', created_at=now,
            body=json.dumps({'wait_id': wait['id'], 'continuation_id': intent['id'],
                             'generation': intent['suspension_generation'], 'reason': reason,
                             'instruction': 'Submit aq wait checkpoint, then end your turn now.'}),
        ))
        await conn.execute(insert(events).values(
            event_type=f'wait.{reason}', project_id=wait['project_id'], task_id=task['id'],
            timestamp=now, payload=json.dumps({'wait_id': wait['id'],
                                             'continuation_id': intent['id']}),
        ))
        return {'success': True, 'suspended': True, 'continuation_id': intent['id']}

    async def ceiling(self, conn, deadline, wait, *, now, handler):
        return await self.suspend(conn, wait, reason='ceiling', now=now)

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
        self.medians.clear()
        if not getattr(self.orch.config.wait_policy, 'hibernation_enabled', False):
            return
        async with self.db._engine.connect() as conn:
            ids = list((await conn.execute(select(continuations.c.wait_id).where(
                continuations.c.resume_state.in_(("sleeping", "queued")),
                continuations.c.facts["workspace_released_at"].astext.is_(None),
            ))).scalars())
            # First-turn accounting can arrive after ACK. Retry only recent
            # continuations missing measured usage, at most once per 30 seconds.
            ids.extend((await conn.execute(select(continuations.c.wait_id).where(
                continuations.c.resume_state == 'acknowledged',
                continuations.c.created_at >= self.clock() - 86400,
                continuations.c.facts['costs']['resume_tokens'].astext.is_(None),
                func.coalesce(cast(continuations.c.facts['usage_checked_at'].astext, Float), 0)
                <= self.clock() - 30,
            ))).scalars())
        for wait_id in ids:
            await self.service.command("wait_sleep_decide", {"wait_id": wait_id})
        await self.resume_pending()

    async def resume_pending(self):
        if not getattr(self.orch.config.wait_policy, 'hibernation_enabled', False):
            return
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
