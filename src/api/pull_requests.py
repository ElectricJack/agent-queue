"""Durable, asynchronously refreshed inbox of task-linked open GitHub PRs."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.api.auth import LOCAL_SCOPE, request_operator_viewer
from src.database.queries.pull_request_queries import (
    list_known_pull_requests, mark_pull_request_approved, read_pull_request_snapshot,
    replace_pull_request_snapshot_row, train_states_for_tasks, write_pull_request_snapshot,
)
from src.git.github import GitHubAccess, GitHubClient

log = logging.getLogger(__name__)
REFRESH_SECONDS = 60


class PendingPullRequest(BaseModel):
    title: str
    url: str
    repository: str
    project_id: str
    project_name: str
    task_id: str
    state: Literal["open"] = "open"
    opened_at: float | None = None
    head_sha: str
    review_decision: Literal["approved", "changes requested", "pending"] = "pending"
    ci_status: Literal["success", "failure", "pending"] = "pending"
    train_state: str | None = None


class PendingPullRequestsResponse(BaseModel):
    pull_requests: list[PendingPullRequest]
    snapshot_at: float | None = None
    snapshot_age_seconds: float | None = None
    can_approve: bool = False


class ApprovePendingPullRequestRequest(BaseModel):
    head_sha: str


def _repository_name(url: str) -> str | None:
    """Return the repository only for a safe, canonical GitHub PR link."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    path = parts.path.strip("/").split("/")
    if (
        parts.scheme != "https"
        or parts.hostname not in {"github.com", "www.github.com"}
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
        or parts.query or parts.fragment
        or len(path) != 4 or path[2] != "pull"
        or re.fullmatch(r"[A-Za-z0-9_.-]+", path[0]) is None
        or re.fullmatch(r"[A-Za-z0-9_.-]+", path[1]) is None
        or not path[3].isdigit() or int(path[3]) <= 0
    ):
        return None
    return f"{path[0]}/{path[1]}"


def _opened_at(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pr_key(url: str) -> tuple[str, int] | None:
    repository = _repository_name(url)
    return (repository.lower(), int(url.rsplit("/", 1)[-1])) if repository else None


def _review_decision(reviews: list[dict], head_sha: str) -> str:
    # A reviewer's last decision at this exact head supersedes earlier ones.
    latest: dict[str, str] = {}
    for review in sorted(reviews, key=lambda row: row.get("id") or 0):
        user = review.get("user")
        if (not isinstance(user, dict) or user.get("type") != "User"
                or review.get("commit_id") != head_sha):
            continue
        login = user.get("login")
        if isinstance(login, str) and login:
            latest[login.lower()] = review.get("state", "")
    if "CHANGES_REQUESTED" in latest.values():
        return "changes requested"
    if "APPROVED" in latest.values():
        return "approved"
    return "pending"


def _ci_status(checks: list[dict]) -> str:
    # Reruns leave older check runs on the same SHA.  GitHub's latest run of
    # each check name and App is the verdict the PR UI presents.
    latest: dict[tuple[object, object], dict] = {}
    for check in sorted(checks, key=lambda row: row.get("id") or 0):
        app = check.get("app")
        key = (check.get("name"), app.get("id") if isinstance(app, dict) else None)
        latest[key] = check
    current = list(latest.values())
    if not current or any(check.get("status") != "completed" for check in current):
        return "pending"
    if any(check.get("conclusion") not in {"success", "neutral", "skipped"}
           for check in current):
        return "failure"
    return "success"


class PendingPullRequestInbox:
    def __init__(self, db, github_access: GitHubAccess, *, interval: float = REFRESH_SECONDS,
                 client_factory: Callable[..., Any] = GitHubClient) -> None:
        self.db = db
        self.access = github_access
        self.interval = interval
        self.client_factory = client_factory
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def list(self, *, can_approve: bool = False) -> PendingPullRequestsResponse:
        items, updated_at = await read_pull_request_snapshot(self.db)
        return PendingPullRequestsResponse(
            pull_requests=items,
            snapshot_at=updated_at,
            snapshot_age_seconds=max(0, time.time() - updated_at) if updated_at else None,
            can_approve=can_approve,
        )

    async def refresh_row(self, task_id: str) -> None:
        """Re-read the approved PR and its badges without rescanning other PRs."""
        rows = [row for row in await list_known_pull_requests(self.db)
                if row["task_id"] == task_id]
        if len(rows) != 1:
            return
        row = rows[0]
        binding = await self.access.bind_repository(row["repository_url"])
        client = self.client_factory(binding, access=self.access)
        pull = await client.pull_request(row["pr_url"])
        if pull.get("state") != "open":
            await replace_pull_request_snapshot_row(self.db, task_id, None)
            return
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or re.fullmatch(r"[a-fA-F0-9]{40}", sha) is None:
            raise ValueError("GitHub PR had no valid head SHA")
        number = GitHubAccess.validate_pr_url(binding, row["pr_url"])
        reviews, checks = await asyncio.gather(
            client.paged_list(
                f"repos/{binding.full_name}/pulls/{number}/reviews?per_page=100", max_pages=20,
            ),
            client.paged_items(
                f"repos/{binding.full_name}/commits/{sha}/check-runs?per_page=100",
                key="check_runs", max_pages=20,
            ),
        )
        states = await train_states_for_tasks(self.db, {task_id})
        item = PendingPullRequest(
            title=pull.get("title") or row["task_title"], url=row["pr_url"],
            repository=_repository_name(row["pr_url"]) or "", project_id=row["project_id"],
            project_name=row["project_name"], task_id=task_id,
            opened_at=_opened_at(pull.get("created_at")), head_sha=sha,
            review_decision=_review_decision(reviews, sha), ci_status=_ci_status(checks),
            train_state=(states.get(task_id, "awaiting review")
                         if row["integration_mode"] == "train" else None),
        )
        await replace_pull_request_snapshot_row(self.db, task_id, item.model_dump())

    async def refresh(self) -> None:
        async with self._lock:
            links = await list_known_pull_requests(self.db)
            by_repo: dict[str, list[dict]] = {}
            for row in links:
                key = _pr_key(row["pr_url"])
                if key is None:
                    log.warning("Skipping invalid GitHub PR URL on task %s", row["task_id"])
                    continue
                repository_url = row["repository_url"]
                if not repository_url:
                    continue
                by_repo.setdefault(repository_url, []).append(row)

            matched: list[tuple[dict, dict, Any]] = []
            for repository_url, rows in by_repo.items():
                binding = await self.access.bind_repository(repository_url)
                client = self.client_factory(binding, access=self.access)
                pulls = await client.paged_list(
                    f"repos/{binding.full_name}/pulls?state=open&per_page=100", max_pages=50,
                )
                open_pulls = {}
                for pull in pulls:
                    url = pull.get("html_url")
                    key = _pr_key(url) if isinstance(url, str) else None
                    if key is not None:
                        GitHubAccess.validate_pr_url(binding, url)
                        open_pulls[key] = pull
                for row in rows:
                    pull = open_pulls.get(_pr_key(row["pr_url"]))
                    if pull is not None:
                        matched.append((row, pull, client))

            train_states = await train_states_for_tasks(
                self.db, {row["task_id"] for row, _, _ in matched},
            )
            limit = asyncio.Semaphore(8)

            async def project(row: dict, pull: dict, client: Any) -> PendingPullRequest:
                head = pull.get("head")
                sha = head.get("sha") if isinstance(head, dict) else None
                if not isinstance(sha, str) or re.fullmatch(r"[a-fA-F0-9]{40}", sha) is None:
                    raise ValueError(f"GitHub PR {row['pr_url']} had no valid head SHA")
                number = int(row["pr_url"].rsplit("/", 1)[-1])
                async with limit:
                    reviews, checks = await asyncio.gather(
                        client.paged_list(
                            f"repos/{client.repository.full_name}/pulls/{number}/reviews?per_page=100",
                            max_pages=20,
                        ),
                        client.paged_items(
                            f"repos/{client.repository.full_name}/commits/{sha}/check-runs?per_page=100",
                            key="check_runs", max_pages=20,
                        ),
                    )
                return PendingPullRequest(
                    title=pull.get("title") or row["task_title"],
                    url=row["pr_url"], repository=_repository_name(row["pr_url"]) or "",
                    project_id=row["project_id"], project_name=row["project_name"],
                    task_id=row["task_id"], opened_at=_opened_at(pull.get("created_at")),
                    head_sha=sha, review_decision=_review_decision(reviews, sha),
                    ci_status=_ci_status(checks),
                    train_state=(train_states.get(row["task_id"], "awaiting review")
                                 if row["integration_mode"] == "train" else None),
                )

            items = await asyncio.gather(*(project(*entry) for entry in matched))
            items.sort(key=lambda item: item.opened_at or 0, reverse=True)
            await write_pull_request_snapshot(
                self.db, [item.model_dump() for item in items],
            )

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not refresh pending GitHub PR snapshot")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task


def _operator_scope(request: Request) -> bool:
    scope = getattr(request.state, "scope", LOCAL_SCOPE)
    return scope.kind == "local" and request_operator_viewer(request)


def build_pull_requests_router(*, db, github_access=None, command_handler=None,
                               inbox: PendingPullRequestInbox | None = None) -> APIRouter:
    inbox = inbox or PendingPullRequestInbox(db, github_access)

    @asynccontextmanager
    async def lifespan(_router: APIRouter):
        inbox.start()
        try:
            yield
        finally:
            await inbox.stop()

    router = APIRouter(lifespan=lifespan)

    @router.get("/api/reviews/pull-requests", response_model=PendingPullRequestsResponse)
    async def get_pending_pull_requests(request: Request) -> PendingPullRequestsResponse:
        scope = getattr(request.state, "scope", LOCAL_SCOPE)
        if scope.kind != "local" and not (scope.elevated and scope.project_id is None):
            raise HTTPException(status_code=403, detail="Pull requests are out of scope")
        return await inbox.list(can_approve=_operator_scope(request))

    @router.post("/api/reviews/pull-requests/{task_id}/approve")
    async def approve_pull_request(task_id: str, body: ApprovePendingPullRequestRequest,
                                   request: Request) -> dict:
        if not _operator_scope(request):
            raise HTTPException(status_code=403, detail="Operator dashboard required")
        if command_handler is None:
            raise HTTPException(status_code=503, detail="Approval service unavailable")
        result = await command_handler.execute("approve_pull_request", {
            "task_id": task_id, "head_sha": body.head_sha,
        })
        if not result.get("success"):
            status = 409 if result.get("error_code") == "stale_head" else 400
            raise HTTPException(status_code=status, detail=result.get("error", "Approval failed"))
        try:
            await mark_pull_request_approved(db, result["url"], body.head_sha)
            await inbox.refresh_row(task_id)
            # A just-created GitHub review can lag its list endpoint briefly.
            # The POST response already confirmed this exact human approval.
            await mark_pull_request_approved(db, result["url"], body.head_sha)
        except Exception:
            log.warning("Could not refresh approved PR row %s", task_id, exc_info=True)
        return result

    return router
