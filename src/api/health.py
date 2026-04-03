"""Health, readiness, and plan viewer endpoints.

Consolidates the functionality from the old raw TCP health server
(src/health.py) into FastAPI routes.
"""

from __future__ import annotations

import html as _html
import logging
import re as _re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from src.api import dependencies as deps

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_ok(value: Any) -> bool:
    """Return True when a single check result indicates success."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("ok", False))
    return bool(value)


async def _get_checks() -> dict[str, Any]:
    """Invoke the health provider, returning an empty dict on failure."""
    if deps._health_provider is None:
        return {}
    try:
        return await deps._health_provider()
    except Exception:
        logger.exception("Health provider raised an exception")
        return {"_provider_error": {"ok": False, "error": "provider failed"}}


@router.get("/health")
async def health() -> JSONResponse:
    """Full system health status.

    Returns 200 when all checks pass, 503 when any check indicates
    a problem (ok: False).
    """
    checks = await _get_checks()

    all_ok = all((c.get("ok", False) if isinstance(c, dict) else bool(c)) for c in checks.values())
    status = "healthy" if all_ok else "degraded"

    uptime = round(time.monotonic() - deps._started_at, 2) if deps._started_at is not None else 0

    body = {
        "status": status,
        "uptime_seconds": uptime,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }

    return JSONResponse(body, status_code=200 if all_ok else 503)


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe — checks messaging platform and database.

    Returns 200 when ready, 503 when not.
    """
    checks = await _get_checks()

    messaging_ok = _check_ok(checks.get("messaging"))
    database_ok = _check_ok(checks.get("database"))
    is_ready = messaging_ok and database_ok

    body = {
        "ready": is_ready,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "messaging": checks.get("messaging", {"ok": False}),
            "database": checks.get("database", {"ok": False}),
        },
    }

    return JSONResponse(body, status_code=200 if is_ready else 503)


@router.get("/plans/{task_id}", response_model=None)
async def plan_viewer(task_id: str) -> HTMLResponse | JSONResponse:
    """Serve plan content as an HTML page."""
    if deps._plan_content_provider is None:
        return JSONResponse({"error": "plan viewer not configured"}, status_code=404)

    if not _re.match(r"^[a-zA-Z0-9_-]+$", task_id):
        return JSONResponse({"error": "invalid task id"}, status_code=400)

    content = await deps._plan_content_provider(task_id)
    if content is None:
        return JSONResponse({"error": "plan not found"}, status_code=404)

    return HTMLResponse(_render_plan_html(task_id, content))


def get_plan_url(task_id: str) -> str:
    """Return the full URL to view a plan for the given task."""
    base = deps._base_url.rstrip("/") if deps._base_url else "http://localhost:8081"
    return f"{base}/plans/{task_id}"


def _render_plan_html(task_id: str, markdown_content: str) -> str:
    """Render plan markdown as a simple HTML page.

    Uses a CDN-hosted markdown renderer (marked.js) for rich rendering
    with a fallback to preformatted text if JS is disabled.
    """
    escaped = _html.escape(markdown_content)
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plan — {_html.escape(task_id)}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    max-width: 860px; margin: 2rem auto; padding: 0 1rem;
    color: #e0e0e0; background: #1a1a2e;
    line-height: 1.6;
  }}
  h1, h2, h3 {{ color: #f39c12; }}
  pre {{ background: #16213e; padding: 1rem; border-radius: 6px; overflow-x: auto; }}
  code {{ background: #16213e; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  pre code {{ background: none; padding: 0; }}
  a {{ color: #3498db; }}
  hr {{ border: none; border-top: 1px solid #333; }}
  .task-id {{ font-size: 0.85em; color: #888; }}
</style>
</head>
<body>
<div class="task-id">Task: {_html.escape(task_id)}</div>
<div id="content">
  <noscript><pre>{escaped}</pre></noscript>
</div>
<script id="raw-md" type="text/plain">{escaped}</script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
  try {{
    const md = document.getElementById('raw-md').textContent;
    document.getElementById('content').innerHTML = marked.parse(md);
  }} catch(e) {{
    document.getElementById('content').innerHTML = '<pre>' +
      document.getElementById('raw-md').textContent + '</pre>';
  }}
</script>
</body>
</html>"""
