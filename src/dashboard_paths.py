"""Paths inside the dashboard that posted messages link to.

``src.remote_links.dashboard_link_base`` decides the origin; this module owns
the path. Task and report links open the focus routes (mobile dashboard spec
§4.1): a full view with no sidebar at every width, whose header links the
full dashboard page. The SPA keeps ``/tasks/:id`` and ``/reports/:id``, so
reverting these two functions restores the old links without breaking any
link already posted.

The Discord design spec adds a scheme on top: §6.1's table of which page a
posted link opens, per object, on a phone and on a desktop. :data:`SCHEME` is
that table, so a renderer asks for ``escalation_path(id)`` instead of writing
a route, and a new focus route is added in one place. A ``/settings/`` page is
never a post's link target: a link is how the reader *acts* on the post, and a
settings page asks them to configure the machine that sent it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote


def task_path(task_id: str) -> str:
    return f"/focus/tasks/{quote(task_id, safe='')}"


def session_path(session_id: str) -> str:
    return f"/focus/sessions/{quote(session_id, safe='')}"


def report_path(report_id: str) -> str:
    return f"/focus/reports/{quote(report_id, safe='')}"


def review_path(review_id: str) -> str:
    return f"/focus/reviews/{quote(review_id, safe='')}"


def escalation_path(escalation_id: str) -> str:
    return f"/focus/escalations/{quote(escalation_id, safe='')}"


def conversation_path(conversation_id: str) -> str:
    return f"/focus/conversations/{quote(conversation_id, safe='')}"


def batch_path(batch_id: str) -> str:
    return f"/focus/batches/{quote(batch_id, safe='')}"


def inbox_path() -> str:
    return "/focus/inbox"


def dashboard_href(base: str, path: str) -> str:
    """``base`` (an origin; a trailing slash is tolerated) joined to ``path``."""
    return f"{base.rstrip('/')}{path}"


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """One row of the §6.1 scheme.

    ``focus`` is the phone-first path a posted link uses and ``desktop`` the
    page a wide viewport redirects to, or the same path when no desktop page
    exists.  ``status`` is ``"live"`` for a route the SPA serves today and
    ``"planned"`` for one the scheme adds, so the table cannot quietly claim a
    route exists when it does not.
    """

    name: str
    focus: Callable[[str], str]
    desktop: str
    status: str


SCHEME: dict[str, LinkTarget] = {
    target.name: target
    for target in (
        LinkTarget("task", task_path, "/tasks/{id}", "live"),
        LinkTarget("session", session_path, "/sessions/{id}", "live"),
        LinkTarget("report", report_path, "/reports/{id}", "live"),
        LinkTarget("review", review_path, "/reviews/{id}", "planned"),
        LinkTarget("escalation", escalation_path, "/focus/escalations/{id}", "planned"),
        LinkTarget("batch", batch_path, "/projects/{project}/graph?batch={id}", "planned"),
        LinkTarget("inbox", lambda _id="": inbox_path(), "/reviews", "planned"),
        LinkTarget("conversation", conversation_path, "/conversations", "planned"),
    )
}
