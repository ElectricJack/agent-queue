"""Retire an exact stopped worker's claim file without touching a successor."""
from __future__ import annotations

import os

from sqlalchemy import select

from src.claim_file import (
    claim_file_identity,
    read_claim_file,
    remove_claim_file_if_identical,
    remove_claim_file_if_matches,
)
from src.database.tables import sessions, task_session_attempts, tasks, workspaces

_LIVE_STATES = ('starting', 'running', 'draining')


async def retire_stopped_slot_claim(db, path: str) -> bool:
    claim = read_claim_file(path)
    if claim is None:
        return True
    if not all(claim.get(key) is not None for key in ('task_id', 'session_id', 'claim_epoch')):
        return False
    async with db.immediate() as conn:
        slot = (await conn.execute(select(workspaces).where(
            workspaces.c.workspace_path == path,
        ).with_for_update())).mappings().one_or_none()
        if (slot is None or slot['base_workspace_id'] is None
                or slot['locked_by_task_id'] is not None
                or slot['locked_by_agent_id'] is not None):
            return False
        session = (await conn.execute(select(sessions).where(
            sessions.c.id == claim['session_id'],
        ).with_for_update())).mappings().one_or_none()
        if (session is None or session['state'] != 'stopped'
                or session['desired_state'] != 'stopped'
                or session['task_id'] not in (None, claim['task_id'])
                or session['last_claim_epoch'] != claim['claim_epoch']
                or session['project_id'] != slot['project_id']
                or os.path.realpath(session['work_dir']) != os.path.realpath(path)):
            return False
        if session['task_id'] is None:
            ended = (await conn.execute(select(task_session_attempts.c.id).where(
                task_session_attempts.c.session_id == session['id'],
                task_session_attempts.c.task_id == claim['task_id'],
                task_session_attempts.c.session_started_at == session['started_at'],
                task_session_attempts.c.ended_at.is_not(None),
            ).limit(1))).scalar_one_or_none()
            if ended is None:
                return False
        live = (await conn.execute(select(sessions.c.id).where(
            sessions.c.work_dir == path,
            sessions.c.state.in_(_LIVE_STATES),
        ).limit(1))).first()
        if live is not None or read_claim_file(path) != claim:
            return False
        remove_claim_file_if_matches(path, claim['task_id'], claim['claim_epoch'])
        return read_claim_file(path) is None


async def retire_paused_slot_claim(
    db, workspace_id: str, path: str, *, task_id: str, claim_epoch, agent_id, captured,
) -> bool:
    """Retire a manually paused pool claim's own marker before its slot is unlocked.

    Called by the pause after it stopped the captured sessions and checkpointed
    the workspace, while the slot is still locked to the paused task, so no
    successor can be bound to it yet.  Every proof is re-read under row locks;
    the file is removed only if it is still the exact inode read beforehand.
    """
    identity = claim_file_identity(path)
    claim = read_claim_file(path)
    if identity is None or claim is None or claim_file_identity(path) != identity:
        return False
    tokens = {item.get('id'): item.get('instance_token') for item in captured or ()}
    if (claim.get('task_id') != task_id or claim_epoch is None
            or claim.get('claim_epoch') != claim_epoch
            or claim.get('session_id') not in tokens
            or not tokens[claim['session_id']]):
        return False
    async with db.immediate() as conn:
        task = (await conn.execute(select(tasks.c.project_id, tasks.c.claim_epoch).where(
            tasks.c.id == task_id,
        ).with_for_update())).mappings().one_or_none()
        slot = (await conn.execute(select(workspaces).where(
            workspaces.c.id == workspace_id,
        ).with_for_update())).mappings().one_or_none()
        if (task is None or (task['claim_epoch'] or 0) <= claim_epoch
                or slot is None or slot['project_id'] != task['project_id']
                or slot['locked_by_task_id'] != task_id
                or slot['locked_by_agent_id'] not in (None, agent_id)
                or os.path.realpath(slot['workspace_path']) != os.path.realpath(path)):
            return False
        session = (await conn.execute(select(sessions).where(
            sessions.c.id == claim['session_id'],
        ).with_for_update())).mappings().one_or_none()
        if (session is None or session['state'] != 'stopped'
                or session['desired_state'] != 'stopped'
                or session['instance_token'] != tokens[session['id']]
                or session['task_id'] is not None
                or session['last_claim_epoch'] != claim_epoch
                or session['last_claim_result'] != 'manually_paused'
                or session['project_id'] != task['project_id']
                or os.path.realpath(session['work_dir']) != os.path.realpath(path)):
            return False
        attempts = (await conn.execute(select(
            task_session_attempts.c.session_started_at, task_session_attempts.c.ended_at,
        ).where(
            task_session_attempts.c.session_id == session['id'],
            task_session_attempts.c.task_id == task_id,
        ))).all()
        if (any(ended is None for _, ended in attempts)
                or not any(started == session['started_at'] for started, _ in attempts)):
            return False
        live = (await conn.execute(select(sessions.c.id).where(
            sessions.c.work_dir.in_(sorted({path, session['work_dir']})),
            sessions.c.state.in_(_LIVE_STATES),
        ).limit(1))).first()
        if live is not None:
            return False
        return remove_claim_file_if_identical(path, claim, identity)


#: Task states whose claim is released: a marker naming one is a leftover.
_RELEASED_STATUSES = ('COMPLETED', 'FAILED', 'PAUSED', 'BLOCKED', 'READY', 'DEFINED')


async def retire_stale_slot_claim(
    db, workspace_id: str, path: str, *,
    claimant_task_id: str | None = None, claimant_session_id: str | None = None,
    conn=None,
) -> dict | None:
    """Retire a slot's claim marker whose holder provably no longer holds it.

    A completed, paused or released task can leave ``.aq/claim.json`` behind
    after its workspace lock is gone, and every later claim of the slot was
    refused as a foreign claim (matter-engine-cpp slot-4/slot-5, 2026-10-14).
    The marker is stale when, re-read under row locks:

    * the slot is unlocked, or locked only by *claimant_task_id*;
    * the holder task is gone, its claim epoch has moved past the marker's,
      or its status is one with no active claim (``_RELEASED_STATUSES``);
    * the marker's session no longer holds that task at that epoch; and
    * no live session other than *claimant_session_id* works in the slot.

    Dirty or unpushed work is not this function's concern: the slot reset
    that follows salvages it.  Returns the retired marker plus the reason,
    or ``None`` when the claim may still be live (the caller keeps refusing).
    """
    identity = claim_file_identity(path)
    claim = read_claim_file(path)
    if identity is None or claim is None or claim_file_identity(path) != identity:
        return None
    holder = claim.get('task_id')
    epoch = claim.get('claim_epoch')
    if not isinstance(holder, str) or not holder:
        return None
    if conn is None:
        async with db.immediate() as connection:
            return await retire_stale_slot_claim(
                db, workspace_id, path, claimant_task_id=claimant_task_id,
                claimant_session_id=claimant_session_id, conn=connection,
            )
    slot = (await conn.execute(select(workspaces).where(
        workspaces.c.id == workspace_id,
    ).with_for_update())).mappings().one_or_none()
    if (slot is None or slot['base_workspace_id'] is None
            or os.path.realpath(slot['workspace_path']) != os.path.realpath(path)
            or slot['locked_by_task_id'] not in (None, claimant_task_id)
            or (claimant_task_id is None and slot['locked_by_agent_id'] is not None)):
        return None
    task = (await conn.execute(select(
        tasks.c.status, tasks.c.claim_epoch,
    ).where(tasks.c.id == holder).with_for_update())).mappings().one_or_none()
    if task is None:
        reason = 'holder task no longer exists'
    elif isinstance(epoch, int) and (task['claim_epoch'] or 0) > epoch:
        reason = f"claim epoch {epoch} superseded by {task['claim_epoch']}"
    elif holder == claimant_task_id:
        return None
    elif task['status'] in _RELEASED_STATUSES:
        reason = f"holder task is {task['status']}"
    else:
        return None
    session_id = claim.get('session_id')
    if session_id is not None and session_id != claimant_session_id:
        session = (await conn.execute(select(
            sessions.c.state, sessions.c.task_id, sessions.c.last_claim_epoch,
        ).where(sessions.c.id == session_id).with_for_update())).mappings().one_or_none()
        if (session is not None and session['state'] in _LIVE_STATES
                and session['task_id'] == holder
                and session['last_claim_epoch'] == epoch):
            return None
    live = select(sessions.c.id).where(
        sessions.c.work_dir.in_(sorted({path, os.path.realpath(path)})),
        sessions.c.state.in_(_LIVE_STATES),
    )
    if claimant_session_id is not None:
        live = live.where(sessions.c.id != claimant_session_id)
    if (await conn.execute(live.limit(1))).first() is not None:
        return None
    if not remove_claim_file_if_identical(path, claim, identity):
        return None
    return {**claim, 'workspace_id': workspace_id, 'path': path, 'reason': reason}
