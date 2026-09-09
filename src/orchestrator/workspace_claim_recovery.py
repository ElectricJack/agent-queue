"""Retire an exact stopped worker's claim file without touching a successor."""
from __future__ import annotations

import os

from sqlalchemy import select

from src.claim_file import read_claim_file, remove_claim_file_if_matches
from src.database.tables import sessions, task_session_attempts, workspaces


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
            sessions.c.state.in_(('starting', 'running', 'draining')),
        ).limit(1))).first()
        if live is not None or read_claim_file(path) != claim:
            return False
        remove_claim_file_if_matches(path, claim['task_id'], claim['claim_epoch'])
        return read_claim_file(path) is None
