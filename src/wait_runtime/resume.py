"""Reserve common capacity and launch a fresh continuation through task execution."""

import errno
import json
import time
import uuid
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from sqlalchemy import insert, select, update

from src.database.queries.continuation_queries import wait_outcome_envelope
from src.database.tables import (
    admission_reservations, agents, continuations, escalations, events, messages, provider_availability,
    tasks, sessions, workspaces,
)
from src.models import Agent, AgentState
from src.scheduler import AssignAction
from src.waits.conditions import WaitError
from src.waits.admission import AdmissionReader

from src.waits.graph import background_graph_work, lock_graph
from src.wait_runtime.hibernate import locked_suspension, suspension_writer
from src.waits.mail import held_messages
from src.waits.resume_ack import bind_live_claim
from src.waits.sources import insert_message_with_source_event
from src.wait_runtime.legacy_parking import parked


WORKSPACE_ATTEMPTS = 3
WORKSPACE_CAPACITY_REASONS = frozenset({'slots_full', 'slot_warming', 'slot_lost_race'})
WORKSPACE_CAPACITY_BACKOFF_MAX = 60
# Outlast one listener batch's 30-second lease budget, including its handoff.
RESUME_GRAPH_LOCK_TIMEOUT = 35.0
# (base, ceiling) seconds between reservation attempts after a refusal. A
# capacity wait looks again soon; a policy, provider or budget hold rarely clears.
RESERVE_CAPACITY_BACKOFF = (5, 30)
RESERVE_HOLD_BACKOFF = (10, 300)


def retryable_workspace_error(exc):
    """Retry contention and interrupted transport, never checkout identity failures."""
    if isinstance(exc, WaitError):
        return exc.code == 'wait.graph_busy'
    return (isinstance(exc, (TimeoutError, ConnectionError))
            or isinstance(exc, OSError) and exc.errno in {
                errno.EAGAIN, errno.EBUSY, errno.ETIMEDOUT, errno.ECONNRESET,
            })


async def verify_saved_branch(orch, task, path):
    """Refuse drift before ordinary preparation can switch or reset anything."""
    async with orch.db._engine.connect() as conn:
        intent = (await conn.execute(select(continuations).where(
            continuations.c.task_id == task.id, continuations.c.resume_state == 'reserved',
        ))).mappings().one()
    project = await orch.db.get_project(task.project_id)
    repository = await orch.db.get_repo(task.repo_id or project.integration_repository_id)
    if not repository or not intent['saved_head'] or task.branch_name != intent['branch']:
        raise WaitError('continuation.head_drift', 'saved branch binding is unavailable')
    await orch.git.afetch_origin(path, repository_url=repository.url)
    for ref in (f"refs/heads/{intent['branch']}", f"refs/remotes/origin/{intent['branch']}"):
        await orch.git._arun(['merge-base', '--is-ancestor', intent['saved_head'], ref], cwd=path)


async def capacity_request(conn, task, intent, observed, generation, *, now, budget):
    """The continuation's durable admission request, created once per attempt.

    A blocked reserve records when resume first asked for capacity; retries
    reuse that row, so slot accounting can split condition from capacity time.
    """
    scope = {**observed.binding['scope'], 'generation': generation}
    row = (await conn.execute(select(admission_reservations).where(
        admission_reservations.c.continuation_id == intent['id'],
        admission_reservations.c.state.in_(('pending', 'overdue', 'granted')),
    ).with_for_update())).mappings().first()
    if row is None:
        row = (await conn.execute(insert(admission_reservations).values(
            id=str(uuid.uuid4()), project_id=task['project_id'], continuation_id=intent['id'],
            scope=scope, effective_priority=task['effective_priority'], provenance='resume',
            deadline_at=now + budget, state='pending', created_at=now,
        ).returning(admission_reservations))).mappings().one()
    elif row['scope'] != scope or row['effective_priority'] != task['effective_priority']:
        await conn.execute(update(admission_reservations).where(
            admission_reservations.c.id == row['id'],
        ).values(scope=scope, effective_priority=task['effective_priority']))
    return dict(row)


class ResumeTransaction:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.orch = service.orch
        self.db = self.orch.db
        self.clock = clock

    @background_graph_work()
    async def reserve(self, continuation_id, generation):
        """Replace inherited admission with fresh evidence for this task alone."""
        async with self.db._engine.connect() as conn:
            task_id = await conn.scalar(select(continuations.c.task_id).where(
                continuations.c.id == continuation_id, continuations.c.resume_state == 'queued'))
        reader = getattr(getattr(self.service, 'scheduler', None), 'reader', None)
        async with (reader.prepared(task_ids=[task_id], cached_only=False)
                    if task_id and isinstance(reader, AdmissionReader) else nullcontext()):
            return await self._reserve(continuation_id, generation)

    async def _reserve(self, continuation_id, generation):
        async with self.db._engine.begin() as conn:
            # Join PostgreSQL's lock queue before transaction reads. A listener
            # draining a backlog must hand off to this waiter between batches.
            await lock_graph(conn, queued=True, timeout=RESUME_GRAPH_LOCK_TIMEOUT)
            wait_id = await conn.scalar(select(continuations.c.wait_id).where(
                continuations.c.id == continuation_id,
            ))
            task, wait, intent = await locked_suspension(conn, wait_id)
            if intent['id'] != continuation_id or intent['suspension_generation'] != generation:
                raise WaitError('continuation.stale_generation', 'resume generation changed')
            if intent['facts'].get('workspace_capacity', {}).get('due_at', 0) > self.clock():
                return None
            if intent['resume_state'] == 'reserved' and task['status'] == 'RESUME_PENDING':
                return AssignAction(task['assigned_agent_id'], task['id'], task['project_id'],
                                    continuation_id=intent['id'], generation=generation)
            if (intent['resume_state'] != 'queued' or task['status'] != 'RESUME_PENDING'
                    or wait['outcome'] == 'pending'
                    or not await parked(conn, task, wait, intent)):
                return None
            observed = await self.service.scheduler.reader.observe_task(
                conn, task['id'], continuation_id=continuation_id,
            )
            if observed is None:
                return None
            blocked = {key for key, value in observed.constraints.items() if value['blocked']}
            now = self.clock()
            budget = self.orch.config.wait_policy.transition_budget_seconds
            request = None
            if blocked <= {'slot', 'workspace'}:
                # Persist demand before capacity is available; other blockers
                # (provider, quarantine) hold no place in the capacity queue.
                request = await capacity_request(conn, task, intent, observed, generation,
                                                 now=now, budget=budget)
            if blocked:
                if blocked <= {'slot', 'workspace'}:
                    # A continuation requires a new process. Retire an unused
                    # pool process through reconciliation; it still occupies
                    # capacity until the provider has actually stopped it.
                    idle = await conn.scalar(select(sessions.c.id).where(
                        sessions.c.project_id == task['project_id'],
                        sessions.c.profile_id == task['profile_id'],
                        sessions.c.lifecycle == 'pool', sessions.c.state == 'running',
                        sessions.c.desired_state == 'running', sessions.c.task_id.is_(None),
                        sessions.c.claim_phase.is_(None),
                    ).order_by(sessions.c.started_at).with_for_update(skip_locked=True).limit(1))
                    if idle:
                        await conn.execute(update(sessions).where(sessions.c.id == idle).values(
                            desired_state='stopped'))
                return await self._back_off(conn, intent, blocked, now)
            agent = Agent(id=str(uuid.uuid4()), name=f"resume-{continuation_id}",
                          profile_id=task['profile_id'], state=AgentState.BUSY,
                          current_task_id=task['id'], last_heartbeat=now,
                          intelligence_class=task['intelligence_class'])
            await self.db._insert_agent_on(conn, agent)
            # The grant keeps the first request's created_at and FIFO position.
            await conn.execute(update(admission_reservations).where(
                admission_reservations.c.id == request['id'],
            ).values(state='granted', granted_at=request['granted_at'] or now,
                     deadline_at=now + budget))
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                resume_state='reserved', version=intent['version'] + 1,
                launch_attempts=intent['launch_attempts'] + 1,
                facts={**intent['facts'], 'resume_attempt_started_at': now, 'reserve_retry': None},
            ))
            await conn.execute(update(tasks).where(tasks.c.id == task['id']).values(
                assigned_agent_id=agent.id, updated_at=now,
            ))
            return AssignAction(agent.id, task['id'], task['project_id'],
                                continuation_id=intent['id'], generation=generation)

    async def _back_off(self, conn, intent, blocked, now):
        """Record when to look at a refused reservation again, and why it waits.

        Retrying each tick would retake the graph lock only to observe the same
        blocker. ``blocked`` lets occupancy drop a policy, provider or budget
        hold from queued pool demand: only a capacity refusal wants a seat.
        """
        refusals = (intent['facts'].get('reserve_retry') or {}).get('refusals', 0) + 1
        base, ceiling = (RESERVE_CAPACITY_BACKOFF if blocked <= {'slot', 'workspace'}
                         else RESERVE_HOLD_BACKOFF)
        await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
            facts={**intent['facts'], 'reserve_retry': {
                'refusals': refusals, 'due_at': now + min(ceiling, base * 2 ** (refusals - 1)),
                'blocked': sorted(blocked)}}))

    async def fresh_workspace(self, action, task, agent, intent):
        """A newly provisioned directory, even when reusing a numbered slot.

        Acquisition, requirements and branch preparation use the ordinary path.
        The checkout is then rebuilt at a generation-specific path: no cached
        transcripts or ignored files from the previous writer can survive.
        """
        stage = intent['facts'].get('fresh_workspace', {})
        workspace = await self.db.get_workspace_for_task(task.id)
        if (workspace and stage.get('workspace_id') == workspace.id
                and stage.get('attempt') == intent['launch_attempts']
                and Path(stage['new_path']).is_dir()):
            target = stage['new_path']
            git = self.orch.git
            if await git._arun(['branch', '--show-current'], cwd=target) != intent['branch']:
                raise WaitError('continuation.head_drift', 'recovered checkout branch changed')
            await git._arun(['merge-base', '--is-ancestor', intent['saved_head'], 'HEAD'], cwd=target)
            if await git._arun(['status', '--porcelain'], cwd=target):
                raise WaitError('continuation.head_drift', 'recovered checkout is not clean')
            kind = await self.db.resolve_workspace_kind(task.project_id, workspace.kind_id)
            base = await self.db.get_workspace(workspace.base_workspace_id)
            if base is None:
                raise WaitError('wait.unavailable', 'worktree base disappeared')
            await self.db.update_workspace(workspace.id, workspace_path=target)
            self.orch._worktree_base_paths[target] = base.workspace_path
            await self.reprovision_assigned_slot(
                replace(workspace, workspace_path=target), kind, task, intent['branch'],
            )
            # A restart may have landed before the old folder was retired.
            await self.retire_checkout(stage['old_path'], target, base)
            return target
        path = await self.orch._prepare_workspace_locked(task, agent)
        workspace = await self.db.get_workspace_for_task(task.id)
        reason = getattr(self.orch, '_workspace_wait_reasons', {}).get(task.id)
        if not path and workspace is None and reason in WORKSPACE_CAPACITY_REASONS:
            raise WaitError('wait.workspace_capacity', reason)
        if not path or not workspace or not workspace.is_slot:
            raise WaitError('wait.unavailable', 'fresh continuation requires a worktree slot')
        base = await self.db.get_workspace(workspace.base_workspace_id)
        kind = await self.db.resolve_workspace_kind(task.project_id, workspace.kind_id)
        if base is None or kind is None:
            raise WaitError('wait.unavailable', 'worktree base or kind disappeared')
        git = self.orch.git
        # This is deliberately an ancestor check, never a reset to saved_head.
        await git._arun(['merge-base', '--is-ancestor', intent['saved_head'], 'HEAD'], cwd=path)
        if await git._arun(['status', '--porcelain'], cwd=path):
            raise WaitError('continuation.head_drift', 'prepared checkout is not clean')
        target = str(Path(base.workspace_path) / '.aq' / 'worktrees'
                     / f"resume-{intent['id']}-{intent['launch_attempts']}")
        # Persist the destination before touching Git. A restart retains the
        # acquired workspace and can inspect both paths without launching twice.
        async with self.db._engine.begin() as conn:
            _, _, current = await locked_suspension(conn, intent['wait_id'])
            if current['resume_state'] != 'reserved':
                raise WaitError('continuation.stale_generation', 'resume reservation changed')
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                facts={**current['facts'], 'fresh_workspace': {
                    'old_path': path, 'new_path': target, 'workspace_id': workspace.id,
                    'attempt': intent['launch_attempts']}},
            ))
        async with self.orch._git_mutex(base.workspace_path):
            await git._arun_unlocked(['switch', '--detach'], cwd=path)
            await git._arun_unlocked(['worktree', 'add', target, intent['branch']],
                                     cwd=base.workspace_path)
        fresh = replace(workspace, workspace_path=target)
        # Cleanup must see the checkout holding the branch even if setup fails.
        await self.db.update_workspace(workspace.id, workspace_path=target)
        self.orch._worktree_base_paths[target] = base.workspace_path
        # reprovision reuses this *new* checkout and applies the configured
        # setup and ownership sentinel. It never copies an old agent folder.
        await self.reprovision_assigned_slot(fresh, kind, task, intent['branch'])
        await git._arun(['merge-base', '--is-ancestor', intent['saved_head'], 'HEAD'], cwd=target)
        await self.retire_checkout(path, target, base)
        return target

    async def reprovision_assigned_slot(self, workspace, kind, task, branch):
        """Restore the active assignment after reprovision writes a free sentinel."""
        slots = self.orch._worktree_slots()
        await slots.reprovision_slot(workspace, kind, prune=False)
        sentinel = slots.read_sentinel(workspace.workspace_path)
        if sentinel is None:
            raise WaitError('wait.unavailable', 'reprovisioned slot sentinel is missing')
        slots.write_sentinel(workspace.workspace_path, replace(
            sentinel, task_id=task.id, branch=branch, assigned_at=self.clock(),
        ))

    async def retire_checkout(self, old_path, target, base):
        """Remove the superseded detached folder once no row names it.

        The old folder is clean and unowned after the atomic path update;
        ``worktree remove`` without ``--force`` refuses anything else, so a
        failure is harmless retained disk, never a second writer.
        """
        old = Path(old_path)
        if old.resolve() == Path(target).resolve() or not old.is_dir():
            return
        for row in await self.db.list_workspaces():
            if row.workspace_path and Path(row.workspace_path).resolve() == old.resolve():
                return
        try:
            await self.orch.git._arun(['worktree', 'remove', str(old)], cwd=base.workspace_path)
        except Exception:
            pass

    async def dispatch(self, args, *, principal, scope, handler):
        async with self.db._engine.connect() as conn:
            intent = (await conn.execute(select(continuations).where(
                continuations.c.id == args['continuation_id'],
            ))).mappings().first()
        if intent is None:
            raise WaitError('continuation.stale_generation', 'continuation disappeared')
        async with self.orch._task_control_lock(intent['task_id']):
            if intent['resume_state'] == 'launching':
                if intent['suspension_generation'] != args['generation']:
                    raise WaitError('continuation.stale_generation', 'launch generation changed')
                session = await self.db.get_session(intent['new_session_id'])
                start = intent['facts'].get('resume_attempt_started_at', self.clock())
                if session and session.state == 'running':
                    return {'success': True, 'awaiting_ack': True}
                if self.clock() - start < self.orch.config.wait_policy.transition_budget_seconds:
                    return {'success': True, 'launching': True}
                task = await self.db.get_task(intent['task_id'])
                action = AssignAction(task.assigned_agent_id, task.id, task.project_id,
                                      intent['id'], args['generation'])
                return await self.failed(action, task, 'resume launch did not finish', handler=handler)
            action = await self.reserve(args['continuation_id'], args['generation'])
            if action is None:
                return {'success': True, 'dispatched': False}
            task = await self.db.get_task(action.task_id)
            agent = await self.db.get_agent(action.agent_id)
            async with self.db._engine.connect() as conn:
                intent = (await conn.execute(select(continuations).where(
                    continuations.c.id == action.continuation_id,
                ))).mappings().one()
            try:
                workspace = await self.fresh_workspace(action, task, agent, dict(intent))
                async with self.db._engine.begin() as conn:
                    _, _, current = await locked_suspension(conn, intent['wait_id'])
                    facts = {**current['facts'], 'costs': {**current['facts'].get('costs', {}),
                        'setup_seconds': max(0, self.clock() - current['facts'][
                            'resume_attempt_started_at'])}}
                    await conn.execute(update(continuations).where(
                        continuations.c.id == current['id'],
                    ).values(facts=facts))
            except Exception as exc:
                if isinstance(exc, WaitError) and exc.code == 'wait.workspace_capacity':
                    retry_at = await self.workspace_capacity_retry(action, intent['wait_id'], exc)
                    return {'success': True, 'retry_queued': True, 'retry_at': retry_at,
                            'capacity_blocked': True}
                if retry_at := await self.workspace_retry(action, intent['wait_id'], exc):
                    return {'success': True, 'retry_queued': True, 'retry_at': retry_at}
                return await self.failed(action, task, f'workspace preparation: {exc}',
                                         handler=handler, hold_reason='resume_workspace_failed')
            try:
                await self.orch._launch_session_for_task_locked(action, task, None, workspace)
            except Exception as exc:
                return await self.failed(action, task, str(exc), handler=handler)
            return {'success': True, 'dispatched': True}

    async def workspace_capacity_retry(self, action, wait_id, exc):
        """Return an unacquired seat to admission; contention spends no failure budget."""
        async with self.db._engine.begin() as conn:
            task, _, intent = await locked_suspension(conn, wait_id)
            if ((intent['id'], intent['suspension_generation'], intent['resume_state']) != (
                    action.continuation_id, action.generation, 'reserved')
                    or task['status'] != 'RESUME_PENDING'
                    or task['assigned_agent_id'] != action.agent_id
                    or intent['new_session_id'] is not None
                    or await conn.scalar(select(workspaces.c.id).where(
                        workspaces.c.locked_by_task_id == task['id']).limit(1))
                    or await conn.scalar(select(sessions.c.id).where(
                        sessions.c.task_id == task['id'],
                        sessions.c.state.not_in(('stopped', 'failed'))).limit(1))):
                raise WaitError('continuation.stale_generation', 'capacity retry ownership changed')
            previous = intent['facts'].get('workspace_capacity', {})
            attempts = previous.get('attempts', 0) + 1
            now = self.clock()
            # Cap the exponent as well as the delay for arbitrarily long queues.
            retry_at = now + min(WORKSPACE_CAPACITY_BACKOFF_MAX, 5 * 2 ** min(attempts - 1, 4))
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                resume_state='queued', version=intent['version'] + 1,
                launch_attempts=intent['launch_attempts'] - 1,
                facts={**intent['facts'], 'workspace_capacity': {
                    'attempts': attempts, 'first_blocked_at': previous.get('first_blocked_at', now),
                    'due_at': retry_at, 'reason': str(exc)}},
            ))
            await conn.execute(update(admission_reservations).where(
                admission_reservations.c.continuation_id == intent['id'],
                admission_reservations.c.state == 'granted',
            ).values(state='pending', granted_at=None))
            await conn.execute(update(tasks).where(tasks.c.id == task['id']).values(
                assigned_agent_id=None))
            await conn.execute(update(agents).where(agents.c.id == action.agent_id).values(
                state='RETIRED', current_task_id=None))
            return retry_at

    async def workspace_retry(self, action, wait_id, exc):
        """Keep the reservation and checkout for a backoff; None means hold instead."""
        async with self.db._engine.begin() as conn:
            task, _, intent = await locked_suspension(conn, wait_id)
            if (intent['id'], intent['suspension_generation'], intent['resume_state']) != (
                    action.continuation_id, action.generation, 'reserved'):
                return None
            failures = intent['facts'].get('workspace_retry', {}).get('failures', 0) + 1
            retry_at = self.clock() + 5 * 2 ** (failures - 1)
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                facts={**intent['facts'], 'workspace_retry': {
                    'failures': failures, 'due_at': retry_at, 'error': str(exc)}}))
        if task['status'] != 'PAUSED' and failures < WORKSPACE_ATTEMPTS and retryable_workspace_error(exc):
            return retry_at

    async def requeue(self, args, *, principal, scope, handler, incident_id=None):
        """Supervisor repair of a resolved wait's held resume, fenced by generation."""
        async with self.db._engine.begin() as conn:
            # Join the same fair queue as automatic resume. Acquire before any
            # read or mutation so refusal/cancellation leaves the hold intact.
            await lock_graph(conn, queued=True)
            task, wait, intent = await locked_suspension(conn, args['wait_id'])
            if scope.get('project_id') not in (None, task['project_id']):
                raise WaitError('wait.out_of_scope', 'continuation is outside supervisor scope')
            await self.service.decisions.authorize(conn, principal, task['project_id'])
            if (intent['id'], intent['suspension_generation']) != (
                    args['continuation_id'], args['expected_generation']):
                raise WaitError('continuation.stale_generation', 'held resume generation changed')
            if (intent['resume_state'] == 'held' and intent['hold_reason'] == 'push_failed'
                    and intent['stopped_at'] is None and task['status'] in {'WAITING', 'IN_PROGRESS'}
                    and wait['cleared_at'] is None):
                # The stored session can still say running after its process died.
                # Requeue only schedules suspension; advance repeats provider death
                # proof before any publication or checkout release.
                await suspension_writer(conn, task, wait, intent)
                audit = {'wait_id': wait['id'], 'continuation_id': intent['id'],
                         'generation': intent['suspension_generation'], 'at': self.clock(),
                         'note': args['note'], 'principal_kind': principal.kind,
                         'session_id': principal.session_id, 'hold_reason': intent['hold_reason']}
                await conn.execute(update(continuations).where(
                    continuations.c.id == intent['id'],
                ).values(version=intent['version'] + 1, facts={
                    **intent['facts'], 'resume_requeue': audit,
                    'hibernation': {**intent['facts'].get('hibernation', {}), 'push_retry_at': 0},
                }))
                await conn.execute(insert(events).values(
                    event_type='wait.resume_requeued', project_id=task['project_id'],
                    task_id=task['id'], timestamp=audit['at'], payload=json.dumps(audit)))
                return {'success': True, 'wait_id': wait['id'], 'decision': 'requeue',
                        'generation': intent['suspension_generation'], 'next_step': 'end_turn'}
            if (intent['resume_state'] != 'held' or task['status'] != 'RESUME_PENDING'
                    or intent['hold_reason'] not in {'resume_workspace_failed', 'resume_launch_failed'}
                    or wait['outcome'] == 'pending' or wait['cleared_at'] is not None):
                raise WaitError('wait.unavailable', 'continuation has no repairable resume hold')
            if task['branch_name'] != intent['branch']:
                raise WaitError('continuation.head_drift', 'saved branch binding changed')
            if incident_id is not None:
                incident = (await conn.execute(select(escalations).where(
                    escalations.c.id == incident_id,
                ).with_for_update())).mappings().first()
                identity = (f"continuation:{intent['id']}:{intent['suspension_generation']}:"
                            f"{intent['hold_reason']}")
                if (not incident or intent['facts'].get('hold_escalation_id') != incident_id
                        or incident['task_id'] != task['id']
                        or incident['project_id'] != task['project_id']
                        or incident['source_kind'] != 'continuation'
                        or incident['source_identity'] != identity
                        or incident['incident_key'] != identity
                        or incident['state'] not in {'needs_human', 'reply_received', 'resolving'}):
                    raise WaitError('wait.unavailable', 'continuation incident is stale or not found')
            if await conn.scalar(select(sessions.c.id).where(sessions.c.task_id == task['id'],
                                 sessions.c.state.not_in(('stopped', 'failed'))).limit(1)):
                raise WaitError('continuation.stop_uncertain', 'task still has a live process')
            audit = {'wait_id': wait['id'], 'continuation_id': intent['id'],
                     'generation': intent['suspension_generation'], 'at': self.clock(),
                     'note': args['note'], 'principal_kind': principal.kind,
                     'session_id': principal.session_id, 'hold_reason': intent['hold_reason'],
                     'incident_id': incident_id}
            # A workspace hold keeps its reserved agent and grant; a launch hold released both.
            kept = task['assigned_agent_id'] is not None
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                resume_state='reserved' if kept else 'queued', hold_reason=None,
                launch_attempts=intent['launch_attempts'] if kept else 0, new_session_id=None,
                envelope_id=None, version=intent['version'] + 1,
                facts={**intent['facts'], 'workspace_retry': {}, 'workspace_capacity': {},
                       'resume_requeue': audit}))
            await conn.execute(insert(events).values(
                event_type='wait.resume_requeued', project_id=task['project_id'],
                task_id=task['id'], timestamp=audit['at'], payload=json.dumps(audit)))
            return {'success': True, 'wait_id': wait['id'], 'decision': 'requeue',
                    'generation': intent['suspension_generation'], 'next_step': 'end_turn'}

    async def bind_launch(self, action, record):
        """Commit the next writer's acquisition receipt and outcome before spawn."""
        async with self.db._engine.begin() as conn:
            wait_id = await conn.scalar(select(continuations.c.wait_id).where(
                continuations.c.id == action.continuation_id,
            ))
            task, wait, intent = await locked_suspension(conn, wait_id)
            reservation = (await conn.execute(select(admission_reservations).where(
                admission_reservations.c.continuation_id == intent['id'],
                admission_reservations.c.state == 'granted',
            ).with_for_update())).mappings().first()
            if (intent['id'] != action.continuation_id
                    or intent['suspension_generation'] != action.generation
                    or intent['resume_state'] != 'reserved' or not reservation
                    or task['status'] != 'RESUME_PENDING'
                    or task['assigned_agent_id'] != action.agent_id
                    or task['claim_epoch'] != record.last_claim_epoch):
                raise WaitError('continuation.stale_generation', 'launch reservation changed')
            workspace = (await conn.execute(select(workspaces).where(
                workspaces.c.workspace_path == record.work_dir,
            ).with_for_update())).mappings().one_or_none()
            if (workspace is None or not workspace['enabled']
                    or workspace['project_id'] != task['project_id']
                    or workspace['locked_by_task_id'] != task['id']
                    or workspace['locked_by_agent_id'] != action.agent_id
                    or record.task_id != task['id'] or record.agent_id != action.agent_id
                    or record.project_id != task['project_id']):
                raise WaitError('continuation.stale_generation', 'launch workspace binding changed')
            await self.db.set_task_meta(task['id'], 'claim_workspace', {
                'workspace_id': workspace['id'], 'generation': workspace['generation'],
                'session_id': record.id, 'instance_token': record.instance_token,
                'claim_epoch': record.last_claim_epoch,
            }, conn=conn)
            await bind_live_claim(conn, task_id=task['id'], agent_id=action.agent_id,
                                  session_id=record.id, now=self.clock())
            held = await held_messages(conn, wait)
            ids = [m['id'] for m in held]
            envelope = wait_outcome_envelope(wait, intent, held_messages=ids)
            envelope_id = f"continuation:{intent['id']}:{action.generation}:{record.id}:outcome"
            await insert_message_with_source_event(conn, insert(messages).values(
                id=envelope_id, project_id=task['project_id'], from_kind='system',
                from_id='wait-resume', to_kind='session', to_id=record.id,
                body_kind='wait_outcome', body=json.dumps(envelope), created_at=self.clock(),
            ))
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                resume_state='launching', new_session_id=record.id, envelope_id=envelope_id,
                version=intent['version'] + 1,
                facts={**intent['facts'], 'resume_delivery': {
                    'instance_token': record.instance_token, 'claim_epoch': record.last_claim_epoch,
                    'held_message_ids': ids, 'started_at': self.clock()}},
            ))
            await conn.execute(update(admission_reservations).where(
                admission_reservations.c.id == reservation['id'],
            ).values(state='consumed', consumed_by_session_id=record.id, consumed_at=self.clock()))

    async def failed(self, action, task, reason, *, handler, hold_reason=None):
        from src.database.queries.claim_queries import retire_owner_attachments
        from src.orchestrator.workspace_attachments import detach_task_slot

        async with self.db._engine.connect() as conn:
            original = (await conn.execute(select(continuations).where(
                continuations.c.id == action.continuation_id,
            ))).mappings().one()
        session = await self.db.get_session(original['new_session_id']) if original['new_session_id'] else None
        workspace = await self.db.get_workspace_for_task(action.task_id)
        if session and session.state != 'stopped':
            hold_reason = 'resume_stop_uncertain'
        if not hold_reason and workspace and not await detach_task_slot(
            self.db, self.orch, task, workspace, original['branch'],
        ):
            hold_reason = 'resume_detach_uncertain'
        async with self.db._engine.begin() as conn:
            task_row, wait, intent = await locked_suspension(conn, original['wait_id'])
            if intent['suspension_generation'] != action.generation:
                raise WaitError('continuation.stale_generation', 'failed launch generation changed')
            # A failed launch is not a wake. Persist its timestamp on the
            # owning command transaction so the metrics window can count it.
            await conn.execute(insert(events).values(
                event_type='wait.resume_launch_failed', project_id=wait['project_id'],
                task_id=task_row['id'], timestamp=self.clock(),
                payload=json.dumps({'wait_id': wait['id'], 'continuation_id': intent['id'],
                                    'attempt': intent['launch_attempts']}),
            ))
            if task_row['status'] == 'PAUSED':
                hold_reason = 'paused'
            if hold_reason:
                return await self.service.lifecycle.hibernate.hold(
                    conn, task_row, wait, intent, hold_reason, handler, detail=reason,
                )
            if session and workspace:
                await retire_owner_attachments(conn, task_row['id'], {session.id}, {workspace.id},
                    WaitError('continuation.stop_uncertain', 'resume attachment changed'),
                    self.clock())
            if intent['launch_attempts'] >= 2:
                result = await self.service.lifecycle.hibernate.hold(
                    conn, task_row, wait, intent, 'resume_launch_failed', handler, detail=reason,
                )
            else:
                await conn.execute(update(continuations).where(
                    continuations.c.id == intent['id'],
                ).values(resume_state='queued', new_session_id=None, envelope_id=None,
                         version=intent['version'] + 1))
                result = {'success': True, 'retry_queued': True}
            await conn.execute(update(admission_reservations).where(
                admission_reservations.c.continuation_id == intent['id'],
            ).values(state='cancelled'))
            await conn.execute(update(tasks).where(tasks.c.id == task_row['id']).values(
                assigned_agent_id=None,
            ))
            await conn.execute(update(agents).where(agents.c.id == action.agent_id).values(
                state='IDLE', current_task_id=None,
            ))
        if session:
            await self.orch.token_store.revoke_session(session.id)
        if workspace:
            await self.orch._release_workspace_and_cleanup(workspace)
        return result

    async def recovery_event(self, conn, event, *, project_id=None):
        """Only a new durable provider transition can clear the two-attempt hold."""
        if not event['source_key'].startswith('resource_invalidation:provider:'):
            return
        provider = event['payload'].get('identity')
        current = (await conn.execute(select(provider_availability).where(
            provider_availability.c.provider == provider,
        ))).mappings().first()
        facts = event['payload'].get('facts', {})
        if (current is None or current['state'] != 'available'
                or facts.get('generation') != current['generation']
                or event['source_revision'] != str(current['generation'])):
            return
        for wait_id in await conn.scalars(select(continuations.c.wait_id).where(
            continuations.c.resume_state == 'held',
            continuations.c.hold_reason == 'resume_launch_failed',
            continuations.c.task_id.in_(select(tasks.c.id).where(
                tasks.c.project_id == project_id)) if project_id is not None else True,
        )):
            task, wait, intent = await locked_suspension(conn, wait_id)
            started = intent['facts'].get('resume_attempt_started_at', float('inf'))
            if event['occurred_at'] <= started or task['status'] != 'RESUME_PENDING':
                continue
            profile = await self.db.get_profile(task['profile_id'], conn=conn)
            availability = self.orch.provider_availability
            if (not profile or availability.provider_for_profile(
                    profile, project_id=task['project_id']) != provider):
                continue
            incident_id = intent['facts'].get('hold_escalation_id')
            if incident_id:
                resolved = await self.service.decisions.resolve_escalation(
                    conn, {'escalation_id': incident_id, 'wait_id': wait_id,
                           'continuation_id': intent['id']}, event,
                    outcome='satisfied', now=self.clock(),
                )
                if not resolved['success']:
                    raise WaitError('wait.unavailable', 'provider recovery incident changed')
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                resume_state='queued', hold_reason=None, launch_attempts=0,
                new_session_id=None, envelope_id=None, version=intent['version'] + 1,
            ))
