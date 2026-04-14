"""Backward-compatible /api/execute endpoint.

Preserves the existing CLI contract: POST a command name and args dict,
get back {"ok": true, "result": {...}} or {"ok": false, "error": "..."}.

This endpoint exists so the current CLI keeps working unchanged during
the migration.  New code should use the typed per-command endpoints.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.dependencies import get_command_handler

logger = logging.getLogger(__name__)

router = APIRouter()


class ExecuteRequest(BaseModel):
    command: str
    args: dict = {}


@router.post("/api/execute")
async def api_execute(body: ExecuteRequest, ch=Depends(get_command_handler)) -> JSONResponse:
    """Run a CommandHandler command (backward-compat envelope)."""
    try:
        result = await ch.execute(body.command, body.args)
    except Exception:
        logger.exception("Error executing command %s", body.command)
        return JSONResponse(
            {"ok": False, "error": "Internal server error"},
            status_code=500,
        )

    if "error" in result:
        return JSONResponse(
            {"ok": False, "error": result["error"]},
            status_code=200,
        )
    return JSONResponse(
        {"ok": True, "result": json.loads(json.dumps(result, default=str))},
        status_code=200,
    )


@router.get("/api/tools")
async def api_tools() -> JSONResponse:
    """Return all tool definitions for CLI auto-generation."""
    from src.mcp_registration import _discover_all_commands
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS

    explicit = {t["name"]: t for t in _ALL_TOOL_DEFINITIONS}
    discovered = _discover_all_commands()
    merged = {**discovered, **explicit}
    return JSONResponse(list(merged.values()))


@router.get("/api/health")
async def api_health_simple() -> JSONResponse:
    """Simple liveness check (backward-compat)."""
    return JSONResponse({"status": "ok"})
