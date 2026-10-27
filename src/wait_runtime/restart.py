"""Release a stopped continuation before an operator cold restart admits work.

The task-control lock serializes resume and restart. External proofs and detaches
retain workspace reservations, but no database transaction. A second short
transaction takes that fence, rechecks every observed row, and commits retirement.
Git effects are idempotent detaches; failed proofs retain database ownership.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

from sqlalchemy import or_, select, update

from src.database.queries.claim_queries import retire_owner_attachments
from src.database.queries.continuation_queries import retire_task_waits
from src.database.queries.job_queries import unpinned_workspace
from src.database.tables import agents, continuations, sessions, tasks, wait_records, workspaces
from src.git.manager import GitError
from src.models import SUSPENDED_TASK_STATUSES, TaskStatus
from src.orchestrator.workspace_attachments import detach_task_slot
from src.orchestrator.worktree_manager import slot_path
from src.sessions.provider import SessionHandle
from src.waits.conditions import WaitError
from src.waits.graph import lock_graph
from src.waits.sources import execute_workspace_release

RESTART_TIMEOUT_SECONDS = 20.0
logger = logging.getLogger(__name__)
_restart_progress = ContextVar('restart_progress', default=None)


def restart_phase(phase):
    progress = _restart_progress.get()
    if progress is not None:
        progress['phase'] = phase


@asynccontextmanager
async def restart_attempt(task_id):
    """Bound every restart await, including the locks before continuation cleanup."""
    progress = {'phase': 'recovery hold guard'}
    token = _restart_progress.set(progress)
    deadline = asyncio.timeout(RESTART_TIMEOUT_SECONDS)
    try:
        async with deadline:
            yield
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        logger.warning('restart_task timeout task_id=%s phase=%s budget_seconds=%g',
                       task_id, progress['phase'], RESTART_TIMEOUT_SECONDS)
        raise WaitError('task_restart.timeout',
                        f"Restart of {task_id} exceeded {RESTART_TIMEOUT_SECONDS:g}s "
                        f"during {progress['phase']}; inspect task state before retrying") from exc
    finally:
        _restart_progress.reset(token)


def restart_lock(handler, task_id):
    """Resume's task-control lock, held through cleanup and readmission."""
    orch = getattr(handler, 'orchestrator', None)
    return orch._task_control_lock(task_id) if orch is not None else nullcontext()


async def readmit(handler, task, released, *, hold_override=None):
    """Return *task* to READY; after a release, only from the generation released."""
    restart_phase('readmission (wait graph/task row locks)')
    if not released and task.status not in SUSPENDED_TASK_STATUSES:
        await handler.db.transition_task(task.id, TaskStatus.READY, context='restart_task',
                                         retry_count=0, assigned_agent_id=None,
                                         _recovery_hold_override=hold_override)
        return True
    return await handler.db.transition_task_with_meta(
        task.id, TaskStatus.READY, meta={}, context='restart_task',
        from_statuses=(task.status,), extra_where=tasks.c.claim_epoch == task.claim_epoch,
        force=task.status in SUSPENDED_TASK_STATUSES, retry_count=0, assigned_agent_id=None,
        _recovery_hold_override=hold_override,
    )


async def release_continuations(handler, task):
    """Release *task*'s stopped continuations; True when this restart must fence."""
    try:
        return await _release(handler, task)
    except (GitError, OSError) as exc:
        raise WaitError('wait.unavailable', f'cannot release continuation: {exc}') from exc


async def _release(handler, task):
    db = handler.db
    restart_phase('continuation snapshot (task read)')
    async with db._engine.begin() as conn:
        current = (await conn.execute(select(tasks).where(
            tasks.c.id == task.id,
        ))).mappings().one()
        restart_phase('continuation snapshot (wait read)')
        waits = (await conn.execute(select(wait_records).where(
            wait_records.c.owner_id == task.id,
            wait_records.c.owner_kind.in_(('task', 'scheduler')),
        ).order_by(wait_records.c.id))).mappings().all()
        restart_phase('continuation snapshot (continuation read)')
        history = (await conn.execute(select(continuations).where(
            continuations.c.task_id == task.id,
        ).order_by(continuations.c.id))).mappings().all()
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
        restart_phase('continuation snapshot (workspace read)')
        rows = (await conn.execute(select(workspaces).where(or_(
            workspaces.c.locked_by_task_id == task.id, workspaces.c.id.in_(stage_ids),
        )).order_by(workspaces.c.id))).mappings().all()
        by_id = {row['id']: db._row_to_workspace(row) for row in rows}
        if any(row['job_pin_count'] for row in rows):
            raise WaitError('wait.unavailable', 'continuation workspace is pinned by a job')
        if not set(stage_ids) <= by_id.keys():
            raise WaitError('wait.unavailable', 'continuation workspace reservation is missing')
        paths = {row.workspace_path for row in by_id.values()}
        candidates = {}
        for workspace in by_id.values():
            if (workspace.project_id != task.project_id
                    or workspace.locked_by_task_id != task.id
                    or not workspace.is_slot):
                raise WaitError('wait.unavailable', 'continuation workspace ownership changed')
            candidates[workspace.workspace_path] = workspace
        bases = {}
        for intent in intents:
            stage = intent['facts'].get('fresh_workspace')
            if not stage:
                continue
            workspace = by_id[stage['workspace_id']]
            base_row = (await conn.execute(select(workspaces).where(
                workspaces.c.id == workspace.base_workspace_id,
            ))).mappings().first()
            base = db._row_to_workspace(base_row) if base_row else None
            if not base or base.project_id != task.project_id:
                raise WaitError('wait.unavailable', 'continuation workspace base is unavailable')
            bases[workspace.id] = base
            paths.update((stage['old_path'], stage['new_path']))
        foreign = await conn.scalar(select(workspaces.c.id).where(
            workspaces.c.workspace_path.in_(paths), workspaces.c.id.not_in(by_id),
        ).limit(1))
        if foreign:
            raise WaitError('wait.unavailable', 'continuation path has another workspace owner')
        wait_ids = {row['wait_id'] for row in intents}
        session_ids = {row['session_id'] for row in waits
                       if row['id'] in wait_ids and row['session_id']}
        if any(not row['session_id'] for row in waits if row['id'] in wait_ids):
            raise WaitError('continuation.stop_uncertain', 'continuation writer identity is missing')
        session_ids.update(row['new_session_id'] for row in intents if row['new_session_id'])
        restart_phase('continuation snapshot (session read)')
        writers = (await conn.execute(select(sessions).where(or_(
            sessions.c.id.in_(session_ids), sessions.c.task_id == task.id,
            sessions.c.work_dir.in_(paths),
        )).order_by(sessions.c.id))).mappings().all()
        if not session_ids <= {writer['id'] for writer in writers}:
            raise WaitError('continuation.stop_uncertain', 'continuation writer identity is missing')
    # Task control and durable reservations exclude resume/reuse. External
    # operations must never retain a connection or block graph writers on rows.
    restart_phase('continuation checkout identity')
    for intent in intents:
        stage = intent['facts'].get('fresh_workspace')
        if not stage:
            continue
        workspace, base = by_id[stage['workspace_id']], bases[stage['workspace_id']]
        expected = (Path(base.workspace_path) / '.aq' / 'worktrees'
                    / f"resume-{intent['id']}-{stage['attempt']}")
        if Path(stage['new_path']).resolve() != expected.resolve():
            raise WaitError('wait.unavailable', 'continuation destination identity changed')
        sources = {slot_path(base.workspace_path, workspace.slot_index).resolve()}
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
            if Path(path).exists():
                candidates[path] = replace(workspace, workspace_path=path)
    for writer in writers:
        if writer['state'] != 'stopped' or writer['desired_state'] != 'stopped':
            raise WaitError('continuation.stop_uncertain', 'cold restart requires stopped writers')
        restart_phase(f"provider stop proof (session {writer['id']})")
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
    restart_phase('saved branch proof (repository lock/Git)')
    if candidates:
        checkout = next(iter(candidates))
        for intent in intents:
            if await orch.git.ais_ancestor(
                checkout, intent['saved_head'], f'refs/heads/{task.branch_name}', strict=True,
            ) is not True:
                raise WaitError('continuation.head_drift', 'saved continuation HEAD changed')
    for path, workspace in sorted(candidates.items()):
        restart_phase(f'checkout detach (repository lock/Git, {path})')
        if not await detach_task_slot(db, orch, task, workspace, task.branch_name,
                                      allow_published_detached_head=True):
            raise WaitError('continuation.head_drift',
                            f'cannot release continuation checkout {path}: '
                            'clean, published branch proof is unavailable')
    # Revalidate every observation under graph -> task -> wait -> continuation
    # exclusion before retirement. Failed proofs retain all DB ownership.
    restart_phase('wait graph lock')
    async with db._engine.begin() as conn:
        await lock_graph(conn)
        snapshots = (
            (tasks, tasks.c.id == task.id, [current]),
            (wait_records, wait_records.c.owner_id == task.id,
             waits),
            (continuations, continuations.c.task_id == task.id, history),
            (workspaces, or_(workspaces.c.locked_by_task_id == task.id,
                             workspaces.c.id.in_(stage_ids)), rows),
            (sessions, or_(sessions.c.id.in_(session_ids), sessions.c.task_id == task.id,
                           sessions.c.work_dir.in_(paths)), writers),
        )
        for table, predicate, expected in snapshots:
            restart_phase(f'continuation revalidation ({table.name} row locks)')
            if table is wait_records:
                predicate &= wait_records.c.owner_kind.in_(('task', 'scheduler'))
            fresh = (await conn.execute(select(table).where(predicate)
                .order_by(table.c.id).with_for_update())).mappings().all()
            if {r['id']: dict(r) for r in fresh} != {r['id']: dict(r) for r in expected}:
                raise WaitError('continuation.stale_generation',
                                'continuation cleanup observation changed; retry')
        if await conn.scalar(select(workspaces.c.id).where(
                workspaces.c.workspace_path.in_(paths), workspaces.c.id.not_in(by_id)).limit(1)):
            raise WaitError('wait.unavailable', 'continuation path has another workspace owner')
        restart_phase('continuation ownership retirement')
        now = time.time()
        # Every live attachment still belongs to the writer proven stopped above.
        await retire_owner_attachments(
            conn, task.id, session_ids, by_id.keys(),
            WaitError('wait.unavailable', 'continuation branch ownership changed'), now,
            exclusive=True,
        )
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
