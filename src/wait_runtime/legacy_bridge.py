"""Transactional adoption of seat-retaining v1 waits; never enables v2.

Erratum to implementation §14.3(3), approved in supervisor message
msg-4caf5678594a428a8d6cc99bc8e2a9dd: preserve deadline_at exactly. In
particular, expired and near-expiry records receive no sixty-second grace.
"""

import hashlib
import json
import time
import uuid

from sqlalchemy import func, insert, select, update

from src.agent_waits import WaitResolution
from src.database.queries.wait_queries import verify_owner
from src.database.queries.continuation_queries import measurement_medians
from src.database.tables import agent_waits, messages, tasks, wait_deadlines, wait_records
from src.waits.conditions import NodeKey, Scope, WaitError, canonical
from src.waits.decision import decide_sleep, measured_inputs, project_policy
from src.waits.graph import add_edges, lock_graph, recompute_from, would_create_cycle
from src.wait_runtime.hibernate import begin_suspension
from src.waits.sources import execute_task_status_mutation, insert_message_with_source_event


def legacy_condition(row):
    """Translate persisted identities, not prose or an untrusted result message."""
    kind = row['kind']
    keys = {'task': ('task_id',), 'job': ('job_id',),
            'message': ('thread_id', 'after_seq'), 'timer': ('due_at',)}
    if kind not in keys or any(key not in row['match'] for key in keys[kind]):
        raise WaitError('wait.condition_required', 'legacy source identity is incomplete')
    return {'kind': kind, **{key: row['match'][key] for key in keys[kind]}}


def legacy_times(row):
    """The v1 schema already bounds the interval; preserve both endpoints."""
    import math

    created, expires = row['created_at'], row['deadline_at']
    if not all(type(v) in (float, int) and math.isfinite(v) for v in (created, expires)):
        raise WaitError('wait.invalid', 'legacy timestamps must be finite')
    if not created < expires <= created + 86400:
        raise WaitError('wait.invalid', 'legacy interval violates the v1 contract')
    return created, expires


class LegacyWaitBridge:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.orch = service.orch
        self.db = self.orch.db
        self.clock = clock

    async def run(self, args, *, principal, scope=None, handler=None):
        from src.wait_runtime.legacy_pass import run_pass

        return await run_pass(self, args, principal=principal, scope=scope, handler=handler)

    async def adopt_agent_wait(self, legacy_id, *, handler):
        """One replayable identity; v1 cancellation locks session before task.

        Take that same ordering after the graph fence. Watermark is acquired on
        its dedicated connection before any owner/source lock. A concurrent v1
        resolver must recheck bridge ownership under its own row lock.
        """
        from src.database.tables import sessions

        watermark = await self.db.wait_source_watermark()
        now = self.clock()
        async with self.db._engine.begin() as conn:
            await lock_graph(conn)
            old = (await conn.execute(select(agent_waits).where(
                agent_waits.c.id == legacy_id,
            ))).mappings().first()
            if old is None:
                raise WaitError('wait.not_found', 'legacy wait disappeared')
            session = (await conn.execute(select(sessions).where(
                sessions.c.id == old['session_id'],
            ).with_for_update())).mappings().first()
            task = (await conn.execute(select(tasks).where(
                tasks.c.id == old['owner_id'],
            ).with_for_update())).mappings().first() if old['owner_kind'] == 'task' else None
            old = dict((await conn.execute(select(agent_waits).where(
                agent_waits.c.id == legacy_id,
            ).with_for_update())).mappings().one())
            previous = (await conn.execute(select(wait_records).where(
                wait_records.c.legacy_kind == 'agent_wait',
                wait_records.c.legacy_id == legacy_id,
            ))).mappings().first()
            if previous:
                return {'success': True, 'wait_id': previous['id'], 'replayed': True}
            if old['state'] != 'active' and (old.get('digest') or {}).get(
                    'reason') == 'source_unavailable':
                return {'success': True, 'legacy_id': legacy_id, 'source_unavailable': True}
            policy = project_policy(self.orch.config.wait_policy, old['project_id'])
            if not policy.v2_enabled:
                raise WaitError('wait.disabled', 'verified waits are disabled for this project')
            scope = Scope(old['project_id'], 'task' if task else 'session', old['owner_id'],
                          old['session_id'], old['session_instance_token'], old['claim_epoch'],
                          elevated=old['owner_kind'] == 'supervisor')
            if not session:
                raise WaitError('wait.migration_hold', 'legacy session is missing')
            await verify_owner(conn, scope)
            if old['state'] not in {'active', 'satisfied', 'expired', 'cancelled'}:
                raise WaitError('wait.migration_hold', 'terminal v1 outcome needs explicit disposition')
            created, expires = legacy_times(old)
            registry = self.service.registry
            cond = registry.normalise(legacy_condition(old))
            adapter = registry.adapter(cond.kind)
            try:
                # An elapsed, previously admitted timer is valid migration
                # evidence. Only fresh timer registration has a future bound.
                if cond.kind == 'timer' and cond.parameters['due_at'] <= now:
                    from src.waits.adapters.timer import TimerAdapter

                    evidence = await TimerAdapter(clock=lambda: created).verify(conn, cond, scope)
                else:
                    evidence = await registry.verify(conn, cond, scope)
            except WaitError as exc:
                if exc.code != 'wait.source_unresolved' or old['state'] != 'active':
                    raise
                await self.db._finish_agent_wait(conn, old, WaitResolution(
                    'satisfied', None, {'reason': 'source_unavailable'},
                ), now=now)
                return {'success': True, 'legacy_id': legacy_id, 'source_unavailable': True}
            cond = evidence.condition
            decision = decide_sleep(measured_inputs(
                cond, evidence, policy=policy, medians=await measurement_medians(conn, scope.project_id, now=now),
                pressure=getattr(self.orch, '_wait_slot_pressure', {}).get(
                    (scope.project_id, task['profile_id'] if task else None), 0),
                anchor=created, now=now,
            ))
            if decision.mode == 'hibernate' and (not task or not policy.hibernation_enabled):
                raise WaitError('wait.migration_hold', 'legacy owner cannot safely hibernate')
            if await conn.scalar(select(wait_records.c.id).where(
                wait_records.c.owner_kind == scope.owner_kind,
                wait_records.c.owner_id == scope.owner_id,
                wait_records.c.origin != 'scheduler', wait_records.c.cleared_at.is_(None),
            )):
                raise WaitError('wait.active_exists', 'owner already has a verified wait')
            edges = adapter.edges(cond, NodeKey(f'{scope.owner_kind}:{scope.owner_id}',
                                                scope.project_id))
            if await would_create_cycle(conn, edges):
                raise WaitError('wait.cycle', 'legacy source introduces a wait cycle')
            wait_id = str(uuid.uuid4())
            ceiling = created + policy.in_place_ceiling_seconds if decision.mode == 'in_place' else None
            receipt = {'success': True, 'wait_id': wait_id, 'condition': cond.as_dict(),
                       'escalate_at': expires, 'ceiling_at': ceiling,
                       'decision': {'mode': decision.mode, 'reason': decision.reason,
                                    'inputs': dict(decision.inputs)}}
            inputs = {**decision.inputs, 'registration_receipt': receipt,
                      'legacy_created_at': created, 'legacy_deadline_at': expires,
                      'legacy_terminal': {'state': old['state'], 'digest': old.get('digest'),
                                          'result_ref': old.get('result_ref')},
                      'delivery': {'instance_token': scope.instance_token,
                                   'held_cursor_seq': int(await conn.scalar(select(
                                       func.max(messages.c.created_seq))) or 0)}}
            values = dict(
                id=wait_id, project_id=scope.project_id, owner_kind=scope.owner_kind,
                owner_id=scope.owner_id, origin='agent', session_id=scope.session_id,
                claim_epoch=scope.claim_epoch, condition_kind=cond.kind,
                condition=cond.as_record(), source_key=evidence.source.identity,
                source_generation=evidence.source.generation, source_watermark_seq=watermark,
                verification_state='verified', evidence_ref=evidence.evidence_ref,
                verified_at=now, created_at=created, escalate_at=expires,
                due_at=cond.parameters.get('due_at'), ceiling_anchor_at=created if ceiling else None,
                sleep_decision=decision.mode, decision_reason=decision.reason,
                decision_inputs=inputs, legacy_kind='agent_wait', legacy_id=legacy_id,
                idempotency_key=f'legacy:{legacy_id}',
                request_hash=hashlib.sha256(canonical(legacy_condition(old)).encode()).hexdigest(),
                blocking=True,
            )
            row = dict((await conn.execute(insert(wait_records).values(**values).returning(
                wait_records,
            ))).mappings().one())
            for kind, due in (('escalation', expires), ('ceiling', ceiling),
                              ('due', values['due_at'])):
                if due is not None:
                    deadline_id = str(uuid.uuid4())
                    await conn.execute(insert(wait_deadlines).values(
                        id=deadline_id, wait_id=wait_id, deadline_type=kind, due_at=due, generation=1,
                    ))
                    await conn.execute(select(func.pg_notify('aq_wait_deadline', deadline_id)))
            await add_edges(conn, wait_id, edges,
                            source_revision=evidence.observation.revision, now=now)
            continuation = None
            if decision.mode == 'hibernate':
                continuation = await begin_suspension(
                    conn, row, task, now=now, budget=policy.transition_budget_seconds,
                )
                await insert_message_with_source_event(conn, insert(messages).values(
                    id=f"wait:{wait_id}:suspend:{continuation['suspension_generation']}",
                    project_id=scope.project_id, from_kind='system', from_id='wait-service',
                    to_kind='session', to_id=scope.session_id, body_kind='wait_suspend',
                    created_at=now, body=json.dumps({
                        'wait_id': wait_id, 'continuation_id': continuation['id'],
                        'generation': continuation['suspension_generation'],
                        'instruction': 'Submit aq wait checkpoint, then end your turn now.',
                    }),
                ))
            elif task:
                await execute_task_status_mutation(conn, update(tasks).where(
                    tasks.c.id == task['id'], tasks.c.status == 'IN_PROGRESS',
                ).values(status='WAITING', updated_at=now))
            await self.service.lock_source(conn, cond)
            observed = await registry.observe(conn, cond)
            if old['state'] == 'satisfied' and observed.state == 'unsatisfied':
                raise WaitError('wait.migration_hold', 'legacy outcome lacks current source proof')
            legacy_cancelled = old['state'] in {'expired', 'cancelled'}
            if observed.state != 'unsatisfied' or legacy_cancelled:
                await self.service.dispatcher.finish(
                    conn, row, outcome='cancelled' if legacy_cancelled else
                    'satisfied' if observed.state == 'satisfied'
                    else 'source_unavailable', result_ref=observed.result_ref,
                    event={}, task=task, continuation=continuation,
                    changed_keys={f'{scope.owner_kind}:{scope.owner_id}'},
                )
                # Only archive the exact v1 outbox after its replacement commits
                # in this same transaction. Message text is never a source proof.
                if old['result_message_id']:
                    await conn.execute(update(messages).where(
                        messages.c.id == old['result_message_id'],
                        messages.c.body_kind == 'wait_result',
                        messages.c.delivered_at.is_(None),
                    ).values(archived_at=now))
            await recompute_from(conn, {f'{scope.owner_kind}:{scope.owner_id}'}, now=now)
            return receipt
