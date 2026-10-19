"""Join actual continuation timings and attributed usage to the outcome sample."""

from sqlalchemy import func, select, update

from src.database.tables import sessions, token_ledger, wait_measurements


async def cost_values(conn, wait, intent):
    if not intent:
        return {}
    costs = dict(intent['facts'].get('costs', {}))
    summary = await conn.scalar(select(func.sum(token_ledger.c.tokens_used)).where(
        token_ledger.c.session_id == wait['session_id'],
        token_ledger.c.timestamp >= intent['created_at'],
        token_ledger.c.timestamp <= (intent['stopped_at'] or float('inf')),
    ))
    if summary is not None:
        costs['summary_tokens'] = summary
    if intent['new_session_id']:
        # A call is attributed by its stable session/call identity, not guessed
        # from the task's overlapping sessions. Missing provider usage stays null.
        first = await conn.scalar(select(token_ledger.c.tokens_used).where(
            token_ledger.c.session_id == intent['new_session_id'],
        ).order_by(token_ledger.c.timestamp, token_ledger.c.id).limit(1))
        if first is not None:
            costs['resume_tokens'] = first
        started = await conn.scalar(select(sessions.c.started_at).where(
            sessions.c.id == intent['new_session_id'],
        ))
        if started and intent['resumed_at']:
            costs.setdefault('setup_seconds', max(0, intent['resumed_at'] - started))
    return costs


async def update_sample(conn, wait, intent):
    await conn.execute(update(wait_measurements).where(
        wait_measurements.c.wait_id == wait['id'],
    ).values(hibernated=intent['stopped_at'] is not None,
             evicted=wait['decision_reason'] == 'evicted',
             **await cost_values(conn, wait, intent)))
