"""Nightly issue scan and review-gated fix policy against a real task database."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.commands.github_issue_commands import FIX_KEY, INVESTIGATION_KEY, with_fix_closing_line
from src.event_bus import EventBus
from src.git.github import GitHubClient
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.github_contracts import GitHubAccessError
from src.intelligence_classes import load_intelligence_classes
from src.models import AgentProfile, Project, Task, TaskStatus
from src.vault import ensure_default_intelligence_classes


class Issues:
    def __init__(self, count: int = 7) -> None:
        self.open = list(range(1, count + 1))
        self.labels: set[int] = set()
        self.fail_label: int | None = None
        self.closures: list[tuple[int, str, str]] = []

    async def list_open_issues_without_label(self, label: str):
        assert label == "aq-triaged"
        return [
            {"number": number, "title": f"Problem {number}"}
            for number in self.open if number not in self.labels
        ]

    async def add_issue_label(self, number: int, label: str):
        assert label == "aq-triaged"
        if number == self.fail_label:
            raise ValueError("label write failed")
        self.labels.add(number)

    async def close_issue_with_reason(self, number: int, reason: str, marker: str):
        self.closures.append((number, reason, marker))


def test_fix_pr_body_gets_one_exact_closing_line():
    key = f"{FIX_KEY}42:rev-sample"
    body = with_fix_closing_line("Why this change matters", key)
    assert body.endswith("\nFixes #42\n")
    assert with_fix_closing_line(body, key) == body
    assert with_fix_closing_line("Another PR", None) == "Another PR"


@pytest.fixture
async def env(command_handler_factory, monkeypatch):
    handler = await command_handler_factory()
    db = handler.db
    handler.orchestrator.bus = EventBus(env="dev")
    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes = load_intelligence_classes(handler.config.data_dir)
    await db.create_project(Project(
        id="agent-queue", name="Agent Queue",
        repo_url="https://github.com/ElectricJack/agent-queue.git",
    ))
    await db.create_profile(AgentProfile(
        id="worker", name="Worker", harness="codex", lifecycle="pool",
        default_class="standard-high", aq_commands=["review_submit"],
        harness_tools=[], plugin_tools=[],
    ))
    await db.update_project("agent-queue", default_profile_id="worker")
    issues = Issues()
    monkeypatch.setattr(handler, "_github_issue_client", AsyncMock(return_value=issues))
    try:
        yield handler, db, issues
    finally:
        await db.close()


async def test_issue_listing_excludes_labelled_issues_and_pull_requests_oldest_first():
    class Page:
        repository = GitHubRepositoryBinding(44, "ElectricJack/agent-queue")

        async def paged_list(self, path, *, max_pages):
            assert "state=open&sort=created&direction=asc" in path
            assert max_pages == 50
            return [
                {"number": 3, "state": "open", "created_at": "2026-01-03", "labels": []},
                {"number": 1, "state": "open", "created_at": "2026-01-01", "labels": []},
                {"number": 2, "state": "open", "created_at": "2026-01-02", "labels": [{"name": "aq-triaged"}]},
                {"number": 4, "state": "open", "created_at": "2026-01-04", "labels": [],
                 "pull_request": {}},
            ]

    rows = await GitHubClient.list_open_issues_without_label(Page(), "aq-triaged")
    assert [row["number"] for row in rows] == [1, 3]


async def test_uncertain_label_write_is_reconciled_by_read():
    class Remote:
        repository = GitHubRepositoryBinding(44, "ElectricJack/agent-queue")

        async def request_json(self, method, path, **kwargs):
            assert method == "POST"
            assert path.endswith("/issues/8/labels")
            raise GitHubAccessError("transient", "write outcome unknown")

        async def issue(self, number):
            return {"number": number, "labels": [{"name": "aq-triaged"}]}

    await GitHubClient.add_issue_label(Remote(), 8, "aq-triaged")


async def test_uncertain_closing_comment_does_not_post_twice():
    class Remote:
        repository = GitHubRepositoryBinding(44, "ElectricJack/agent-queue")

        def __init__(self):
            self.comments = []
            self.closed = False
            self.posts = 0

        async def issue(self, number):
            return {"number": number, "state": "closed" if self.closed else "open"}

        async def paged_list(self, path, *, max_pages):
            return list(self.comments)

        async def request_json(self, method, path, **kwargs):
            if method == "POST":
                self.posts += 1
                self.comments.append({"body": kwargs["json_body"]["body"]})
                raise GitHubAccessError("transient", "comment outcome unknown")
            assert method == "PATCH"
            self.closed = True
            return {"state": "closed"}

    remote = Remote()
    await GitHubClient.close_issue_with_reason(
        remote, 8, "Please close this issue because it is intended.", "aq-close:review"
    )
    await GitHubClient.close_issue_with_reason(
        remote, 8, "Please close this issue because it is intended.", "aq-close:review"
    )
    assert remote.posts == 1
    assert remote.closed is True


async def test_nightly_cap_and_replay_do_not_file_a_sixth_task(env):
    handler, db, issues = env
    first = await handler.execute("github_issue_triage", {"project_id": "agent-queue"})
    assert first["success"] is True, first
    assert first["filed"] == [1, 2, 3, 4, 5]
    assert issues.labels == {1, 2, 3, 4, 5}
    second = await handler.execute("github_issue_triage", {"project_id": "agent-queue"})
    assert second["filed"] == []
    assert second["remaining_capacity"] == 0
    assert len(await db.list_tasks_by_dedup_prefix("agent-queue", INVESTIGATION_KEY)) == 5


async def test_label_failure_leaves_task_for_recovery_and_still_respects_cap(env):
    handler, db, issues = env
    issues.fail_label = 2
    failed = await handler.execute("github_issue_triage", {"project_id": "agent-queue"})
    assert failed["success"] is False
    assert issues.labels == {1}
    assert len(await db.list_tasks_by_dedup_prefix("agent-queue", INVESTIGATION_KEY)) == 2
    issues.fail_label = None
    recovered = await handler.execute("github_issue_triage", {"project_id": "agent-queue"})
    assert recovered["recovered"] == [2]
    assert recovered["filed"] == [3, 4, 5]
    assert issues.labels == {1, 2, 3, 4, 5}
    assert len(await db.list_tasks_by_dedup_prefix("agent-queue", INVESTIGATION_KEY)) == 5


async def test_archived_investigation_still_counts_and_prevents_refiling(env):
    handler, db, issues = env
    first = await handler.execute("github_issue_triage", {"project_id": "agent-queue"})
    task = await db.find_task_by_dedup_key("agent-queue", f"{INVESTIGATION_KEY}1")
    await db.update_task(task.id, status=TaskStatus.COMPLETED)
    assert await db.archive_task(task.id)
    archived = await db.get_archived_task(task.id)
    assert archived["dedup_key"] == f"{INVESTIGATION_KEY}1"
    issues.labels.remove(1)
    again = await handler.execute("github_issue_triage", {"project_id": "agent-queue"})
    assert again["filed"] == []
    assert again["recovered"] == [1]
    assert again["remaining_capacity"] == 0
    assert first["filed"] == [1, 2, 3, 4, 5]


async def _submit_report(handler, db, number: int) -> str:
    await db.create_task(Task(
        id=f"issue-investigation-{number}", project_id="agent-queue",
        title=f"Investigate #{number}", description="Research only",
        status=TaskStatus.IN_PROGRESS,
        dedup_key=f"{INVESTIGATION_KEY}{number}",
    ))
    result = await handler.execute("review_submit", {
        "project_id": "agent-queue", "task_id": f"issue-investigation-{number}",
        "kind": "other", "title": f"GitHub issue #{number} investigation",
        "content": f"# Issue #{number}\n\nCause: src/example.py:8. Fix: change the guard.\n",
    })
    assert result["success"] is True
    return result["review_id"]


async def test_approval_files_one_fix_with_issue_closing_pr_instruction(env):
    handler, db, _ = env
    review_id = await _submit_report(handler, db, 11)
    decision = await handler.execute("review_decide", {
        "review_id": review_id, "revision": 1, "decision": "approve",
    })
    assert decision["state"] == "approved"
    args = {"project_id": "agent-queue", "review_id": review_id, "revision": 1}
    first = await handler.execute("github_issue_fix_approved", args)
    second = await handler.execute("github_issue_fix_approved", args)
    assert first["outcome"] == "created"
    assert second == {"success": True, "outcome": "reused", "task_id": first["task_id"]}
    task = await db.get_task(first["task_id"])
    assert "Fixes #11" in task.description
    assert "src/example.py:8" in task.description


async def test_approval_works_after_investigation_is_archived(env):
    handler, db, _ = env
    review_id = await _submit_report(handler, db, 17)
    await db.update_task("issue-investigation-17", status=TaskStatus.COMPLETED)
    assert await db.archive_task("issue-investigation-17")
    await handler.execute("review_decide", {
        "review_id": review_id, "revision": 1, "decision": "approve",
    })
    result = await handler.execute("github_issue_fix_approved", {
        "project_id": "agent-queue", "review_id": review_id, "revision": 1,
    })
    assert result["outcome"] == "created"


async def test_unrelated_approved_review_is_ignored(env):
    handler, db, _ = env
    await db.create_task(Task(
        id="other-research", project_id="agent-queue", title="Other report",
        description="No GitHub issue", status=TaskStatus.IN_PROGRESS,
    ))
    submitted = await handler.execute("review_submit", {
        "project_id": "agent-queue", "task_id": "other-research", "kind": "other",
        "title": "Other report", "content": "This report is unrelated to issue triage.",
    })
    assert submitted["success"] is True
    await handler.execute("review_decide", {
        "review_id": submitted["review_id"], "revision": 1, "decision": "approve",
    })
    result = await handler.execute("github_issue_fix_approved", {
        "project_id": "agent-queue", "review_id": submitted["review_id"], "revision": 1,
    })
    assert result == {"success": True, "outcome": "ignored"}


async def test_request_changes_and_reject_without_close_keep_issue_open(env, monkeypatch):
    handler, db, issues = env
    changed_id = await _submit_report(handler, db, 12)
    changed = await handler.execute("review_decide", {
        "review_id": changed_id, "revision": 1,
        "decision": "request_changes", "note": "Add a test plan.",
    })
    assert changed["state"] == "changes_requested"
    assert await db.find_task_by_dedup_key("agent-queue", f"review-revision:{changed_id}:1")

    rejected_id = await _submit_report(handler, db, 13)
    rejected = await handler.execute("review_decide", {
        "review_id": rejected_id, "revision": 1,
        "decision": "reject", "note": "Use the smaller guard change instead.",
    })
    assert rejected["state"] == "rejected"
    response_task = await db.find_task_by_dedup_key(
        "agent-queue", f"review-revision:{rejected_id}:1"
    )
    assert response_task and "Do not close by default" in response_task.description
    ignored = await handler.execute("github_issue_rejection", {
        "project_id": "agent-queue", "review_id": rejected_id, "revision": 1,
    })
    assert ignored["outcome"] == "ignored"
    monkeypatch.setattr(handler, "_scoped_held_task_id", AsyncMock(return_value=response_task.id))
    refused = await handler.execute("github_issue_close_rejected", {"review_id": rejected_id})
    assert refused["success"] is False
    assert issues.closures == []
    revised = await handler.execute("review_submit", {
        "review_id": rejected_id,
        "content": "# Revised approach\n\nUse the smaller guard change.\n",
        "changes": "Addressed Jack's alternative approach.",
    })
    assert revised["revision"] == 2
    assert (await db.get_review(rejected_id))["state"] == "in_review"


async def test_reject_with_explicit_close_posts_jacks_reason(env, monkeypatch):
    handler, db, issues = env
    review_id = await _submit_report(handler, db, 14)
    rejected = await handler.execute("review_decide", {
        "review_id": review_id, "revision": 1,
        "decision": "reject", "note": "Please close this issue because the behavior is intended.",
    })
    assert rejected["state"] == "rejected"
    response_task = await db.find_task_by_dedup_key(
        "agent-queue", f"review-revision:{review_id}:1"
    )
    monkeypatch.setattr(handler, "_scoped_held_task_id", AsyncMock(return_value=response_task.id))
    result = await handler.execute("github_issue_close_rejected", {"review_id": review_id})
    assert result == {"success": True, "outcome": "closed", "number": 14}
    assert issues.closures == [
        (14, "Please close this issue because the behavior is intended.",
         f"aq-issue-close:{review_id}")
    ]


async def test_reject_saying_not_to_close_cannot_close_issue(env, monkeypatch):
    handler, db, issues = env
    review_id = await _submit_report(handler, db, 15)
    await handler.execute("review_decide", {
        "review_id": review_id, "revision": 1, "decision": "reject",
        "note": "Do not close this issue; revise the proposed fix instead.",
    })
    commented = await handler.execute("review_comment", {
        "review_id": review_id, "revision": 1, "body": "Please close this issue.",
    })
    assert commented["success"] is True
    response_task = await db.find_task_by_dedup_key(
        "agent-queue", f"review-revision:{review_id}:1"
    )
    monkeypatch.setattr(handler, "_scoped_held_task_id", AsyncMock(return_value=response_task.id))
    result = await handler.execute("github_issue_close_rejected", {"review_id": review_id})
    assert result["success"] is False
    automatic = await handler.execute("github_issue_rejection", {
        "project_id": "agent-queue", "review_id": review_id, "revision": 1,
    })
    assert automatic["outcome"] == "ignored"
    assert issues.closures == []


async def test_rejection_event_closes_only_on_jacks_explicit_current_decision(env):
    handler, db, issues = env
    review_id = await _submit_report(handler, db, 16)
    await handler.execute("review_decide", {
        "review_id": review_id, "revision": 1, "decision": "reject",
        "note": "Please close this issue because it is a duplicate.",
    })
    stale = await handler.execute("github_issue_rejection", {
        "project_id": "agent-queue", "review_id": review_id, "revision": 2,
    })
    assert stale["outcome"] == "ignored"
    result = await handler.execute("github_issue_rejection", {
        "project_id": "agent-queue", "review_id": review_id, "revision": 1,
    })
    assert result == {"success": True, "outcome": "closed", "number": 16}
    assert issues.closures == [
        (16, "Please close this issue because it is a duplicate.", f"aq-issue-close:{review_id}")
    ]
