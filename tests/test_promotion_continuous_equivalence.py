"""Run the typed-source port and frozen shipped policy through the same real engine."""

import copy
import json
from pathlib import Path

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.models import CommandResult
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.playbooks.definition import artifact_sha256, load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.profiles.capabilities import CapabilityPolicy
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingBus,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

FIXTURES = Path(__file__).parent / 'fixtures/playbooks'
EVIDENCE = FIXTURES / 'promotion-continuous-equivalence'
SCENARIOS = json.loads((EVIDENCE / 'scenarios.json').read_text())
BEFORE = load_definition_json((EVIDENCE / 'shipped-before.artifact.json').read_text())
AFTER = load_definition_json((FIXTURES / 'v2/promotion-continuous/artifact.json').read_text())


def test_continuous_port_preserves_executable_graph_and_contracts():
    assert artifact_sha256(BEFORE) == SCENARIOS['baseline_artifact_sha256']

    def executable(artifact):
        graph = artifact.model_dump(mode='json')
        for node in [*graph['rules'], *graph['steps'].values()]:
            node.pop('source')  # The rewritten source relocates these provenance spans.
        return graph['rules'], graph['steps']

    assert executable(AFTER) == executable(BEFORE)
    assert AFTER.compiled_against == BEFORE.compiled_against
    assert AFTER.required_features == BEFORE.required_features
    assert set(AFTER.trusted_execution.required_capabilities) == set(
        AFTER.compiled_against.commands
    )


async def execute_scenario(artifact, scenario):
    store, runs = InMemoryArtifactStore(), RecordingRunRepository()
    store.put(artifact)
    registry = ContractRegistry()
    calls = []
    defaults = {
        'integration_promotion_policy_input': 'policy_input',
        'promote_request': 'requested',
        'promote_cancel': 'cancelled',
        'integration_promotion_publish': 'delivered',
        'integration_backmerge_source': 'backmerges_authored',
        'message_send': 'queued',
    }
    for name in defaults:
        registration = CONTRACTS.get(name)

        async def invoke(args, ctx, name=name, contract=registration.contract.execution):
            calls.append((name, args.model_dump(mode='json')))
            outcome = scenario['outcomes'].get(name, defaults[name])
            if outcome == 'runtime_error':
                raise RuntimeError('scenario command failure')
            value = {}
            if name == 'integration_promotion_policy_input' and outcome == 'policy_input':
                value['policy'] = {
                    'source_sha': 'a' * 40, 'backmerge_pending': False,
                    'newer_source': False, 'active_request_id': None,
                    **scenario['policy'],
                }
            if name == 'message_send':
                value = {'message_id': 'notice' if outcome == 'queued' else '', 'state': outcome}
            return CommandResult(
                outcome=outcome, value=contract.result_model(**value), summary=outcome,
            )

        registry.register(CommandRegistration(name, registration.contract, invoke))

    ref = artifact_ref_for(artifact)
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry, artifact_store=store, bus=RecordingBus(), clock=lambda: 1000.0,
        ),
        runs=runs, waits=None, activations=StubActivations([ref]),
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=list(defaults)),
        service_name='fixture-promotion-continuous', project_id='p',
    )
    event = {
        'project_id': 'p', 'step_id': 'release', 'notes_reviewed': False,
        **scenario['event'],
    }
    result = await engine.run_rule(ref, scenario['rule'], event, principal)
    return calls, result.lifecycle.value


@pytest.mark.parametrize('scenario', SCENARIOS['scenarios'], ids=lambda item: item['name'])
async def test_continuous_scenario_matches_shipped_policy(scenario):
    before = await execute_scenario(BEFORE, copy.deepcopy(scenario))
    after = await execute_scenario(AFTER, copy.deepcopy(scenario))
    assert after == before
    calls, lifecycle = after
    assert lifecycle == scenario['lifecycle']
    assert [name for name, _ in calls] == scenario['expected']
    assert all(not args.get('allow_breaking_migration') for _, args in calls)
    for name, args in calls:
        if name == 'promote_request':
            assert args['source_sha'] == 'a' * 40
            assert args['notes_reviewed'] == scenario['event'].get('notes_reviewed', False)
            assert args['from_task'] == scenario['event'].get('from_task')
        elif name == 'message_send':
            assert args['to_kind'] == args['to_id'] == 'user'
            assert args['project_id'] == 'p'
            assert args['body'] == (
                'Promotion needs operator attention. Inspect aq promote status; '
                'use prepare, approve or cancel after resolving the named hold.'
            )
