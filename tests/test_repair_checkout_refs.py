from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.database.queries.hierarchy_queries import HierarchyError
from src.git.manager import GitError, RemoteRefResult, RemoteRefState
from src.integration.hierarchy import resolve_workspace_checkpoint
from src.integration.ownership import BranchKey, Fence
from src.orchestrator.workspace import WorkspaceMixin


@pytest.mark.parametrize('prefix', ['', 'refs/heads/'])
async def test_checkpoint_accepts_same_canonical_full_ref(prefix):
    head = 'a' * 40
    ws = SimpleNamespace(locked_by_task_id='repair', workspace_path='/slot')
    db = SimpleNamespace(get_workspace_for_task=AsyncMock(return_value=ws))
    git = SimpleNamespace(
        aget_current_branch=AsyncMock(return_value='aq/integration/batch'),
        _arun=AsyncMock(side_effect=['', head]),
        als_remote_ref=AsyncMock(return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=head)),
    )
    task = {'id': 'repair', 'repo_id': 'repo', 'branch_name': prefix + 'aq/integration/batch'}
    assert await resolve_workspace_checkpoint(db, git, task, SimpleNamespace(id='repo')) == head
    git.als_remote_ref.assert_awaited_once_with('/slot', 'aq/integration/batch')


@pytest.mark.parametrize('ancestor', [True, False])
async def test_repair_retry_fetches_published_tip_with_exact_ref(ancestor):
    head = 'b' * 40
    git = SimpleNamespace(
        _arun=AsyncMock(side_effect=['', head]),
        ais_ancestor=AsyncMock(return_value=ancestor),
    )
    orch = SimpleNamespace(git=git)
    fence = Fence(target=BranchKey(repository_id='repo', branch='refs/heads/aq/integration/batch'), owner_id='repair', token=4)
    if ancestor:
        assert await WorkspaceMixin._hierarchy_repair_start(
            orch, '/slot', {'base_sha': 'a' * 40}, fence
        ) == head
    else:
        with pytest.raises(GitError, match='frozen starting commit'):
            await WorkspaceMixin._hierarchy_repair_start(
                orch, '/slot', {'base_sha': 'a' * 40}, fence
            )
    assert git._arun.await_args_list[0].args[0][-1] == (
        '+refs/heads/aq/integration/batch:refs/remotes/origin/aq/integration/batch'
    )


@pytest.mark.parametrize(
    ('workspace_task', 'repo_id', 'branch_name', 'expected'),
    [
        (None, 'repo', 'aq/t', 'task has no exact owned integration workspace'),
        ('other', 'repo', 'aq/t', 'task has no exact owned integration workspace'),
        ('t', 'elsewhere', 'aq/t', 'task is not bound to the integration repository'),
        ('t', 'repo', None, 'task has no recorded delivery branch'),
        ('t', 'repo', '', 'task has no recorded delivery branch'),
    ],
)
async def test_checkpoint_refusal_names_the_condition_that_failed(
    workspace_task, repo_id, branch_name, expected
):
    """Each guard has a different remedy, so each names itself.

    Folded into one message, a missing ``tasks.branch_name`` read as a
    workspace-lock problem — the refusal a pool worker in a development-mode
    project could neither diagnose nor fix.
    """
    ws = None if workspace_task is None else SimpleNamespace(
        locked_by_task_id=workspace_task, workspace_path='/slot'
    )
    db = SimpleNamespace(get_workspace_for_task=AsyncMock(return_value=ws))
    git = SimpleNamespace(aget_current_branch=AsyncMock(return_value='aq/t'))
    task = {'id': 't', 'repo_id': repo_id, 'branch_name': branch_name}
    with pytest.raises(HierarchyError, match=expected) as raised:
        await resolve_workspace_checkpoint(db, git, task, SimpleNamespace(id='repo'))
    assert raised.value.code == 'dirty'
    git.aget_current_branch.assert_not_awaited()
