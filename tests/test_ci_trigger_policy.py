"""Full CI runs on pull requests and integration boundaries, and on `main` only unattested."""
import json
import math
import os
import re
import shlex
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_e2e_cli_stateful import E2E_TEST_TIMEOUT_SECONDS, SCENARIO_GROUPS

WORKFLOWS = Path('.github/workflows')
CANDIDATE_REF = 'aq/integration/p-' + '5' * 32 + '/r-' + '6' * 32


def workflow(name='tests.yml'):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def _pushed(triggers, branch):
    """Whether a push to *branch* starts a workflow with these ``on:`` triggers."""
    if 'push' not in triggers:
        return False
    push = triggers['push'] or {}
    if 'branches' in push:
        return any(fnmatchcase(branch, pattern) for pattern in push['branches'])
    if 'branches-ignore' in push:
        return not any(fnmatchcase(branch, pattern) for pattern in push['branches-ignore'])
    return True


class _Null:
    """A missing context property, which GitHub expressions read as null."""

    def __getattr__(self, _name):
        return self

    def __bool__(self):
        return False

    def __eq__(self, other):
        return other is None or isinstance(other, _Null)

    def __hash__(self):
        return 0


def _context(value):
    if isinstance(value, dict):
        fields = {key: _context(item) for key, item in value.items()}
        return type('Context', (SimpleNamespace,), {'__getattr__': lambda self, _: _Null()})(
            **fields
        )
    return value


def _evaluate(expression, **contexts):
    """Evaluate a GitHub ``if:`` expression over plain-dict contexts."""
    python = re.sub(r'!(?!=)', ' not ', expression.replace('||', ' or ').replace('&&', ' and '))
    names = {name: _context(value) for name, value in contexts.items()}
    return bool(eval(python, {'__builtins__': {}}, {**names, 'startsWith': _starts_with}))


def _runs(event_name, pull_request=None, repository='acme/widgets', job='test'):
    """Evaluate a tests.yml job's ``if:`` guard for one event."""
    return _evaluate(workflow()['jobs'][job]['if'], github={
        'event_name': event_name,
        'repository': repository,
        'event': {'pull_request': pull_request} if pull_request else {},
    })


def _starts_with(text, prefix):
    # GitHub's startsWith ignores case.
    return str(text).lower().startswith(prefix.lower())


def _pull_request(head_ref, *, head_repository='acme/widgets', draft=False):
    return {'draft': draft, 'head': {'ref': head_ref, 'repo': {'full_name': head_repository}}}


@pytest.mark.parametrize(('branch', 'expected'), [
    ('main', False),
    ('aq/parent/example/0123/1/' + 'a' * 40, True),
    (CANDIDATE_REF, True),
    ('aq/sound-current', False),
    ('aq/feature/example', False),
    ('aq/example', False),
    ('aq/repair-example', False),
    ('fix/feature-merge-policy', False),
])
def test_push_ci_only_covers_integration_boundaries(branch, expected):
    assert _pushed(workflow()['on'], branch) is expected


def test_only_the_attestation_audit_runs_on_a_push_to_main():
    files = sorted(WORKFLOWS.glob('*.yml')) + sorted(WORKFLOWS.glob('*.yaml'))
    assert files
    pushed = {path.name for path in files if _pushed(workflow(path.name)['on'], 'main')}
    assert pushed == {'main-attestation.yml'}


def test_pull_requests_into_main_run_ci():
    pull_request = workflow()['on']['pull_request']
    assert pull_request['branches'] == ['main']
    assert pull_request['types'] == ['opened', 'synchronize', 'reopened', 'ready_for_review']


def test_manual_ci_is_available_without_unused_merge_queue_runs():
    triggers = workflow()['on']
    assert 'workflow_dispatch' in triggers
    assert 'merge_group' not in triggers


def test_tests_keeps_its_triggers_and_job_names_and_can_be_called():
    tests = workflow()
    assert list(tests['on']) == ['pull_request', 'push', 'workflow_dispatch', 'workflow_call']
    assert tests['on']['push']['branches'] == ['aq/parent/**', 'aq/integration/**']
    assert tests['on']['workflow_call'] == ''  # No inputs or secrets: it runs as pushed.
    assert list(tests['jobs']) == ['test', 'e2e-cli']
    assert tests['jobs']['test']['name'] == 'Tests (${{ matrix.suite.name }})'
    assert tests['jobs']['e2e-cli']['name'] == 'E2E CLI (${{ matrix.group }})'


@pytest.mark.parametrize('job', ['test', 'e2e-cli'])
def test_called_suite_runs_every_job_for_the_callers_push(job):
    # A called workflow sees the caller's context: main-attestation.yml runs on `push`.
    assert _runs('push', job=job) is True


def _checkout_action():
    return workflow()['jobs']['test']['steps'][0]['uses']


def test_main_attestation_is_the_specified_audit_workflow():
    audit = workflow('main-attestation.yml')
    assert audit['name'] == 'Main attestation'
    assert audit['on'] == {'push': {'branches': ['main']}}
    assert audit['permissions'] == {'contents': 'read', 'checks': 'read'}
    # Its own group would share tests.yml's `tests-refs/heads/main` and deadlock the call.
    assert 'concurrency' not in audit
    assert list(audit['jobs']) == ['attestation', 'unattested-ci']

    job = audit['jobs']['attestation']
    assert job['name'] == 'Main attestation'
    assert job['runs-on'] == 'ubuntu-latest'
    assert job['outputs'] == {
        'attested': '${{ steps.verify.outputs.attested }}',
        'configured': '${{ steps.verify.outputs.configured }}',
    }
    checkout, verify = job['steps']
    assert checkout == {
        'uses': _checkout_action(),
        'with': {
            'ref': '${{ github.sha }}',
            'sparse-checkout': 'src/integration/hosted_attestation.py',
            'sparse-checkout-cone-mode': 'false',
        },
    }
    assert Path(checkout['with']['sparse-checkout']).is_file()
    assert verify == {
        'id': 'verify',
        'env': {
            'GH_TOKEN': '${{ github.token }}',
            'REPOSITORY': '${{ github.repository }}',
            'REPOSITORY_ID': '${{ github.repository_id }}',
            'SHA': '${{ github.sha }}',
            'APP_ID': '${{ vars.AQ_INTEGRATION_ATTESTATION_APP_ID }}',
            'CHECK_VERSION': '${{ vars.AQ_INTEGRATION_REQUIRED_CHECK_VERSION }}',
        },
        'run': 'python3 src/integration/hosted_attestation.py',
    }


def test_unattested_ci_calls_the_full_suite():
    audit = workflow('main-attestation.yml')['jobs']['unattested-ci']
    assert set(audit) == {'needs', 'if', 'uses'}
    assert audit['needs'] == 'attestation'
    assert audit['uses'] == './.github/workflows/tests.yml'
    assert 'workflow_call' in workflow(Path(audit['uses']).name)['on']


@pytest.mark.parametrize(('configured', 'attested', 'runs'), [
    ('true', 'false', True),
    ('true', 'true', False),
    # Variables unset: not in App mode yet, so no fallback CI.
    ('false', 'false', False),
    ('', '', False),
])
def test_unattested_ci_runs_only_for_a_configured_unattested_push(configured, attested, runs):
    expression = workflow('main-attestation.yml')['jobs']['unattested-ci']['if']
    outputs = {'configured': configured, 'attested': attested}
    assert _evaluate(expression, needs={'attestation': {'outputs': outputs}}) is runs


@pytest.mark.parametrize(('event_name', 'pull_request', 'expected'), [
    ('push', None, True),
    ('workflow_dispatch', None, True),
    ('pull_request', _pull_request('aq/crisp-forge'), True),
    ('pull_request', _pull_request('feature', head_repository='fork/widgets'), True),
    # The push trigger already tests an integration candidate's head SHA.
    ('pull_request', _pull_request(CANDIDATE_REF), False),
    # A fork's branch gets no push run here, whatever its name.
    ('pull_request', _pull_request(CANDIDATE_REF, head_repository='fork/widgets'), True),
    # A draft runs once it is marked ready_for_review.
    ('pull_request', _pull_request('aq/crisp-forge', draft=True), False),
])
def test_test_job_runs_once_per_head(event_name, pull_request, expected):
    assert _runs(event_name, pull_request) is expected


def test_skipped_integration_pr_heads_are_covered_by_the_push_trigger():
    # The guard skips PRs by head-ref prefix; every such head must get a push run.
    assert "startsWith(github.event.pull_request.head.ref, 'aq/integration/')" in (
        workflow()['jobs']['test']['if']
    )
    assert _pushed(workflow()['on'], 'aq/integration/anything')


def test_new_push_cancels_only_its_own_obsolete_ci():
    concurrency = workflow()['concurrency']
    assert concurrency['group'] == 'tests-${{ github.ref }}'
    assert concurrency['cancel-in-progress'] == 'true'


def test_test_job_checks_out_the_exact_event_revision_read_only():
    text = (WORKFLOWS / 'tests.yml').read_text()
    assert 'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683' in text
    assert 'ref: ${{ github.sha }}' in text
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in text
    assert workflow()['permissions'] == {'contents': 'read'}
    assert list(workflow()['jobs']) == ['test', 'e2e-cli']


def test_default_shards_cover_each_group_once_with_four_workers():
    suites = workflow()["jobs"]["test"]["strategy"]["matrix"]["suite"]
    shards = [suite for suite in suites if suite["name"].startswith("default")]
    assert len(shards) == 8
    groups = []
    for shard in shards:
        args = shlex.split(shard["command"])
        assert args[:2] == ["pytest", "tests/"]
        assert args[args.index("-n") + 1] == "4"
        assert args[args.index("--dist") + 1] == "loadfile"
        assert args[args.index("--splits") + 1] == "8"
        assert args[args.index("--splitting-algorithm") + 1] == "least_duration"
        assert "-m" not in args  # Inherit the same default marker selection on every shard.
        group = int(args[args.index("--group") + 1])
        assert shard["name"] == f"default-{group}/8"
        assert int(shard["group"]) == group
        assert "--store-durations" in args and "--clean-durations" in args
        groups.append(group)
    assert sorted(groups) == list(range(1, 9))
    assert len({suite["name"] for suite in suites}) == len(suites)
    assert {suite["name"] for suite in suites} - {shard["name"] for shard in shards} == {
        "cli-conformance",
        "migration-and-slow",
        "postgres-integration",
    }


def test_committed_shard_timings_are_valid_pytest_split_data():
    durations = json.loads(Path(".test_durations").read_text())
    assert durations
    for nodeid, duration in durations.items():
        assert nodeid.startswith("tests/") and "::" in nodeid
        assert isinstance(duration, (int, float)) and not isinstance(duration, bool)
        assert math.isfinite(duration) and duration >= 0


def test_e2e_matrix_keeps_smoke_on_prs_and_off_the_postgres_suite():
    jobs = workflow()['jobs']
    e2e = jobs['e2e-cli']
    assert e2e['if'] == jobs['test']['if']
    assert e2e['strategy']['matrix']['group'] == list(SCENARIO_GROUPS)
    assert e2e['strategy']['fail-fast'] == 'false'
    # A passing smoke test must have time to finish before action cleanup.
    # Reserve a minute beyond the local test deadline for runner overhead,
    # while retaining the same bounded job cap as the suite arms.
    job_timeout_seconds = int(e2e['timeout-minutes']) * 60
    assert E2E_TEST_TIMEOUT_SECONDS + 60 <= job_timeout_seconds <= 600
    run = e2e['steps'][-1]['run']
    assert run == (
        "pytest 'tests/test_e2e_cli_stateful.py::"
        "test_disposable_daemon_stateful_cli_smoke[${{ matrix.group }}]' "
        "-m integration -s --durations=0"
    )
    suites = {suite['name']: suite['command'] for suite in jobs['test']['strategy']['matrix']['suite']}
    assert '--ignore=tests/test_e2e_cli_stateful.py' in suites['postgres-integration']
    checkout = e2e['steps'][0]
    assert checkout['uses'] == 'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683'
    assert checkout['with']['ref'] == '${{ github.sha }}'


def test_e2e_groups_cover_every_scenario_once_and_keep_claim_dependencies_together():
    scenarios = [scenario for group in SCENARIO_GROUPS.values() for scenario in group]
    expected = {f'S{index}' for index in range(1, 20)} - {'S16'} | {'S16a', 'S16b'}
    assert set(scenarios) == expected
    assert len(scenarios) == len(expected)
    assert SCENARIO_GROUPS['claims'][:3] == ('S1', 'S2', 'S3')


def _install_steps():
    jobs = workflow()['jobs']
    return {
        job: next(step for step in jobs[job]['steps'] if step['name'] == 'Install dependencies on cache miss')
        for job in ('test', 'e2e-cli')
    }


def _run_install(tmp_path, script, fail_times):
    """Run a cache-miss install script as Actions does, with pip failing *fail_times* times."""
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    for name, body in {'python': 'exit 0', 'sleep': 'exit 0'}.items():
        (stubs / name).write_text(f'#!/bin/sh\n{body}\n')
        (stubs / name).chmod(0o755)
    venv_python = tmp_path / '.venv' / 'bin' / 'python'
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(
        '#!/bin/sh\n'
        'echo "$*" >> calls.log\n'
        'test "$(wc -l < calls.log)" -gt "$FAIL_TIMES"\n'
    )
    venv_python.chmod(0o755)
    result = subprocess.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
        cwd=tmp_path,
        env={'PATH': f'{stubs}:{os.environ["PATH"]}', 'FAIL_TIMES': str(fail_times)},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, (tmp_path / 'calls.log').read_text().splitlines()


def test_cache_miss_installs_retry_a_stalled_pypi_download(tmp_path):
    # Run 36828686954 lost its failover E2E group to one ReadTimeoutError
    # mid-download; pip retries a refused connection, never a stalled body.
    steps = _install_steps()
    assert steps['test'] == steps['e2e-cli']
    assert steps['test']['if'] == "steps.venv.outputs.cache-hit != 'true'"
    result, calls = _run_install(tmp_path, steps['test']['run'], fail_times=2)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 3
    for call in calls:
        args = shlex.split(call)
        assert args[:2] == ['-m', 'pip'] and args[2] == 'install'
        assert args[args.index('--timeout') + 1] == '60'
        assert {'-e', '.[dev,cli]', 'poetry-core>=2.0.0,<3.0.0'} <= set(args)


def test_cache_miss_install_gives_up_after_bounded_attempts(tmp_path):
    result, calls = _run_install(tmp_path, _install_steps()['test']['run'], fail_times=99)
    assert result.returncode != 0
    assert len(calls) == 3
    assert '::error::' in result.stdout
