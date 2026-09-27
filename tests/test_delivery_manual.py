"""``task_deliver`` — delivering a passed task's pushed branch by hand.

agile-flare and stark-vault passed, pushed ``aq/<task>`` and then blocked at
close ("Could not authorize PR repository"): their projects push to bare
repositories on disk.  These tests pin the supervisor's way out on real git:
fast-forward or merge into the remote default in a private repository, never
in an operator checkout, then complete the task -- and every refusal that
keeps the control from bypassing a pull request, a conflict or a live task.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.git_commands import GitCommandsMixin
from src.database import Database
from src.git.manager import GitManager
from src.integration.manual_delivery import ManualDelivery
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A bare 'local remote' with main and a pushed task branch, plus the
    operator's own checkout of it carrying uncommitted edits."""
    origin = tmp_path / "local-remotes" / "site.git"
    origin.parent.mkdir()
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    checkout = tmp_path / "site"
    _git(tmp_path, "clone", str(origin), str(checkout))
    (checkout / "README.md").write_text("init\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "init")
    _git(checkout, "push", "origin", "main")
    _git(checkout, "switch", "-c", "aq/t-1")
    (checkout / "work.txt").write_text("work\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "task work")
    _git(checkout, "push", "origin", "aq/t-1")
    _git(checkout, "switch", "main")
    (checkout / "README.md").write_text("operator edit\n")
    return SimpleNamespace(origin=origin, checkout=checkout)


@pytest.fixture
async def db(tmp_path, remote):
    d = Database(lease_dsn("manual-delivery.db"))
    await d.initialize()
    await d.create_project(Project(id="site", name="site", repo_url=str(remote.origin)))
    await d.create_task(Task(
        id="t-1", project_id="site", title="t-1", description="",
        status=TaskStatus.BLOCKED, branch_name="aq/t-1",
    ))
    yield d
    await d.close()


def _service(db, tmp_path) -> ManualDelivery:
    return ManualDelivery(db, GitManager(), data_dir=tmp_path / "data")


async def _deliver(db, tmp_path, task_id="t-1", **kw):
    kw.setdefault("reason", "worker passed; PR impossible on a local remote")
    return await _service(db, tmp_path).deliver(
        task_id, operator_id="human:local-operator", default_mode="pull_request", **kw
    )


async def test_fast_forwards_default_and_completes_the_task(db, tmp_path, remote):
    head = _git(remote.origin, "rev-parse", "aq/t-1")

    result = await _deliver(db, tmp_path)

    assert result["success"] is True, result
    assert result["outcome"] == "delivered"
    assert result["method"] == "fast_forward"
    assert _git(remote.origin, "rev-parse", "main") == head
    task = await db.get_task("t-1")
    assert task.status == TaskStatus.COMPLETED
    record = await db.get_task_meta("t-1", "manual_delivery")
    assert record["delivered_sha"] == head
    assert record["operator"] == "human:local-operator"
    # The operator's checkout was never touched, and nothing is left behind.
    assert (remote.checkout / "README.md").read_text() == "operator edit\n"
    assert list((tmp_path / "data" / "manual-delivery").iterdir()) == []


async def test_merges_when_default_moved_on(db, tmp_path, remote):
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote.origin), str(other))
    (other / "other.txt").write_text("other\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "someone else")
    _git(other, "push", "origin", "main")
    main_before = _git(remote.origin, "rev-parse", "main")
    head = _git(remote.origin, "rev-parse", "aq/t-1")

    result = await _deliver(db, tmp_path)

    assert result["outcome"] == "delivered", result
    assert result["method"] == "merge"
    parents = _git(remote.origin, "rev-list", "--parents", "-n", "1", "main").split()[1:]
    assert parents == [main_before, head]
    message = _git(remote.origin, "log", "-1", "--format=%B", "main")
    assert "Delivered by hand for task t-1" in message


async def test_dry_run_changes_nothing(db, tmp_path, remote):
    main_before = _git(remote.origin, "rev-parse", "main")

    result = await _deliver(db, tmp_path, dry_run=True)

    assert result["outcome"] == "would_deliver"
    assert result["method"] == "fast_forward"
    assert _git(remote.origin, "rev-parse", "main") == main_before
    assert (await db.get_task("t-1")).status == TaskStatus.BLOCKED


async def test_already_delivered_work_just_completes(db, tmp_path, remote):
    _git(remote.origin, "update-ref", "refs/heads/main", "refs/heads/aq/t-1")

    result = await _deliver(db, tmp_path)

    assert result["outcome"] == "delivered"
    assert result["method"] == "already_delivered"
    assert (await db.get_task("t-1")).status == TaskStatus.COMPLETED


async def test_conflict_is_refused_and_names_the_files(db, tmp_path, remote):
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote.origin), str(other))
    (other / "work.txt").write_text("conflicting\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "conflict")
    _git(other, "push", "origin", "main")
    main_before = _git(remote.origin, "rev-parse", "main")

    result = await _deliver(db, tmp_path)

    assert result["success"] is False
    assert result["outcome"] == "conflict"
    assert result["conflict_files"] == ["work.txt"]
    assert _git(remote.origin, "rev-parse", "main") == main_before
    assert (await db.get_task("t-1")).status == TaskStatus.BLOCKED


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ({"status": TaskStatus.IN_PROGRESS}, "not_blocked"),
        ({"branch_name": "aq/never-pushed"}, "branch_not_pushed"),
        ({"integration_mode": "pull_request"}, None),
    ],
)
async def test_refusals(db, tmp_path, remote, change, outcome):
    await db.update_task("t-1", **change)

    result = await _deliver(db, tmp_path)

    if outcome is None:
        # An explicit pull_request on a repository that cannot host one is
        # exactly the stranded case the control exists for.
        assert result["outcome"] == "delivered", result
    else:
        assert result["success"] is False
        assert result["outcome"] == outcome


async def test_pull_request_mode_on_github_is_refused(db, tmp_path):
    await db.update_project("site", repo_url="https://github.com/org/site.git")

    result = await _deliver(db, tmp_path)

    assert result["outcome"] == "pull_request_mode"
    assert "aq pr merge" in result["error"]


async def test_managed_projects_are_refused(db, tmp_path):
    import sqlalchemy as sa

    async with db._engine.begin() as conn:
        await conn.execute(sa.text(
            "UPDATE projects SET hierarchical_integration_mode = 'development' WHERE id = 'site'"
        ))

    result = await _deliver(db, tmp_path)

    assert result["outcome"] == "integration_managed"


async def test_a_reason_is_required(db, tmp_path):
    result = await _deliver(db, tmp_path, reason="  ")

    assert result["outcome"] == "invalid"


class _Handler(GitCommandsMixin):
    def __init__(self, db, tmp_path):
        self.db = db
        self.config = SimpleNamespace(
            data_dir=str(tmp_path / "data"),
            integration=SimpleNamespace(default_mode="pull_request"),
        )
        self.orchestrator = SimpleNamespace(git=GitManager(), _emit_task_event=AsyncMock())


async def test_command_delivers_and_announces_completion(db, tmp_path, remote):
    handler = _Handler(db, tmp_path)

    result = await handler._cmd_task_deliver({"task_id": "t-1", "reason": "stranded"})

    assert result["outcome"] == "delivered", result
    event, task = handler.orchestrator._emit_task_event.await_args.args
    assert event == "task.completed"
    assert task.id == "t-1"


async def test_command_refuses_a_worker_session(db, tmp_path, monkeypatch):
    from src.commands import supervisor_authority

    monkeypatch.setattr(
        supervisor_authority, "current_principal",
        lambda: SimpleNamespace(kind=supervisor_authority.PrincipalKind.SESSION,
                                elevated=False),
    )
    handler = _Handler(db, tmp_path)

    result = await handler._cmd_task_deliver({"task_id": "t-1", "reason": "x"})

    assert result["outcome"] == "unauthorized"
    assert (await db.get_task("t-1")).status == TaskStatus.BLOCKED
    handler.orchestrator._emit_task_event.assert_not_awaited()


def test_task_deliver_is_an_operator_control():
    from src.api.scope import OPERATOR_INTEGRATION_CONTROLS

    assert "task_deliver" in OPERATOR_INTEGRATION_CONTROLS
    profile = Path("src/profiles/defaults/supervisor/profile.md").read_text()
    assert '"task_deliver"' in profile
