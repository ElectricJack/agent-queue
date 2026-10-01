"""Full CI runs on pull requests and integration boundaries, and on `main` only unattested."""
import json
import math
import re
import shlex
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_e2e_cli_stateful import SCENARIO_GROUPS

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


SEED = 'venv-cache.yml'
INSTALL = 'Install dependencies on cache miss'


def test_only_the_attestation_audit_and_venv_seed_run_on_a_push_to_main():
    files = sorted(WORKFLOWS.glob('*.yml')) + sorted(WORKFLOWS.glob('*.yaml'))
    assert files
    pushed = {path.name for path in files if _pushed(workflow(path.name)['on'], 'main')}
    assert pushed == {'main-attestation.yml', SEED}


def _step(job, name):
    (step,) = [step for step in job['steps'] if step.get('name') == name]
    return step


def _venv_commands(script):
    """Each ``-m venv`` and ``-m pip`` command in a step script, network tuning aside.

    A retry loop, ``--timeout`` or ``--retries`` decides whether an install
    survives a slow PyPI, not which environment it builds.
    """
    commands = []
    for line in script.replace('\\\n', ' ').splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split = True
        command = []
        for word in [*lexer, ';']:
            if word.strip(';&|'):
                command.append(word)
                continue
            module = next((i for i in range(1, len(command) - 1) if command[i] == '-m'), None)
            if module is not None and command[module + 1] in ('venv', 'pip'):
                commands.append(_without_network_options(command[module - 1:]))
            command = []
    return commands


def _without_network_options(command):
    kept, skip = [], False
    for word in command:
        if skip:
            skip = False
        elif word in ('--timeout', '--retries'):
            skip = True
        elif not word.startswith(('--timeout=', '--retries=')):
            kept.append(word)
    return kept


def test_venv_command_comparison_ignores_install_retries():
    plain = 'python -m venv .venv\n.venv/bin/python -m pip install -e ".[dev,cli]" pkg\n'
    retried = (
        'python -m venv .venv\n'
        '# Retry the complete install.\n'
        'for attempt in 1 2 3; do\n'
        '  if .venv/bin/python -m pip install --timeout 60 \\\n'
        '    -e ".[dev,cli]" pkg; then\n'
        '    exit 0\n'
        '  fi\n'
        '  echo "::warning::pip install attempt $attempt of 3 failed"\n'
        'done\n'
    )
    assert _venv_commands(retried) == _venv_commands(plain) == [
        ['python', '-m', 'venv', '.venv'],
        ['.venv/bin/python', '-m', 'pip', 'install', '-e', '.[dev,cli]', 'pkg'],
    ]
    assert _venv_commands(plain.replace('pkg', 'other')) != _venv_commands(plain)


@pytest.mark.parametrize('job', ['test', 'e2e-cli'])
def test_venv_seed_saves_the_venv_each_suite_job_restores(job):
    # Actions caches are ref-scoped: a run restores only its own ref's caches
    # or main's. tests.yml never runs on main, so the seed saves main's entry
    # and every PR's or candidate's first run restores it.
    seed = workflow(SEED)['jobs']['seed']
    suite = workflow()['jobs'][job]
    assert seed['runs-on'] == suite['runs-on']
    for name in ('Checkout repository', 'Set up Python', 'Cache virtual environment'):
        assert _step(seed, name) == _step(suite, name)
    # An exact key only: a venv is bound to its interpreter's path.
    assert 'restore-keys' not in _step(seed, 'Cache virtual environment')['with']
    install = _step(seed, INSTALL)
    assert install['if'] == _step(suite, INSTALL)['if']
    commands = _venv_commands(install['run'])
    assert [command[:3] for command in commands] == [
        ['python', '-m', 'venv'],
        ['.venv/bin/python', '-m', 'pip'],
    ]
    assert commands == _venv_commands(_step(suite, INSTALL)['run'])


def test_venv_seed_runs_no_suite():
    seed = workflow(SEED)
    assert seed['permissions'] == {'contents': 'read'}
    assert seed['concurrency'] == {
        'group': 'venv-cache-${{ github.ref }}',
        'cancel-in-progress': 'true',
    }
    assert list(seed['jobs']) == ['seed']
    job = seed['jobs']['seed']
    assert 'uses' not in job and 'services' not in job
    assert [step['name'] for step in job['steps']] == [
        'Checkout repository',
        'Set up Python',
        'Cache virtual environment',
        INSTALL,
    ]


def test_venv_seed_runs_whenever_mains_key_can_change():
    triggers = workflow(SEED)['on']
    assert list(triggers) == ['push', 'schedule', 'workflow_dispatch']
    assert triggers['push']['branches'] == ['main']
    # A manifest the key hashes, or the seed itself, changed on main.
    key = _step(workflow()['jobs']['test'], 'Cache virtual environment')['with']['key']
    (hashed,) = re.findall(r'hashFiles\(([^)]*)\)', key)
    manifests = re.findall(r"'([^']+)'", hashed)
    assert manifests
    assert triggers['push']['paths'] == [*manifests, f'.github/workflows/{SEED}']
    # A runner's new Python patch version and GitHub's seven-day eviction of
    # an unused entry change no file, so a daily run seeds those keys.
    (schedule,) = triggers['schedule']
    minute, hour, *days = schedule['cron'].split()
    assert minute.isdigit() and hour.isdigit() and days == ['*', '*', '*']


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
    assert e2e['timeout-minutes'] == '10'
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
