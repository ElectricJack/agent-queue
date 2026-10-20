"""Server-owned no-writer provenance for legacy sessionless continuations."""

from sqlalchemy import select

from src.database.tables import (
    agents, sessions, task_integration_checkpoints, wait_bridge_progress, workspaces,
)


async def no_writer(conn, task):
    if task['assigned_agent_id'] is not None:
        return False
    for table, predicate in (
        (sessions, (sessions.c.task_id == task['id']) &
                   sessions.c.state.not_in(('stopped', 'failed'))),
        (agents, agents.c.current_task_id == task['id']),
        (workspaces, workspaces.c.locked_by_task_id == task['id']),
    ):
        if await conn.scalar(select(table.c.id).where(predicate).limit(1)):
            return False
    return True


async def checkpoint_proof(bridge, task_id):
    """Prefetch real published checkpoint evidence; no Git writes or fake stop."""
    if not getattr(bridge.orch, 'git', None):
        return None
    task = await bridge.db.get_task(task_id)
    if not task or not task.branch_name:
        return None
    async with bridge.db._engine.connect() as conn:
        checkpoint = (await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == task_id,
        ))).mappings().first()
    if (not checkpoint or checkpoint['branch'] != task.branch_name
            or not checkpoint['checkpoint_sha']
            or checkpoint['verified_sha'] != checkpoint['checkpoint_sha']
            or checkpoint['verified_generation'] != checkpoint['generation']):
        return None
    repository = await bridge.db.get_repo(checkpoint['repository_id'])
    if not repository:
        return None
    from src.git.manager import GitError

    try:
        binding = await bridge.orch.git.bind_github_repository(repository.url)
        head = await bridge.orch.git.aremote_branch_head(repository=binding, branch=task.branch_name)
    except (GitError, ValueError):
        return None
    if head != checkpoint['checkpoint_sha']:
        return None
    return {'task_created_at': task.created_at, 'claim_epoch': task.claim_epoch,
            'branch': task.branch_name, 'head': head, 'checkpoint_version': checkpoint['version'],
            'checkpoint_generation': checkpoint['generation'], 'repository_id': repository.id}


async def current_proof(conn, task, proof):
    if (not proof or task['created_at'] != proof.get('task_created_at')
            or task['claim_epoch'] != proof.get('claim_epoch')
            or task['branch_name'] != proof.get('branch') or not await no_writer(conn, task)):
        return False
    checkpoint = (await conn.execute(select(task_integration_checkpoints).where(
        task_integration_checkpoints.c.task_id == task['id'],
    ).with_for_update(read=True))).mappings().first()
    return bool(checkpoint and checkpoint['repository_id'] == proof.get('repository_id')
                and checkpoint['branch'] == proof['branch']
                and checkpoint['checkpoint_sha'] == proof.get('head')
                and checkpoint['verified_sha'] == proof.get('head')
                and checkpoint['version'] == proof.get('checkpoint_version')
                and checkpoint['generation'] == proof.get('checkpoint_generation')
                and checkpoint['verified_generation'] == checkpoint['generation'])


async def legacy_parked(conn, task, wait, intent):
    """Facts JSON alone never grants the exception to a genuine stopped writer."""
    if (wait['legacy_kind'] != 'waiting_input' or wait['session_id'] is not None
            or intent['stopped_at'] is not None or not intent['saved_head']
            or intent['hold_reason'] is not None or intent['wait_id'] != wait['id']
            or intent['task_id'] != task['id']):
        return False
    record = (await conn.execute(select(wait_bridge_progress).where(
        wait_bridge_progress.c.project_id == task['project_id'],
        wait_bridge_progress.c.phase == 'sessionless',
        wait_bridge_progress.c.identity == intent['id'],
        wait_bridge_progress.c.state == 'complete',
    ))).mappings().first()
    if not record:
        return False
    proof = record['evidence']
    return (proof.get('wait_id') == wait['id']
            and proof.get('claim_epoch') == wait['claim_epoch']
            and proof.get('suspension_generation') == intent['suspension_generation']
            and proof.get('head') == intent['saved_head']
            and proof.get('branch') == intent['branch']
            and await current_proof(conn, task, proof))


async def parked(conn, task, wait, intent):
    if intent['stopped_at'] is not None:
        return bool(intent['saved_head'] and 'workspace_released_at' in intent['facts'])
    return await legacy_parked(conn, task, wait, intent)
