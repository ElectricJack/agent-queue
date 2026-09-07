"""Full CI is scheduled only at integration boundaries."""
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml


def workflow():
    return yaml.load(Path('.github/workflows/tests.yml').read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize(('branch', 'expected'), [
    ('main', True),
    ('aq/parent/example', True),
    ('aq/integration/p-123/r-456', True),
    ('aq/sound-current', True),  # Frozen legacy parent, intentionally retained.
    ('aq/feature/example', False),
    ('aq/example', False),
    ('aq/repair-example', False),
    ('fix/feature-merge-policy', False),
])
def test_push_ci_only_covers_integration_boundaries(branch, expected):
    patterns = (workflow()['on']['push'] or {}).get('branches', ['**'])
    assert any(fnmatchcase(branch, pattern) for pattern in patterns) is expected


def test_manual_ci_is_available_without_duplicate_pr_or_unused_merge_queue_runs():
    triggers = workflow()['on']
    assert 'workflow_dispatch' in triggers
    assert 'pull_request' not in triggers
    assert 'merge_group' not in triggers


def test_new_candidate_cancels_only_its_own_obsolete_ci():
    concurrency = workflow()['concurrency']
    assert 'github.ref' in concurrency['group']
    assert concurrency['cancel-in-progress'] == "${{ github.ref != 'refs/heads/main' }}"
