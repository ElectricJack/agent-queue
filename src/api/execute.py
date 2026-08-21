"""Backward-compatible /api/execute endpoint.

Preserves the existing CLI contract: POST a command name and args dict,
get back {"ok": true, "result": {...}} or {"ok": false, "error": "..."}.

This endpoint exists so the current CLI keeps working unchanged during
the migration.  New code should use the typed per-command endpoints.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.dependencies import get_command_handler
from src.api.scope import check_command_scope

logger = logging.getLogger(__name__)

router = APIRouter()


class ExecuteRequest(BaseModel):
    command: str
    args: dict = {}


@router.post("/api/execute")
async def api_execute(
    body: ExecuteRequest,
    request: Request,
    ch=Depends(get_command_handler),
) -> JSONResponse:
    """Run a CommandHandler command (backward-compat envelope).

    Honours :data:`src.api.codegen.API_EXCLUDED`.  Without this the endpoint
    is a back door around the typed routes' exclusion set — notably for
    ``run_command``, whose containment (trust-and-ops §2.5) depends on it
    being unreachable from every remote surface.
    """
    from src.api.codegen import API_EXCLUDED

    if body.command in API_EXCLUDED:
        logger.warning("Refused /api/execute for excluded command %s", body.command)
        return JSONResponse(
            {"ok": False, "error": f"Command '{body.command}' is not available over the API"},
            status_code=403,
        )

    # aq-surface Phase S2: strip any client-supplied ``_scope`` BEFORE we
    # inject the middleware-derived one — a client cannot spoof identity.
    args = dict(body.args)
    args.pop("_scope", None)

    scope: RequestScope = getattr(request.state, "scope", LOCAL_SCOPE)
    scope_err = check_command_scope(body.command, args, scope)
    if scope_err is not None:
        return JSONResponse({"ok": False, "error": scope_err}, status_code=403)

    # Forward the server-derived scope so surface commands can resolve
    # ``task_id``/``project_id``/``session_id`` without an explicit arg.
    args["_scope"] = {
        "kind": scope.kind,
        "session_id": scope.session_id,
        "task_id": scope.task_id,
        "project_id": scope.project_id,
    }

    try:
        result = await ch.execute(body.command, args)
    except Exception:
        logger.exception("Error executing command %s", body.command)
        return JSONResponse(
            {"ok": False, "error": "Internal server error"},
            status_code=500,
        )

    if "error" in result:
        # Forward the rest of the error payload under `details`. Commands like
        # `create_task_graph` return a structured `errors`/`warnings` list
        # alongside the one-line summary; dropping it here is what made the
        # "report every finding at once" design unreachable from the CLI,
        # which only ever saw "graph validation failed with 3 error(s)".
        # `ok`/`error` are unchanged, so existing consumers are unaffected.
        payload: dict = {"ok": False, "error": result["error"]}
        details = {k: v for k, v in result.items() if k != "error"}
        if details:
            payload["details"] = json.loads(json.dumps(details, default=str))
        return JSONResponse(payload, status_code=200)
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
