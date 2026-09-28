import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.integration.parent_ci import publish_parent_snapshot

from sqlalchemy import select, update
from tests.test_integration_ci import ci_db as ci_db, trust, payload_dict, policy_snapshot, SHA
from src.database.tables import (
    integration_check_evidence, integration_outbox, integration_repair_operations,
    task_integration_checkpoints,
)
from src.git.github_contracts import GitHubCredentialIdentity, GitHubRepositoryBinding
from src.integration.attestation import IntegrationAttestationService
from src.integration.ci import TrustedFixtureObserver, TrustedCIObservation, AttestationPayload
from src.integration.parent_ci import ParentCIService


@pytest.mark.parametrize('old', [None, 'a' * 40])
async def test_parent_snapshot_is_exact_and_idempotent(tmp_path, old):
    git = SimpleNamespace(
        afetch_repository_oid=AsyncMock(return_value='a' * 40),
        apush_repository_oid=AsyncMock(return_value='a' * 40),
    )
    client = SimpleNamespace(
        exact_head_ref=AsyncMock(
            side_effect=[None, 'a' * 40] if old is None else [old]
        ),
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
        push = git.apush_repository_oid.call_args.kwargs
        assert push['tip_oid'] == 'a' * 40
        assert push['expected_old_oid'] == '0' * 40
    else:
        git.apush_repository_oid.assert_not_called()


@pytest.mark.parametrize('old,current', [('b' * 40, True), (None, False)])
async def test_parent_snapshot_never_overwrites_or_publishes_stale_subject(tmp_path, old, current):
    git = SimpleNamespace(
        afetch_repository_oid=AsyncMock(return_value='a' * 40),
        apush_repository_oid=AsyncMock(),
    )
    client = SimpleNamespace(
        exact_head_ref=AsyncMock(return_value=old),
        installation_token=AsyncMock(return_value=None), repository=object(),
    )
    assert not await publish_parent_snapshot(
        git, client, tmp_path, 'aq/parent/parent/episode/1/' + 'a' * 40,
        'a' * 40, AsyncMock(return_value=current),
    )
    git.apush_repository_oid.assert_not_called()



@pytest.mark.parametrize('stale', [False, True])
async def test_legacy_parent_tick_publishes_exact_ci_and_fenced_event(ci_db, tmp_path, monkeypatch, stale):
    observation = TrustedCIObservation(
        payload=AttestationPayload.model_validate(payload_dict()), workflow_ids={21: 301, 22: 302}
    )
    monkeypatch.setattr('src.integration.parent_ci.AuthenticatedGitHubObserver',
                        lambda client, **_kwargs: TrustedFixtureObserver(observation))
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
        db=ci_db, git=SimpleNamespace(afetch_repository_oid=fetch,
                                    apush_repository_oid=AsyncMock(side_effect=push)),
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
        backend.git.apush_repository_oid.assert_awaited_once()


async def test_first_parent_initializes_a_real_bare_ci_store(tmp_path):
    from src.git.manager import GitManager
    from src.integration.parent_ci import ensure_parent_store

    store = tmp_path / 'integration-repositories' / 'new-repository.git'
    git = GitManager()
    await ensure_parent_store(git, store)
    await ensure_parent_store(git, store)
    result = await git.arun_git_result(['rev-parse', '--is-bare-repository'], cwd=str(store))
    assert result.returncode == 0 and result.stdout.strip() == 'true'


class _AppParentTree:
    """The parent subject tree, read and published through the daemon's App."""

    def __init__(self, manifest):
        self.manifest = manifest
        self.pushed = {}

    async def afetch_repository_oid(self, _store, **kwargs):
        return kwargs['oid']

    async def apush_repository_oid(self, _store, **kwargs):
        self.pushed[kwargs['branch']] = kwargs['tip_oid']
        return kwargs['tip_oid']

    async def arun_git_result(self, args, **_kwargs):
        if args[0] == 'init':
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        assert args == ['show', f'{SHA}:.github/agent-queue-integration.json']
        if self.manifest is None:
            return SimpleNamespace(returncode=128, stdout='', stderr='fatal: path does not exist')
        return SimpleNamespace(returncode=0, stdout=self.manifest, stderr='')


class _AppParentClient:
    auth_mode = 'gh'

    def __init__(self, tree):
        self.credential_identity = GitHubCredentialIdentity.app(101, 202)
        self.repository = GitHubRepositoryBinding(303, 'acme/widgets')
        self.tree = tree
        self.asked = []

    async def installation_token(self):
        return 'installation-token'

    async def exact_head_ref(self, branch):
        return self.tree.pushed.get(branch)

    async def paged_items(self, path, *, key, max_pages=20):
        if key == 'workflow_runs':
            return [{'id': 31, 'workflow_id': 301, 'run_attempt': 1, 'check_suite_id': 21,
                     'head_sha': SHA, 'status': 'completed', 'conclusion': 'success',
                     'event': 'push'}]
        name = path.split('check_name=', 1)[1].split('&', 1)[0]
        self.asked.append(name)
        return [{'id': 11, 'name': name, 'head_sha': SHA, 'status': 'completed',
                 'conclusion': 'success', 'app': {'id': 404}, 'check_suite': {'id': 21}}]


def _app_parent_service(ci_db, tmp_path, manifest):
    tree = _AppParentTree(manifest)
    client = _AppParentClient(tree)
    attestation = IntegrationAttestationService(
        ci_db, data_dir=tmp_path, git_manager=tree,
        github_client_factory=lambda binding: client, clock=lambda: 100.0,
    )
    return ParentCIService(attestation, AsyncMock(return_value=client.repository)), client


async def test_app_parent_observes_its_snapshot_checks_not_the_tree_manifest(ci_db, tmp_path):
    # The tree's manifest lists the root set; the parent boundary is narrower.
    policy = policy_snapshot()
    policy['parent'] = {
        'required_checks': {'version': 'focused-v1', 'names': ['unit'], 'producer_id': '404'}
    }
    async with ci_db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == 'parent-op')
            .values(policy_snapshot=policy, required_check_version='focused-v1')
        )
    manifest = json.dumps(trust().model_dump(mode='json', by_alias=True))
    service, client = _app_parent_service(ci_db, tmp_path, manifest)

    await service.tick(100.0)

    assert client.asked == ['unit']
    assert list(client.tree.pushed.values()) == [SHA]
    async with ci_db._engine.connect() as conn:
        evidence = (await conn.execute(select(integration_check_evidence))).mappings().all()
        events = (await conn.execute(select(integration_outbox))).mappings().all()
    assert [(row['required_check_version'], row['checks']) for row in evidence] == [
        ('focused-v1', {'unit': 'success'})
    ]
    assert [event['payload']['conclusion'] for event in events] == ['success']
    assert await service.attestation.subject_trust_blockers('p') == []


async def test_app_parent_tree_without_manifest_is_named_in_status(ci_db, tmp_path, caplog):
    service, client = _app_parent_service(ci_db, tmp_path, None)

    with caplog.at_level(logging.WARNING, logger='src.integration'):
        await service.tick(100.0)

    assert client.tree.pushed == {} and client.asked == []
    async with ci_db._engine.connect() as conn:
        assert (await conn.execute(select(integration_outbox))).mappings().all() == []
    blockers = await service.attestation.subject_trust_blockers('p')
    assert [{k: v for k, v in blocker.items() if k != 'detail'} for blocker in blockers] == [{
        'code': 'subject_trust_invalid', 'ref': 'parent-op', 'target_kind': 'parent',
        'subject': {'parent_task_id': 'parent', 'generation': 3}, 'head_sha': SHA,
        'cause': 'missing', 'fields': [],
    }]
    assert blockers[0]['detail'].startswith(f'parent parent generation 3 at {SHA}: ')
    # One named warning instead of a retry traceback every poll.
    assert 'Integration subject trust invalid' in caplog.text
    assert 'Parent CI remains retryable' not in caplog.text
