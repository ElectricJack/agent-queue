"""Read-only list of open GitHub PRs already linked to AQ tasks."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Awaitable, Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.api.auth import LOCAL_SCOPE
from src.database.queries.pull_request_queries import list_known_pull_requests
from src.git.github import GitHubClient

log = logging.getLogger(__name__)
CACHE_SECONDS = 60


class PendingPullRequest(BaseModel):
    title: str
    url: str
    repository: str
    project_id: str
    project_name: str
    task_id: str
    state: Literal["open", "unknown"]  # closed and merged PRs are omitted
    opened_at: float | None


class PendingPullRequestsResponse(BaseModel):
    pull_requests: list[PendingPullRequest]


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
        or parts.query
        or parts.fragment
        or len(path) != 4
        or path[2] != "pull"
        or re.fullmatch(r"[A-Za-z0-9_.-]+", path[0]) is None
        or re.fullmatch(r"[A-Za-z0-9_.-]+", path[1]) is None
        or not path[3].isdigit()
        or int(path[3]) <= 0
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


async def _github_lookup(access, repository_url: str, pr_url: str) -> dict:
    # The task's repository association supplies authority; the PR URL is
    # validated against that binding by GitHubClient.pull_request.
    binding = await access.bind_repository(repository_url)
    return await GitHubClient(binding, access=access).pull_request(pr_url)


class PendingPullRequestInbox:
    def __init__(
        self,
        db,
        lookup: Callable[[str, str], Awaitable[dict]],
        *,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self.db = db
        self.lookup = lookup
        self.cache_seconds = cache_seconds
        self._cached: PendingPullRequestsResponse | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def list(self) -> PendingPullRequestsResponse:
        if self._cached is not None and time.monotonic() < self._expires_at:
            return self._cached
        async with self._lock:
            if self._cached is not None and time.monotonic() < self._expires_at:
                return self._cached
            rows = await list_known_pull_requests(self.db)
            limit = asyncio.Semaphore(8)

            async def resolve(row: dict) -> PendingPullRequest | None:
                url = row["pr_url"]
                repository = _repository_name(url)
                if repository is None:
                    log.warning("Skipping invalid GitHub PR URL on task %s", row["task_id"])
                    return None
                try:
                    async with limit:
                        payload = await self.lookup(row["repository_url"] or "", url)
                    if payload.get("state") == "closed":
                        return None
                    state = "open" if payload.get("state") == "open" else "unknown"
                except Exception:
                    log.warning("Could not read GitHub PR state for %s", url, exc_info=True)
                    payload = {}
                    state = "unknown"
                title = payload.get("title")
                return PendingPullRequest(
                    title=title if isinstance(title, str) and title else row["task_title"],
                    url=url,
                    repository=repository,
                    project_id=row["project_id"],
                    project_name=row["project_name"],
                    task_id=row["task_id"],
                    state=state,
                    opened_at=_opened_at(payload.get("created_at")),
                )

            resolved = await asyncio.gather(*(resolve(row) for row in rows))
            items = [item for item in resolved if item is not None]
            items.sort(key=lambda item: item.opened_at or 0, reverse=True)
            self._cached = PendingPullRequestsResponse(pull_requests=items)
            self._expires_at = time.monotonic() + self.cache_seconds
            return self._cached


def build_pull_requests_router(*, db, github_access=None, lookup=None) -> APIRouter:
    if lookup is None:
        async def lookup(repository_url: str, pr_url: str) -> dict:
            return await _github_lookup(github_access, repository_url, pr_url)

    inbox = PendingPullRequestInbox(db, lookup)
    router = APIRouter()

    @router.get("/api/reviews/pull-requests", response_model=PendingPullRequestsResponse)
    async def get_pending_pull_requests(request: Request) -> PendingPullRequestsResponse:
        scope = getattr(request.state, "scope", LOCAL_SCOPE)
        if scope.kind != "local" and not (scope.elevated and scope.project_id is None):
            raise HTTPException(status_code=403, detail="Pull requests are out of scope")
        return await inbox.list()

    return router
