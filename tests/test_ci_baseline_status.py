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
    REPAIR_RECORD_META,
    RepairAttempt,
    failure_signature,
    plan_repair,
    render_repair_task,
)
from src.commands.contracts.builtin import _outcome_of
from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitManager
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

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
    _run("Lint", "success", job_id=13),
]
GREEN_RUNS = [_run("Tests (default)", "success", job_id=21)]
FAILED_TESTS = ["tests/test_a.py::test_one", "tests/test_b.py::TestX::test_two"]


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("ci.db"))
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
        database=DatabaseConfig(url=lease_dsn("test.db")),
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


def test_an_unreadable_verdict_is_unknown_even_though_it_carries_an_error():
    # The command explains an ``unknown`` in ``error`` while still succeeding;
    # that is the next tick's problem, not a broken step.
    for error in (
        "could not read check runs for example/widgets@main",
        f"project {PROJECT} has no GitHub repo_url to read CI from",
    ):
        raw = {"success": True, "state": "unknown", "ref": "main", "error": error}
        assert _outcome_of("ci_baseline_status", raw) == "unknown"


def test_a_refusal_or_an_escaped_exception_is_still_rejected():
    refusal = {"success": False, "error": "unknown project: nope"}
    assert _outcome_of("ci_baseline_status", refusal) == "rejected"
    # ``CommandHandler.execute``'s exception path carries no ``success`` key.
    assert _outcome_of("ci_baseline_status", {"error": "gh exploded"}) == "rejected"


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


async def test_the_contract_adapter_reports_unreadable_checks_as_unknown(handler, git):
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import CiBaselineStatusArgs, set_handler_provider

    git.acommit_check_runs.return_value = None
    set_handler_provider(lambda: handler)
    try:
        registration = CONTRACTS.get("ci_baseline_status")
        result = await registration.invoke(CiBaselineStatusArgs(project_id=PROJECT), None)
    finally:
        set_handler_provider(None)
    assert result.outcome == "unknown"
    assert (result.value.state, result.value.ref) == ("unknown", "main")
    assert "could not read check runs" in result.summary


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


# -- repair ownership: manual repairs, shrinking and growing failures ---------

CHECKS = ["Tests (default)"]
THIRD = "tests/test_c.py::test_three"
ALL_TESTS = sorted([*FAILED_TESTS, THIRD])


def _key(tests: list[str], n: int = 1) -> str:
    return f"{REPAIR_KEY_PREFIX}:{failure_signature(tests, CHECKS)}:{n}"


async def _adopt(handler, task_id: str, tests: list[str], **extra) -> dict:
    """What the sentinel's record step does right after ``ensure_task``."""
    return await handler._cmd_ci_repair_adopt(
        {
            "project_id": PROJECT,
            "task_id": task_id,
            "failing_tests": list(tests),
            "failing_checks": CHECKS,
            **extra,
        }
    )


async def _recorded(handler, db, tests: list[str], status: TaskStatus = TaskStatus.IN_PROGRESS) -> str:
    """A sentinel repair keyed for *tests* whose record step ran, left in *status*."""
    task_id = await _attempt(db, _key(tests), TaskStatus.IN_PROGRESS)
    assert (await _adopt(handler, task_id, tests))["outcome"] == "recorded"
    if status is not TaskStatus.IN_PROGRESS:
        await db.update_task(task_id, status=status)
    return task_id


async def test_a_manual_repair_is_adopted_and_then_reused_by_the_sentinel(handler, db, git):
    await db.create_task(
        Task(
            id="steady-quest",
            project_id=PROJECT,
            title="Fix main by hand",
            description="",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    # Unadopted, a hand-filed repair is invisible and the sentinel would file a second.
    before = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert before["dedup_key"] == _key(FAILED_TESTS) and before["in_flight"] == []

    reads = git.ajob_failed_tests.await_count
    adopted = await handler._cmd_ci_repair_adopt(
        {"project_id": PROJECT, "task_id": "steady-quest"}
    )

    # With no failure named, adoption reads CI itself and owns the whole failure.
    assert git.ajob_failed_tests.await_count == reads + 1
    assert adopted["success"] is True and adopted["outcome"] == "adopted"
    assert _outcome_of("ci_repair_adopt", adopted) == "adopted"
    assert adopted["dedup_key"] == _key(FAILED_TESTS)
    assert (adopted["ref"], adopted["head_sha"]) == ("main", SHA)
    assert adopted["failing_tests"] == FAILED_TESTS and adopted["failing_checks"] == CHECKS
    assert (await db.get_task("steady-quest")).dedup_key == _key(FAILED_TESTS)
    record = await db.get_task_meta("steady-quest", REPAIR_RECORD_META)
    assert record["failing_tests"] == FAILED_TESTS and record["ref"] == "main"

    after = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert after["dedup_key"] == adopted["dedup_key"]
    assert after["in_flight"] == ["steady-quest"]
    assert after["attempt"] == 1 and after["escalated"] is False

    # The record is written once; adopting again reads nothing and changes nothing.
    reads = git.ajob_failed_tests.await_count
    again = await handler._cmd_ci_repair_adopt({"project_id": PROJECT, "task_id": "steady-quest"})
    assert again["outcome"] == "unchanged" and again["dedup_key"] == adopted["dedup_key"]
    assert _outcome_of("ci_repair_adopt", again) == "unchanged"
    assert git.ajob_failed_tests.await_count == reads


async def test_a_partial_fix_that_shrinks_the_failing_set_reuses_the_in_flight_repair(
    handler, db, git
):
    legacy = await _attempt(db, _key(ALL_TESTS), TaskStatus.IN_PROGRESS)
    git.acommit_head_sha.return_value = "e" * 40
    git.ajob_failed_tests.return_value = list(FAILED_TESTS)

    # Keyed by signature alone, the smaller remaining set looks like a new failure.
    unrecorded = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert unrecorded["dedup_key"] == _key(FAILED_TESTS)

    assert (await _adopt(handler, legacy, ALL_TESTS))["outcome"] == "recorded"
    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})

    assert result["signature"] == failure_signature(FAILED_TESTS, CHECKS)
    assert result["dedup_key"] == _key(ALL_TESTS)
    assert result["in_flight"] == [legacy]
    assert result["attempt"] == 1 and result["escalated"] is False
    assert _outcome_of("ci_baseline_status", result) == "red"


async def test_a_new_failure_beside_an_in_flight_repair_gets_a_repair_for_just_that_failure(
    handler, db, git
):
    owner = await _recorded(handler, db, FAILED_TESTS)
    git.ajob_failed_tests.return_value = list(ALL_TESTS)

    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})

    assert result["signature"] == failure_signature(ALL_TESTS, CHECKS)
    assert result["in_flight"] == [owner]
    assert result["repair_tests"] == [THIRD] and result["repair_checks"] == CHECKS
    assert result["repair_signature"] == failure_signature([THIRD], CHECKS)
    assert result["dedup_key"] == _key([THIRD])
    assert result["escalation_key"] == f"{ESCALATION_KEY_PREFIX}:{result['repair_signature']}"
    assert result["attempt"] == 1 and result["escalated"] is False
    assert THIRD in result["description"] and FAILED_TESTS[0] not in result["description"]
    assert owner in result["description"]

    # Once that repair is filed and recorded, the two together own the failure.
    second = await _attempt(db, result["dedup_key"], TaskStatus.READY)
    recorded = await _adopt(handler, second, result["repair_tests"])
    assert recorded["outcome"] == "recorded" and recorded["in_flight"] == []
    settled = await handler._cmd_ci_baseline_status({"project_id": PROJECT})
    assert settled["in_flight"] == [owner, second]
    assert settled["dedup_key"] == _key(FAILED_TESTS)


async def test_spent_repairs_that_owned_a_shrunken_failure_still_escalate_it(handler, db, git):
    first = await _recorded(handler, db, ALL_TESTS, TaskStatus.COMPLETED)
    second = await _recorded(handler, db, FAILED_TESTS, TaskStatus.BLOCKED)
    await _recorded(handler, db, [THIRD], TaskStatus.COMPLETED)  # never owned what is red now
    git.ajob_failed_tests.return_value = FAILED_TESTS[:1]

    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})

    assert result["in_flight"] == []
    assert result["prior_attempts"] == [first, second]
    assert result["escalated"] is True
    assert _outcome_of("ci_baseline_status", result) == "red_escalated"
    signature = failure_signature(FAILED_TESTS[:1], CHECKS)
    assert result["escalation_key"] == f"{ESCALATION_KEY_PREFIX}:{signature}"
    assert first in result["escalation_question"] and second in result["escalation_question"]

    # Below the budget it is the next attempt, keyed by the failure it owns.
    below = await handler._cmd_ci_baseline_status({"project_id": PROJECT, "max_attempts": 3})
    assert below["escalated"] is False and below["attempt"] == 3
    assert below["dedup_key"] == _key(FAILED_TESTS[:1])


async def test_unreadable_logs_do_not_duplicate_a_recorded_repair(handler, db, git):
    owner = await _recorded(handler, db, FAILED_TESTS)
    git.ajob_failed_tests.return_value = None

    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})

    assert result["failing_tests"] == []
    assert result["dedup_key"] == _key(FAILED_TESTS) and result["in_flight"] == [owner]


async def test_a_recorded_repair_owns_its_failure_only_on_its_own_ref(handler, db):
    task_id = await _attempt(db, _key(ALL_TESTS), TaskStatus.IN_PROGRESS)
    assert (await _adopt(handler, task_id, ALL_TESTS, ref="release"))["outcome"] == "recorded"

    result = await handler._cmd_ci_baseline_status({"project_id": PROJECT})

    assert result["in_flight"] == [] and result["dedup_key"] == _key(FAILED_TESTS)


async def test_adoption_refuses_a_task_it_cannot_make_the_repair(handler, db, git):
    blocked = await _attempt(db, _key(FAILED_TESTS), TaskStatus.BLOCKED)
    refused = await _adopt(handler, blocked, FAILED_TESTS)
    assert "only a live task" in refused["error"]
    assert _outcome_of("ci_repair_adopt", refused) == "rejected"

    await db.create_task(
        Task(
            id="review-1",
            project_id=PROJECT,
            title="review",
            description="",
            status=TaskStatus.READY,
            dedup_key="review:task:abc",
        )
    )
    foreign = await _adopt(handler, "review-1", FAILED_TESTS)
    assert "not a CI repair key" in foreign["error"]
    assert (await db.get_task("review-1")).dedup_key == "review:task:abc"

    await db.create_project(Project(id="other", name="Other", repo_url=REPO))
    elsewhere = await handler._cmd_ci_repair_adopt({"project_id": "other", "task_id": "review-1"})
    assert "not found in other" in elsewhere["error"]

    await db.create_task(
        Task(id="manual", project_id=PROJECT, title="m", description="", status=TaskStatus.READY)
    )
    git.acommit_check_runs.return_value = list(GREEN_RUNS)
    green = await handler._cmd_ci_repair_adopt({"project_id": PROJECT, "task_id": "manual"})
    assert "is green" in green["error"]
    assert (await db.get_task("manual")).dedup_key is None
    assert await db.get_task_meta("manual", REPAIR_RECORD_META) is None

    bad = await handler._cmd_ci_repair_adopt(
        {"project_id": PROJECT, "task_id": "manual", "failing_tests": "tests/test_a.py"}
    )
    assert "lists of strings" in bad["error"]
    assert "required" in (await handler._cmd_ci_repair_adopt({"project_id": PROJECT}))["error"]


def test_an_unrecorded_repair_keyed_on_exactly_what_is_left_owns_it():
    recorded = RepairAttempt(
        "owner",
        _key(FAILED_TESTS),
        live=True,
        record={"ref": "main", "failing_tests": FAILED_TESTS, "failing_checks": CHECKS},
    )
    legacy = RepairAttempt("legacy", _key([THIRD]), live=True)

    plan = plan_repair(
        ref="main", failing_tests=ALL_TESTS, failing_checks=CHECKS, attempts=[recorded, legacy]
    )

    assert plan.in_flight == ("owner", "legacy")
    assert plan.reuse_key == legacy.dedup_key
    assert plan.tests == (THIRD,) and plan.prior_attempts == ()


def test_a_new_repair_key_never_reuses_an_index_of_its_signature():
    spent = [RepairAttempt(f"t{n}", _key([THIRD], n), live=False) for n in (1, 3)]
    plan = plan_repair(ref="main", failing_tests=[THIRD], failing_checks=CHECKS, attempts=spent)
    assert plan.next_key == _key([THIRD], 4) and plan.prior_attempts == ("t1", "t3")


async def test_task_meta_bulk_reads_one_key_for_a_set_of_tasks(db):
    first = await _attempt(db, "ci-baseline:abc:1", TaskStatus.READY)
    second = await _attempt(db, "ci-baseline:abc:2", TaskStatus.READY)
    await db.set_task_meta(first, REPAIR_RECORD_META, {"ref": "main"})
    await db.set_task_meta(second, "other", "x")
    assert await db.get_task_meta_bulk([first, second], REPAIR_RECORD_META) == {
        first: {"ref": "main"}
    }
    assert await db.get_task_meta_bulk([], REPAIR_RECORD_META) == {}
