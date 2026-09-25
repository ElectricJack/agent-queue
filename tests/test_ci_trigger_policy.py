"""Full CI runs on pull requests and integration boundaries, never on a push to main."""
import re
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

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


def _runs(event_name, pull_request=None, repository='acme/widgets'):
    """Evaluate the test job's ``if:`` guard for one event."""
    expression = workflow()['jobs']['test']['if']
    python = re.sub(r'!(?!=)', ' not ', expression.replace('||', ' or ').replace('&&', ' and '))
    github = _context({
        'event_name': event_name,
        'repository': repository,
        'event': {'pull_request': pull_request} if pull_request else {},
    })
    return bool(eval(python, {'__builtins__': {}}, {'github': github, 'startsWith': _starts_with}))


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


def test_no_workflow_runs_on_a_push_to_main():
    files = sorted(WORKFLOWS.glob('*.yml')) + sorted(WORKFLOWS.glob('*.yaml'))
    assert files
    for path in files:
        assert not _pushed(workflow(path.name)['on'], 'main'), path.name


def test_pull_requests_into_main_run_ci():
    pull_request = workflow()['on']['pull_request']
    assert pull_request['branches'] == ['main']
    assert pull_request['types'] == ['opened', 'synchronize', 'reopened', 'ready_for_review']


def test_manual_ci_is_available_without_unused_merge_queue_runs():
    triggers = workflow()['on']
    assert 'workflow_dispatch' in triggers
    assert 'merge_group' not in triggers


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
    assert list(workflow()['jobs']) == ['test']
