"""Read-only escalation projection through the canonical human outcome path."""

import hashlib
import uuid

from sqlalchemy import func, insert, select

from src.database.tables import escalations, wait_records
from src.waits.adapters.legacy_escalation import bind
from src.waits.conditions import Scope, WaitError, canonical
from src.waits.decision import project_policy
from src.waits.graph import lock_graph


async def project_escalation(bridge, escalation_id, *, project_id):
    watermark = await bridge.db.wait_source_watermark()
    now = bridge.clock()
    async with bridge.db._engine.begin() as conn:
        await lock_graph(conn)
        source = (await conn.execute(select(escalations).where(
            escalations.c.id == escalation_id,
            escalations.c.project_id == project_id,
        ).with_for_update(read=True))).mappings().first()
        if not source:
            raise WaitError('wait.source_unresolved', 'escalation binding is missing or outside scope')
        if not project_policy(bridge.orch.config.wait_policy, project_id).v2_enabled:
            raise WaitError('wait.disabled', 'verified waits are disabled')
        existing = (await conn.execute(select(wait_records).where(
            wait_records.c.legacy_kind == 'escalation',
            wait_records.c.legacy_id == escalation_id,
        ))).mappings().first()
        if existing:
            return {'success': True, 'wait_id': existing['id'], 'replayed': True,
                    'hold_reason': (existing['decision_inputs'] or {}).get('migration_hold')}
        cond = bind(source, watermark=watermark)
        scope = Scope(project_id, 'integration', f'legacy-escalation:{escalation_id}',
                      principal='service')
        adapter = bridge.service.registry.adapter('human')
        # Cancelled/stale sources retain visible unavailable bindings, never a
        # fabricated approval. Missing provenance remains a migration error.
        unavailable = source['state'] in {'cancelled', 'stale'}
        if unavailable:
            cond = await adapter.authorise(conn, cond, scope)
            evidence_ref = f"escalation:{escalation_id}@{source['revision']}:unavailable"
        else:
            evidence = await adapter.verify(conn, cond, scope)
            cond, evidence_ref = evidence.condition, evidence.evidence_ref
        source_key = adapter.source_key(cond)
        wait = dict((await conn.execute(insert(wait_records).values(
            id=str(uuid.uuid4()), project_id=project_id, owner_kind='integration',
            owner_id=scope.owner_id, origin='human', condition_kind='human',
            condition=cond.as_record(), source_key=source_key.identity,
            source_generation=source_key.generation, source_watermark_seq=watermark,
            verification_state='unavailable' if unavailable else 'verified',
            evidence_ref=evidence_ref, verified_at=now, created_at=source['created_at'],
            decision_inputs={'migration_hold': 'source_unavailable' if unavailable else None},
            legacy_kind='escalation', legacy_id=escalation_id,
            request_hash=hashlib.sha256(canonical(cond.as_record()).encode()).hexdigest(),
        ).returning(wait_records))).mappings().one())
        observed = await adapter.observe(conn, cond)
        if not unavailable and observed.state == 'satisfied':
            await bridge.service.dispatcher.finish(
                conn, wait, outcome='satisfied', result_ref=observed.result_ref,
                event={}, task=None, continuation=None, changed_keys={scope.owner_id},
            )
        return {'success': True, 'wait_id': wait['id'],
                'hold_reason': 'source_unavailable' if unavailable else None}


async def project_condition(bridge, item, *, project_id):
    """Project a service-owned review, playbook suspension or timed pause."""
    from src.database.tables import wait_deadlines
    from src.waits.adapters.base import Adapter
    from src.waits.conditions import Condition

    watermark = await bridge.db.wait_source_watermark()
    now = bridge.clock()
    async with bridge.db._engine.begin() as conn:
        await lock_graph(conn)
        if item['kind'] == 'timed_pause':
            from src.database.tables import tasks

            source_task = (await conn.execute(select(tasks).where(
                tasks.c.id == item['id'], tasks.c.project_id == project_id,
            ).with_for_update())).mappings().first()
            if (not source_task or source_task['status'] != 'PAUSED'
                    or source_task['resume_after'] != item['condition']['due_at']):
                raise WaitError('wait.migration_hold', 'timed pause source changed')
        old = (await conn.execute(select(wait_records).where(
            wait_records.c.legacy_kind == item['kind'], wait_records.c.legacy_id == item['id'],
        ))).mappings().first()
        if old:
            return {'success': True, 'wait_id': old['id'], 'replayed': True,
                    'hold_reason': (old['decision_inputs'] or {}).get('migration_hold')}
        if not project_policy(bridge.orch.config.wait_policy, project_id).v2_enabled:
            raise WaitError('wait.disabled', 'verified waits are disabled')
        owner = f"legacy-{item['kind']}:{item['id']}"
        scope = Scope(project_id, 'integration', owner, principal='service')
        registry = bridge.service.registry
        cond = registry.normalise(item['condition'])
        await bridge.service.lock_source(conn, cond)
        hold = None
        try:
            if cond.kind == 'timer':
                # These are persisted instants, not fresh client timer requests.
                cond = await Adapter.authorise(registry.adapter('timer'), conn, cond, scope)
                cond = cond.bind(generation=0, source_class='timer')
                source = registry.adapter('timer').source_key(cond)
                evidence_ref = f'{source.identity}@0'
            else:
                evidence = await registry.verify(conn, cond, scope)
                cond, source, evidence_ref = evidence.condition, evidence.source, evidence.evidence_ref
        except (WaitError, ValueError, TypeError) as exc:
            if getattr(exc, 'code', None) == 'wait.out_of_scope':
                raise
            hold = getattr(exc, 'code', 'wait.source_unresolved')
            cond = Condition(cond.kind, cond.parameters, cond.generation or 0).bind(
                generation=cond.generation or 0, project_id=project_id, owner_kind='integration',
                owner_id=owner, session_id=None, claim_epoch=None,
                source_class=f"legacy:{item['kind']}",
            )
            source = registry.adapter(cond.kind).source_key(cond)
            evidence_ref = f'{source.identity}:unavailable'
        row = dict((await conn.execute(insert(wait_records).values(
            id=str(uuid.uuid4()), project_id=project_id, owner_kind='integration', owner_id=owner,
            origin='human' if cond.kind == 'human' else 'playbook' if cond.kind == 'playbook_condition'
            else 'integration', condition_kind=cond.kind, condition=cond.as_record(),
            source_key=source.identity, source_generation=source.generation,
            source_watermark_seq=watermark, verification_state='unavailable' if hold else 'verified',
            evidence_ref=evidence_ref, verified_at=now, created_at=item['created_at'],
            due_at=cond.parameters.get('due_at'), legacy_kind=item['kind'], legacy_id=item['id'],
            decision_inputs={'migration_hold': hold},
            request_hash=hashlib.sha256(canonical(cond.as_record()).encode()).hexdigest(),
        ).returning(wait_records))).mappings().one())
        if not hold:
            observed = await registry.observe(conn, cond)
            if observed.state != 'unsatisfied':
                await bridge.service.dispatcher.finish(
                    conn, row, outcome='satisfied' if observed.state == 'satisfied'
                    else 'source_unavailable', result_ref=observed.result_ref, event={},
                    task=None, continuation=None, changed_keys={owner},
                )
            elif cond.kind == 'timer':
                deadline_id = str(uuid.uuid4())
                await conn.execute(insert(wait_deadlines).values(
                    id=deadline_id, wait_id=row['id'], deadline_type='due',
                    due_at=cond.parameters['due_at'], generation=1,
                ))
                await conn.execute(select(func.pg_notify('aq_wait_deadline', deadline_id)))
        return {'success': True, 'wait_id': row['id'], 'hold_reason': hold}
