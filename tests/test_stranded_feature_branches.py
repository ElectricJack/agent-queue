"""``pools.stranded_feature_branches`` — work merged into a branch nobody merged.

The Pkg 4 outage: PRs #284/#288/#289 were merged into
``feature/playbook-v2-pkg4-core``, all three tasks closed COMPLETED, and no
PR ever took ``pkg4-core`` to ``main`` — so ``main`` never gained
``src/playbooks/executors/agent_task.py`` while every dependent task believed
its prerequisite had shipped.

Git is real here (a bare origin plus a clone, so branch topology and
ahead-counts are genuine); ``gh`` is the only thing stubbed, because a doctor
check cannot open GitHub in a test.  ``gh`` returning ``None`` — offline, no
auth, not installed — is covered explicitly: it must produce *no* findings
rather than accusing every branch.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

import src.doctor  # noqa: F401 -- side effect: populates sys.modules
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.doctor.models import Severity
from src.models import Project, RepoSourceType, Workspace
from tests.pg_dsn import ensure_worker_postgres_dsn

pool_checks = sys.modules["src.doctor.pool_checks"]

PROJECT_ID = "proj"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: str, name: str) -> str:
    pathlib.Path(repo, name).write_text(name)
    _git(["add", name], cwd=repo)
    _git(["commit", "-m", f"add {name}"], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo)


@pytest.fixture
def repo(tmp_path):
    """origin + clone with ``main``, a feature branch ahead of it, and ``aq/x``."""
    origin = str(tmp_path / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", origin],
        check=True,
        capture_output=True,
    )
    clone = str(tmp_path / "clone")
    subprocess.run(["git", "clone", origin, clone], check=True, capture_output=True)
    _git(["config", "user.name", "Test"], cwd=clone)
    _git(["config", "user.email", "t@t.com"], cwd=clone)
    _commit(clone, "README.md")
    _git(["push", "origin", "main"], cwd=clone)

    _git(["checkout", "-b", "feature/pkg4-core"], cwd=clone)
    _commit(clone, "agent_task.py")
    _git(["push", "origin", "feature/pkg4-core"], cwd=clone)

    _git(["checkout", "-b", "aq/task-9", "main"], cwd=clone)
    _commit(clone, "worker.py")
    _git(["push", "origin", "aq/task-9"], cwd=clone)

    _git(["checkout", "main"], cwd=clone)
    return clone


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "d.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture(params=["sqlite", "postgres"])
async def any_db(request, tmp_path, repo):
    """SQLite always; PostgreSQL when ``POSTGRES_TEST_DSN`` is set (CI)."""
    if request.param == "postgres":
        if not POSTGRES_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "doctor.db"))
        await database.initialize()
    await database.create_project(
        Project(id=PROJECT_ID, name="P", repo_default_branch="main")
    )
    await database.create_workspace(
        Workspace(
            id="ws-1",
            project_id=PROJECT_ID,
            workspace_path=repo,
            source_type=RepoSourceType.CLONE,
        )
    )
    yield database
    await database.close()


def _fake_gh(monkeypatch, *, merged_into: dict, open_to_default: set[str] | None = None):
    """Stub ``GitManager.alist_prs``. ``None`` anywhere means "gh can't answer"."""
    open_to_default = open_to_default or set()

    async def _alist_prs(self, checkout_path, *, state="open", base=None, head=None, limit=30):
        if state == "open" and head is not None:
            if head in open_to_default:
                return [{"url": "https://github.com/o/r/pull/900", "headRefName": head}]
            return []
        if state == "merged" and base is not None:
            return merged_into.get(base, [])
        return []

    monkeypatch.setattr(pool_checks.GitManager, "alist_prs", _alist_prs)


async def _run(db, config, repair: bool = False):
    return await pool_checks.run_check(
        db, "pools.stranded_feature_branches", config=config, repair=repair
    )


async def test_branch_with_merged_prs_and_no_pr_to_main_is_stranded(
    any_db, config, monkeypatch
):
    _fake_gh(
        monkeypatch,
        merged_into={
            "feature/pkg4-core": [
                {"url": "https://github.com/o/r/pull/284"},
                {"url": "https://github.com/o/r/pull/288"},
            ]
        },
    )

    result = await _run(any_db, config)

    assert result.severity is Severity.WARN
    assert result.data["count"] == 1
    finding = result.data["branches"][0]
    assert finding["branch"] == "feature/pkg4-core"
    assert finding["ahead"] == 1
    assert "https://github.com/o/r/pull/284" in finding["merged_prs"]
    assert "gh pr create --base main --head feature/pkg4-core" in finding["command"]


async def test_an_open_pr_to_main_clears_the_branch(any_db, config, monkeypatch):
    _fake_gh(
        monkeypatch,
        merged_into={"feature/pkg4-core": [{"url": "https://github.com/o/r/pull/284"}]},
        open_to_default={"feature/pkg4-core"},
    )

    result = await _run(any_db, config)

    assert result.severity is Severity.OK
    assert result.data["count"] == 0


async def test_stale_branch_without_merged_prs_is_reported_not_warned(
    any_db, config, monkeypatch
):
    """``feature/playbook-v2-pkg4`` — ahead of main, never merged, no PR."""
    _fake_gh(monkeypatch, merged_into={})

    result = await _run(any_db, config)

    assert result.severity is Severity.INFO
    assert result.data["count"] == 0
    assert [f["branch"] for f in result.data["stale"]] == ["feature/pkg4-core"]


async def test_task_branches_are_not_reported_as_stale(any_db, config, monkeypatch):
    """Every ``aq/*`` slot branch is ahead of main by design."""
    _fake_gh(monkeypatch, merged_into={})

    result = await _run(any_db, config)

    assert all(f["branch"] != "aq/task-9" for f in result.data["stale"])


async def test_a_task_branch_with_merged_prs_is_still_stranded(
    any_db, config, monkeypatch
):
    """Exclusion is only for the *stale* bucket: a stacked base is a base."""
    _fake_gh(
        monkeypatch,
        merged_into={"aq/task-9": [{"url": "https://github.com/o/r/pull/301"}]},
    )

    result = await _run(any_db, config)

    assert result.severity is Severity.WARN
    assert [f["branch"] for f in result.data["branches"]] == ["aq/task-9"]


async def test_gh_unavailable_reports_nothing_and_counts_it(any_db, config, monkeypatch):
    async def _no_gh(self, checkout_path, *, state="open", base=None, head=None, limit=30):
        return None

    monkeypatch.setattr(pool_checks.GitManager, "alist_prs", _no_gh)

    result = await _run(any_db, config)

    assert result.severity is Severity.OK
    assert result.data["count"] == 0
    assert result.data["unverifiable"] == 2


async def test_fix_prints_the_command_and_opens_no_pr(any_db, config, monkeypatch):
    """``--fix`` must not create a pull request — it hands over the command."""
    created: list = []

    async def _alist_prs(self, checkout_path, *, state="open", base=None, head=None, limit=30):
        if state == "merged" and base == "feature/pkg4-core":
            return [{"url": "https://github.com/o/r/pull/284"}]
        return []

    async def _explode(*a, **kw):  # pragma: no cover - must never run
        created.append(a)
        raise AssertionError("doctor --fix must not open a PR")

    monkeypatch.setattr(pool_checks.GitManager, "alist_prs", _alist_prs)
    monkeypatch.setattr(pool_checks.GitManager, "acreate_pr", _explode, raising=False)

    result = await _run(any_db, config, repair=True)

    assert created == []
    assert result.fix_applied is True
    # Still WARN after the "fix": nothing was repaired, and pretending
    # otherwise would hide the branch from the next run.
    assert result.severity is Severity.WARN
    assert "gh pr create --base main --head feature/pkg4-core" in result.data["commands"][0]
