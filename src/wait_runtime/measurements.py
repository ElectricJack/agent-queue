"""Capture completed-turn usage from the exact recorded session transcript."""

import asyncio

from sqlalchemy import select, update

from src.database.tables import continuations, tasks, wait_records
from src.sessions.transcripts import resolve_reader
from src.waits.graph import lock_graph
from src.waits.measurements import update_sample

TURN_TOKEN_CACHE_SIZE = 128


def _signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


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


async def session_turn_tokens(db, session_id, *, since, cache=None):
    session = await db.get_session(session_id)
    if session is None or not session.session_key:
        return None
    reader = resolve_reader(session.harness)
    if reader is None:
        return None
    key = (session_id, session.harness, session.session_key, session.work_dir, since)
    cached = cache.get(key) if cache is not None else None
    if cached is not None:
        path, signature, tokens = cached
        try:
            if await asyncio.to_thread(_signature, path) == signature:
                # Another wait may evict this entry while stat is in flight.
                if cache.get(key) is cached:
                    cache.move_to_end(key)
                return tokens
        except OSError:
            pass
        if cache.get(key) is cached:
            cache.pop(key, None)
    path = await asyncio.to_thread(reader.resolve_session, session)
    if path is None:
        return None
    try:
        signature = await asyncio.to_thread(_signature, path)
        entries, _ = await reader.read_new(path, 0)
        tokens = await asyncio.to_thread(first_turn_tokens, entries, since=since)
        # Never memoize a partial observation while a writer changes the file.
        if cache is not None and await asyncio.to_thread(_signature, path) == signature:
            cache[key] = (path, signature, tokens)
            cache.move_to_end(key)
            while len(cache) > TURN_TOKEN_CACHE_SIZE:
                cache.popitem(last=False)
    except (OSError, ValueError):
        return None
    return tokens


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
    if ('summary_tokens' in costs or intent['summary_degraded']) and 'resume_tokens' in costs:
        return
    cache = getattr(lifecycle, '_turn_token_cache', None)
    observed = {}
    if 'summary_tokens' not in costs and not intent['summary_degraded']:
        tokens = await session_turn_tokens(db, wait['session_id'], since=intent['created_at'],
                                           cache=cache)
        if tokens is not None:
            observed['summary_tokens'] = tokens
    if 'resume_tokens' not in costs and intent['new_session_id']:
        tokens = await session_turn_tokens(db, intent['new_session_id'], since=0, cache=cache)
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
