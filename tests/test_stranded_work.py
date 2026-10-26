"""``preserve_unpushed_work`` — the push-before-you-close rule.

Real git repositories throughout (a bare "origin" plus clones), because
every interesting case here is a git question — detached HEAD, a branch with
no upstream, a remote branch that diverged — and a mocked GitManager would
only assert that the code calls the methods the code calls.

See ``src/orchestrator/stranded_work.py`` for the outage this guards.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from src.git.manager import GitManager
from src.orchestrator.stranded_work import preserve_unpushed_work, save_wip_to_task_branch


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(repo: str, name: str, body: str = "x") -> str:
    pathlib.Path(repo, name).write_text(body)
    _git(["add", name], cwd=repo)
    _git(["commit", "-m", f"add {name}"], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo)


@pytest.fixture
def origin(tmp_path):
    bare = str(tmp_path / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", bare],
        check=True,
        capture_output=True,
    )
    return bare


@pytest.fixture
def clone(tmp_path, origin):
    path = str(tmp_path / "clone")
    subprocess.run(["git", "clone", origin, path], check=True, capture_output=True)
    _git(["config", "user.name", "Test"], cwd=path)
    _git(["config", "user.email", "t@t.com"], cwd=path)
    _commit(path, "README.md", "init")
    _git(["push", "origin", "main"], cwd=path)
    return path


@pytest.fixture
def git():
    return GitManager()


async def test_clean_workspace_reports_clean(clone, git):
    result = await preserve_unpushed_work(git, clone, "task-1")
    assert result.status == "clean"
    assert result.at_risk is False


async def test_unpushed_commits_land_on_the_task_branch(clone, origin, git):
    _git(["checkout", "-b", "aq/task-1"], cwd=clone)
    sha = _commit(clone, "work.py", "print(1)")

    result = await preserve_unpushed_work(git, clone, "task-1")

    assert result.status == "pushed"
    assert result.branch == "aq/task-1"
    assert result.commit == sha
    assert result.count == 1
    assert _git(["rev-parse", "refs/heads/aq/task-1"], cwd=origin) == sha


async def test_detached_head_is_rescued_too(clone, origin, git):
    """A slot left on a detached HEAD is the shape ``@{u}`` cannot see."""
    _git(["checkout", "--detach"], cwd=clone)
    sha = _commit(clone, "work.py", "print(1)")

    result = await preserve_unpushed_work(git, clone, "task-2")

    assert result.status == "pushed"
    assert result.branch == "aq/task-2"
    assert _git(["rev-parse", "refs/heads/aq/task-2"], cwd=origin) == sha


async def test_rescue_is_not_delivery_and_preserves_daemon_owned_paths(clone, origin, git):
    """Recovery saves the complete commit; delivery validation happens elsewhere."""
    _git(["checkout", "-b", "aq/task-rescue"], cwd=clone)
    reserved = pathlib.Path(clone, ".aq-worktree.json")
    reserved.write_text('{"workspace": "recovery evidence"}\n')
    _git(["add", "-f", ".aq-worktree.json"], cwd=clone)
    _git(["commit", "-m", "preserve recovery evidence"], cwd=clone)
    sha = _git(["rev-parse", "HEAD"], cwd=clone)

    result = await preserve_unpushed_work(git, clone, "task-rescue")

    assert result.status == "pushed"
    assert _git(["rev-parse", "refs/heads/aq/task-rescue"], cwd=origin) == sha
    assert _git(["show", "aq/task-rescue:.aq-worktree.json"], cwd=origin) == (
        '{"workspace": "recovery evidence"}'
    )


async def test_branch_pushed_under_another_name_counts_as_safe(clone, git):
    """Commits already on *some* remote branch are not stranded."""
    _git(["checkout", "-b", "feature/x"], cwd=clone)
    _commit(clone, "work.py", "print(1)")
    _git(["push", "origin", "feature/x"], cwd=clone)

    result = await preserve_unpushed_work(git, clone, "task-3")

    assert result.status == "clean"


async def test_diverged_remote_branch_is_never_overwritten(clone, origin, tmp_path, git):
    """Another run already owns ``aq/task-4`` — the rescue goes to ``-wip``."""
    other = str(tmp_path / "other")
    subprocess.run(["git", "clone", origin, other], check=True, capture_output=True)
    _git(["config", "user.name", "Other"], cwd=other)
    _git(["config", "user.email", "o@o.com"], cwd=other)
    _git(["checkout", "-b", "aq/task-4"], cwd=other)
    theirs = _commit(other, "theirs.py", "other")
    _git(["push", "origin", "aq/task-4"], cwd=other)

    _git(["checkout", "-b", "aq/task-4"], cwd=clone)
    mine = _commit(clone, "mine.py", "mine")

    result = await preserve_unpushed_work(git, clone, "task-4")

    assert result.status == "pushed"
    assert result.branch == "aq/task-4-wip"
    assert _git(["rev-parse", "refs/heads/aq/task-4"], cwd=origin) == theirs
    assert _git(["rev-parse", "refs/heads/aq/task-4-wip"], cwd=origin) == mine


async def test_fast_forward_over_our_own_earlier_push_reuses_the_branch(clone, origin, git):
    _git(["checkout", "-b", "aq/task-5"], cwd=clone)
    _commit(clone, "one.py")
    _git(["push", "origin", "aq/task-5"], cwd=clone)
    second = _commit(clone, "two.py")

    result = await preserve_unpushed_work(git, clone, "task-5")

    assert result.branch == "aq/task-5"
    assert _git(["rev-parse", "refs/heads/aq/task-5"], cwd=origin) == second


async def test_repository_with_no_remote_reports_no_remote(tmp_path, git):
    local = str(tmp_path / "local")
    pathlib.Path(local).mkdir()
    _git(["init", "--initial-branch=main"], cwd=local)
    _git(["config", "user.name", "Test"], cwd=local)
    _git(["config", "user.email", "t@t.com"], cwd=local)
    sha = _commit(local, "a.py")

    result = await preserve_unpushed_work(git, local, "task-6")

    assert result.status == "no_remote"
    assert result.branch == "main"
    assert result.commit == sha
    assert result.at_risk is True


async def test_push_failure_is_reported_not_swallowed(clone, tmp_path, git):
    _git(["checkout", "-b", "aq/task-7"], cwd=clone)
    _commit(clone, "work.py")
    # Point origin at a path that does not exist: every push fails.
    _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], cwd=clone)

    result = await preserve_unpushed_work(git, clone, "task-7")

    assert result.status == "push_failed"
    assert result.at_risk is True
    assert result.count == 1
    assert result.error


async def test_missing_workspace_is_unknown_not_clean(tmp_path, git):
    result = await preserve_unpushed_work(git, str(tmp_path / "nope"), "task-8")
    assert result.status == "unknown"
    assert result.at_risk is False


async def test_wip_save_commits_and_fast_forwards_the_task_branch(clone, origin, git):
    """Usage-exhausted handoff: the work lands on the task's own branch, nowhere else."""
    _git(["checkout", "-b", "aq/task-wip"], cwd=clone)
    base = _commit(clone, "done.py", "print(0)")
    _git(["push", "origin", "aq/task-wip"], cwd=clone)
    _commit(clone, "committed.py", "print(1)")
    pathlib.Path(clone, "unstaged.py").write_text("print(2)")

    result = await save_wip_to_task_branch(
        git, clone, "refs/heads/aq/task-wip", "usage exhausted on opencode"
    )

    assert result.status == "pushed"
    head = _git(["rev-parse", "HEAD"], cwd=clone)
    assert result.commit == head
    assert _git(["rev-parse", "refs/heads/aq/task-wip"], cwd=origin) == head
    assert _git(["merge-base", "--is-ancestor", base, head], cwd=clone) == ""
    assert _git(["log", "-1", "--format=%s", head], cwd=clone).startswith(
        "WIP saved by AQ: usage exhausted on opencode"
    )
    assert _git(["status", "--porcelain"], cwd=clone) == ""
    # Fast-forward: no -wip or preserved name was needed.
    refs = _git(["for-each-ref", "--format=%(refname)"], cwd=origin).split()
    assert refs == ["refs/heads/aq/task-wip", "refs/heads/main"]


async def test_wip_save_of_a_clean_pushed_checkout_is_clean(clone, origin, git):
    _git(["checkout", "-b", "aq/task-clean"], cwd=clone)
    sha = _commit(clone, "done.py")
    _git(["push", "origin", "aq/task-clean"], cwd=clone)

    result = await save_wip_to_task_branch(git, clone, "aq/task-clean", "stopped")

    assert result.status == "clean"
    assert result.commit == sha
    assert _git(["rev-parse", "HEAD"], cwd=clone) == sha


@pytest.mark.parametrize("state", ["untracked", "unstaged", "staged", "renamed"])
async def test_wip_save_refuses_oversized_files_before_changing_index(
    clone, origin, git, monkeypatch, state,
):
    monkeypatch.setattr("src.orchestrator.stranded_work.WIP_MAX_FILE_BYTES", 64)
    _git(["checkout", "-b", "aq/large"], cwd=clone)
    name = "large é\nfile.bin"
    if state != "untracked":
        _commit(clone, name, "small")
    _git(["push", "origin", "aq/large"], cwd=clone)
    if state == "renamed":
        _git(["mv", name, "renamed.bin"], cwd=clone)
        name = "renamed.bin"
    pathlib.Path(clone, name).write_bytes(b"x" * 65)
    if state == "staged":
        _git(["add", "--", name], cwd=clone)
    pathlib.Path(clone, "safe.py").write_text("safe work")
    head = _git(["rev-parse", "HEAD"], cwd=clone)
    status = _git(["status", "--porcelain=v1", "-z"], cwd=clone)
    index = _git(["diff", "--cached", "--binary"], cwd=clone)

    result = await save_wip_to_task_branch(git, clone, "aq/large", "hibernate:test")

    assert result.status == "push_failed" and result.commit == head
    assert repr(name) in result.error and "65 bytes" in result.error
    assert _git(["rev-parse", "HEAD"], cwd=clone) == head
    assert _git(["rev-parse", "refs/heads/aq/large"], cwd=origin) == head
    assert _git(["status", "--porcelain=v1", "-z"], cwd=clone) == status
    assert _git(["diff", "--cached", "--binary"], cwd=clone) == index


@pytest.mark.parametrize("state", ["untracked", "staged", "deleted", "renamed"])
async def test_wip_save_honors_worker_do_not_commit_declaration(clone, origin, git, state):
    _git(["checkout", "-b", "aq/excluded"], cwd=clone)
    name = "generated.json"
    if state != "untracked":
        _commit(clone, name)
    _git(["push", "origin", "aq/excluded"], cwd=clone)
    pathlib.Path(clone, ".aq").mkdir()
    # This is per-checkout control state, like claim.json, never repository content.
    pathlib.Path(clone, ".git/info/exclude").write_text(".aq/\n")
    pathlib.Path(clone, ".aq/wip-do-not-commit").write_text(
        "# Keep generated output local\ngenerated.json\n", encoding="utf-8",
    )
    if state == "deleted":
        pathlib.Path(clone, name).unlink()
    elif state == "renamed":
        _git(["mv", name, "new-name.json"], cwd=clone)
    else:
        pathlib.Path(clone, name).write_text("deliberately uncommitted")
        if state == "staged":
            _git(["add", name], cwd=clone)
    head = _git(["rev-parse", "HEAD"], cwd=clone)
    status = _git(["status", "--porcelain=v1", "-z"], cwd=clone)

    result = await save_wip_to_task_branch(git, clone, "aq/excluded", "hibernate:test")

    assert result.status == "push_failed"
    assert "generated.json" in result.error and ".aq/wip-do-not-commit" in result.error
    assert _git(["rev-parse", "HEAD"], cwd=clone) == head
    assert _git(["status", "--porcelain=v1", "-z"], cwd=clone) == status
    assert _git(["rev-parse", "refs/heads/aq/excluded"], cwd=origin) == head


async def test_wip_save_accepts_size_boundary_deletion_and_symlink(clone, origin, git, monkeypatch):
    monkeypatch.setattr("src.orchestrator.stranded_work.WIP_MAX_FILE_BYTES", 64)
    _git(["checkout", "-b", "aq/safe"], cwd=clone)
    _commit(clone, "deleted.bin", "x" * 65)
    pathlib.Path(clone, "deleted.bin").unlink()
    pathlib.Path(clone, "boundary.bin").write_bytes(b"x" * 64)
    pathlib.Path(clone, "link").symlink_to("boundary.bin")
    result = await save_wip_to_task_branch(git, clone, "aq/safe", "hibernate:test")
    assert result.status == "pushed"
    assert _git(["status", "--porcelain"], cwd=clone) == ""


@pytest.mark.parametrize('declaration', [b'../outside\n', b'/absolute\n', b'\xff'])
async def test_wip_save_refuses_invalid_declaration_without_committing(clone, git, declaration):
    _git(['checkout', '-b', 'aq/invalid'], cwd=clone)
    pathlib.Path(clone, '.aq').mkdir()
    pathlib.Path(clone, '.git/info/exclude').write_text('.aq/\n')
    pathlib.Path(clone, '.aq/wip-do-not-commit').write_bytes(declaration)
    pathlib.Path(clone, 'safe.py').write_text('safe work')
    head = _git(['rev-parse', 'HEAD'], cwd=clone)
    result = await save_wip_to_task_branch(git, clone, 'aq/invalid', 'hibernate:test')
    assert result.status == 'push_failed' and result.error
    assert _git(['rev-parse', 'HEAD'], cwd=clone) == head
    assert _git(['status', '--porcelain'], cwd=clone) == '?? safe.py'


async def test_wip_directory_declaration_does_not_match_sibling_paths(clone, git):
    _git(['checkout', '-b', 'aq/directory'], cwd=clone)
    pathlib.Path(clone, '.aq').mkdir()
    pathlib.Path(clone, '.git/info/exclude').write_text('.aq/\n')
    pathlib.Path(clone, '.aq/wip-do-not-commit').write_text('generated/\n')
    pathlib.Path(clone, 'generated-extra.py').write_text('safe work')
    assert (await save_wip_to_task_branch(git, clone, 'aq/directory', 'hibernate:test')).status == 'pushed'
    pathlib.Path(clone, 'generated').mkdir()
    pathlib.Path(clone, 'generated/data.json').write_text('local data')
    result = await save_wip_to_task_branch(git, clone, 'aq/directory', 'hibernate:test')
    assert result.status == 'push_failed' and 'generated/data.json' in result.error


async def test_wip_save_refuses_lossy_filename_inspection(clone, git, monkeypatch):
    from src.orchestrator import stranded_work

    monkeypatch.setattr(stranded_work, 'WIP_MAX_FILE_BYTES', 64)
    _git(['checkout', '-b', 'aq/filename'], cwd=clone)
    generated = pathlib.Path(clone, 'generated-\udcff.json')
    generated.write_bytes(b'x' * 65)
    head = _git(['rev-parse', 'HEAD'], cwd=clone)
    result = await save_wip_to_task_branch(git, clone, 'aq/filename', 'hibernate:test')
    assert result.status == 'push_failed' and 'decoding loss' in result.error
    assert _git(['rev-parse', 'HEAD'], cwd=clone) == head
    assert generated.read_bytes() == b'x' * 65
    assert _git(['diff', '--cached', '--name-only'], cwd=clone) == ''


async def test_wip_save_never_forces_a_diverged_task_branch(clone, origin, tmp_path, git):
    _git(["checkout", "-b", "aq/task-split"], cwd=clone)
    _commit(clone, "base.py")
    _git(["push", "origin", "aq/task-split"], cwd=clone)
    other = str(tmp_path / "other")
    _git(["clone", origin, other], cwd=str(tmp_path))
    _git(["config", "user.name", "Test"], cwd=other)
    _git(["config", "user.email", "t@t.com"], cwd=other)
    _git(["checkout", "aq/task-split"], cwd=other)
    theirs = _commit(other, "theirs.py")
    _git(["push", "origin", "aq/task-split"], cwd=other)
    pathlib.Path(clone, "mine.py").write_text("mine")

    result = await save_wip_to_task_branch(git, clone, "aq/task-split", "stopped")

    assert result.status == "push_failed"
    assert "does not descend" in result.error
    assert _git(["rev-parse", "refs/heads/aq/task-split"], cwd=origin) == theirs
    # The work is committed locally, for owner recovery to preserve.
    assert _git(["status", "--porcelain"], cwd=clone) == ""


async def test_wip_save_refuses_a_checkout_on_another_branch(clone, origin, git):
    _git(["checkout", "-b", "aq/someone-else"], cwd=clone)
    pathlib.Path(clone, "mine.py").write_text("mine")

    result = await save_wip_to_task_branch(git, clone, "aq/task-mine", "stopped")

    assert result.status == "push_failed"
    assert "not aq/task-mine" in result.error
    assert _git(["status", "--porcelain"], cwd=clone) == "?? mine.py"


async def test_wip_save_refuses_an_origin_outside_the_authorized_repository(
    clone, origin, tmp_path, git
):
    """The save never pushes to a remote the task's repository binding does not name."""
    _git(["checkout", "-b", "aq/task-elsewhere"], cwd=clone)
    _commit(clone, "base.py")
    _git(["push", "origin", "aq/task-elsewhere"], cwd=clone)
    tip = _git(["rev-parse", "refs/heads/aq/task-elsewhere"], cwd=origin)
    pathlib.Path(clone, "mine.py").write_text("mine")

    result = await save_wip_to_task_branch(
        git,
        clone,
        "aq/task-elsewhere",
        "stopped",
        repository_url="https://github.com/example/other-repo.git",
    )

    assert result.status == "push_failed"
    assert "could not read origin/aq/task-elsewhere" in result.error
    assert _git(["rev-parse", "refs/heads/aq/task-elsewhere"], cwd=origin) == tip
    assert _git(["status", "--porcelain"], cwd=clone) == "?? mine.py"


@pytest.mark.parametrize("rewound", [False, True])
async def test_the_next_worker_resumes_from_the_branch_while_it_holds_the_saved_wip(
    clone, origin, git, rewound
):
    """A rewound branch is someone else's decision: start from the filing base."""
    from types import SimpleNamespace

    from src.orchestrator.workspace import WorkspaceMixin

    _git(["checkout", "-b", "aq/task-resume"], cwd=clone)
    base = _commit(clone, "base.py")
    _git(["push", "origin", "aq/task-resume"], cwd=clone)
    pathlib.Path(clone, "wip.py").write_text("wip")
    saved = await save_wip_to_task_branch(git, clone, "aq/task-resume", "usage exhausted")
    assert saved.status == "pushed"
    if rewound:
        _git(["push", "--force", "origin", f"{base}:refs/heads/aq/task-resume"], cwd=clone)
        expected = base
    else:
        expected = _commit(clone, "later.py")
        _git(["push", "origin", "aq/task-resume"], cwd=clone)
    _git(["reset", "--hard", base], cwd=clone)

    start = await WorkspaceMixin._resume_point_start(
        SimpleNamespace(git=git),
        clone,
        {"base_sha": base, "resume_point": {"sha": saved.commit}},
        SimpleNamespace(target=SimpleNamespace(branch="refs/heads/aq/task-resume")),
        repository_url=origin,
    )

    assert start == expected
