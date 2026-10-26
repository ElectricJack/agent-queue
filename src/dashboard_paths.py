"""Canonical paths for posted links into the responsive dashboard.

The origin is selected by ``src.remote_links``. Old ``/focus/*`` links are
redirected by the client; new posts always use the main dashboard routes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote


def task_path(task_id: str) -> str:
    return f"/tasks/{quote(task_id, safe='')}"


def session_path(session_id: str) -> str:
    return f"/sessions/{quote(session_id, safe='')}"


def report_path(report_id: str) -> str:
    return f"/reports/{quote(report_id, safe='')}"


def review_path(review_id: str) -> str:
    return f"/reviews/{quote(review_id, safe='')}"


def escalation_path(escalation_id: str) -> str:
    return f"/escalations/{quote(escalation_id, safe='')}"


def conversation_path(conversation_id: str) -> str:
    return f"/conversations?conversation={quote(conversation_id, safe='')}"


def batch_path(batch_id: str) -> str:
    return f"/batches/{quote(batch_id, safe='')}"


def inbox_path() -> str:
    return "/inbox"


def dashboard_href(base: str, path: str) -> str:
    """``base`` (an origin; a trailing slash is tolerated) joined to ``path``."""
    return f"{base.rstrip('/')}{path}"


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """One canonical route, shared by every viewport."""

    name: str
    path: Callable[[str], str]
    desktop: str
    status: str


SCHEME: dict[str, LinkTarget] = {
    target.name: target
    for target in (
        LinkTarget("task", task_path, "/tasks/{id}", "live"),
        LinkTarget("session", session_path, "/sessions/{id}", "live"),
        LinkTarget("report", report_path, "/reports/{id}", "live"),
        LinkTarget("review", review_path, "/reviews/{id}", "live"),
        LinkTarget("escalation", escalation_path, "/escalations/{id}", "live"),
        LinkTarget("batch", batch_path, "/batches/{id}", "live"),
        LinkTarget("inbox", lambda _id="": inbox_path(), "/inbox", "live"),
        LinkTarget("conversation", conversation_path, "/conversations?conversation={id}", "live"),
    )
}
