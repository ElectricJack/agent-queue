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
from src.api.scope import check_request_scope
from src.commands.principal import SERVER_OWNED_ARG_KEYS

logger = logging.getLogger(__name__)

router = APIRouter()

#: Keys the server owns on a command's args.  Stripped here and again inside
#: ``CommandHandler.execute`` — two independent layers.
_SERVER_OWNED_ARG_KEYS = SERVER_OWNED_ARG_KEYS


class ExecuteRequest(BaseModel):
    command: str
    args: dict = {}


@router.post("/api/execute")
async def api_execute(
    body: ExecuteRequest,
    ch=Depends(get_command_handler),
    request: Request = None,  # type: ignore[assignment]
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

    # aq-surface Phase S2 + Playbook V2 Package 0 §3.7: strip every
    # server-owned key BEFORE we inject the middleware-derived ones — a
    # client cannot spoof identity, policy, or profile.
    args = dict(body.args)
    for key in _SERVER_OWNED_ARG_KEYS:
        args.pop(key, None)

    scope: RequestScope = (
        getattr(request.state, "scope", LOCAL_SCOPE) if request is not None else LOCAL_SCOPE
    )
    scope_err = await check_request_scope(
        body.command, args, scope, db=getattr(ch, "db", None),
    )
    if scope_err is not None:
        return JSONResponse({"ok": False, "error": scope_err}, status_code=403)

    # Forward the server-derived scope so surface commands can resolve
    # ``task_id``/``project_id``/``session_id`` without an explicit arg.
    args["_scope"] = {
        "kind": scope.kind,
        "session_id": scope.session_id,
        "session_instance_token": scope.session_instance_token,
        "task_id": scope.task_id,
        "project_id": scope.project_id,
        # Commands that fence reads on the scope need to know whether this
        # is a plain agent session or an elevated supervisor one.
        "elevated": scope.elevated,
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
        # A capability denial is an authorization failure, not a command
        # error; it is the same 403 the scope gate returns.
        status = 403 if result.get("error_code") == "capability_denied" else 200
        return JSONResponse(payload, status_code=status)
    return JSONResponse(
        {"ok": True, "result": json.loads(json.dumps(result, default=str))},
        status_code=200,
    )


@router.get("/api/tools")
async def api_tools(
    ch=Depends(get_command_handler),
    request: Request = None,  # type: ignore[assignment]
) -> JSONResponse:
    """Return the tool definitions this caller could actually dispatch.

    Filtered with the *same* predicate the dispatch gate uses (Playbook V2
    Package 0 §4.3), so a published name is a runnable name.  A loopback
    caller has no session scope and therefore sees everything, which is the
    behaviour this endpoint has always had for the CLI.
    """
    from src.commands.authorization import filter_tool_definitions
    from src.mcp_registration import _discover_all_commands
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS

    explicit = {t["name"]: t for t in _ALL_TOOL_DEFINITIONS}
    discovered = _discover_all_commands()
    merged = {**discovered, **explicit}

    scope: RequestScope = (
        getattr(request.state, "scope", LOCAL_SCOPE) if request is not None else LOCAL_SCOPE
    )
    principal = await ch._principal_from_scope(
        {
            "kind": scope.kind,
            "session_id": scope.session_id,
            "session_instance_token": scope.session_instance_token,
            "task_id": scope.task_id,
            "project_id": scope.project_id,
            "elevated": scope.elevated,
        }
    )
    definitions = filter_tool_definitions(
        merged.values(), principal, resolver=ch._command_resolver
    )
    return JSONResponse(definitions)


@router.get("/api/health")
async def api_health_simple() -> JSONResponse:
    """Simple liveness check (backward-compat)."""
    return JSONResponse({"status": "ok"})
