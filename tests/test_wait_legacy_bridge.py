"""Legacy adoption preserves age, ownership and exactly one outcome path."""

from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from src.config import AppConfig
from src.database import Database
from src.database.tables import agent_waits, messages, wait_deadlines, wait_records
from src.models import Project, Task
from src.wait_runtime.legacy_bridge import LegacyWaitBridge, legacy_condition, legacy_times
from src.wait_runtime.service import WaitService
from src.waits.conditions import WaitError
from tests.db_fixtures import lease_dsn
from tests.test_wait_queries import NOW, owner


@pytest.fixture
async def bridge():
    db = Database(lease_dsn('wait-bridge'))
    await db.initialize()
    await db.create_project(Project(id='p', name='P'))
    await owner(db)
    await db.create_task(Task(id='source', project_id='p', title='Source', description=''))
    config = AppConfig()
    config.wait_policy.v2_enabled = True
    config.wait_policy.hibernation_enabled = False
    orch = SimpleNamespace(db=db, config=config)
    service = WaitService(orch, clock=lambda: NOW)
    yield LegacyWaitBridge(service, clock=lambda: NOW)
    await db.close()


async def legacy(bridge, *, created=NOW-100, expires=NOW+1, kind='task', match=None):
    async with bridge.db._engine.begin() as conn:
        await conn.execute(insert(agent_waits).values(
            id='old', project_id='p', owner_kind='task', owner_id='owner',
            session_id='s', session_instance_token='token', claim_epoch=1,
            kind=kind, match=match or {'task_id': 'source'}, created_at=created,
            deadline_at=expires, idempotency_key='old',
        ))


@pytest.mark.parametrize('remaining', [-50, 0, 1, 59, 500])
async def test_preserve_exact_age_and_deadline_and_replay(bridge, remaining):
    await legacy(bridge, expires=NOW+remaining)
    result = await bridge.adopt_agent_wait('old', handler=None)
    again = await bridge.adopt_agent_wait('old', handler=None)
    assert again['replayed'] and again['wait_id'] == result['wait_id']
    async with bridge.db._engine.connect() as conn:
        row = (await conn.execute(select(wait_records))).mappings().one()
        deadline = await conn.scalar(select(wait_deadlines.c.due_at).where(
            wait_deadlines.c.deadline_type == 'escalation',
        ))
        assert row['created_at'] == NOW-100
        assert row['escalate_at'] == deadline == NOW+remaining
        assert row['outcome'] == 'pending'
        assert row['sleep_decision'] == 'in_place'
        assert row['legacy_id'] == 'old'
        assert row['decision_inputs']['registration_receipt']['escalate_at'] == deadline


async def test_missing_source_finishes_through_v1_only(bridge):
    await legacy(bridge, match={'task_id': 'missing'})
    result = await bridge.adopt_agent_wait('old', handler=None)
    assert result['source_unavailable']
    async with bridge.db._engine.connect() as conn:
        assert (await conn.execute(select(wait_records))).first() is None
        row = (await conn.execute(select(agent_waits))).mappings().one()
        assert row['state'] == 'satisfied'
        assert row['digest']['reason'] == 'source_unavailable'
        assert len((await conn.execute(select(messages))).all()) == 1


async def test_elapsed_timer_commits_one_v2_outcome(bridge):
    await legacy(bridge, kind='timer', match={'due_at': NOW-10})
    first = await bridge.adopt_agent_wait('old', handler=None)
    await bridge.adopt_agent_wait('old', handler=None)
    async with bridge.db._engine.connect() as conn:
        row = (await conn.execute(select(wait_records))).mappings().one()
        assert row['outcome'] == 'satisfied'
        assert row['id'] == first['wait_id']
        outbox = (await conn.execute(select(messages))).mappings().all()
        assert len(outbox) == 1 and outbox[0]['body_kind'] == 'wait_outcome'


def test_bridge_modes_default_to_read_only_agent_waits():
    from pydantic import ValidationError
    from src.commands.contracts.wait_v2 import WaitLegacyBridgeArgs

    assert WaitLegacyBridgeArgs().mode == 'agent-waits'
    assert WaitLegacyBridgeArgs().apply is False
    assert WaitLegacyBridgeArgs(mode='project').mode == 'project'
    with pytest.raises(ValidationError):
        WaitLegacyBridgeArgs(mode='all')


def test_invalid_legacy_interval_and_identity_refuse():
    with pytest.raises(WaitError):
        legacy_times({'created_at': 1, 'deadline_at': 1})
    with pytest.raises(WaitError):
        legacy_condition({'kind': 'task', 'match': {'text': 'looks finished'}})


async def test_v1_reconciler_skips_adopted_wait_even_after_old_expiry(bridge):
    await legacy(bridge, expires=NOW-1)
    await bridge.adopt_agent_wait('old', handler=None)
    result = await bridge.db.reconcile_agent_waits(now=NOW+100)
    assert result == {'scanned': 0, 'resolved': 0, 'outbox_retried': 0}
    async with bridge.db._engine.connect() as conn:
        assert await conn.scalar(select(agent_waits.c.state)) == 'active'
        assert (await conn.execute(select(messages))).first() is None


async def test_stale_claim_cannot_be_adopted(bridge):
    from sqlalchemy import update
    from src.database.tables import tasks

    await legacy(bridge)
    async with bridge.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(claim_epoch=2))
    with pytest.raises(WaitError, match='current live claim'):
        await bridge.adopt_agent_wait('old', handler=None)
    async with bridge.db._engine.connect() as conn:
        assert (await conn.execute(select(wait_records))).first() is None


async def test_undelivered_v1_timer_result_is_replaced_atomically(bridge):

    await legacy(bridge, kind='timer', match={'due_at': NOW-10})
    await bridge.db.reconcile_agent_waits(now=NOW)
    async with bridge.db._engine.connect() as conn:
        old_message = await conn.scalar(select(agent_waits.c.result_message_id))
    first = await bridge.adopt_agent_wait('old', handler=None)
    await bridge.adopt_agent_wait('old', handler=None)
    async with bridge.db._engine.connect() as conn:
        outbox = (await conn.execute(select(messages))).mappings().all()
        assert len(outbox) == 2
        assert next(row for row in outbox if row['id'] == old_message)['archived_at'] == NOW
        assert next(row for row in outbox if row['id'] != old_message)['id'] == (
            f"wait:{first['wait_id']}:outcome")


async def test_hibernate_records_intent_and_suspend_message(bridge):
    from sqlalchemy import update
    from src.database.tables import continuations, tasks

    bridge.orch.config.wait_policy.hibernation_enabled = True
    bridge.orch.config.wait_policy.kinds['task'].expected_duration_seconds = 7200
    await legacy(bridge)
    async with bridge.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(branch_name='aq/owner'))
    result = await bridge.adopt_agent_wait('old', handler=None)
    async with bridge.db._engine.connect() as conn:
        intent = (await conn.execute(select(continuations))).mappings().one()
        assert intent['wait_id'] == result['wait_id']
        assert intent['stopped_at'] is None
        assert await conn.scalar(select(messages.c.body_kind)) == 'wait_suspend'
        assert await conn.scalar(select(tasks.c.status).where(tasks.c.id == 'owner')) == 'WAITING'


@pytest.mark.parametrize('enabled', [False, True])
async def test_sessionless_waiting_input_is_explicit_checkpoint_hold(bridge, enabled):
    from src.database.tables import continuations, tasks
    from src.models import TaskStatus
    from src.wait_runtime.legacy_human import adopt_waiting_input

    bridge.orch.config.wait_policy.hibernation_enabled = enabled
    await bridge.db.create_task(Task(id='human', project_id='p', title='Human', description='',
                                    status=TaskStatus.WAITING_INPUT, branch_name='aq/human'))
    if not enabled:
        with pytest.raises(WaitError, match='requires verified hibernation'):
            await adopt_waiting_input(bridge, 'human')
        async with bridge.db._engine.connect() as conn:
            assert (await conn.execute(select(wait_records))).first() is None
        return
    first = await adopt_waiting_input(bridge, 'human')
    second = await adopt_waiting_input(bridge, 'human')
    assert second['wait_id'] == first['wait_id']
    assert first['hold_reason'] == 'legacy_checkpoint_unverified'
    async with bridge.db._engine.connect() as conn:
        intent = (await conn.execute(select(continuations))).mappings().one()
        assert intent['summary_degraded'] and intent['resume_state'] == 'held'
        assert intent['stopped_at'] is None and intent['saved_head'] is None
        assert 'workspace_released_at' not in intent['facts']
        assert await conn.scalar(select(tasks.c.status).where(tasks.c.id == 'human')) == 'HIBERNATED'
        wait = (await conn.execute(select(wait_records))).mappings().one()
        assert wait['condition_kind'] == 'human' and wait['escalate_at'] is None
        assert (await conn.execute(select(wait_deadlines))).first() is None


async def test_live_waiting_input_uses_exact_writer_with_degraded_intent(bridge):
    from sqlalchemy import update
    from src.database.tables import continuations, tasks
    from src.wait_runtime.legacy_human import adopt_waiting_input

    bridge.orch.config.wait_policy.hibernation_enabled = True
    async with bridge.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            status='WAITING_INPUT', branch_name='aq/owner', updated_at=NOW-30))
    async with bridge.db._engine.begin() as conn:
        await conn.execute(insert(messages).values(
            id='question', project_id='p', from_kind='session', from_id='s',
            to_kind='user', to_id='dashboard', body='Decision?', created_at=NOW-30))
    result = await adopt_waiting_input(bridge, 'owner')
    async with bridge.db._engine.connect() as conn:
        row = (await conn.execute(select(wait_records))).mappings().one()
        intent = (await conn.execute(select(continuations))).mappings().one()
        assert row['session_id'] == 's' and row['claim_epoch'] == 1
        assert row['condition']['_verified']['after_at'] == NOW-30
        assert row['condition']['_verified']['task_created_at'] == await conn.scalar(
            select(tasks.c.created_at).where(tasks.c.id == 'owner'))
        assert row['condition']['_verified']['source_watermark_seq'] == (
            row['source_watermark_seq'])
        assert intent['wait_id'] == result['wait_id'] and intent['summary_degraded']
        assert intent['prior_status'] == 'WAITING_INPUT'
        assert intent['stopped_at'] is None and intent['saved_head'] is None
        assert await conn.scalar(select(tasks.c.status).where(tasks.c.id == 'owner')) == 'WAITING'


async def test_escalation_projection_uses_canonical_resolution_and_replays(bridge):
    from src.database.tables import wait_source_events
    from src.wait_runtime.legacy_projection import project_escalation
    from tests.test_escalation_queries import incident

    source, _ = await bridge.db.create_escalation(**incident())
    first = await project_escalation(bridge, source['id'], project_id='p')
    again = await project_escalation(bridge, source['id'], project_id='p')
    assert first['wait_id'] == again['wait_id'] and again['replayed']
    resolved = await bridge.db.resolve_escalation_on_recovery(
        source['id'], expected_revision=source['revision'], source_kind=source['source_kind'],
        terminal_outcome='source recovered', terminal_evidence={'checked': True}, now=NOW,
    )
    async with bridge.db._engine.begin() as conn:
        wait = dict((await conn.execute(select(wait_records))).mappings().one())
        event = dict((await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key == 'escalation:esc-1',
        ))).mappings().one())
        await bridge.service.dispatcher.commit(conn, wait, event)
        await bridge.service.dispatcher.commit(conn, wait, event)
    async with bridge.db._engine.connect() as conn:
        assert await conn.scalar(select(wait_records.c.outcome)) == 'satisfied'
        assert await conn.scalar(select(wait_records.c.escalate_at)) is None
        # Projection has no task/session recipient and must not launch work.
        assert not (await conn.execute(select(messages).where(
            messages.c.body_kind == 'wait_outcome',
        ))).first()
    assert (await bridge.db.get_escalation(source['id']))['revision'] == resolved['revision']


async def test_escalation_internal_binding_is_not_a_public_human_subject(bridge):
    from src.wait_runtime.legacy_projection import project_escalation
    from tests.test_escalation_queries import incident

    await bridge.db.create_escalation(**incident())
    with pytest.raises(WaitError):
        bridge.service.registry.normalise({'kind': 'human', 'subject': 'legacy_escalation',
                                           'escalation_id': 'esc-1'})
    with pytest.raises(WaitError):
        await project_escalation(bridge, 'esc-1', project_id='foreign')
    async with bridge.db._engine.connect() as conn:
        assert not (await conn.execute(select(wait_records))).first()


async def test_bridge_preview_and_disabled_apply_write_no_receipts(bridge):
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import wait_bridge_progress

    await legacy(bridge)
    preview = await bridge.run({'project_id': 'p'}, principal=TRUSTED_LOCAL)
    assert preview['result']['projects']['p'] == [{'kind': 'agent_wait', 'id': 'old'}]
    bridge.orch.config.wait_policy.v2_enabled = False
    result = await bridge.run({'project_id': 'p', 'apply': True}, principal=TRUSTED_LOCAL)
    assert result['result']['projects']['p']['state'] == 'disabled'
    async with bridge.db._engine.connect() as conn:
        assert not (await conn.execute(select(wait_records))).first()
        assert not (await conn.execute(select(wait_bridge_progress))).first()


async def test_sessionless_verified_checkpoint_uses_server_receipt_without_fake_stop(bridge):
    from unittest.mock import AsyncMock
    from sqlalchemy import update
    from src.database.tables import continuations, task_integration_checkpoints, tasks
    from src.models import RepoConfig, RepoSourceType, TaskStatus
    from src.wait_runtime.legacy_human import adopt_waiting_input
    from src.wait_runtime.legacy_parking import legacy_parked
    from src.waits.ownership import fence_owner

    bridge.orch.config.wait_policy.hibernation_enabled = True
    await bridge.db.create_repo(RepoConfig(id='repo', project_id='p', url='https://example.test/r',
                                           source_type=RepoSourceType.CLONE))
    await bridge.db.create_task(Task(id='human', project_id='p', title='Human', description='',
                                    status=TaskStatus.WAITING_INPUT, branch_name='aq/human'))
    head = 'a' * 40
    bridge.orch.git = SimpleNamespace(bind_github_repository=AsyncMock(return_value='binding'),
                                     aremote_branch_head=AsyncMock(return_value=head))
    async with bridge.db._engine.begin() as conn:
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id='human', repository_id='repo', branch='aq/human', checkpoint_sha=head,
            verified_sha=head, generation=1, verified_generation=1, version=2, updated_at=NOW))
    from src.database.tables import sessions

    async with bridge.db._engine.begin() as conn:
        historical = dict((await conn.execute(select(sessions))).mappings().one())
        historical.update(id='old-human', task_id='human', state='stopped',
                          desired_state='stopped', agent_id=None, instance_token='historical')
        await conn.execute(insert(sessions).values(**historical))
        await conn.execute(insert(messages).values(
            id='question', project_id='p', from_kind='session', from_id='old-human',
            to_kind='user', to_id='dashboard', body='Decision?', created_at=NOW-30))
    result = await adopt_waiting_input(bridge, 'human')
    assert result['hold_reason'] is None
    bridge.service.command = AsyncMock()
    await bridge.service.lifecycle.tick()
    bridge.service.command.assert_not_awaited()
    async with bridge.db._engine.begin() as conn:
        intent = (await conn.execute(select(continuations))).mappings().one()
        wait = (await conn.execute(select(wait_records))).mappings().one()
        task = (await conn.execute(select(tasks).where(tasks.c.id == 'human'))).mappings().one()
        assert intent['stopped_at'] is None and intent['saved_head'] == head
        assert 'workspace_released_at' not in intent['facts']
        assert await legacy_parked(conn, task, wait, intent)
        await fence_owner(conn, wait, task, parked_required=True)
        assert not await legacy_parked(conn, {**task, 'claim_epoch': task['claim_epoch']+1}, wait, intent)
        assert not await legacy_parked(conn, task, {**wait, 'legacy_kind': None}, intent)
        await conn.execute(update(task_integration_checkpoints).values(version=3))
        assert not await legacy_parked(conn, task, wait, intent)


@pytest.mark.parametrize('state,terminal,expected', [
    ('reply_received', False, 'pending'), ('resolved', True, 'satisfied'),
    ('cancelled', True, 'source_unavailable'), ('stale', True, 'source_unavailable'),
])
async def test_escalation_projection_observes_owner_state_not_transport(bridge, state, terminal, expected):
    from sqlalchemy import update
    from src.database.tables import escalations, wait_source_events
    from src.wait_runtime.legacy_projection import project_escalation
    from src.waits.adapters.legacy_escalation import execute_mutation
    from tests.test_escalation_queries import incident

    source, _ = await bridge.db.create_escalation(**incident())
    await project_escalation(bridge, source['id'], project_id='p')
    async with bridge.db._engine.begin() as conn:
        await execute_mutation(conn, update(escalations).values(
            state=state, revision=1, updated_at=NOW,
            terminal_at=NOW if terminal else None,
            terminal_outcome='canonical outcome' if terminal else None))
        wait = dict((await conn.execute(select(wait_records))).mappings().one())
        event = dict((await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key == 'escalation:esc-1',
        ))).mappings().one())
        await bridge.service.dispatcher.commit(conn, wait, event)
        assert await conn.scalar(select(wait_records.c.outcome)) == expected
        assert await conn.scalar(select(escalations.c.state)) == state


async def test_escalation_projection_rejects_supervisor_lift(bridge):
    from src.commands.principal import TRUSTED_LOCAL
    from src.wait_runtime.legacy_projection import project_escalation
    from tests.test_escalation_queries import incident

    source, _ = await bridge.db.create_escalation(**incident())
    result = await project_escalation(bridge, source['id'], project_id='p')
    with pytest.raises(WaitError, match='owning service'):
        await bridge.service.human.lift({'wait_id': result['wait_id']},
            principal=TRUSTED_LOCAL, scope={'project_id': 'p'}, handler=None)


async def test_escalation_resolved_before_registration_has_one_terminal_projection(bridge):
    from src.wait_runtime.legacy_projection import project_escalation
    from tests.test_escalation_queries import incident

    source, _ = await bridge.db.create_escalation(**incident())
    await bridge.db.resolve_escalation_on_recovery(
        source['id'], expected_revision=0, source_kind=source['source_kind'],
        terminal_outcome='recovered', terminal_evidence={'checked': True}, now=NOW)
    first = await project_escalation(bridge, source['id'], project_id='p')
    replay = await project_escalation(bridge, source['id'], project_id='p')
    assert first['wait_id'] == replay['wait_id']
    async with bridge.db._engine.connect() as conn:
        row = (await conn.execute(select(wait_records))).mappings().one()
        assert row['outcome'] == 'satisfied'
        assert row['source_watermark_seq'] > 0


@pytest.mark.parametrize('change', ['generation', 'missing', 'owner', 'revision'])
async def test_escalation_binding_rejects_changed_or_missing_source(bridge, change):
    from sqlalchemy import delete, update
    from src.database.tables import escalations
    from src.wait_runtime.legacy_projection import project_escalation
    from src.waits.conditions import Condition
    from tests.test_escalation_queries import incident

    source, _ = await bridge.db.create_escalation(**incident())
    async with bridge.db._engine.begin() as conn:
        await conn.execute(update(escalations).values(revision=2))
    await project_escalation(bridge, source['id'], project_id='p')
    async with bridge.db._engine.begin() as conn:
        cond = Condition.from_record(await conn.scalar(select(wait_records.c.condition)))
        if change == 'missing':
            await conn.execute(delete(escalations))
        else:
            values = {'generation': {'created_at': NOW+100},
                      'owner': {'supervisor_owner': 'other'}, 'revision': {'revision': 1}}[change]
            await conn.execute(update(escalations).values(**values))
        observed = await bridge.service.registry.observe(conn, cond)
        assert observed.state == 'unavailable'
        assert await conn.scalar(select(wait_records.c.outcome)) == 'pending'


async def test_transport_and_supervisor_comment_do_not_resolve_projection(bridge):
    from src.database.tables import wait_source_events
    from src.wait_runtime.legacy_projection import project_escalation
    from src.waits.conditions import Condition
    from tests.test_escalation_queries import incident

    source, _ = await bridge.db.create_escalation(**incident())
    await project_escalation(bridge, source['id'], project_id='p')
    async with bridge.db._engine.begin() as conn:
        await conn.execute(insert(messages).values(
            id='comment', project_id='p', from_kind='session', from_id='supervisor-p',
            to_kind='user', to_id='dashboard', thread_id='transport-thread',
            body='approved', created_at=NOW))
        cond = Condition.from_record(await conn.scalar(select(wait_records.c.condition)))
        assert (await bridge.service.registry.observe(conn, cond)).state == 'unsatisfied'
        assert not (await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key == 'escalation:esc-1'))).first()
    assert (await bridge.db.get_escalation(source['id']))['revision'] == 0


@pytest.mark.parametrize('state', ['expired', 'cancelled'])
async def test_legacy_terminal_cancellation_is_not_reinterpreted_as_satisfaction(bridge, state):
    from sqlalchemy import update

    await legacy(bridge)
    async with bridge.db._engine.begin() as conn:
        await conn.execute(update(agent_waits).values(state=state, resolved_at=NOW))
    await bridge.adopt_agent_wait('old', handler=None)
    async with bridge.db._engine.connect() as conn:
        row = (await conn.execute(select(wait_records))).mappings().one()
        assert row['outcome'] == 'cancelled'
        assert row['decision_inputs']['legacy_terminal']['state'] == state


async def test_escalation_resolution_between_watermark_and_registration_is_not_lost(bridge, monkeypatch):
    from src.database.tables import wait_source_events
    from src.wait_runtime.legacy_projection import project_escalation
    from tests.test_escalation_queries import incident

    source, _ = await bridge.db.create_escalation(**incident())
    watermark = bridge.db.wait_source_watermark
    async def resolve_after_watermark():
        seq = await watermark()
        await bridge.db.resolve_escalation_on_recovery(
            source['id'], expected_revision=0, source_kind=source['source_kind'],
            terminal_outcome='racing recovery', terminal_evidence={'checked': True}, now=NOW)
        return seq
    monkeypatch.setattr(bridge.db, 'wait_source_watermark', resolve_after_watermark)
    await project_escalation(bridge, source['id'], project_id='p')
    async with bridge.db._engine.begin() as conn:
        wait = dict((await conn.execute(select(wait_records))).mappings().one())
        event = dict((await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key == 'escalation:esc-1'))).mappings().one())
        assert wait['outcome'] == 'satisfied'
        assert event['seq'] > wait['source_watermark_seq']
        replay = await bridge.service.dispatcher.commit(conn, wait, event)
        assert not replay['committed']


async def test_unavailable_projection_replay_preserves_cutover_hold(bridge):
    from sqlalchemy import update
    from src.database.tables import escalations
    from src.wait_runtime.legacy_projection import project_escalation
    from tests.test_escalation_queries import incident

    source, _ = await bridge.db.create_escalation(**incident())
    async with bridge.db._engine.begin() as conn:
        await conn.execute(update(escalations).values(
            state='cancelled', terminal_at=NOW, terminal_outcome='cancelled'))
    first = await project_escalation(bridge, source['id'], project_id='p')
    replay = await project_escalation(bridge, source['id'], project_id='p')
    assert first['hold_reason'] == replay['hold_reason'] == 'source_unavailable'
    assert first['wait_id'] == replay['wait_id']
