"""Full CI runs on pull requests and integration boundaries, and on `main` only unattested."""
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tomllib
from fnmatch import fnmatchcase
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from _pytest.mark.expression import Expression

from src.integration.batches import candidate_ref
from tests.test_e2e_cli_stateful import E2E_TEST_TIMEOUT_SECONDS, SCENARIO_GROUPS

WORKFLOWS = Path('.github/workflows')
CANDIDATE_REF = 'aq/integration/p-' + '5' * 32 + '/r-' + '6' * 32
BATCH_REF = candidate_ref('batch-1').removeprefix('refs/heads/')
POSTGRES_IMAGE = 'mirror.gcr.io/library/postgres:18'


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
    (BATCH_REF, True),
    ('aq/promote/main/' + 'a' * 40, True),
    ('aq/promote/staging/main/' + 'a' * 40, True),
    ('aq/backmerge/' + 'a' * 64, True),
    ('aq/backmerge/staging/' + 'a' * 40, True),
    ('aq/backmerge-example', False),
    ('aq/promote-example', False),
    ('aq/sound-current', False),
    ('aq/feature/example', False),
    ('aq/example', False),
    ('aq/repair-example', False),
    ('fix/feature-merge-policy', False),
])
def test_push_ci_only_covers_integration_boundaries(branch, expected):
    assert _pushed(workflow()['on'], branch) is expected


@pytest.mark.parametrize('branch', ['dev', 'staging', 'main'])
def test_only_the_attestation_audit_runs_on_a_push_to_delivery_branch(branch):
    files = sorted(WORKFLOWS.glob('*.yml')) + sorted(WORKFLOWS.glob('*.yaml'))
    assert files
    pushed = {path.name for path in files if _pushed(workflow(path.name)['on'], branch)}
    assert pushed == {'main-attestation.yml'}


def _venv_steps(name, job):
    """The steps that build a job's `.venv`, by name."""
    steps = {step.get('name'): step for step in workflow(name)['jobs'][job]['steps']}
    return {
        key: steps[key]
        for key in ('Set up Python', 'Cache virtual environment', 'Install dependencies on cache miss')
    }


def _pip_installs(script):
    """What each ``pip install`` in a step script installs, ignoring network options."""
    installs = []
    for match in re.finditer(r'-m pip install ([^;\n]+)', script.replace('\\\n', ' ')):
        args = shlex.split(match.group(1))
        requirements = []
        while args:
            arg = args.pop(0)
            if arg in ('--timeout', '--retries'):
                args.pop(0)
            else:
                requirements.append(arg)
        installs.append(requirements)
    return installs


def test_the_venv_cache_is_warmed_on_main_without_running_tests():
    # A run restores caches saved on its own ref or on `main`, and tests.yml
    # never runs on `main` (run 36828502487 cold-installed in all 15 jobs).
    warm = workflow('venv-cache.yml')
    # No push or pull_request trigger: train onboarding would count it as a
    # gate, and every run it starts is on `main`, the scope all refs can read.
    assert list(warm['on']) == ['workflow_run', 'schedule', 'workflow_dispatch']
    assert warm['on']['workflow_run'] == {
        'workflows': [workflow('main-attestation.yml')['name']],
        'types': ['completed'],
        'branches': ['main'],
    }
    assert len(warm['on']['schedule']) == 1
    assert warm['permissions'] == {'contents': 'read'}
    assert warm['concurrency']['cancel-in-progress'] == 'false'
    assert list(warm['jobs']) == ['warm']
    steps = warm['jobs']['warm']['steps']
    assert [step['name'] for step in steps] == [
        'Checkout repository',
        'Set up Python',
        'Cache virtual environment',
        'Install dependencies on cache miss',
    ]
    assert steps[0]['uses'] == _checkout_action()


@pytest.mark.parametrize('job', ['test', 'e2e-cli'])
def test_the_warmed_venv_is_the_entry_each_tests_job_restores(job):
    warm = _venv_steps('venv-cache.yml', 'warm')
    tests = _venv_steps('tests.yml', job)
    # The resolved Python patch version is part of the key.
    assert warm['Set up Python'] == tests['Set up Python']
    # Only an identical path and key restore; a hit needs no download here.
    cache = tests['Cache virtual environment']
    assert warm['Cache virtual environment'] == {
        **cache, 'with': {**cache['with'], 'lookup-only': 'true'}
    }
    warm_install = warm['Install dependencies on cache miss']
    tests_install = tests['Install dependencies on cache miss']
    assert warm_install['if'] == tests_install['if'] == "steps.venv.outputs.cache-hit != 'true'"
    assert warm_install['run'].splitlines()[0] == tests_install['run'].splitlines()[0]
    installs = _pip_installs(tests_install['run'])
    assert len(installs) == 1 and '.[dev,cli]' in installs[0]
    assert _pip_installs(warm_install['run']) == installs


def test_pull_requests_into_delivery_branches_run_ci():
    pull_request = workflow()['on']['pull_request']
    assert pull_request['branches'] == ['dev', 'staging', 'main']
    assert pull_request['types'] == ['opened', 'synchronize', 'reopened', 'ready_for_review']


def test_manual_ci_is_available_without_unused_merge_queue_runs():
    triggers = workflow()['on']
    assert 'workflow_dispatch' in triggers
    assert 'merge_group' not in triggers


def test_tests_keeps_its_triggers_and_job_names_and_can_be_called():
    tests = workflow()
    assert list(tests['on']) == ['pull_request', 'push', 'workflow_dispatch', 'workflow_call']
    assert tests['on']['push']['branches'] == [
        'aq/parent/**', 'aq/integration/**', 'aq/batches/**', 'aq/promote/**', 'aq/backmerge/**',
    ]
    assert tests['on']['workflow_call'] == ''  # No inputs or secrets: it runs as pushed.
    assert list(tests['jobs']) == ['test', 'e2e-cli', 'dashboard']
    assert tests['jobs']['test']['name'] == 'Tests (${{ matrix.suite.name }})'
    assert tests['jobs']['e2e-cli']['name'] == 'E2E CLI (${{ matrix.group }})'
    assert tests['jobs']['dashboard']['name'] == 'Dashboard (typecheck/build)'


@pytest.mark.parametrize('job', ['test', 'e2e-cli', 'dashboard'])
def test_called_suite_runs_every_job_for_the_callers_push(job):
    # A called workflow sees the caller's context: main-attestation.yml runs on `push`.
    assert _runs('push', job=job) is True


def _checkout_action():
    return workflow()['jobs']['test']['steps'][0]['uses']


def test_main_attestation_is_the_specified_audit_workflow():
    audit = workflow('main-attestation.yml')
    assert audit['name'] == 'Main attestation'
    assert audit['on'] == {'push': {'branches': ['dev', 'staging', 'main']}}
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
            'ATTESTATION_NAME': "${{ github.ref_name == 'staging' && 'Agent Queue Promotion Attestation (staging)' || github.ref_name == 'main' && 'Agent Queue Promotion Attestation (release)' || 'Agent Queue Integration Attestation' }}",
            'STEP': "${{ github.ref_name == 'staging' && 'staging' || github.ref_name == 'main' && 'release' || '' }}",
            'TARGET_REF': '${{ github.ref }}',
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


@pytest.mark.parametrize('job', ['test', 'e2e-cli', 'dashboard'])
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
def test_test_job_runs_once_per_head(job, event_name, pull_request, expected):
    assert _runs(event_name, pull_request, job=job) is expected


def test_a_git_first_batch_candidate_is_tested_and_marked_for_main():
    # `main` accepts only commits carrying `aq-train/candidate`, which the gate
    # posts after a Tests push run; a batch candidate the train cannot get
    # marked would wait on checks that never start, then be refused by `main`.
    assert _pushed(workflow()['on'], BATCH_REF)
    gate = workflow('train-candidate.yml')['on']['workflow_run']
    assert gate['workflows'] == ['Tests']
    assert any(fnmatchcase(BATCH_REF, pattern) for pattern in gate['branches'])


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
    assert list(workflow()['jobs']) == ['test', 'e2e-cli', 'dashboard']


def test_postgres_comes_from_a_mirror_without_docker_hubs_anonymous_pull_limit():
    # Run 37990643989 (2026-10-09) failed all 15 Postgres jobs with Docker Hub's
    # `toomanyrequests` before a test ran. Google's Docker Hub mirror serves the
    # same official image with no secret, so fork pull requests can pull it too.
    jobs = workflow()['jobs']
    start = next(step for step in jobs['test']['steps'] if step['name'] == 'Start PostgreSQL')
    assert POSTGRES_IMAGE in start['run'].split()
    assert jobs['e2e-cli']['services']['postgres']['image'] == POSTGRES_IMAGE
    for path in WORKFLOWS.glob('*.yml'):
        text = path.read_text()
        assert not re.search(r'(?<![\w./-])postgres:\d', text), path
        for job in workflow(path.name)['jobs'].values():
            images = [service['image'] for service in (job.get('services') or {}).values()]
            images += [job['container']['image']] if 'container' in job else []
            for image in images:
                registry = image.split('/', 1)[0]
                assert '.' in registry and 'docker.io' not in registry, (path, image)


def test_dashboard_checks_the_candidate_with_locked_workspace_dependencies():
    job = workflow()['jobs']['dashboard']
    assert job['if'] == workflow()['jobs']['test']['if']
    assert job['runs-on'] == 'ubuntu-latest'
    assert 'needs' not in job
    assert 'continue-on-error' not in job
    steps = job['steps']
    checkout, verify, node, install, typecheck, build = steps
    assert checkout == workflow()['jobs']['test']['steps'][0]
    assert verify == workflow()['jobs']['test']['steps'][1]
    assert re.fullmatch(r'actions/setup-node@[0-9a-f]{40}', node['uses'])
    assert node['with']['node-version'] == '24'
    assert node['with']['cache'] == 'npm'
    assert node['with']['cache-dependency-path'] == 'package-lock.json'
    assert install['run'] == 'npm ci'
    assert install.get('working-directory', '.') == '.'
    for step, command in ((typecheck, 'npm run typecheck'), (build, 'npm run build')):
        assert step['working-directory'] == 'dashboard'
        assert step['run'] == command
    for step in steps:
        assert 'if' not in step
        assert 'continue-on-error' not in step
    # Install time has its own limit; leave setup/cache cleanup headroom too.
    assert int(job['timeout-minutes']) >= sum(
        int(step['timeout-minutes']) for step in (install, typecheck, build)
    ) + 2


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


@pytest.mark.parametrize('job', ['test', 'e2e-cli'])
@pytest.mark.parametrize('failures', [0, 1, 2, 3])
def test_dependency_install_recovers_but_persistent_failure_is_fatal(tmp_path, job, failures):
    install = next(
        step for step in workflow()['jobs'][job]['steps']
        if step['name'] == 'Install dependencies on cache miss'
    )
    assert install['if'] == "steps.venv.outputs.cache-hit != 'true'"
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    # Execute the workflow's actual shell with stand-ins for venv/pip and
    # sleep. A wheel timeout exits 2; successful installs must stop retrying.
    python_stub = bin_dir / 'python'
    python_stub.write_text(f'#!{sys.executable}\n' + '''
import json
import os
import shutil
import sys
from pathlib import Path

if sys.argv[1:] == ['-m', 'venv', '.venv']:
    target = Path('.venv/bin/python')
    target.parent.mkdir(parents=True)
    shutil.copyfile(__file__, target)
    target.chmod(0o755)
    sys.exit(0)
assert sys.argv[1:4] == ['-m', 'pip', 'install'], sys.argv
assert sys.argv[4:] == [
    '--timeout', '60', '-e', '.[dev,cli]', 'poetry-core>=2.0.0,<3.0.0'
], sys.argv
trace = Path('install-attempts.json')
attempts = json.loads(trace.read_text()) if trace.exists() else []
attempts.append(sys.argv[1:])
trace.write_text(json.dumps(attempts))
sys.exit(2 if len(attempts) <= int(os.environ['CI_INSTALL_FAILURES']) else 0)
''')
    python_stub.chmod(0o755)
    sleep_stub = bin_dir / 'sleep'
    sleep_stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> retry-delays\n')
    sleep_stub.chmod(0o755)

    result = subprocess.run(
        ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', install['run']],
        cwd=tmp_path,
        env={
            **os.environ,
            'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
            'CI_INSTALL_FAILURES': str(failures),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    attempts = json.loads((tmp_path / 'install-attempts.json').read_text())
    assert len(attempts) == min(failures + 1, 3)
    assert result.returncode == (2 if failures == 3 else 0), result.stderr
    delays = tmp_path / 'retry-delays'
    assert (delays.read_text().splitlines() if delays.exists() else []) == (
        ['5'] * min(failures, 2)
    )
    assert ('::error::' in result.stdout) is (failures == 3)


def test_e2e_matrix_keeps_smoke_on_prs_and_off_the_postgres_suite():
    jobs = workflow()['jobs']
    e2e = jobs['e2e-cli']
    assert e2e['if'] == jobs['test']['if']
    assert e2e['strategy']['matrix']['group'] == list(SCENARIO_GROUPS)
    assert e2e['strategy']['fail-fast'] == 'false'
    # The cap covers a cold dependency install (up to 7m30s observed on
    # hosted runners) on top of an item's own pytest limit, so pytest's
    # timeout, with its diagnostics, fires before the job is cancelled.
    job_timeout_seconds = int(e2e['timeout-minutes']) * 60
    assert job_timeout_seconds - E2E_TEST_TIMEOUT_SECONDS >= 450
    run = next(step['run'] for step in e2e['steps'] if step['name'] == 'Run scenario group')
    assert run == (
        "pytest tests/test_e2e_cli_stateful.py -k '${{ matrix.group }}-' "
        "-m integration -s --durations=0 --junitxml=e2e-results.xml"
    )
    suites = {suite['name']: suite['command'] for suite in jobs['test']['strategy']['matrix']['suite']}
    assert '--ignore=tests/test_e2e_cli_stateful.py' in suites['postgres-integration']
    checkout = e2e['steps'][0]
    assert checkout['uses'] == 'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683'
    assert checkout['with']['ref'] == '${{ github.sha }}'


def test_e2e_groups_cover_every_scenario_once_and_keep_claim_dependencies_together():
    scenarios = [scenario for group in SCENARIO_GROUPS.values() for scenario in group]
    expected = {f'S{index}' for index in range(1, 21)} - {'S16'} | {'S16a', 'S16b'}
    assert set(scenarios) == expected
    assert len(scenarios) == len(expected)
    assert SCENARIO_GROUPS['claims'][:3] == ('S1', 'S2', 'S3')
    assert 'S20' in SCENARIO_GROUPS['cli']


@pytest.mark.parametrize(
    ('job', 'run_step', 'run_seconds'),
    [
        # The slowest passing default shard in the 40 runs up to 36831813696.
        ('test', 'Run tests', 332),
        # tests/test_e2e_cli_stateful.py gives each item its own limit; let it
        # fire first, with a minute for its diagnostics and runner overhead.
        ('e2e-cli', 'Run scenario group', E2E_TEST_TIMEOUT_SECONDS + 59),
    ],
)
def test_a_slow_cache_miss_install_cannot_spend_the_run_budget(job, run_step, run_seconds):
    # Run 36830122857 spent 439 s of E2E CLI (cli)'s shared 10-minute job
    # budget installing from PyPI at 40-300 kB/s, then cancelled the passing
    # group 158 s into its scenarios. Eight jobs in 40 runs died that way, with
    # installs of 306-600 s and run steps no longer than 332 s.
    spec = workflow()['jobs'][job]
    steps = {step['name']: step for step in spec['steps']}
    install = int(steps['Install dependencies on cache miss']['timeout-minutes'])
    run = int(steps[run_step]['timeout-minutes'])
    assert install * 60 > 600
    assert run * 60 > run_seconds
    # Checkout, PostgreSQL, interpreter, cache restore and save, the editable
    # refresh, migrations and artifact upload take well under three minutes.
    assert int(spec['timeout-minutes']) >= install + run + 3


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


def test_venv_seed_retries_a_stalled_pypi_download(tmp_path):
    # A failed seed leaves main without an entry, so every ref's first run
    # would install from the same slow PyPI the retry exists for.
    install = _venv_steps('venv-cache.yml', 'warm')['Install dependencies on cache miss']
    result, calls = _run_install(tmp_path, install['run'], fail_times=2)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 3


def _selecting_arms(path, markers):
    """Apply paths and markers, treating the disjoint default shards as one arm."""
    addopts = shlex.split(
        tomllib.loads(Path('pyproject.toml').read_text())['tool']['pytest']['ini_options']['addopts']
    )
    default_expression = addopts[addopts.index('-m') + 1]
    selected = set()
    for suite in workflow()['jobs']['test']['strategy']['matrix']['suite']:
        args = shlex.split(suite['command'])
        files = [arg for arg in args if arg.startswith('tests/') and arg.endswith('.py')]
        if files and path not in files:
            continue
        if f'--ignore={path}' in args:
            continue
        expression = args[args.index('-m') + 1] if '-m' in args else default_expression
        if Expression.compile(expression).evaluate(lambda marker: marker in markers):
            name = suite['name']
            selected.add('default' if name.startswith('default-') else name)
    return sorted(selected)


@pytest.mark.parametrize('flags', list(product([False, True], repeat=5)))
def test_ci_marker_partition_runs_every_selected_test_once(flags):
    names = ('migration', 'slow', 'integration', 'perf', 'tmux')
    markers = {name for name, enabled in zip(names, flags) if enabled}
    selected = _selecting_arms('tests/test_example.py', markers)
    # tmux-only tests are intentionally opt-in; all other combinations were
    # covered by the old union of default, migration/slow and integration/perf.
    should_run = markers != {'tmux'}
    assert len(selected) == int(should_run), (markers, selected)


@pytest.mark.parametrize('path', ['tests/test_cli_inventory.py', 'tests/test_cli_conformance.py'])
def test_cli_conformance_files_run_only_in_their_dedicated_arm(path):
    assert _selecting_arms(path, set()) == ['cli-conformance']


def test_scratch_migration_check_runs_once():
    steps = workflow()['jobs']['test']['steps']
    checks = [
        step for step in steps
        if step.get('name') == 'Apply migrations to a scratch PostgreSQL database'
    ]
    assert len(checks) == 1
    assert checks[0]['if'] == "matrix.suite.name == 'migration-and-slow'"
