"""Internal read-only projection of canonical escalation decisions.

Public human-condition parsing deliberately has no escalation subject. Only
server migration code constructs this binding; transport threads are not proof.
"""

import hashlib

from sqlalchemy import select

from src.database.tables import escalations
from src.waits.adapters.base import foreign
from src.waits.conditions import Condition, Observation, Snapshot, SourceKey

SUBJECT = 'legacy_escalation'


def generation(row):
    identity = repr((row['id'], row['created_at'], row['source_kind'], row['source_identity']))
    return int(hashlib.sha256(identity.encode()).hexdigest()[:7], 16)


def bind(row, *, watermark):
    return Condition('human', {'subject': SUBJECT, 'escalation_id': row['id']}, generation(row), {
        'legacy_projection': True, 'project_id': row['project_id'],
        'owner_kind': 'integration', 'owner_id': f"legacy-escalation:{row['id']}",
        'session_id': None, 'claim_epoch': None,
        'source_watermark_seq': watermark, 'revision': row['revision'],
        'supervisor_owner': row['supervisor_owner'],
    })


async def authorise(conn, cond, scope):
    if (scope.principal != 'service' or scope.owner_kind != 'integration'
            or scope.owner_id != f"legacy-escalation:{cond.parameters['escalation_id']}"
            or not cond.context.get('legacy_projection')):
        foreign()
    if cond.context.get('project_id') != scope.project_id:
        foreign()
    return cond


async def snapshot(conn, cond):
    row = (await conn.execute(select(escalations).where(
        escalations.c.id == cond.parameters['escalation_id'],
    ))).mappings().first()
    if row is None:
        return None
    if (not cond.context.get('legacy_projection')
            or row['supervisor_owner'] != cond.context.get('supervisor_owner')
            or row['revision'] < cond.context.get('revision', 0)):
        return Snapshot(row['project_id'], generation(row), Observation(
            str(row['revision']), 'unavailable', facts={'reason': 'provenance_changed'},
        ), 'human:legacy_escalation')
    state = ('satisfied' if row['state'] == 'resolved' else
             'unavailable' if row['state'] in {'cancelled', 'stale'} else 'unsatisfied')
    return Snapshot(row['project_id'], generation(row), Observation(
        str(row['revision']), state, f"escalation:{row['id']}@{row['revision']}",
        {'state': row['state'], 'terminal_outcome': row['terminal_outcome']},
    ), 'human:legacy_escalation')


def source_key(cond):
    return SourceKey('escalation', cond.parameters['escalation_id'], cond.generation)


async def execute_mutation(conn, statement):
    """Keep canonical state CAS and its replayable source event in one transaction."""
    from src.waits.sources import record_source_event

    # Every current caller either returns the complete escalation or ignores the
    # result. Freeze it so event recording never consumes the caller's rows.
    if not statement._returning:
        statement = statement.returning(escalations)
    result = (await conn.execute(statement)).freeze()
    for row in result().mappings():
        await record_source_event(
            conn, source_key=f"escalation:{row['id']}", generation=generation(row),
            revision=str(row['revision']), outcome_ref=f"escalation:{row['id']}@{row['revision']}",
            payload={'project_id': row['project_id'], 'state': row['state']},
            occurred_at=row['updated_at'],
        )
    return result()
