"""Capture completed-turn usage from the exact recorded session transcript."""

import asyncio

from sqlalchemy import select, update

from src.database.tables import continuations, tasks, wait_records
from src.sessions.transcripts import resolve_reader
from src.waits.graph import lock_graph
from src.waits.measurements import update_sample


def first_turn_tokens(entries, *, since):
    """Count every call in the turn, deduplicating streamed call revisions."""
    calls = {}
    for entry in entries:
        if entry.type == 'assistant' and entry.usage:
            key = entry.usage_call_id or entry.uuid
            previous = calls.setdefault(key, {})
            for field in ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                          'cache_creation_input_tokens'):
                previous[field] = max(previous.get(field, 0), int(entry.usage.get(field) or 0))
        if entry.turn_complete:
            if entry.ts >= since:
                return sum(sum(call.values()) for call in calls.values()) if calls else None
            calls.clear()
    # An unfinished turn has no measured total; one API call is not a turn.
    return None


async def session_turn_tokens(db, session_id, *, since):
    session = await db.get_session(session_id)
    if session is None or not session.session_key:
        return None
    reader = resolve_reader(session.harness)
    if reader is None:
        return None
    path = await asyncio.to_thread(reader.resolve_session, session)
    if path is None:
        return None
    try:
        entries, _ = await reader.read_new(path, 0)
    except (OSError, ValueError):
        return None
    return first_turn_tokens(entries, since=since)


async def capture_costs(lifecycle, wait_id):
    """Called inside a service command, with transcript I/O outside SQL locks."""
    db, now = lifecycle.db, lifecycle.clock()
    async with db._engine.connect() as conn:
        intent = (await conn.execute(select(continuations).where(
            continuations.c.wait_id == wait_id,
        ))).mappings().first()
        wait = (await conn.execute(select(wait_records).where(
            wait_records.c.id == wait_id,
        ))).mappings().first()
    if not intent or not wait or now - intent['facts'].get('usage_checked_at', 0) < 30:
        return
    costs = dict(intent['facts'].get('costs', {}))
    observed = {}
    if 'summary_tokens' not in costs and not intent['summary_degraded']:
        tokens = await session_turn_tokens(db, wait['session_id'], since=intent['created_at'])
        if tokens is not None:
            observed['summary_tokens'] = tokens
    if 'resume_tokens' not in costs and intent['new_session_id']:
        tokens = await session_turn_tokens(db, intent['new_session_id'], since=0)
        if tokens is not None:
            observed['resume_tokens'] = tokens
    async with db._engine.begin() as conn:
        await lock_graph(conn)
        await conn.execute(select(tasks.c.id).where(tasks.c.id == intent['task_id'])
                           .with_for_update())
        current_wait = (await conn.execute(select(wait_records).where(
            wait_records.c.id == wait_id,
        ).with_for_update())).mappings().first()
        current = (await conn.execute(select(continuations).where(
            continuations.c.id == intent['id'],
        ).with_for_update())).mappings().first()
        if not current or not current_wait:
            return
        if (current['suspension_generation'] != intent['suspension_generation']
                or current['new_session_id'] != intent['new_session_id']):
            return
        facts = {**current['facts'], 'usage_checked_at': now,
                 'costs': {**current['facts'].get('costs', {}), **observed}}
        await conn.execute(update(continuations).where(continuations.c.id == current['id']).values(
            facts=facts,
        ))
        await update_sample(conn, current_wait, {**current, 'facts': facts})
