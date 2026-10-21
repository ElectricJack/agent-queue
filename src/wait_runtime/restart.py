"""Release a stopped continuation before an operator cold restart admits work.

The command owns this operation. Keep the graph, task and workspace rows locked
through the external proofs so a resume or a new reservation cannot reuse them.
Git effects are idempotent detaches; failed proofs retain database ownership.
"""

import asyncio
import time
from dataclasses import replace
from pathlib import Path

from sqlalchemy import or_, select, update

from src.database.queries.continuation_queries import retire_task_waits
from src.database.queries.job_queries import unpinned_workspace
from src.database.tables import agents, continuations, integration_branch_owners, sessions, tasks
from src.database.tables import wait_records, workspaces
from src.orchestrator.workspace_attachments import detach_slot_for_integration_handoff
from src.orchestrator.worktree_manager import slot_path
from src.sessions.provider import SessionHandle
from src.waits.conditions import WaitError
from src.waits.graph import lock_graph
from src.waits.sources import execute_workspace_release


async def release_restart_continuations(handler, task):
    db = handler.db
    async with db._engine.begin() as conn:
        await lock_graph(conn)
        current = (await conn.execute(select(tasks).where(
            tasks.c.id == task.id,
        ).with_for_update())).mappings().one()
        waits = (await conn.execute(select(wait_records).where(
            wait_records.c.owner_id == task.id,
            wait_records.c.owner_kind.in_(('task', 'scheduler')),
        ).order_by(wait_records.c.id).with_for_update())).mappings().all()
        history = (await conn.execute(select(continuations).where(
            continuations.c.task_id == task.id,
        ).order_by(continuations.c.id).with_for_update())).mappings().all()
        intents = [row for row in history if row['resume_state'] not in ('acknowledged', 'failed')]
        already_released = any(row['facts'].get('restart_released_at') for row in intents)
        intents = [row for row in intents if row['resume_state'] != 'cancelled'
                   or (row['facts'].get('fresh_workspace')
                       and not row['facts'].get('restart_released_at'))]
        if not intents:
            return already_released
        if (current['status'] != task.status.value
                or current['claim_epoch'] != task.claim_epoch or current['status'] == 'IN_PROGRESS'):
            raise WaitError('continuation.stale_generation', 'task changed during cold restart')
        if any(not row['saved_head'] or row['branch'] != task.branch_name for row in intents):
            raise WaitError('continuation.head_drift', 'saved continuation branch is unavailable')
        orch = getattr(handler, 'orchestrator', None)
        if orch is None:
            raise WaitError('wait.unavailable', 'continuation cleanup requires the orchestrator')
        stage_ids = [row['facts']['fresh_workspace']['workspace_id'] for row in intents
                     if row['facts'].get('fresh_workspace')]
        rows = (await conn.execute(select(workspaces).where(or_(
            workspaces.c.locked_by_task_id == task.id, workspaces.c.id.in_(stage_ids),
        )).order_by(workspaces.c.id).with_for_update())).mappings().all()
        by_id = {row['id']: db._row_to_workspace(row) for row in rows}
        if any(row['job_pin_count'] for row in rows):
            raise WaitError('wait.unavailable', 'continuation workspace is pinned by a job')
        if not set(stage_ids) <= by_id.keys():
            raise WaitError('wait.unavailable', 'continuation workspace reservation is missing')
        paths = {row.workspace_path for row in by_id.values()}
        candidates = {}
        for workspace in by_id.values():
            if (workspace.project_id != task.project_id
                    or workspace.locked_by_task_id not in (None, task.id)
                    or (workspace.locked_by_task_id is None and workspace.locked_by_agent_id)
                    or not workspace.is_slot):
                raise WaitError('wait.unavailable', 'continuation workspace ownership changed')
            candidates[workspace.workspace_path] = workspace
        for intent in intents:
            stage = intent['facts'].get('fresh_workspace')
            if not stage:
                continue
            workspace = by_id[stage['workspace_id']]
            base = await db.get_workspace(workspace.base_workspace_id)
            if not base or base.project_id != task.project_id:
                raise WaitError('wait.unavailable', 'continuation workspace base is unavailable')
            expected = (Path(base.workspace_path) / '.aq' / 'worktrees'
                        / f"resume-{intent['id']}-{stage['attempt']}")
            if Path(stage['new_path']).resolve() != expected.resolve():
                raise WaitError('wait.unavailable', 'continuation destination identity changed')
            sources = {slot_path(base.workspace_path, workspace.slot_index).resolve()}
            # A second hibernation starts from the previous resume checkout.
            # Accept only destinations independently identified by its audit.
            for prior in history:
                prior_stage = prior['facts'].get('fresh_workspace')
                if not prior_stage or prior_stage['workspace_id'] != workspace.id:
                    continue
                prior_path = (Path(base.workspace_path) / '.aq' / 'worktrees'
                              / f"resume-{prior['id']}-{prior_stage['attempt']}")
                if Path(prior_stage['new_path']).resolve() == prior_path.resolve():
                    sources.add(prior_path.resolve())
            if Path(stage['old_path']).resolve() not in sources:
                raise WaitError('wait.unavailable', 'continuation source identity changed')
            for path in (stage['old_path'], stage['new_path']):
                paths.add(path)
                # A completed relocation removes old_path; an interrupted one
                # can still have both. Never discard either checkout's work.
                if Path(path).exists():
                    candidates[path] = replace(workspace, workspace_path=path)
        foreign = await conn.scalar(select(workspaces.c.id).where(
            workspaces.c.workspace_path.in_(paths), workspaces.c.id.not_in(by_id),
        ).limit(1).with_for_update())
        if foreign:
            raise WaitError('wait.unavailable', 'continuation path has another workspace owner')
        wait_ids = {row['wait_id'] for row in intents}
        session_ids = {row['session_id'] for row in waits
                       if row['id'] in wait_ids and row['session_id']}
        if any(not row['session_id'] for row in waits if row['id'] in wait_ids):
            raise WaitError('continuation.stop_uncertain', 'continuation writer identity is missing')
        session_ids.update(row['new_session_id'] for row in intents if row['new_session_id'])
        writers = (await conn.execute(select(sessions).where(or_(
            sessions.c.id.in_(session_ids), sessions.c.task_id == task.id,
            sessions.c.work_dir.in_(paths),
        )).order_by(sessions.c.id).with_for_update())).mappings().all()
        if not session_ids <= {writer['id'] for writer in writers}:
            raise WaitError('continuation.stop_uncertain', 'continuation writer identity is missing')
        for writer in writers:
            if writer['state'] != 'stopped' or writer['desired_state'] != 'stopped':
                raise WaitError('continuation.stop_uncertain', 'cold restart requires stopped writers')
            try:
                provider = orch.session_providers.create(writer['provider'], orch.config)
                stopped = await asyncio.wait_for(provider.confirm_stopped(SessionHandle(
                    writer['name'], writer['provider'], writer['instance_token'],
                )), timeout=10)
            except Exception as exc:
                raise WaitError('continuation.stop_uncertain',
                                'writer stop proof is unavailable') from exc
            if stopped is not True:
                raise WaitError('continuation.stop_uncertain', 'writer stop proof is unavailable')
        owners = (await conn.execute(select(integration_branch_owners).where(
            integration_branch_owners.c.owner_id == task.id,
            integration_branch_owners.c.handoff_state != 'released',
        ).with_for_update())).mappings().all()
        for owner in owners:
            if owner['handoff_state'] == 'reserved':
                continue
            if (owner['handoff_state'] != 'attached' or owner['session_id'] not in session_ids
                    or owner['workspace_id'] not in by_id):
                raise WaitError('wait.unavailable', 'continuation branch ownership changed')
        project = await db.get_project(task.project_id)
        repository = await db.get_repo(task.repo_id or project.integration_repository_id)
        if candidates and repository is None:
            raise WaitError('wait.unavailable', 'continuation repository binding is unavailable')
        if candidates:
            checkout = next(iter(candidates))
            for intent in intents:
                if await orch.git.ais_ancestor(
                    checkout, intent['saved_head'], f'refs/heads/{task.branch_name}', strict=True,
                ) is not True:
                    raise WaitError('continuation.head_drift', 'saved continuation HEAD changed')
        for path, workspace in sorted(candidates.items()):
            if not await detach_slot_for_integration_handoff(
                db, orch.git, orch._git_mutex, workspace, expected_branch=task.branch_name,
                repository_url=repository.url, default_branch=repository.default_branch,
                allow_published_detached_head=True,
            ):
                raise WaitError('continuation.head_drift',
                                f'cannot release continuation checkout {path}: '
                                'clean, published branch proof is unavailable')
        now = time.time()
        for owner in owners:
            if owner['handoff_state'] == 'attached':
                await conn.execute(update(integration_branch_owners).where(
                    integration_branch_owners.c.id == owner['id'],
                ).values(handoff_state='reserved', session_id=None, workspace_id=None,
                         confirmed_workspace_id=owner['workspace_id'], updated_at=now))
        released = await execute_workspace_release(conn, update(workspaces).where(
            workspaces.c.id.in_(by_id), unpinned_workspace(),
            or_(workspaces.c.locked_by_task_id == task.id, workspaces.c.locked_by_task_id.is_(None)),
        ).values(locked_by_task_id=None, locked_by_agent_id=None, locked_at=None, lock_mode=None))
        if released.rowcount != len(by_id):
            raise WaitError('wait.unavailable', 'continuation workspace is pinned by a job')
        await retire_task_waits(conn, task.id, reason='task_restarted', now=now)
        for intent in intents:
            await conn.execute(update(continuations).where(
                continuations.c.id == intent['id'],
            ).values(facts={**intent['facts'], 'restart_released_at': now}))
        await conn.execute(update(agents).where(agents.c.current_task_id == task.id).values(
            state='IDLE', current_task_id=None,
        ))
        return True
