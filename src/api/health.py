"""Health, readiness, and plan viewer endpoints.

Consolidates the functionality from the old raw TCP health server
(src/health.py) into FastAPI routes.
"""

from __future__ import annotations

import logging
import re as _re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.api import dependencies as deps
from src.api.models.plans import PlanContentResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Task ids are ``adjective-noun[-NN]`` at the root and ``<parent>.<ordinal>`` for
# every level below it (``solid-grove``, ``solid-grove.26``, ``solid-grove.26.1``),
# so the plan viewer has to accept dots.  Matching dot-separated segments rather
# than adding ``.`` to the character class keeps ``..`` — and with it any path
# traversal — unrepresentable, and ``/`` was never in the allowlist.
_TASK_ID_RE = _re.compile(r"^[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*$")


def _check_ok(value: Any) -> bool:
    """Return True when a single check result indicates success."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("ok", False))
    return bool(value)


async def _get_checks() -> dict[str, Any]:
    """Read the production snapshot; support standalone router providers too."""
    monitor = deps._health_monitor
    if monitor is not None and monitor.provider is deps._health_provider:
        return monitor.checks()
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
    snapshot_reason = checks.get("health_snapshot", {}).get("reason")
    busy = snapshot_reason in {"initializing", "stale", "timeout"}
    status = "busy" if busy else ("healthy" if all_ok else "degraded")

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
    required_playbooks_ok = _check_ok(checks.get("required_playbooks", {"ok": True}))
    private_key_ok = _check_ok(checks.get("playbook_private_value_key", {"ok": True}))
    snapshot_ok = _check_ok(checks.get("health_snapshot", {"ok": True}))
    is_ready = (
        messaging_ok and database_ok and required_playbooks_ok and private_key_ok and snapshot_ok
    )

    body = {
        "ready": is_ready,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "messaging": checks.get("messaging", {"ok": False}),
            "database": checks.get("database", {"ok": False}),
            "required_playbooks": checks.get("required_playbooks", {"ok": True}),
            "playbook_private_value_key": checks.get("playbook_private_value_key", {"ok": True}),
            "health_snapshot": checks.get("health_snapshot", {"ok": True}),
        },
    }

    return JSONResponse(body, status_code=200 if is_ready else 503)


@router.get("/api/plans/{task_id}", response_model=PlanContentResponse, operation_id="getPlan")
async def plan_content(task_id: str) -> PlanContentResponse | JSONResponse:
    """Return plan markdown as data for the standalone dashboard viewer."""
    if not _TASK_ID_RE.fullmatch(task_id):
        return JSONResponse({"error": "invalid task id"}, status_code=400)

    if deps._plan_content_provider is None:
        return JSONResponse({"error": "plan viewer not configured"}, status_code=404)

    content = await deps._plan_content_provider(task_id)
    if content is None:
        return JSONResponse({"error": "plan not found"}, status_code=404)

    return PlanContentResponse(task_id=task_id, content=content)
