"""Reserve common capacity and launch a fresh continuation through task execution."""

import json
import time
import uuid
from dataclasses import replace
from pathlib import Path

from sqlalchemy import insert, select, update

from src.database.queries.continuation_queries import wait_outcome_envelope
from src.database.tables import (
    admission_reservations, agents, continuations, events, messages, provider_availability,
    tasks, sessions,
)
from src.models import Agent, AgentState
from src.scheduler import AssignAction
from src.waits.conditions import WaitError
from src.wait_runtime.hibernate import locked_suspension
from src.waits.mail import held_messages
from src.waits.sources import insert_message_with_source_event


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


class ResumeTransaction:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.orch = service.orch
        self.db = self.orch.db
        self.clock = clock

    async def reserve(self, continuation_id, generation):
        async with self.db._engine.begin() as conn:
            wait_id = await conn.scalar(select(continuations.c.wait_id).where(
                continuations.c.id == continuation_id,
            ))
            task, wait, intent = await locked_suspension(conn, wait_id)
            if intent['id'] != continuation_id or intent['suspension_generation'] != generation:
                raise WaitError('continuation.stale_generation', 'resume generation changed')
            if intent['resume_state'] == 'reserved' and task['status'] == 'RESUME_PENDING':
                return AssignAction(task['assigned_agent_id'], task['id'], task['project_id'],
                                    continuation_id=intent['id'], generation=generation)
            if intent['resume_state'] != 'queued':
                return None
            if (task['status'] != 'RESUME_PENDING' or wait['outcome'] == 'pending'
                    or not intent['stopped_at'] or not intent['saved_head']
                    or 'workspace_released_at' not in intent['facts']):
                return None
            observed = await self.service.scheduler.reader.observe_task(
                conn, task['id'], continuation_id=continuation_id,
            )
            if observed is None:
                return None
            blocked = {key for key, value in observed.constraints.items() if value['blocked']}
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
                return None
            now = self.clock()
            agent = Agent(id=str(uuid.uuid4()), name=f"resume-{continuation_id}",
                          profile_id=task['profile_id'], state=AgentState.BUSY,
                          current_task_id=task['id'], last_heartbeat=now,
                          intelligence_class=task['intelligence_class'])
            await self.db._insert_agent_on(conn, agent)
            await conn.execute(insert(admission_reservations).values(
                id=str(uuid.uuid4()), project_id=task['project_id'], continuation_id=intent['id'],
                scope={**observed.binding['scope'], 'generation': generation},
                effective_priority=task['effective_priority'], provenance='resume',
                deadline_at=now + self.orch.config.wait_policy.transition_budget_seconds,
                state='granted', created_at=now, granted_at=now,
            ))
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                resume_state='reserved', version=intent['version'] + 1,
                launch_attempts=intent['launch_attempts'] + 1,
                facts={**intent['facts'], 'resume_attempt_started_at': now},
            ))
            await conn.execute(update(tasks).where(tasks.c.id == task['id']).values(
                assigned_agent_id=agent.id, updated_at=now,
            ))
            return AssignAction(agent.id, task['id'], task['project_id'],
                                continuation_id=intent['id'], generation=generation)

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
            await self.orch._worktree_slots().reprovision_slot(
                replace(workspace, workspace_path=target), kind, prune=False,
            )
            await self.db.update_workspace(workspace.id, workspace_path=target)
            return target
        path = await self.orch._prepare_workspace_locked(task, agent)
        workspace = await self.db.get_workspace_for_task(task.id)
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
        # reprovision reuses this *new* checkout and applies the configured
        # setup and ownership sentinel. It never copies an old agent folder.
        await self.orch._worktree_slots().reprovision_slot(fresh, kind, prune=False)
        await git._arun(['merge-base', '--is-ancestor', intent['saved_head'], 'HEAD'], cwd=target)
        await self.db.update_workspace(workspace.id, workspace_path=target)
        # Old detached folder is clean and unowned after the atomic path update.
        # Removal failure is harmless retained disk, never a second writer.
        try:
            await git._arun(['worktree', 'remove', path], cwd=base.workspace_path)
        except Exception:
            pass
        return target

    async def dispatch(self, args, *, principal, scope, handler):
        if not self.orch.config.wait_policy.hibernation_enabled:
            raise WaitError('wait.disabled', 'hibernation is disabled')
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
                await self.failed(action, task, f'workspace preparation: {exc}', handler=handler,
                                  hold_reason='resume_workspace_failed')
                return {'success': True, 'held': True}
            try:
                await self.orch._launch_session_for_task_locked(action, task, None, workspace)
            except Exception as exc:
                return await self.failed(action, task, str(exc), handler=handler)
            return {'success': True, 'dispatched': True}

    async def bind_launch(self, action, record):
        """Commit the next process identity and immutable envelope before spawn."""
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
        from src.database.tables import integration_branch_owners
        from src.orchestrator.workspace_attachments import detach_slot_for_integration_handoff

        async with self.db._engine.connect() as conn:
            original = (await conn.execute(select(continuations).where(
                continuations.c.id == action.continuation_id,
            ))).mappings().one()
        session = await self.db.get_session(original['new_session_id']) if original['new_session_id'] else None
        workspace = await self.db.get_workspace_for_task(action.task_id)
        if session and session.state != 'stopped':
            hold_reason = 'resume_stop_uncertain'
        if not hold_reason and workspace:
            project = await self.db.get_project(task.project_id)
            repository = await self.db.get_repo(task.repo_id or project.integration_repository_id)
            detached = bool(repository and await detach_slot_for_integration_handoff(
                self.db, self.orch.git, self.orch._git_mutex, workspace,
                expected_branch=original['branch'], repository_url=repository.url,
                default_branch=repository.default_branch,
            ))
            if not detached:
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
                owners = (await conn.execute(select(integration_branch_owners).where(
                    integration_branch_owners.c.owner_id == task_row['id'],
                    integration_branch_owners.c.session_id == session.id,
                ).with_for_update())).mappings().all()
                for owner in owners:
                    if owner['handoff_state'] != 'attached' or owner['workspace_id'] != workspace.id:
                        raise WaitError('continuation.stop_uncertain', 'resume attachment changed')
                    await conn.execute(update(integration_branch_owners).where(
                        integration_branch_owners.c.id == owner['id'],
                    ).values(handoff_state='reserved', session_id=None, workspace_id=None,
                             confirmed_workspace_id=workspace.id, updated_at=self.clock()))
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

    async def recovery_event(self, conn, event):
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
        ids = list((await conn.execute(select(continuations.c.wait_id).where(
            continuations.c.resume_state == 'held',
            continuations.c.hold_reason == 'resume_launch_failed',
        ))).scalars())
        for wait_id in ids:
            task, wait, intent = await locked_suspension(conn, wait_id)
            started = intent['facts'].get('resume_attempt_started_at', float('inf'))
            if event['occurred_at'] <= started:
                continue
            profile = await self.db.get_profile(task['profile_id'], conn=conn)
            availability = self.orch.provider_availability
            if (not profile or availability.provider_for_profile(
                    profile, project_id=task['project_id']) != provider):
                continue
            if task['status'] != 'RESUME_PENDING':
                continue
            incident_id = intent['facts'].get('hold_escalation_id')
            if incident_id:
                resolved = await self.service.decisions.resolve_escalation(
                    conn, {'escalation_id': incident_id, 'wait_id': wait_id}, event,
                    outcome='satisfied', now=self.clock(),
                )
                if not resolved['success']:
                    raise WaitError('wait.unavailable', 'provider recovery incident changed')
            await conn.execute(update(continuations).where(continuations.c.id == intent['id']).values(
                resume_state='queued', hold_reason=None, launch_attempts=0,
                new_session_id=None, envelope_id=None, version=intent['version'] + 1,
            ))
