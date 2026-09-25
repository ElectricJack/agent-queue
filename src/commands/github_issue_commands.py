"""Repository-bound GitHub issue triage and reviewed fix commands."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from src.git.github_contracts import GitHubAccessError
from src.projects.github import GitHubError, parse_github_repository

PROJECT_ID = "agent-queue"
REPOSITORY = "ElectricJack/agent-queue"
TRIAGED_LABEL = "aq-triaged"
INVESTIGATION_KEY = f"github-issue:{REPOSITORY}:"
FIX_KEY = f"github-issue-fix:{REPOSITORY}:"
DAILY_CAP = 5
_CLOSE_REQUEST = re.compile(
    r"(?im)^\s*(?:please\s+)?close\s+(?:(?:this|the)\s+)?(?:github\s+)?issue\b"
    r"|^\s*(?:please\s+)?close\s+it\b"
)
_KEEP_OPEN = re.compile(r"\b(?:do\s+not|don't|never)\s+close\b|\bkeep\s+(?:it|the\s+issue)\s+open\b", re.I)


def investigation_description(number: int, title: str) -> str:
    url = f"https://github.com/{REPOSITORY}/issues/{number}"
    return (
        f"Investigate GitHub issue #{number}: {title}\n\n"
        f"Issue: {url}\n\n"
        "Research only; do not change code or open a PR for this task. Read the issue and "
        "relevant code; reproduce it when cheap. Write a markdown report with: summary, "
        "likely cause with file:line evidence, proposed fix (approach, files, scope, risks "
        "and test plan), alternatives, and recommendation (fix, needs more info, or close). "
        f"Submit the report to the Reviews tab with `aq review submit --task-id <this-task-id> "
        f"--kind other --title 'GitHub issue #{number} investigation' --file <report.md>`. "
        "The report must link this issue. Close this research task with the no-op work "
        "outcome so it does not produce a code PR.\n\n"
        "If Jack requests changes, address every comment and resubmit. A rejection is "
        "feedback unless Jack explicitly asks to close the issue: follow his alternative "
        "fix approach or ask one clarifying question in a revised report. Only use "
        "`aq github-issue close-rejected` if Jack explicitly requested closure."
    )


def fix_description(number: int, report: str, review_id: str) -> str:
    return (
        f"Implement the approved fix for https://github.com/{REPOSITORY}/issues/{number}.\n"
        f"Approved document review: {review_id}. Follow its proposed approach and test plan.\n"
        f"The PR body must include the exact line `Fixes #{number}` so GitHub closes "
        "the issue when the integration train lands the PR.\n\n"
        "## Approved investigation\n\n" + report
    )


def with_fix_closing_line(body: str, dedup_key: str | None) -> str:
    """Ensure a fix task's PR body has its exact GitHub closing keyword."""
    if not isinstance(dedup_key, str) or not dedup_key.startswith(FIX_KEY):
        return body
    number = dedup_key.removeprefix(FIX_KEY).split(":", 1)[0]
    if not number.isdecimal():
        return body
    closing_line = f"Fixes #{number}"
    if re.search(rf"(?m)^{re.escape(closing_line)}\s*$", body):
        return body
    return f"{body.rstrip()}\n\n{closing_line}\n"


class GitHubIssueCommandsMixin:
    """Three commands used by the nightly and review decision playbook rules."""

    def _github_issue_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_issue_triage_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._issue_triage_lock = lock
        return lock

    async def _github_issue_client(self, project_id: str):
        if project_id != PROJECT_ID:
            raise ValueError("GitHub issue triage is scoped to project agent-queue")
        project = await self.db.get_project(project_id)
        if project is None or not project.repo_url:
            raise ValueError("agent-queue has no bound GitHub repository")
        try:
            parsed = parse_github_repository(project.repo_url)
        except GitHubError as exc:
            raise ValueError("agent-queue has no valid GitHub repository") from exc
        if parsed.full_name != REPOSITORY:
            raise ValueError("GitHub issue triage is bound to ElectricJack/agent-queue")
        binding = await self.orchestrator.git.bind_github_repository(project.repo_url)
        if binding.full_name != REPOSITORY:
            raise ValueError("GitHub repository binding changed")
        factory = self.orchestrator.github_client_factory
        if factory is None:
            raise ValueError("GitHub client is unavailable")
        return factory(binding)

    async def _issue_task(self, key: str):
        # A replay must never file a second task after the first was archived.
        rows = await self.db.list_tasks_by_dedup_prefix(PROJECT_ID, key)
        live = next((row for row in rows if row.dedup_key == key), None)
        if live is not None:
            return live.id
        archived = await self.db.list_archived_tasks_by_dedup_prefix(PROJECT_ID, key)
        match = next((row for row in archived if row["dedup_key"] == key), None)
        return match["id"] if match else None

    async def _cmd_github_issue_triage(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        try:
            client = await self._github_issue_client(project_id)
            async with self._github_issue_lock():
                issues = await client.list_open_issues_without_label(TRIAGED_LABEL)
                live_tasks = await self.db.list_tasks_by_dedup_prefix(
                    PROJECT_ID, INVESTIGATION_KEY
                )
                archived_tasks = await self.db.list_archived_tasks_by_dedup_prefix(
                    PROJECT_ID, INVESTIGATION_KEY
                )
                today = dt.datetime.now().astimezone().date()
                created_today = sum(
                    dt.datetime.fromtimestamp(task.created_at).astimezone().date() == today
                    for task in live_tasks
                ) + sum(
                    dt.datetime.fromtimestamp(task["created_at"]).astimezone().date() == today
                    for task in archived_tasks
                )
                existing_keys = {task.dedup_key for task in live_tasks}
                existing_keys.update(task["dedup_key"] for task in archived_tasks)
                filed: list[int] = []
                recovered: list[int] = []
                for issue in issues:
                    number = issue.get("number")
                    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                        raise ValueError("GitHub returned an issue without a valid number")
                    key = f"{INVESTIGATION_KEY}{number}"
                    if key not in existing_keys:
                        if created_today >= DAILY_CAP:
                            continue
                        created = await self._cmd_ensure_task({
                            "project_id": PROJECT_ID,
                            "dedup_key": key,
                            "title": f"Investigate GitHub issue #{number}: {issue.get('title', '')}",
                            "description": investigation_description(number, str(issue.get("title", ""))),
                            "intelligence_class": "standard-high",
                            "root": True,
                        })
                        if not created.get("success"):
                            return {"success": False, "error": created.get("error", "task filing failed")}
                        created_today += int(bool(created.get("created")))
                        filed.append(number)
                    else:
                        recovered.append(number)
                    # The durable task is already present. If this remote write
                    # fails, tomorrow's scan finds the unlabelled issue again.
                    await client.add_issue_label(number, TRIAGED_LABEL)
                return {"success": True, "filed": filed, "recovered": recovered,
                        "remaining_capacity": max(0, DAILY_CAP - created_today)}
        except (GitHubAccessError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    async def _issue_review(self, review_id: str):
        review = await self.db.get_review(review_id)
        if not review or review["project_id"] != PROJECT_ID or review["kind"] != "other":
            raise ValueError("review is not an agent-queue issue investigation")
        author_id = review.get("author_task_id")
        task = await self.db.get_task(author_id) if author_id else None
        archived = await self.db.get_archived_task(author_id) if author_id and task is None else None
        key = task.dedup_key if task else archived.get("dedup_key") if archived else None
        if not isinstance(key, str) or not key.startswith(INVESTIGATION_KEY):
            raise ValueError("review has no GitHub issue investigation task")
        suffix = key.removeprefix(INVESTIGATION_KEY)
        if not suffix.isdecimal() or int(suffix) < 1:
            raise ValueError("review has an invalid GitHub issue number")
        return review, int(suffix)

    async def _cmd_github_issue_fix_approved(self, args: dict) -> dict:
        try:
            if args.get("project_id") != PROJECT_ID:
                raise ValueError("project_id must be agent-queue")
            try:
                review, number = await self._issue_review(str(args.get("review_id") or ""))
            except ValueError:
                return {"success": True, "outcome": "ignored"}
            if review["state"] != "approved" or review["current_revision"] != args.get("revision"):
                return {"success": True, "outcome": "ignored"}
            key = f"{FIX_KEY}{number}:{review['id']}"
            async with self._github_issue_lock():
                existing_id = await self._issue_task(key)
                if existing_id:
                    return {"success": True, "outcome": "reused", "task_id": existing_id}
                revision = await self.db.get_review_revision(review["id"], review["current_revision"])
                if revision is None:
                    raise ValueError("approved review revision is missing")
                created = await self._cmd_ensure_task({
                    "project_id": PROJECT_ID,
                    "dedup_key": key,
                    "title": f"Fix GitHub issue #{number}",
                    "description": fix_description(number, revision["content"], review["id"]),
                    "intelligence_class": "standard-high",
                    "root": True,
                })
                if not created.get("success"):
                    return {"success": False, "error": created.get("error", "fix task filing failed")}
                return {"success": True, "outcome": "created", "task_id": created["task_id"]}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    async def _explicit_close_reason(self, review: dict) -> str | None:
        if review["state"] != "rejected" or review.get("decided_by") != "human:local-operator":
            return None
        reasons = [str(review.get("decision_note") or "")]
        reasons.extend(
            str(item.get("body") or "")
            for item in await self.db.list_review_comments(review["id"])
            if item.get("author") == "human:local-operator"
            and item.get("revision") == review["current_revision"]
        )
        if any(_KEEP_OPEN.search(text) for text in reasons):
            return None
        return next(
            (text.strip() for text in reasons
             if _CLOSE_REQUEST.search(text)),
            None,
        )

    async def _cmd_github_issue_rejection(self, args: dict) -> dict:
        """Apply Jack's explicit close request after a Reject decision event."""
        try:
            if args.get("project_id") != PROJECT_ID:
                raise ValueError("project_id must be agent-queue")
            try:
                review, number = await self._issue_review(str(args.get("review_id") or ""))
            except ValueError:
                return {"success": True, "outcome": "ignored"}
            if review["current_revision"] != args.get("revision"):
                return {"success": True, "outcome": "ignored"}
            reason = await self._explicit_close_reason(review)
            if reason is None:
                return {"success": True, "outcome": "ignored"}
            client = await self._github_issue_client(PROJECT_ID)
            await client.close_issue_with_reason(number, reason, f"aq-issue-close:{review['id']}")
            return {"success": True, "outcome": "closed", "number": number}
        except (GitHubAccessError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    async def _cmd_github_issue_close_rejected(self, args: dict) -> dict:
        """Close only for an explicit human rejection asking for closure."""
        try:
            review, number = await self._issue_review(str(args.get("review_id") or ""))
            if review["state"] != "rejected" or review.get("decided_by") != "human:local-operator":
                raise ValueError("Jack must reject this review before the issue may be closed")
            held_id = await self._scoped_held_task_id()
            response = await self.db.get_task_meta(held_id, "review_response") if held_id else None
            if not isinstance(response, dict) or response.get("review_id") != review["id"]:
                raise ValueError("only the assigned review response worker may close this issue")
            reason = await self._explicit_close_reason(review)
            if not reason:
                raise ValueError("Jack did not explicitly ask to close this issue")
            client = await self._github_issue_client(PROJECT_ID)
            await client.close_issue_with_reason(number, reason, f"aq-issue-close:{review['id']}")
            return {"success": True, "outcome": "closed", "number": number}
        except (GitHubAccessError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
