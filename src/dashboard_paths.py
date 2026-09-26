"""Paths inside the dashboard that posted messages link to.

``src.remote_links.dashboard_link_base`` decides the origin; this module owns
the path. Task and report links open the focus routes (mobile dashboard spec
§4.1): a full view with no sidebar at every width, whose header links the
full dashboard page. The SPA keeps ``/tasks/:id`` and ``/reports/:id``, so
reverting these two functions restores the old links without breaking any
link already posted.
"""

from __future__ import annotations

from urllib.parse import quote


def task_path(task_id: str) -> str:
    return f"/focus/tasks/{quote(task_id, safe='')}"


def report_path(report_id: str) -> str:
    return f"/focus/reports/{quote(report_id, safe='')}"


def dashboard_href(base: str, path: str) -> str:
    """``base`` (an origin; a trailing slash is tolerated) joined to ``path``."""
    return f"{base.rstrip('/')}{path}"
