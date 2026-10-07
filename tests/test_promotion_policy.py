"""Reviewed promotion policies on real Git/PostgreSQL release and hotfix lanes."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from src.commands.contracts import CONTRACTS
from src.commands.contracts.models import CommandResult
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import projects
from src.integration.git_truth import GitTruth
from src.integration.promotion_notes import draft_notes
from src.integration.promotion_steps import (
    FlowSchema, PromotionChecks, PromotionVisit, StepAdmission, StepPullRequestGate,
)
from src.integration.train import TrainLane, TrainTarget
from src.integration.train_sources import DatabaseBatches
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.run_state import RunLifecycle
from src.playbooks.services import resolve_integration_route
from src.profiles.capabilities import DENY_ALL
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore, RecordingBus, RecordingRunRepository, StubActivations, artifact_ref_for,
)
from tests.test_integration_gitops import commit, git, setup as setup
from tests.test_promotion_backmerge import (
    MultipleIntentGitHub, source_world,
)
from tests.test_promote_commands import HumanGitHub, promote_env as promote_env
from tests.test_promotion_steps import promotion as promotion


class ChainGitHub(MultipleIntentGitHub):
    def __init__(self, env):
        super().__init__(env)
        self.review_sets = {}

    async def create_pull_request(self, **kwargs):
        url = await super().create_pull_request(**kwargs)
        self.review_sets.setdefault(int(url.rsplit('/', 1)[1]), [])
        return url

    async def paged_list(self, path):
        number = int(path.split('/pulls/', 1)[1].split('/', 1)[0])
        return copy.deepcopy(self.review_sets[number])

    async def close_pull_request(self, *, number):
        self.by_number[number]['state'] = 'closed'


class ChainHuman(HumanGitHub):
    async def request_json(self, method, path, *, body=None, **kwargs):
        if method == 'POST' and path.endswith('/reviews'):
            body = kwargs['json_body']
            number = int(path.split('/pulls/', 1)[1].split('/', 1)[0])
            review = self.app.review(len(self.app.review_sets[number]) + 1,
                                     body['commit_id'], login=self.login)
            self.app.review_sets[number].append(review)
            return copy.deepcopy(review)
        return await super().request_json(method, path, body=body, **kwargs)


async def policy_engine(e, kind):
    artifact = load_definition_json(Path(
        f'tests/fixtures/playbooks/v2/promotion-{kind}/artifact.json').read_text())
    store, runs = InMemoryArtifactStore(), RecordingRunRepository()
    store.put(artifact)
    registry = ContractRegistry()
    calls = []
    for name in {str(step.command) for step in artifact.steps.values() if step.type == 'command'}:
        registration = CONTRACTS.get(name)

        async def invoke(args, ctx, name=name, contract=registration.contract.execution):
            calls.append((name, args.model_dump(mode='json')))
            if name == 'message_send':
                raw = {'outcome': 'queued', 'message_id': 'notice', 'state': 'queued'}
            else:
                with principal_context(ctx):
                    raw = await getattr(e.handler, '_cmd_' + name)(args.model_dump(mode='json'))
            value = contract.result_model(**{k: v for k, v in raw.items()
                                             if k in contract.result_model.model_fields})
            return CommandResult(outcome=raw['outcome'], value=value, summary=raw.get('error', ''))

        registry.register(CommandRegistration(name, registration.contract, invoke))
    ref = artifact_ref_for(artifact)
    engine = PlaybookEngine(services=EngineServices(contracts=registry, artifact_store=store,
        bus=RecordingBus(), clock=lambda: 1000.0), runs=runs, waits=None,
        activations=StubActivations([ref]))
    principal = ExecutionPrincipal(kind=PrincipalKind.SERVICE, policy=DENY_ALL,
                                   service_name='fixture-promotion', project_id='p')

    async def run(rule, *, step='release', **facts):
        event = {'project_id': 'p', 'step_id': step, 'notes_reviewed': False, **facts}
        outcome = await engine.run_rule(ref, rule, event, principal)
        assert outcome.lifecycle is RunLifecycle.COMPLETED, (outcome, runs.receipts)
        return outcome

    return SimpleNamespace(run=run, calls=calls, runs=runs, artifact=artifact, ref=ref)


async def chain_lanes(e, flow):
    lanes = {}
    truth = GitTruth(e.ops.git)
    for step in flow:
        target = TrainTarget('p', 'r', 'refs/heads/' + step['target'], 'promotion', step=step)
        admission = StepAdmission(e.db, e.ops, step)
        exact = SimpleNamespace(read=AsyncMock(return_value=SimpleNamespace(green=True)),
                                refresh=AsyncMock())
        checks = PromotionChecks(admission, AsyncMock(return_value=exact),
                                 pull_request=lambda _: StepPullRequestGate(e.github, e.repo.binding))

        async def snapshot(target=target):
            return await truth.snapshot(str(e.repo.store), project_id='p', repository_id='r',
                repository_url=str(e.ops.git.remote_path), target_ref=target.target_ref)

        service = PromotionVisit(e.store, e.ops, admission=admission, checks=checks,
            publish=e.service.publish, publish_tag=e.service.publish_tag, delete_ref=e.service.delete_ref,
            attest=AsyncMock(return_value='published'), snapshot=snapshot)
        lanes[target.target_ref] = TrainLane(snapshot=snapshot, service=service, checks=checks)
    e.handler.orchestrator.integration_train.lane_for = AsyncMock(
        side_effect=lambda target: lanes[target.target_ref])
    e.handler.orchestrator.integration_train.batches = DatabaseBatches(e.db)
    e.handler._cmd_integration_promotion_publish = IntegrationCommandsMixin._cmd_integration_promotion_publish.__get__(e.handler)
    return lanes


async def latest(e, step):
    status = await e.handler._cmd_promote_status({'project_id': 'p', 'step_id': step})
    assert status['success'], status
    return status['promotions'][0]


async def test_two_step_policies_publish_notes_and_hotfix_backmerges(promote_env, tmp_path, monkeypatch):
    e = promote_env
    e.github, e.human = ChainGitHub(e), None
    e.human = ChainHuman(e.github)
    e.handler.orchestrator.promotion_user_client_factory = lambda _: e.human
    flow = FlowSchema.validate([
        {'id': 'staging', 'source': 'dev', 'target': 'staging', 'type': 'continuous',
         'gate': {'approval': 'none'}, 'after': {'backmerge': False}},
        {'id': 'release', 'source': 'staging', 'target': 'main',
         'versioning': {'kind': 'semver_tag', 'source': 'pyproject'},
         'notes': {'kind': 'file_template', 'path': 'notes/{version}.md'},
         'after': {'backmerge': True}},
    ], default_branch='dev').flow
    e.trust = e.trust.model_copy(update={'promotion_attestation_names': tuple(step['gate']['attestation'] for step in flow)})
    e.handler._promotion_manifest = AsyncMock(return_value=e.trust.model_dump(mode='json', by_alias=True))
    git(e.repo.store, 'push', 'origin', f'{e.base}:refs/heads/staging')
    digest = hashlib.sha256(b'').hexdigest()
    notes_input = {'sources': [], 'bare_commits': [], 'migration_files': [], 'source_digest': digest,
                   'range': {'base': e.base, 'head': e.source}}
    notes = draft_notes(notes_input, kind='file_template', version='0.2.0')
    e.source = commit(e.repo.store, {'notes/0.2.0.md': notes}, base=e.source)
    git(e.repo.store, 'push', 'origin', f'{e.source}:dev')
    e.github.runs[e.source] = 'success'
    async with e.db.immediate() as conn:
        await conn.execute(update(projects).values(promotion_flow=flow, hierarchical_integration_mode='train'))
    lanes = await chain_lanes(e, flow)
    continuous, requested = await policy_engine(e, 'continuous'), await policy_engine(e, 'request')
    trace = []

    async def record(label):
        status = await e.handler._cmd_promote_status({'project_id': 'p'})
        # The daemon's integration status and promote status share frozen inputs.
        from src.integration.status import IntegrationStatusService
        integration = await IntegrationStatusService(e.db, git_first='active').status('p')
        trace.append({'state': label, 'promote_status': status, 'integration_status': integration})

    await continuous.run('advance-source', step='staging')
    staging = await latest(e, 'staging')
    assert staging['promotion']['source_sha'] == e.source
    assert e.github.by_number[staging['promotion']['pr_number']]['base']['ref'] == 'staging'
    await record('staging-requested')
    await continuous.run('visit-intent', step='staging', batch_id=staging['batch_id'])
    assert git(e.ops.git.remote_path, 'rev-parse', 'staging') == e.source
    assert not git(e.ops.git.remote_path, 'for-each-ref', 'refs/tags/')
    await record('staging-delivered')
    await requested.run('advance-source')
    assert not [name for name, _ in requested.calls if name == 'promote_request']
    await requested.run('explicit-request', notes_reviewed=True)
    release = await latest(e, 'release')
    assert release['promotion']['source_sha'] == e.source
    assert release['promotion']['pr_url'] != staging['promotion']['pr_url']
    await record('release-requested')
    await requested.run('visit-intent', batch_id=release['batch_id'])
    assert git(e.ops.git.remote_path, 'rev-parse', 'main') == e.base
    await record('release-held-for-operator')
    approved = await e.handler._cmd_promote_approve({'project_id': 'p', 'request_id': release['promotion']['request_id']})
    assert approved['success'], approved
    await requested.run('visit-intent', batch_id=release['batch_id'])
    assert git(e.ops.git.remote_path, 'rev-parse', 'main') == e.source
    assert git(e.ops.git.remote_path, 'rev-parse', 'refs/tags/v0.2.0^{}') == e.source
    await record('release-delivered')

    # Cut a hotfix from the released target, retain the ordinary task completion.
    from src.commands.handler import CommandHandler
    from src.config import AppConfig
    from src.integration.hierarchy import HierarchyIntegration
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
    from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY
    filer = CommandHandler(SimpleNamespace(db=e.db, bus=SimpleNamespace(emit=AsyncMock()), playbook_manager=None),
                           AppConfig(data_dir=str(tmp_path / 'data'), workspace_dir=str(tmp_path / 'ws')))
    filer._hierarchy_integration_service = lambda: HierarchyIntegration(e.db,
        default_head_resolver=lambda _repo, ref: git(e.ops.git.remote_path, 'rev-parse', ref))
    e.handler._cmd_create_task = filer._cmd_create_task
    filed = await e.handler._cmd_promote_hotfix({'project_id': 'p', 'step_id': 'release', 'title': 'Urgent fix'})
    assert filed['success'], filed
    task = await e.db.get_task(filed['task_id'])
    hotfix = commit(e.repo.store, {'pyproject.toml': '[project]\nversion = "0.2.1"\n',
        'notes/0.2.1.md': draft_notes(notes_input, kind='file_template', version='0.2.1'),
        'hotfix.txt': 'fixed\n'}, base=e.source)
    git(e.repo.store, 'push', 'origin', f'{hotfix}:refs/heads/{task.branch_name}')
    await e.db.update_task(task.id, status='COMPLETED')
    await e.db.set_task_meta(task.id, DEVELOPMENT_COMPLETION_ID_KEY, 'hotfix-close')
    await GitProvenance(e.ops.git, str(e.repo.store), repository_url='https://github.com/test/repo.git').write_completion(
        CompletedSource(CompletionIdentity('p', 'r', task.id, 'hotfix-close'), hotfix))
    e.github.runs[hotfix] = 'success'
    await requested.run('hotfix-completed', from_task=task.id, notes_reviewed=True)
    urgent = await latest(e, 'release')
    assert urgent['promotion']['kind'] == 'hotfix'
    await record('hotfix-requested')
    await requested.run('visit-intent', batch_id=urgent['batch_id'])
    assert git(e.ops.git.remote_path, 'rev-parse', 'main') == e.source
    await record('hotfix-held-for-operator')
    await e.handler._cmd_promote_approve({'project_id': 'p', 'request_id': urgent['promotion']['request_id']})
    await requested.run('visit-intent', batch_id=urgent['batch_id'])
    assert git(e.ops.git.remote_path, 'rev-parse', 'refs/tags/v0.2.1^{}') == hotfix
    await record('hotfix-delivered')

    # Author both lower routes through the public daemon mechanism.
    fixture = SimpleNamespace(db=e.db, origin=SimpleNamespace(url=str(e.ops.git.remote_path), clone=e.repo.store))
    from tests.test_integration_train_sources import HostedGitHub
    monkeypatch.setattr(HostedGitHub, 'full_name', e.repo.binding.full_name)
    lower = await source_world(fixture, prepare=False)
    e.handler.orchestrator.integration_train.lane_for = AsyncMock(side_effect=lambda target:
        lower.lane if target.target_ref == 'refs/heads/dev' else lanes[target.target_ref])
    # Ordinary backmerge PR creation uses the same GitHub producer as its root gate.
    e.handler.orchestrator.github_client_factory = lambda _: lower.github
    # The staging request still needs the promotion client; route that create by head.
    original_create = lower.github.create_pull_request
    async def create(**kwargs):
        return await e.github.create_pull_request(**kwargs) if kwargs['head'].startswith('aq/backmerge-intent/') else await original_create(**kwargs)
    lower.github.create_pull_request = create
    # Step intent requests use aq/promote; preserve the chain PR transport.
    async def runtime(project, repository, step):
        return lanes['refs/heads/' + step['target']], e.ops, e.repo, e.github
    e.handler._promotion_runtime = runtime
    # BackmergeAdmission uses the ordinary root GitHub client only for its source.
    from src.integration.train_sources import BackmergeAdmission
    original_author = BackmergeAdmission.author
    async def author(self, *args, **kwargs):
        args = (*args[:-1], lower.github)
        return await original_author(self, *args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(BackmergeAdmission, 'author', author)
        authored = await e.handler._cmd_integration_backmerge_source({'project_id': 'p', 'step_id': 'release'})
    assert authored['success'], authored
    assert len(authored['backmerges']) == 2
    await record('lower-backmerges-requested')
    ff = await latest(e, 'staging')
    assert ff['promotion']['kind'] == 'backmerge'
    assert ff['promotion']['version'] is None
    await continuous.run('visit-intent', step='staging', batch_id=ff['batch_id'])
    assert git(e.ops.git.remote_path, 'rev-parse', 'staging') == hotfix
    await record('staging-backmerge-delivered')
    source = next(item for item in authored['backmerges'] if 'task_id' in item and item.get('kind') == 'source')
    denied = await lower.train.visit(lower.target)
    assert denied.batch_id is None
    await record('dev-backmerge-awaiting-pr-checks')
    lower.github.pr_runs[hotfix] = 'success'
    lower.now[0] += 61
    lower.train.wake('p')
    merging = await lower.train.visit(lower.target)
    assert merging.state == 'testing', merging
    await record('dev-backmerge-testing')
    assert merging.batch_id not in {release['batch_id'], urgent['batch_id'], ff['batch_id']}
    assert [m.task_id for m in await e.store.members(merging.batch_id)] == [source['task_id']]
    lower.github.runs[merging.candidate_sha] = 'success'
    lower.now[0] += 61
    lower.train.wake('p')
    delivered = await lower.train.visit(lower.target)
    assert delivered.state == 'delivered', delivered
    assert git(e.ops.git.remote_path, 'merge-base', '--is-ancestor', hotfix, 'dev') == ''
    await record('hotfix-contained-on-staging-and-dev')
    Path('/tmp/aq-release-two-step-trace.json').write_text(json.dumps(trace, indent=2, default=str))
    assert all(row['promote_status']['success'] for row in trace)


async def test_routes_bind_stored_types_and_disabled_activations(promote_env):
    e = promote_env
    flow = FlowSchema.validate([{'id': 'staging', 'source': 'dev', 'target': 'staging', 'type': 'continuous',
        'gate': {'approval': 'none'}}, {'id': 'release', 'source': 'staging', 'target': 'main'}], default_branch='dev').flow
    async with e.db.immediate() as conn:
        await conn.execute(update(projects).values(promotion_flow=flow, hierarchical_integration_mode='train'))
    rows = [{'id': kind, 'playbook_id': 'promotion-' + kind, 'scope': 'project', 'scope_identifier': 'p',
             'health': 'ready', 'active_artifact_sha256': 'sha256:' + ('a' if kind == 'continuous' else 'b') * 64}
            for kind in ('continuous', 'request')]
    e.db.list_playbook_activations = AsyncMock(return_value=rows)
    for step, kind in (('staging', 'continuous'), ('release', 'request')):
        route = await resolve_integration_route(e.db, 'promotion.source_settled', {'project_id': 'p', 'step_id': step})
        assert route.playbook_id == 'promotion-' + kind
    e.db.list_playbook_activations = AsyncMock(return_value=[])
    assert await resolve_integration_route(e.db, 'promotion.source_settled', {'project_id': 'p', 'step_id': 'staging'}) is None
    e.db.list_playbook_activations = AsyncMock(return_value=[{
        **rows[0], 'scope': 'system', 'scope_identifier': '',
    }])
    assert await resolve_integration_route(e.db, 'promotion.source_settled', {'project_id': 'p', 'step_id': 'staging'}) is None


async def test_continuous_policy_cancels_and_re_requests_only_new_heads(promote_env):
    e = promote_env
    e.github = ChainGitHub(e)
    e.github.runs[e.source] = 'success'
    flow = FlowSchema.validate([{'id': 'release', 'source': 'dev', 'target': 'main',
        'type': 'continuous', 'gate': {'approval': 'none'}, 'after': {'backmerge': False}}],
        default_branch='dev').flow
    async with e.db.immediate() as conn:
        await conn.execute(update(projects).values(promotion_flow=flow))
    await chain_lanes(e, flow)
    policy = await policy_engine(e, 'continuous')
    await policy.run('advance-source')
    first = await latest(e, 'release')
    await policy.run('advance-source')
    assert sum(name == 'promote_request' for name, _ in policy.calls) == 1
    newer = commit(e.repo.store, {'next.txt': 'next head\n'}, base=e.source)
    git(e.repo.store, 'push', 'origin', f'{newer}:dev')
    e.github.runs[newer] = 'success'
    await policy.run('advance-source')
    second = await latest(e, 'release')
    assert second['batch_id'] != first['batch_id']
    assert second['promotion']['source_sha'] == newer
    assert (await e.store.get(first['batch_id'])).intent == 'aborted'
    assert [name for name, _ in policy.calls][-2:] == ['promote_cancel', 'promote_request']


@pytest.mark.parametrize('kind', ['promotion', 'backmerge'])
async def test_public_filing_and_edit_paths_refuse_daemon_types(promote_env, tmp_path, kind):
    from sqlalchemy import insert

    from src.commands.handler import CommandHandler
    from src.commands.task_changes import ChangeSetError, normalize, snapshot
    from src.config import AppConfig
    from src.database.tables import tasks
    from src.task_graph.models import GraphNode, TaskGraph
    from src.task_graph.validator import _check_task_types

    e = promote_env
    filer = CommandHandler(SimpleNamespace(db=e.db, bus=SimpleNamespace(emit=AsyncMock()),
        playbook_manager=None), AppConfig(data_dir=str(tmp_path / 'data'), workspace_dir=str(tmp_path / 'ws')))
    args = {'project_id': 'p', 'title': 'Forged intent', 'task_type': kind}
    assert 'daemon' in (await filer._cmd_create_task(args))['error']
    assert 'daemon' in (await filer._cmd_ensure_task({**args, 'dedup_key': 'forge'}))['error']
    async with e.db.immediate() as conn:
        await conn.execute(insert(tasks).values(id='normal', project_id='p', repo_id='r',
            title='Ordinary work', description='', task_type='bugfix', created_at=1000, updated_at=1000))
        await conn.execute(insert(tasks).values(id='intent', project_id='p', repo_id='r',
            title='Daemon intent', description='', task_type=kind, created_at=1000, updated_at=1000))
    for task_id, replacement in [('normal', kind), ('intent', 'bugfix'), ('intent', None)]:
        assert 'daemon' in (await filer._cmd_edit_task({'task_id': task_id, 'task_type': replacement}))['error']
    for payload in [{'tasks': [{'tempId': 'fake', 'title': 'Forged', 'description': '', 'task_type': kind}]},
                    {'edits': [{'task_id': 'normal', 'task_type': kind}]}]:
        with pytest.raises(ChangeSetError, match='daemon'):
            normalize(payload)
    payload = normalize({'edits': [{'task_id': 'intent', 'task_type': None}]})
    async with e.db._engine.connect() as conn:
        with pytest.raises(ChangeSetError, match='daemon'):
            await snapshot(conn, 'p', payload)
    assert _check_task_types(TaskGraph(nodes=[GraphNode(key='fake', task_type=kind)]))[0].rule == 'daemon_task_type'


async def test_granted_supervisor_cannot_directly_author_backmerge(promote_env):
    from src.profiles.capabilities import CapabilityPolicy

    e = promote_env
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, elevated=True,
        policy=CapabilityPolicy.from_namespaces(aq_commands=['integration_backmerge_source']),
        session_id='supervisor', project_id=None)
    with principal_context(principal):
        result = await e.handler._cmd_integration_backmerge_source({'project_id': 'p', 'step_id': 'release'})
    assert result['outcome'] == 'unauthorized'


@pytest.mark.parametrize('value', ['{', '[]', '{"source_sha":"bad","target_ref":"refs/heads/main"}'])
async def test_malformed_debt_is_named_and_status_performs_no_network(promote_env, value):
    from sqlalchemy import insert

    from src.database.tables import task_metadata, tasks

    e = promote_env
    async with e.db.immediate() as conn:
        await conn.execute(insert(tasks).values(id='debt', project_id='p', repo_id='r',
            title='Debt', task_type='backmerge', created_at=1000, updated_at=1000))
        await conn.execute(insert(task_metadata).values(task_id='debt', key='backmerge', value=value))
    e.ops.remote = AsyncMock(side_effect=AssertionError('status performed network I/O'))
    result = await e.handler._cmd_promote_status({'project_id': 'p'})
    assert result['outcome'] == 'backmerge_ledger_invalid'
    e.ops.remote.assert_not_awaited()


async def test_hotfix_notification_survives_task_completed_subscriber_failure(promote_env):
    from sqlalchemy import insert, select

    from src.database.tables import integration_outbox, task_metadata, tasks
    from src.orchestrator.events import EventsMixin

    e = promote_env
    async with e.db.immediate() as conn:
        await conn.execute(insert(tasks).values(id='hotfix', project_id='p', repo_id='r',
            title='Hotfix', status='COMPLETED', task_type='bugfix', created_at=1000, updated_at=1000))
        await conn.execute(insert(task_metadata).values(task_id='hotfix', key='promotion_hotfix',
            value=json.dumps({'step_id': 'release'})))
    task = await e.db.get_task('hotfix')
    emitter = SimpleNamespace(db=e.db, bus=SimpleNamespace(emit=AsyncMock(side_effect=RuntimeError('consumer failed'))))
    for _ in range(2):
        with pytest.raises(RuntimeError, match='consumer failed'):
            await EventsMixin._emit_task_event(emitter, 'task.completed', task)
    async with e.db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == 'promotion.hotfix_completed'))).mappings().all()
    assert len(rows) == 1
    assert rows[0]['payload']['from_task'] == 'hotfix'
