"""Resumable guarded migration passes and read-only previews."""

import json

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import (
    agent_waits, doc_reviews, escalations, messages, playbook_artifacts, playbook_v2_runs,
    playbook_waits, projects, tasks, wait_bridge_progress,
)
from src.wait_runtime.legacy_human import adopt_waiting_input
from src.wait_runtime.legacy_projection import project_condition, project_escalation
from src.waits.conditions import WaitError
from src.waits.decision import project_policy
from src.waits.graph import lock_graph


async def candidates(conn, project_id, mode):
    items = []
    if mode == 'agent-waits':
        rows = (await conn.execute(select(agent_waits).where(
            agent_waits.c.project_id == project_id,
            or_(agent_waits.c.state == 'active', exists(select(messages.c.id).where(
                messages.c.id == agent_waits.c.result_message_id,
                messages.c.delivered_at.is_(None), messages.c.archived_at.is_(None)))),
        ))).mappings()
        items.extend({'kind': 'agent_wait', 'id': row['id']} for row in rows)
        items.extend({'kind': 'waiting_input', 'id': tid} for tid in (await conn.execute(
            select(tasks.c.id).where(tasks.c.project_id == project_id,
                                     tasks.c.status == 'WAITING_INPUT'))).scalars())
        return sorted(items, key=lambda i: (i['kind'], i['id']))
    items.extend({'kind': 'escalation', 'id': ident} for ident in (await conn.execute(
        select(escalations.c.id).where(escalations.c.project_id == project_id,
            escalations.c.source_kind.in_(('question', 'gate')),
            escalations.c.state.in_(('needs_human', 'reply_received', 'resolving'))))).scalars())
    reviews = (await conn.execute(select(doc_reviews).join(tasks, and_(
        tasks.c.id == doc_reviews.c.author_task_id, tasks.c.project_id == project_id,
    )).where(doc_reviews.c.project_id == project_id, doc_reviews.c.state == 'in_review',
             tasks.c.status.not_in(('COMPLETED', 'FAILED'))))).mappings()
    for row in reviews:
        items.append({'kind': 'review', 'id': row['id'], 'created_at': row['created_at'],
                      'condition': {'kind': 'human', 'subject': 'review', 'review_id': row['id'],
                                    'revision': row['current_revision']}})
    for row in (await conn.execute(select(tasks).where(tasks.c.project_id == project_id,
            tasks.c.status == 'PAUSED', tasks.c.resume_after.is_not(None)))).mappings():
        items.append({'kind': 'timed_pause', 'id': row['id'], 'created_at': row['updated_at'],
                      'condition': {'kind': 'timer', 'due_at': row['resume_after']}})
    rows = (await conn.execute(select(playbook_waits, playbook_v2_runs.c.snapshot,
        playbook_artifacts.c.scope, playbook_artifacts.c.scope_identifier).join(
        playbook_v2_runs, playbook_v2_runs.c.run_id == playbook_waits.c.run_id).join(
        playbook_artifacts,
        playbook_artifacts.c.artifact_sha256 == playbook_v2_runs.c.artifact_sha256,
    ).where(playbook_waits.c.state == 'active'))).mappings()
    for row in rows:
        try:
            snapshot = json.loads(row['snapshot'])
            context = snapshot.get('context', {}) if isinstance(snapshot, dict) else {}
            if not isinstance(context, dict):
                context = {}
        except (ValueError, TypeError):
            # Project-scoped artifacts still provide a trustworthy audience;
            # the source adapter will retain the malformed binding as a hold.
            context = {}
        pid = row['scope_identifier'] if row['scope'] == 'project' else (
            context.get('project_id') or context.get('event', {}).get('project_id'))
        if pid == project_id:
            items.append({'kind': 'playbook_wait', 'id': row['wait_id'],
                          'created_at': row['created_at'], 'condition': {
                              'kind': 'playbook_condition', 'run_id': row['run_id'],
                              'wait_id': row['wait_id']}})
    return sorted(items, key=lambda i: (i['kind'], i['id']))


async def receipt(conn, project_id, phase, identity, state, evidence, now):
    values = dict(project_id=project_id, phase=phase, identity=identity, state=state,
                  evidence=evidence, updated_at=now)
    await conn.execute(insert(wait_bridge_progress).values(**values).on_conflict_do_update(
        index_elements=['project_id', 'phase', 'identity'],
        set_={key: values[key] for key in ('state', 'evidence', 'updated_at')},
    ))


async def run_pass(bridge, args, *, principal, scope=None, handler=None):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind

    if principal is None or principal.kind not in {PrincipalKind.SERVICE, PrincipalKind.LOCAL}:
        raise WaitError('wait.out_of_scope', 'legacy migration requires operator or service')
    mode = args.get('mode', 'agent-waits')
    if mode not in {'project', 'agent-waits'}:
        raise WaitError('wait.invalid', 'unknown bridge mode')
    async with bridge.db._engine.connect() as conn:
        pids = list((await conn.execute(select(projects.c.id).where(
            projects.c.id == args['project_id'] if args.get('project_id') else True,
        ))).scalars())
        plans = {pid: await candidates(conn, pid, mode) for pid in pids}
    if not args.get('apply', False):
        return {'success': True, 'result': {'mode': mode, 'apply': False, 'projects': plans}}
    reports = {}
    for pid, items in plans.items():
        if not project_policy(bridge.orch.config.wait_policy, pid).v2_enabled:
            reports[pid] = {'state': 'disabled'}
            continue
        results = []
        for item in items:
            try:
                if item['kind'] == 'agent_wait':
                    result = await bridge.adopt_agent_wait(item['id'], handler=handler)
                elif item['kind'] == 'waiting_input':
                    result = await adopt_waiting_input(bridge, item['id'])
                elif item['kind'] == 'escalation':
                    result = await project_escalation(bridge, item['id'], project_id=pid)
                else:
                    result = await project_condition(bridge, item, project_id=pid)
            except WaitError as exc:
                result = {'success': False, 'error_code': exc.code, 'reason': str(exc)}
            state = 'complete' if result.get('success') and not result.get('hold_reason') else 'held'
            async with bridge.db._engine.begin() as conn:
                await receipt(conn, pid, mode, f"{item['kind']}:{item['id']}", state,
                              result, bridge.clock())
            results.append({**item, 'state': state, 'result': result})
        # The scheduler owns all verdicts and reservations. No supplied blocker
        # list is accepted as proof, and this bridge never calls a pool allocator.
        try:
            audit = await bridge.service.scheduler.sync(
                {'project_id': pid, 'cycle_id': 'legacy-first-audit', 'blockers': [],
                 'initial_audit': True}, principal=ExecutionPrincipal.service('wait-legacy-bridge'),
                handler=handler,
            )
        except WaitError as exc:
            audit = {'success': False, 'error_code': exc.code}
        async with bridge.db._engine.begin() as conn:
            await lock_graph(conn)
            unfinished = await conn.scalar(select(wait_bridge_progress.c.identity).where(
                wait_bridge_progress.c.project_id == pid, wait_bridge_progress.c.phase == mode,
                wait_bridge_progress.c.identity != '*', wait_bridge_progress.c.state != 'complete',
            ).limit(1))
            current = await candidates(conn, pid, mode)
            known = {(i['kind'], i['id']) for i in items}
            new = any((i['kind'], i['id']) not in known for i in current)
            complete = not unfinished and not new and audit.get('success') is True
            await receipt(conn, pid, mode, '*', 'complete' if complete else 'held',
                          {'audit': audit, 'new_candidates': new}, bridge.clock())
        reports[pid] = {'state': 'complete' if complete else 'held', 'items': results, 'audit': audit}
    return {'success': True, 'result': {'mode': mode, 'apply': True, 'projects': reports}}
