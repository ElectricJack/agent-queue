"""``ci_baseline_status`` — the observation half of the ci-main-sentinel.

The GitHub reads are stubbed on an autospec'd :class:`GitManager`; the
attempt bookkeeping runs against a real SQLite database because the
signature/attempt rules are exactly what must not drift from the queries.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from src.commands.ci_commands import (
    ESCALATION_KEY_PREFIX,
    REPAIR_KEY_PREFIX,
    failure_signature,
    render_repair_task,
)
from src.commands.contracts.builtin import _outcome_of
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitManager
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT = "proj-ci"
REPO = "https://github.com/example/widgets.git"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _run(name: str, conclusion: str | None, *, job_id: int, status: str = "completed") -> dict:
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/example/widgets/actions/runs/1/job/{job_id}",
    }


RED_RUNS = [
    _run("Tests (default)", "failure", job_id=11),
    _run("Tests (postgres-integration)", "success", job_id=12),
    _run("Deploy Documentation to GitHub Pages", "success", job_id=13),
]
GREEN_RUNS = [_run("Tests (default)", "success", job_id=21)]
FAILED_TESTS = ["tests/test_a.py::test_one", "tests/test_b.py::TestX::test_two"]


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "ci.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="CI", repo_url=REPO))
    yield database
    await database.close()


@pytest.fixture
def git():
    manager = create_autospec(GitManager, instance=True)
    manager.github_repo_slug.side_effect = GitManager.github_repo_slug
    manager.acommit_head_sha.return_value = SHA
    manager.acommit_check_runs.return_value = list(RED_RUNS)
    manager.ajob_failed_tests.return_value = list(FAILED_TESTS)
    return manager


@pytest.fixture
async def handler(db, git, tmp_path):
    config = AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = git
    return CommandHandler(orch, config)


async def _attempt(db, dedup_key: str, status: TaskStatus) -> str:
    task_id = dedup_key.replace(":", "-")
    await db.create_task(
        Task(
            id=task_id,
            project_id=PROJECT,
            title=task_id,
            description="",
            status=status,
            dedup_key=dedup_key,
        )
    )
    return task_id


# -- pure pieces -------------------------------------------------------------


def test_signature_depends_on_what_failed_not_on_which_commit():
    assert failure_signature(FAILED_TESTS, ["Tests (default)"]) == failure_signature(
        list(reversed(FAILED_TESTS)), ["other"]
    )
    assert failure_signature(FAILED_TESTS, []) != failure_signature(FAILED_TESTS[:1], [])


def test_signature_falls_back_to_check_names_when_no_tests_were_read():
    by_checks = failure_signature([], ["Tests (default)"])
    assert by_checks == failure_signature([], ["Tests (default)"])
    assert by_checks != failure_signature([], ["Tests (postgres-integration)"])


def test_repair_task_text_names_the_failure_and_the_rules():
    title, description = render_repair_task(
        ref="main",
        head_sha=SHA,
        failing_checks=["Tests (default)"],
        failing_tests=FAILED_TESTS,
        run_url="https://example/run",
        attempt=2,
    )
    assert title == f"Fix red CI on main @ {SHA[:8]} (attempt 2)"
    for needle in (FAILED_TESTS[0], "Tests (default)", "https://example/run", "pull request", "attempt 2"):
        assert needle in description


def test_github_repo_slug_accepts_https_and_ssh_forms():
    assert GitManager.github_repo_slug(REPO) == "example/widgets"
    assert GitManager.github_repo_slug("git@github.com:example/widgets.git") == "example/widgets"
    assert GitManager.github_repo_slug("https://github.com/example/widgets") == "example/widgets"
    assert GitManager.github_repo_slug("https://gitlab.com/example/widgets") is None
    assert GitManager.github_repo_slug("") is None


def test_failed_test_line_regex_reads_pytest_summary_lines():
    pattern = GitManager._FAILED_TEST_LINE
    line = "2026-09-05T00:20:00Z FAILED tests/test_x.py::test_y - AssertionError: boom"
    assert pattern.search(line).group(1) == "tests/test_x.py::test_y"
    assert pattern.search("2026-09-05T00:20:00Z ERROR tests/test_z.py::TestA::test_b").group(1) == (
        "tests/test_z.py::TestA::test_b"
    )
    assert pattern.search("collected 12 items") is None


def test_contract_outcome_follows_state_and_escalation():
    assert _outcome_of("ci_baseline_status", {"state": "green"}) == "green"
    assert _outcome_of("ci_baseline_status", {"state": "red"}) == "red"
    assert _outcome_of("ci_baseline_status", {"state": "red", "escalated": True}) == "red_escalated"
    assert _outcome_of("ci_baseline_status", {"state": "pending"}) == "pending"
    assert _outcome_of("ci_baseline_status", {}) == "unknown"
    assert _outcome_of("ci_baseline_status", {"state": "weird"}) == "unknown"


# -- the command -------------------------------------------------------------


async def test_green_head_reports_green_and_files_nothing(handler, git):
    git.acommit_check_runs.return_value = list(GREEN_RUNS)
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert result["success"] is True
    assert (result["state"], result["ref"], result["head_sha"]) == ("green", "main", SHA)
    assert result["failing_checks"] == [] and "dedup_key" not in result
    git.ajob_failed_tests.assert_not_awaited()


async def test_unreadable_checks_are_unknown_never_green(handler, git):
    git.acommit_check_runs.return_value = None
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert result["state"] == "unknown"
    assert "could not read" in result["error"]


async def test_pending_head_is_pending(handler, git):
    git.acommit_check_runs.return_value = [
        _run("Tests (default)", None, job_id=31, status="in_progress")
    ]
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert result["state"] == "pending"
    assert result["pending_checks"] == ["Tests (default)"]


async def test_red_head_derives_the_first_repair_attempt(handler, git):
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    signature = failure_signature(FAILED_TESTS, ["Tests (default)"])
    assert result["state"] == "red"
    assert result["failing_checks"] == ["Tests (default)"]
    assert result["failing_tests"] == FAILED_TESTS
    assert result["run_url"] == RED_RUNS[0]["html_url"]
    assert result["signature"] == signature
    assert result["attempt"] == 1 and result["escalated"] is False
    assert result["dedup_key"] == f"{REPAIR_KEY_PREFIX}:{signature}:1"
    assert result["escalation_key"] == f"{ESCALATION_KEY_PREFIX}:{signature}"
    assert result["title"].startswith("Fix red CI on main @ ")
    assert FAILED_TESTS[1] in result["description"]
    # Only the failed job's log is read; the green ones are not.
    git.ajob_failed_tests.assert_awaited_once()
    assert git.ajob_failed_tests.await_args.args[1] == 11


async def test_red_head_reuses_the_in_flight_attempt(handler, db, git):
    signature = failure_signature(FAILED_TESTS, ["Tests (default)"])
    live = await _attempt(db, f"{REPAIR_KEY_PREFIX}:{signature}:1", TaskStatus.IN_PROGRESS)
    git.acommit_head_sha.return_value = "f" * 40  # a newer commit, same failure

    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})

    assert result["dedup_key"] == f"{REPAIR_KEY_PREFIX}:{signature}:1"
    assert result["attempt"] == 1 and result["escalated"] is False
    assert result["prior_attempts"] == []
    assert live not in result["prior_attempts"]


async def test_a_blocked_attempt_is_spent_and_the_next_key_is_new(handler, db):
    signature = failure_signature(FAILED_TESTS, ["Tests (default)"])
    blocked = await _attempt(db, f"{REPAIR_KEY_PREFIX}:{signature}:1", TaskStatus.BLOCKED)

    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})

    assert result["dedup_key"] == f"{REPAIR_KEY_PREFIX}:{signature}:2"
    assert result["attempt"] == 2 and result["escalated"] is False
    assert result["prior_attempts"] == [blocked]


async def test_two_spent_attempts_escalate_instead_of_filing_a_third(handler, db):
    signature = failure_signature(FAILED_TESTS, ["Tests (default)"])
    first = await _attempt(db, f"{REPAIR_KEY_PREFIX}:{signature}:1", TaskStatus.COMPLETED)
    second = await _attempt(db, f"{REPAIR_KEY_PREFIX}:{signature}:2", TaskStatus.BLOCKED)

    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})

    assert result["state"] == "red" and result["escalated"] is True
    assert result["prior_attempts"] == [first, second]
    assert "2 repair attempt" in result["escalation_title"]
    assert first in result["escalation_question"] and second in result["escalation_question"]
    assert _outcome_of("ci_baseline_status", result) == "red_escalated"


async def test_max_attempts_is_honoured(handler, db):
    signature = failure_signature(FAILED_TESTS, ["Tests (default)"])
    await _attempt(db, f"{REPAIR_KEY_PREFIX}:{signature}:1", TaskStatus.BLOCKED)
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT, "max_attempts": 1})
    assert result["escalated"] is True
    assert (await handler._cmd_ci_baseline_status({"project_id": PROJECT, "max_attempts": 0}))[
        "error"
    ] == "max_attempts must be at least 1"


async def test_a_different_failure_does_not_share_attempts(handler, db, git):
    other = failure_signature(["tests/test_other.py::test_z"], ["Tests (default)"])
    await _attempt(db, f"{REPAIR_KEY_PREFIX}:{other}:1", TaskStatus.BLOCKED)
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert result["attempt"] == 1 and result["prior_attempts"] == []


async def test_unreadable_logs_still_key_by_failing_checks(handler, git):
    git.ajob_failed_tests.return_value = None
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert result["failing_tests"] == []
    assert result["signature"] == failure_signature([], ["Tests (default)"])
    assert "No pytest node ids" in result["description"]


async def test_ref_override_and_bad_project(handler, git, db):
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT, "ref": "release"})
    assert result["ref"] == "release"
    assert git.acommit_head_sha.await_args.args[1] == "release"
    assert (await handler._cmd_ci_baseline_status({}))["error"] == "project_id is required"
    assert "unknown project" in (await handler._cmd_ci_baseline_status({"project_id": "nope"}))["error"]
    await db.create_project(Project(id="local", name="Local", repo_url="/srv/repo.git"))
    local = await handler._cmd_ci_baseline_status({"project_id": "local"})
    assert local["state"] == "unknown" and "repo_url" in local["error"]


async def test_dedup_prefix_query_escapes_like_wildcards(db):
    await _attempt(db, "ci-baseline:abc:1", TaskStatus.READY)
    await _attempt(db, "ci-baseline:abcX:1", TaskStatus.READY)
    await _attempt(db, "ci-baselineXabc:1", TaskStatus.READY)
    rows = await db.list_tasks_by_dedup_prefix(PROJECT, "ci-baseline:abc:")
    assert [row.dedup_key for row in rows] == ["ci-baseline:abc:1"]
    assert await db.list_tasks_by_dedup_prefix(PROJECT, "ci_baseline:abc:") == []
