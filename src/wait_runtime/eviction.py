"""Pool arbitration requests suspension; a seat is reusable only after park."""

from sqlalchemy import Float, cast, func, select

from src.database.tables import sessions, tasks, wait_records, workspaces
from src.waits.conditions import WaitError
from src.waits.graph import lock_graph
from src.waits.holding import execution_waits


async def candidate(conn, project_id, profile_id, *, workspace_kind=None):
    cost = func.coalesce(cast(wait_records.c.decision_inputs['resume_cost_minutes'].astext, Float), 10)
    query = execution_waits().where(
        wait_records.c.project_id == project_id, sessions.c.profile_id == profile_id,
        wait_records.c.owner_kind == 'task', wait_records.c.outcome == 'pending',
        wait_records.c.sleep_decision == 'in_place', tasks.c.status == 'WAITING',
    )
    if workspace_kind is not None:
        query = query.where(select(workspaces.c.id).where(
            workspaces.c.project_id == project_id,
            workspaces.c.workspace_path == sessions.c.work_dir,
            func.coalesce(workspaces.c.kind_id, 'project-repo') == workspace_kind,
        ).exists())
    return (await conn.execute(query.order_by(
        wait_records.c.created_at.desc(), tasks.c.effective_priority.desc(), cost,
        wait_records.c.id).limit(1))).mappings().first()


async def evict(lifecycle, args):
    db = lifecycle.db
    async with db._engine.begin() as conn:
        await lock_graph(conn)
        wait = (await conn.execute(select(wait_records).where(
            wait_records.c.id == args['wait_id'],
        ))).mappings().first()
        if not wait or wait['owner_kind'] != 'task':
            raise WaitError('wait.out_of_scope', 'eviction requires an active task wait')
        if wait['sleep_decision'] != 'in_place' or wait['outcome'] != 'pending':
            return {'success': True, 'suspended': False}
        profile = await conn.scalar(select(sessions.c.profile_id).where(
            sessions.c.id == wait['session_id'],
        ))
        kind = await conn.scalar(
            select(func.coalesce(workspaces.c.kind_id, 'project-repo'))
            .join(sessions, sessions.c.work_dir == workspaces.c.workspace_path).where(
                sessions.c.id == wait['session_id'],
                workspaces.c.project_id == wait['project_id'],
            ))
        selected = await candidate(conn, wait['project_id'], profile, workspace_kind=kind)
        if selected is None:
            return {'success': True, 'suspended': False}
        await conn.execute(select(tasks.c.id).where(tasks.c.id == selected['owner_id'])
                           .with_for_update())
        current = (await conn.execute(select(wait_records).where(
            wait_records.c.id == selected['id'],
        ).with_for_update())).mappings().one()
        return await lifecycle.suspend(conn, current, reason='evicted', now=lifecycle.clock())


async def request_evictions(orch, starts):
    """Separate checkpoint requests from starts so they cannot over-admit."""
    ordinary = [action for action in starts if action.reason != 'evict']
    service = getattr(orch, 'wait_service', None)
    if not service or not orch.config.wait_policy.hibernation_enabled:
        return ordinary
    for action in starts:
        if action.reason != 'evict':
            continue
        for _ in range(action.count):
            async with orch.db._engine.connect() as conn:
                wait = await candidate(conn, action.project_id, action.key.profile_id,
                                       workspace_kind=action.workspace_kind)
            if not wait:
                break
            result = await service.command('wait_evict', {'wait_id': wait['id'], 'reason': 'pressure'})
            if not result.get('success'):
                raise WaitError('wait.unavailable', f'eviction refused: {result}')
    return ordinary
