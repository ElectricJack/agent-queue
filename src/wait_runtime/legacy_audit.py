"""First scheduler synchronization preserves evidenced age or marks it unknown."""

import json

from sqlalchemy import select, update

from src.database.tables import events, wait_deadlines, wait_records


async def audit_scheduler_age(conn, project_id, *, now, deadline_seconds):
    rows = (await conn.execute(select(wait_records).where(
        wait_records.c.project_id == project_id, wait_records.c.origin == 'scheduler',
        wait_records.c.outcome == 'pending',
    ).order_by(wait_records.c.id).with_for_update())).mappings().all()
    for row in rows:
        inputs = dict(row['decision_inputs'] or {})
        if 'legacy_audit' in inputs:
            continue
        history = (await conn.execute(select(events).where(
            events.c.project_id == project_id, events.c.task_id == row['owner_id'],
            events.c.timestamp <= now,
        ).order_by(events.c.timestamp.desc(), events.c.id.desc()).limit(1000))).mappings()
        age = None
        event_id = None
        for event in history:
            try:
                payload = json.loads(event['payload'])
            except (TypeError, ValueError):
                continue
            if (isinstance(payload, dict) and payload.get('source_key') == row['source_key']
                    and payload.get('source_generation') == row['source_generation']):
                age, event_id = event['timestamp'], event['id']
                break
        inputs.update(age_unknown=age is None,
                      legacy_audit={'at': now, 'age_event_id': event_id, 'blocked_at': age})
        await conn.execute(update(wait_records).where(wait_records.c.id == row['id']).values(
            decision_inputs=inputs, **({'created_at': age} if age is not None else {}),
        ))
        # A reservation exists only after the real scheduler verified that this
        # task retains a branch and lacks capacity. Unknown age is already due.
        deadlines = (await conn.execute(select(wait_deadlines).where(
            wait_deadlines.c.wait_id == row['id'],
            wait_deadlines.c.deadline_type == 'reservation',
            wait_deadlines.c.fired_at.is_(None), wait_deadlines.c.cancelled_at.is_(None),
        ).with_for_update())).mappings()
        for deadline in deadlines:
            await conn.execute(update(wait_deadlines).where(
                wait_deadlines.c.id == deadline['id'],
            ).values(due_at=min(deadline['due_at'], now if age is None else age + deadline_seconds)))
