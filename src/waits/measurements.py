"""Join actual continuation timings and attributed usage to the outcome sample."""

from sqlalchemy import select, update

from src.database.tables import sessions, wait_measurements


async def cost_values(conn, wait, intent):
    if not intent:
        return {}
    costs = dict(intent['facts'].get('costs', {}))
    if intent['new_session_id']:
        started = await conn.scalar(select(sessions.c.started_at).where(
            sessions.c.id == intent['new_session_id'],
        ))
        if started and intent['resumed_at']:
            costs.setdefault('rebuild_seconds', max(0, intent['resumed_at'] - started))
    return costs


async def update_sample(conn, wait, intent):
    await conn.execute(update(wait_measurements).where(
        wait_measurements.c.wait_id == wait['id'],
    ).values(hibernated=intent['stopped_at'] is not None,
             evicted=wait['decision_reason'] == 'evicted',
             **await cost_values(conn, wait, intent)))
