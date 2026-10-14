"""Join actual continuation timings and attributed usage to the outcome sample."""

from sqlalchemy import select, update

from src.database.tables import jobs, sessions, wait_measurements


async def cost_values(conn, wait, intent):
    costs = dict(intent['facts'].get('costs', {})) if intent else {}
    if intent and intent['new_session_id']:
        started = await conn.scalar(select(sessions.c.started_at).where(
            sessions.c.id == intent['new_session_id'],
        ))
        if started and intent['resumed_at']:
            costs.setdefault('rebuild_seconds', max(0, intent['resumed_at'] - started))
    condition = wait.get('condition') or {}
    if condition.get('kind') == 'job':
        job_id = condition.get('job_id')
        result = await conn.scalar(select(jobs.c.result).where(jobs.c.id == job_id))
        measured = (result or {}).get('build_measurements') or {}
        costs.update({key: measured[key] for key in (
            'rebuild_seconds', 'cache_hit_rate', 'workspace_path',
        ) if key in measured})
    return costs


async def update_sample(conn, wait, intent):
    await conn.execute(update(wait_measurements).where(
        wait_measurements.c.wait_id == wait['id'],
    ).values(hibernated=intent['stopped_at'] is not None,
             evicted=wait['decision_reason'] == 'evicted',
             **await cost_values(conn, wait, intent)))
