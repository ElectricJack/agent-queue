from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.integration.parent_ci import publish_parent_snapshot

from sqlalchemy import select, update
from tests.test_integration_ci import ci_db as ci_db, trust, payload_dict, SHA
from src.database.tables import integration_outbox, task_integration_checkpoints
from src.integration.ci import TrustedFixtureObserver, TrustedCIObservation, AttestationPayload
from src.integration.parent_ci import ParentCIService


@pytest.mark.parametrize('old', [None, 'a' * 40])
async def test_parent_snapshot_is_exact_and_idempotent(tmp_path, old):
    git = SimpleNamespace(
        afetch_exact_oid_with_app_auth=AsyncMock(return_value='a' * 40),
        apush_oid_with_app_auth=AsyncMock(return_value='a' * 40),
    )
    client = SimpleNamespace(
        exact_head_ref=AsyncMock(return_value=old),
        installation_token=AsyncMock(return_value=None),
        repository=object(),
    )
    current = AsyncMock(return_value=True)
    result = await publish_parent_snapshot(
        git, client, tmp_path, 'aq/parent/parent/episode/1/' + 'a' * 40,
        'a' * 40, current,
    )
    assert result is True
    if old is None:
        push = git.apush_oid_with_app_auth.call_args.kwargs
        assert push['tip_oid'] == 'a' * 40
        assert push['expected_old_oid'] == '0' * 40
    else:
        git.apush_oid_with_app_auth.assert_not_called()


@pytest.mark.parametrize('old,current', [('b' * 40, True), (None, False)])
async def test_parent_snapshot_never_overwrites_or_publishes_stale_subject(tmp_path, old, current):
    git = SimpleNamespace(
        afetch_exact_oid_with_app_auth=AsyncMock(return_value='a' * 40),
        apush_oid_with_app_auth=AsyncMock(),
    )
    client = SimpleNamespace(
        exact_head_ref=AsyncMock(return_value=old),
        installation_token=AsyncMock(return_value=None), repository=object(),
    )
    assert not await publish_parent_snapshot(
        git, client, tmp_path, 'aq/parent/parent/episode/1/' + 'a' * 40,
        'a' * 40, AsyncMock(return_value=current),
    )
    git.apush_oid_with_app_auth.assert_not_called()



@pytest.mark.parametrize('stale', [False, True])
async def test_legacy_parent_tick_publishes_exact_ci_and_fenced_event(ci_db, tmp_path, monkeypatch, stale):
    observation = TrustedCIObservation(
        payload=AttestationPayload.model_validate(payload_dict()), workflow_ids={21: 301, 22: 302}
    )
    monkeypatch.setattr('src.integration.parent_ci.AuthenticatedGitHubObserver',
                        lambda client: TrustedFixtureObserver(observation))
    remote = {}
    async def exact(branch):
        return remote.get(branch)
    async def push(_store, **kwargs):
        remote[kwargs['branch']] = kwargs['tip_oid']
        return kwargs['tip_oid']
    async def fetch(_store, **kwargs):
        if stale:
            async with ci_db.immediate() as conn:
                await conn.execute(update(task_integration_checkpoints).values(generation=4))
        return kwargs['oid']
    client = SimpleNamespace(exact_head_ref=exact, repository=object(),
                             installation_token=AsyncMock(return_value=None))
    backend = SimpleNamespace(
        db=ci_db, git=SimpleNamespace(afetch_exact_oid_with_app_auth=fetch,
                                    apush_oid_with_app_auth=AsyncMock(side_effect=push)),
        _load_trust=AsyncMock(return_value=(trust(), client)),
        _store=lambda _: tmp_path, clock=lambda: 100.0,
    )
    service = ParentCIService(backend, AsyncMock(return_value=SimpleNamespace(
        repository_id=303, full_name='acme/widgets',
    )))
    await service.tick(100.0)
    if not stale:
        service.after = ''
        await service.tick(140.0)
    async with ci_db._engine.connect() as conn:
        events = (await conn.execute(select(integration_outbox))).mappings().all()
    if stale:
        assert not remote and not events
    else:
        assert len(remote) == 1
        assert list(remote)[0].startswith('aq/parent/parent/')
        assert list(remote.values()) == [SHA]
        assert len(events) == 1
        assert events[0]['payload']['conclusion'] == 'success'
        assert events[0]['payload']['head_sha'] == SHA
        assert events[0]['payload']['generation'] == 3
        assert len(events[0]['payload']['evidence_ids']) == 2
        backend.git.apush_oid_with_app_auth.assert_awaited_once()


async def test_first_parent_initializes_a_real_bare_ci_store(tmp_path):
    from src.git.manager import GitManager
    from src.integration.parent_ci import ensure_parent_store

    store = tmp_path / 'integration-repositories' / 'new-repository.git'
    git = GitManager()
    await ensure_parent_store(git, store)
    await ensure_parent_store(git, store)
    result = await git.arun_git_result(['rev-parse', '--is-bare-repository'], cwd=str(store))
    assert result.returncode == 0 and result.stdout.strip() == 'true'
