"""Historical failed-push damage, with real Git objects and PostgreSQL fences."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.claim_file import write_claim_file
from src.commands import retained_holder_repair as repair_module
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    agents, continuations, integration_branch_owners, jobs, job_workspace_pins, projects,
    sessions, task_session_attempts, tasks, wait_records, workspaces,
)
from src.git.manager import GitManager
from src.models import (
    Agent, Project, RepoConfig, RepoSourceType, SessionRecord, Task, TaskStatus, Workspace,
    WorktreeSentinel,
)
from src.orchestrator.worktree_manager import WorktreeSlotManager
from src.profiles.capabilities import CapabilityPolicy
from src.wait_runtime.restart import release_continuations

COMMAND = 'recover_retained_holder'


@pytest.fixture
async def env(command_handler_factory, tmp_path, monkeypatch):
    handler = await command_handler_factory()
    git = GitManager()
    handler.orchestrator.git = git
    provider = SimpleNamespace(confirm_stopped=AsyncMock(return_value=True))
    handler.orchestrator.session_providers = SimpleNamespace(create=lambda *_: provider)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(repair_module, 'workspace_quiescent', probe)
    origin, base, slot = (tmp_path / n for n in ('origin.git', 'base', 'slot'))
    await git._arun(['init', '--bare', '--initial-branch=main', str(origin)], cwd=str(tmp_path))
    await git._arun(['clone', str(origin), str(base)], cwd=str(tmp_path))
    await git._arun(['config', 'user.name', 'Tester'], cwd=str(base))
    await git._arun(['config', 'user.email', 'tester@example.test'], cwd=str(base))
    (base / 'work.txt').write_text('original\n')
    await git._arun(['add', '.'], cwd=str(base))
    await git._arun(['commit', '-m', 'original'], cwd=str(base))
    await git._arun(['push', 'origin', 'main'], cwd=str(base))
    await git._arun(['worktree', 'add', '-b', 'aq/task', str(slot)], cwd=str(base))
    (slot / 'committed.txt').write_text('unpublished commit\n')
    await git._arun(['add', '.'], cwd=str(slot))
    await git._arun(['commit', '-m', 'unpublished'], cwd=str(slot))
    head = await git._arun(['rev-parse', 'HEAD'], cwd=str(slot))
    WorktreeSlotManager.ensure_git_exclude_path(WorktreeSlotManager.resolve_exclude_path(slot))
    WorktreeSlotManager.write_sentinel(slot, WorktreeSentinel(
        slot='slot-0', slot_index=0, base_workspace_id='base', project_id='p',
        workspace_id='slot', task_id='task', branch='aq/task',
    ))
    (slot / 'work.txt').write_text('staged\n')
    await git._arun(['add', 'work.txt'], cwd=str(slot))
    (slot / 'work.txt').write_text('unstaged\n')
    (slot / 'untracked.txt').write_text('untracked\n')
    db = handler.db
    await db.create_project(Project(id='p', name='P', repo_url=str(origin)))
    await db.create_repo(RepoConfig(id='r', project_id='p', url=str(origin),
                                    source_type=RepoSourceType.CLONE))
    await db.create_agent(Agent(id='a', name='Agent', profile_id='worker'))
    await db.create_task(Task(id='task', project_id='p', title='Task', description='',
                             branch_name='aq/task', repo_id='r', status=TaskStatus.READY,
                             assigned_agent_id='a', claim_epoch=7, retry_count=2))
    await db.create_session(SessionRecord(
        id='dead', project_id='p', task_id='task', agent_id='a', profile_id='worker',
        harness='codex', provider='fake', name='same-name', lifecycle='pool',
        state='stopped', desired_state='stopped', work_dir=str(slot), epoch='old',
        instance_token='exact-instance', started_at=10, ended_at=20,
        claim_phase='active', last_claim_epoch=7,
    ))
    await db.create_workspace(Workspace(id='base', project_id='p', workspace_path=str(base),
                                         source_type=RepoSourceType.CLONE))
    await db.create_workspace(Workspace(
        id='slot', project_id='p', workspace_path=str(slot), source_type=RepoSourceType.WORKTREE,
        base_workspace_id='base', slot_index=0, locked_by_task_id='task', locked_by_agent_id='a',
    ))
    async with db.immediate() as conn:
        await conn.execute(update(projects).values(integration_repository_id='r'))
        await conn.execute(update(tasks).where(tasks.c.id == 'task').values(claim_epoch=7))
        await conn.execute(update(workspaces).where(workspaces.c.id == 'slot').values(generation=301))
        await conn.execute(update(agents).values(current_task_id='task', state='WORKING'))
        await conn.execute(insert(integration_branch_owners).values(
            id='owner', repository_id='r', ref='aq/task', owner_id='task',
            owner_role='worker', fence_token=12, handoff_state='attached', session_id='dead',
            workspace_id='slot', created_at=1, updated_at=1,
        ))
    write_claim_file(str(slot), {'task_id': 'task', 'session_id': 'dead', 'claim_epoch': 7})
    args = dict(project_id='p', task_id='task', session_id='dead', workspace_id='slot',
                reason='Historical failed push, proved stopped holder')
    yield SimpleNamespace(h=handler, db=db, git=git, base=base, slot=slot, head=head,
                          args=args, provider=provider, probe=probe)
    await db.close()


async def rows(env, table):
    async with env.db._engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(table))).mappings()]


async def audit(env):
    return await env.db.get_recent_events(event_type='project.retained_holder_recovered',
                                          project_id='p')


async def apply(env, preview=None):
    preview = preview or await env.h.execute(COMMAND, env.args)
    assert preview['success'], preview
    return await env.h.execute(COMMAND, {**env.args, 'apply': True,
                                         'expected_proof': preview['proof']})


async def add_pin(env, **changes):
    async with env.db.immediate() as conn:
        await conn.execute(insert(jobs).values(**{
            'id': 'job', 'project_id': 'p', 'task_id': 'task', 'owner_kind': 'task',
            'owner_id': 'task', 'submitter_session_id': 'dead', 'claim_epoch': 7,
            'workspace_id': 'slot', 'workspace_generation': 301, 'state': 'failed',
            'state_version': 2, 'ended_at': 20, 'idempotency_key': 'key', 'request_hash': 'hash',
            'preset': 'test', 'preset_version': 1, 'argv': [], 'contract': {},
            'input_mode': 'live', 'job_class': 'shared', 'weight': 1, 'priority_band': 0,
            'submitted_at': 10, 'queue_deadline': 100, 'run_timeout': 30,
            'runner_nonce': 'dead-job-exact-nonce', **changes,
        }))
        await conn.execute(insert(job_workspace_pins).values(
            job_id='job', workspace_id='slot', generation=301, created_at=10,
        ))
        await conn.execute(update(workspaces).where(workspaces.c.id == 'slot').values(job_pin_count=1))


async def add_continuation(env, **changes):
    async with env.db.immediate() as conn:
        await conn.execute(insert(wait_records).values(
            id='wait', project_id='p', owner_kind='task', owner_id='task', session_id='dead',
            claim_epoch=7, origin='agent', condition_kind='timer', condition={},
            source_key='timer:task', source_generation=1, source_watermark_seq=0,
            verification_state='verified', evidence_ref='timer', verified_at=10, created_at=10,
            request_hash='hash', blocking=True, sleep_decision='hibernate',
        ))
        await conn.execute(insert(continuations).values(**{
            'id': 'continuation', 'task_id': 'task', 'wait_id': 'wait',
            'suspension_generation': 1, 'facts': {}, 'branch': 'aq/unavailable-old-branch',
            'saved_head': env.head, 'prior_status': 'IN_PROGRESS', 'held_cursor_seq': 0,
            'resume_state': 'held', 'created_at': 10, 'stopped_at': 20, **changes,
        }))


async def test_historical_owner_and_terminal_job_release_preserves_all_git_work(env):
    await add_pin(env)
    before_index = await env.git._arun(['show', ':work.txt'], cwd=str(env.slot))
    before_status = await env.git._arun(['status', '--porcelain'], cwd=str(env.slot))
    before_refs = await env.git._arun(['show-ref'], cwd=str(env.slot))
    preview = await env.h.execute(COMMAND, env.args)
    assert preview['success'], preview
    assert preview['preview'] and not preview['changed']
    assert before_refs == await env.git._arun(['show-ref'], cwd=str(env.slot))
    assert not await audit(env)
    assert (await rows(env, integration_branch_owners))[0]['fence_token'] == 12
    result = await apply(env, preview)
    assert result['success'], result
    owner = (await rows(env, integration_branch_owners))[0]
    assert owner['handoff_state'] == 'released' and owner['fence_token'] == 13
    assert owner['session_id'] is None and owner['confirmed_workspace_id'] == 'slot'
    slot = await env.db.get_workspace('slot')
    assert not slot.enabled and not slot.locked_by_agent_id and slot.generation == 302
    assert slot.disabled_reason.startswith('operator:')
    assert await rows(env, job_workspace_pins) == []
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(workspaces.c.job_pin_count).where(
            workspaces.c.id == 'slot')) == 0
    task = await env.db.get_task('task')
    assert task.status == TaskStatus.READY and task.retry_count == 2
    assert task.assigned_agent_id is None and task.claim_epoch == 7
    assert (await env.db.get_session('dead')).claim_phase is None
    assert (await env.db.get_agent('a')).current_task_id is None
    assert before_status == await env.git._arun(['status', '--porcelain'], cwd=str(env.slot))
    assert before_index == await env.git._arun(['show', ':work.txt'], cwd=str(env.slot))
    checkpoint = result['checkpoints'][0]
    assert await env.git._arun(['show', f"{checkpoint['ref']}:work.txt"], cwd=str(env.base)) == 'unstaged'
    assert await env.git._arun(['show', f"{checkpoint['ref']}^:work.txt"], cwd=str(env.base)) == 'staged'
    assert await env.git._arun(['show', f"{checkpoint['ref']}:untracked.txt"], cwd=str(env.base)) == 'untracked'
    assert await env.git.ais_ancestor(str(env.base), env.head, checkpoint['commit'], strict=True)
    record = json.loads((await audit(env))[0]['payload'])
    assert record['proof'] == preview['proof'] and record['checkpoints'] == result['checkpoints']
    assert record['snapshot']['pins'][0]['job_id'] == 'job'
    # Exact instance was checked both before preservation and at release.
    assert env.provider.confirm_stopped.await_count >= 3
    assert env.provider.confirm_stopped.call_args.args[0].instance_token == 'exact-instance'
    assert not (await apply(env, preview))['success']


async def test_ready_stale_assignment_with_unavailable_continuation_branch(env):
    async with env.db.immediate() as conn:
        await conn.execute(delete(integration_branch_owners))
    await add_continuation(env)
    with pytest.raises(Exception, match='saved continuation branch is unavailable'):
        await release_continuations(env.h, await env.db.get_task('task'))
    await env.db.set_task_meta('task', 'manual_pause', {'why': 'operator hold'})
    await env.db.set_task_meta('task', 'needs_attention', {'why': 'push_failed'})
    result = await apply(env)
    assert result['success'], result
    intent = (await rows(env, continuations))[0]
    assert intent['resume_state'] == 'cancelled' and intent['facts']['restart_released_at']
    assert (await rows(env, wait_records))[0]['outcome'] == 'cancelled'
    assert (await env.db.get_task('task')).assigned_agent_id is None
    assert await env.db.get_task_meta('task', 'manual_pause') == {'why': 'operator hold'}
    assert await env.db.get_task_meta('task', 'needs_attention') == {'why': 'push_failed'}
    receipt = next(r for r in result['checkpoints'] if r.get('source_identity') == 'continuation:continuation')
    assert await env.git._arun(['rev-parse', receipt['ref']], cwd=str(env.base)) == env.head
    assert await release_continuations(env.h, await env.db.get_task('task')) is True


@pytest.mark.parametrize('drift', ['generation', 'fence', 'instance', 'assignment', 'content', 'branch'])
async def test_preview_apply_rejects_changed_exact_evidence(env, drift):
    preview = await env.h.execute(COMMAND, env.args)
    assert preview['success'], preview
    async with env.db.immediate() as conn:
        if drift == 'generation':
            await conn.execute(update(workspaces).where(workspaces.c.id == 'slot').values(generation=302))
        elif drift == 'fence':
            await conn.execute(update(integration_branch_owners).values(fence_token=13))
        elif drift == 'instance':
            await conn.execute(update(sessions).values(instance_token='successor'))
        elif drift == 'assignment':
            await conn.execute(update(tasks).values(claim_epoch=8))
    if drift == 'content':
        (env.slot / 'untracked.txt').write_text('changed\n')
    if drift == 'branch':
        await env.git._arun(['update-ref', 'refs/heads/aq/task', 'main'], cwd=str(env.base))
    result = await apply(env, preview)
    assert not result['success']
    assert (await env.db.get_workspace('slot')).locked_by_task_id == 'task'
    assert not await audit(env)


@pytest.mark.parametrize('blocker', ['provider_live', 'provider_unknown', 'process_live',
    'process_unknown', 'running', 'sleeping', 'missing_binding', 'foreign_claim',
    'foreign_owner', 'modern_owner', 'job_running', 'job_cleanup', 'missing_commit'])
async def test_positive_proof_required_and_refusal_keeps_reservations(env, blocker):
    if blocker.startswith('provider_'):
        env.provider.confirm_stopped.return_value = False if blocker == 'provider_live' else None
    elif blocker == 'process_live':
        env.probe.return_value = False
    elif blocker == 'process_unknown':
        env.probe.side_effect = RuntimeError('unreadable process inventory')
    elif blocker in ('running', 'sleeping'):
        async with env.db.immediate() as conn:
            await conn.execute(update(sessions).values(state=blocker))
    elif blocker == 'missing_binding':
        async with env.db.immediate() as conn:
            await conn.execute(update(sessions).values(task_id=None))
            await conn.execute(delete(task_session_attempts))
    elif blocker == 'foreign_claim':
        write_claim_file(str(env.slot), {'task_id': 'successor'})
    elif blocker in ('foreign_owner', 'modern_owner'):
        async with env.db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).values(**(
                {'owner_id': 'other'} if blocker == 'foreign_owner' else
                {'holder': 'session:dead', 'fence': 1, 'expires_at': 100})))
    elif blocker.startswith('job_'):
        await add_pin(env, **({'state': 'running'} if blocker == 'job_running' else
                              {'cleanup_blocked': True}))
    elif blocker == 'missing_commit':
        await add_continuation(env, saved_head='f' * 40)
    result = await env.h.execute(COMMAND, env.args)
    assert not result['success'], result
    assert (await env.db.get_workspace('slot')).locked_by_task_id == 'task'
    assert (await env.db.get_task('task')).assigned_agent_id == 'a'
    assert not await audit(env)


async def test_historical_attempt_is_positive_binding_after_session_task_cleared(env):
    async with env.db.immediate() as conn:
        await conn.execute(insert(task_session_attempts).values(
            id='attempt', session_id='dead', task_id='task', project_id='p', agent_id='a',
            profile_id='worker', name='Agent', harness='codex', provider='fake',
            lifecycle='pool', state='stopped', work_dir=str(env.slot), started_at=11,
            session_started_at=10, ended_at=20,
        ))
        await conn.execute(update(sessions).values(task_id=None))
    result = await apply(env)
    assert result['success'], result
    assert (await env.db.get_session('dead')).claim_phase is None


async def test_audit_failure_rolls_back_ownership_after_preserving_refs(env, monkeypatch):
    preview = await env.h.execute(COMMAND, env.args)
    monkeypatch.setattr(env.db, 'log_event', AsyncMock(side_effect=RuntimeError('audit failed')))
    result = await apply(env, preview)
    assert not result['success'] and 'audit failed' in result['error']
    assert (await env.db.get_workspace('slot')).locked_by_task_id == 'task'
    assert (await env.db.get_task('task')).assigned_agent_id == 'a'
    assert (await rows(env, integration_branch_owners))[0]['fence_token'] == 12
    assert await env.git._arun(['for-each-ref', 'refs/aq/task-pauses/'], cwd=str(env.base))


@pytest.mark.parametrize('drift', ['database', 'files', 'provider'])
async def test_rechecks_after_preservation_before_any_release(env, monkeypatch, drift):
    preview = await env.h.execute(COMMAND, env.args)
    capture = repair_module.capture_checkpoint

    async def changed(*args, **kwargs):
        await capture(*args, **kwargs)
        if drift == 'database':
            async with env.db.immediate() as conn:
                await conn.execute(update(workspaces).where(workspaces.c.id == 'slot').values(
                    generation=302))
        elif drift == 'files':
            (env.slot / 'work.txt').write_text('new writer after capture\n')
        else:
            env.provider.confirm_stopped.return_value = False

    monkeypatch.setattr(repair_module, 'capture_checkpoint', changed)
    result = await apply(env, preview)
    assert not result['success'], result
    assert (await env.db.get_workspace('slot')).locked_by_task_id == 'task'
    assert (await rows(env, integration_branch_owners))[0]['fence_token'] == 12
    assert (await env.db.get_task('task')).assigned_agent_id == 'a'
    assert not await audit(env)


async def test_apply_requires_preview_and_wrong_digest_creates_no_refs(env):
    before = await env.git._arun(['show-ref'], cwd=str(env.base))
    result = await env.h.execute(COMMAND, {**env.args, 'apply': True})
    assert result['error_code'] == 'proof_required'
    result = await env.h.execute(COMMAND, {**env.args, 'apply': True, 'expected_proof': 'wrong'})
    assert result['error_code'] == 'proof_changed'
    assert before == await env.git._arun(['show-ref'], cwd=str(env.base))
    assert (await env.db.get_workspace('slot')).locked_by_task_id == 'task'


async def test_worker_cannot_dispatch_repair_even_with_capability(env):
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, session_id='dead', project_id='p',
        policy=CapabilityPolicy.from_namespaces(harness_tools=['Bash'], aq_commands=[COMMAND]))
    with principal_context(principal):
        result = await env.h._cmd_recover_retained_holder(env.args)
    assert result['error_code'] == 'global_operator_required'
    for scope in (RequestScope(kind='session', session_id='dead', project_id='p', elevated=True),
                  RequestScope(kind='session', session_id='dead', elevated=False)):
        assert check_command_scope(COMMAND, env.args, scope)
    assert not await audit(env)
