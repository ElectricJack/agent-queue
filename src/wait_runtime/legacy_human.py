"""Bridge-only adoption of legacy WAITING_INPUT tasks.

Ordinary registration and stop guards are unchanged. A task without a live
writer is held for checkpoint verification, never given a fabricated stopped
session or published head. The migration report exposes that repair obligation.
"""

import hashlib
import uuid

from sqlalchemy import func, insert, select, update

from src.database.queries.wait_queries import verify_owner
from src.database.tables import agents, continuations, messages, sessions, tasks, wait_records
from src.wait_runtime.hibernate import begin_suspension
from src.waits.conditions import Scope, WaitError, canonical
from src.waits.decision import project_policy
from src.waits.graph import lock_graph
from src.waits.sources import execute_task_status_mutation


async def adopt_waiting_input(bridge, task_id):
    from src.wait_runtime.legacy_parking import checkpoint_proof, current_proof

    proof = await checkpoint_proof(bridge, task_id)
    watermark = await bridge.db.wait_source_watermark()
    now = bridge.clock()
    async with bridge.db._engine.begin() as conn:
        await lock_graph(conn)
        # Session before task follows the legacy claim/reconciler ordering.
        live = [dict(row) for row in (await conn.execute(select(sessions).where(
            sessions.c.task_id == task_id,
            sessions.c.state.not_in(('stopped', 'failed')),
        ).order_by(sessions.c.id).with_for_update())).mappings()]
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == task_id,
        ).with_for_update())).mappings().first()
        if not task:
            raise WaitError('wait.not_found', 'legacy task disappeared')
        task = dict(task)
        old = (await conn.execute(select(wait_records).where(
            wait_records.c.legacy_kind == 'waiting_input', wait_records.c.legacy_id == task_id,
        ))).mappings().first()
        if old:
            return {'success': True, 'wait_id': old['id'], 'replayed': True,
                    'hold_reason': old['decision_inputs'].get('migration_hold')}
        policy = project_policy(bridge.orch.config.wait_policy, task['project_id'])
        if not policy.v2_enabled or not policy.hibernation_enabled:
            raise WaitError('wait.disabled', 'human migration requires verified hibernation')
        if task['status'] != 'WAITING_INPUT':
            raise WaitError('wait.migration_hold', 'task is no longer WAITING_INPUT')
        if not task['branch_name']:
            raise WaitError('wait.migration_hold', 'legacy task has no known branch')
        if len(live) > 1:
            raise WaitError('wait.migration_hold', 'multiple possible legacy writers')
        session = live[0] if live else None
        if session:
            # Rollback restores WAITING_INPUT if any subsequent fence fails.
            await execute_task_status_mutation(conn, update(tasks).where(
                tasks.c.id == task_id, tasks.c.status == 'WAITING_INPUT',
            ).values(status='WAITING', updated_at=now))
            scope = Scope(task['project_id'], 'task', task_id, session['id'],
                          session['instance_token'], task['claim_epoch'])
            await verify_owner(conn, scope)
        else:
            claimant = await conn.scalar(select(agents.c.id).where(
                agents.c.current_task_id == task_id,
            ).limit(1))
            if task['assigned_agent_id'] is not None or claimant:
                raise WaitError('wait.migration_hold', 'sessionless task still has a claimant')
            scope = Scope(task['project_id'], 'task', task_id,
                          claim_epoch=task['claim_epoch'], principal='service')
        if await conn.scalar(select(wait_records.c.id).where(
            wait_records.c.owner_kind == 'task', wait_records.c.owner_id == task_id,
            wait_records.c.origin != 'scheduler', wait_records.c.cleared_at.is_(None),
        )):
            raise WaitError('wait.active_exists', 'legacy task already has an execution wait')
        # Only a server-read question can supply a historical cursor. A caller
        # cannot provide an after_at override through ordinary human registration.
        question = (await conn.execute(select(messages).where(
            messages.c.project_id == task['project_id'], messages.c.to_kind == 'user',
            messages.c.from_kind == 'session', messages.c.from_id.in_(
                select(sessions.c.id).where(sessions.c.task_id == task_id)),
        ).order_by(messages.c.created_seq.desc()).limit(1))).mappings().first()
        raw = {'kind': 'human', 'subject': 'task_comment', 'task_id': task_id}
        if question and question['thread_id']:
            raw = {'kind': 'human', 'subject': 'message_thread',
                   'thread_id': question['thread_id'], 'after_seq': question['created_seq']}
            # Participation is bound to the actual historical sender, never an
            # invented session; execution ownership remains separately fenced.
            from dataclasses import replace
            source_scope = replace(scope, session_id=question['from_id']
                                   if question['from_kind'] == 'session' else scope.session_id)
        else:
            source_scope = scope
        evidence = await bridge.service.registry.verify(
            conn, bridge.service.registry.normalise(raw), source_scope,
        )
        condition = evidence.condition
        anchor = question['created_at'] if question else task['updated_at']
        if raw['subject'] == 'task_comment':
            condition = condition.bind(
                generation=condition.generation, after_at=anchor,
                task_created_at=task['created_at'], source_watermark_seq=watermark,
            )
        wait_id = str(uuid.uuid4())
        held_cursor = int(await conn.scalar(select(func.max(messages.c.created_seq))) or 0)
        verified_park = not session and await current_proof(conn, task, proof)
        hold = None if session or verified_park else 'legacy_checkpoint_unverified'
        if question is None and hold is None:
            hold = 'legacy_question_cursor_unverified'
        inputs = {'migration': {'kind': 'waiting_input', 'id': task_id,
                                'question_id': question['id'] if question else None,
                                'after_at': anchor, 'sessionless': session is None},
                  'delivery': {'instance_token': session['instance_token'] if session else None,
                               'held_cursor_seq': held_cursor},
                  'registration_receipt': {'success': True, 'wait_id': wait_id,
                                           'decision': {'mode': 'hibernate', 'reason': 'human'},
                                           'escalate_at': None},
                  'migration_hold': hold}
        wait = dict((await conn.execute(insert(wait_records).values(
            id=wait_id, project_id=task['project_id'], owner_kind='task', owner_id=task_id,
            origin='human', session_id=session['id'] if session else None,
            claim_epoch=task['claim_epoch'], condition_kind='human', condition=condition.as_record(),
            source_key=evidence.source.identity, source_generation=evidence.source.generation,
            source_watermark_seq=watermark,
            verification_state='verified' if question else 'unavailable',
            evidence_ref=evidence.evidence_ref, verified_at=now, created_at=anchor,
            sleep_decision='hibernate', decision_reason='human', decision_inputs=inputs,
            legacy_kind='waiting_input', legacy_id=task_id, blocking=True,
            idempotency_key=f'waiting-input:{task_id}',
            request_hash=hashlib.sha256(canonical(raw).encode()).hexdigest(),
        ).returning(wait_records))).mappings().one())
        facts = {'migration': inputs['migration'], 'task_id': task_id,
                 'branch': task['branch_name'], 'prior_status': 'WAITING_INPUT'}
        if session:
            intent = await begin_suspension(conn, wait, task, now=now,
                                            budget=policy.transition_budget_seconds)
            await conn.execute(update(continuations).where(
                continuations.c.id == intent['id'],
            ).values(summary_degraded=True, facts=facts,
                     **({'resume_state': 'held', 'hold_reason': hold} if hold else {})))
        else:
            generation = 1 + (await conn.scalar(select(func.max(
                continuations.c.suspension_generation,
            )).where(continuations.c.task_id == task_id)) or 0)
            intent = dict((await conn.execute(insert(continuations).values(
                id=str(uuid.uuid4()), task_id=task_id, wait_id=wait_id,
                suspension_generation=generation, summary_degraded=True, facts=facts,
                branch=task['branch_name'], prior_status='WAITING_INPUT',
                saved_head=proof['head'] if verified_park else None,
                held_cursor_seq=held_cursor, resume_state='sleeping' if not hold else 'held',
                hold_reason=hold, created_at=now,
            ).returning(continuations))).mappings().one())
            from src.wait_runtime.legacy_pass import receipt

            await receipt(conn, task['project_id'], 'sessionless', intent['id'],
                          'complete' if not hold else 'held',
                          {**(proof or {}), 'wait_id': wait_id,
                           'suspension_generation': generation, 'hold_reason': hold}, now)
            await execute_task_status_mutation(conn, update(tasks).where(
                tasks.c.id == task_id, tasks.c.status == 'WAITING_INPUT',
            ).values(status='HIBERNATED', updated_at=now))
        # A verified human response that already exists is re-observed by the
        # owning adapter. Its outcome can commit while a live writer retires;
        # a sessionless migration hold needs checkpoint repair before delivery.
        await bridge.service.lock_source(conn, condition)
        observed = await bridge.service.registry.observe(conn, condition)
        if observed.state != 'unsatisfied' and not hold and (session or verified_park):
            await bridge.service.dispatcher.finish(
                conn, wait, outcome='satisfied' if observed.state == 'satisfied'
                else 'source_unavailable', result_ref=observed.result_ref,
                event={}, task=task, continuation=intent, changed_keys={f'task:{task_id}'},
            )
        return {'success': True, 'wait_id': wait_id, 'continuation_id': intent['id'],
                'hold_reason': hold}
