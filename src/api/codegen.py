"""Auto-generate FastAPI routes from tool_registry definitions.

Mirrors the pattern in ``src/cli/auto_commands.py`` but generates typed
FastAPI endpoints instead of Click commands.  Each tool_registry command
becomes a ``POST /api/{category}/{command-name}`` endpoint with:

- A Pydantic request model generated from the tool's ``input_schema``
- A Pydantic response model looked up from ``src.api.models``
- A handler that delegates to ``CommandHandler.execute()``

Category grouping, prefix stripping, and naming all follow the same
logic as the CLI so that ``aq git commit`` maps to ``POST /api/git/commit``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, create_model

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.dependencies import get_command_handler
from src.api.models import get_all_response_models
from src.api.scope import check_command_scope
from src.cli.auto_commands import _strip_category_prefix
from src.tools import (
    CATEGORIES,
    _ALL_TOOL_DEFINITIONS,
    _CLI_CATEGORY_OVERRIDES,
    _TOOL_CATEGORIES,
)

logger = logging.getLogger(__name__)

# Commands to exclude from the API entirely (internal/MCP-only, or too
# dangerous to expose over HTTP).  Enforced in two places: here, so no typed
# route is generated, and in ``/api/execute`` (``src/api/execute.py``), which
# would otherwise reach the same commands through the back door.
API_EXCLUDED = {
    "load_tools",
    "send_message",
    "reply_to_user",
    # Runs an LLM-authored string through /bin/sh on the daemon host
    # (trust-and-ops §2.5).  Already out of MCP and the CLI; the API is the
    # third surface, and it becomes reachable with an agent-held credential
    # once task-scoped session tokens land.
    "run_command",
    # message_send is served by the dedicated per-session route
    # ``POST /api/sessions/{name}/message`` (src/api/messages.py); the
    # codegen route would duplicate it and confuse the dashboard chat page.
    "message_send",
}


# ---------------------------------------------------------------------------
# Codegen-only input schema overrides
# ---------------------------------------------------------------------------
#
# These commands are categorized in ``_TOOL_CATEGORIES`` but have no entry in
# ``_ALL_TOOL_DEFINITIONS``.  ``_discover_all_commands`` provides a fallback
# tool dict with an empty input schema, which makes the auto-generated FastAPI
# request model reject every client arg — verified live as
# ``POST /api/task/explain {"task_id": "x"} -> {"error": "task_id is required"}``
# because the empty request model silently drops ``task_id``.
#
# Overriding at the codegen layer (rather than inserting into
# ``_ALL_TOOL_DEFINITIONS``) preserves current LLM tool exposure: adding these
# to the master list would surface them when the supervisor calls
# ``load_tools("system")`` / ``load_tools("task")``, which they historically
# aren't (they had no schema, so the LLM registry never actually included
# them).  Codegen is the only consumer that needs the schema.
_CODEGEN_INPUT_SCHEMAS: dict[str, dict] = {
    # -- explain + ready frontier (work-graph WG-4) ------------------------
    "explain_task": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task id to explain"},
        },
        "required": ["task_id"],
    },
    "project_ready": {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Project id (falls back to the active project when omitted)",
            },
            "labels": {
                "type": "array",
                "description": "Restrict frontier to tasks carrying all of these labels",
                "items": {"type": "string"},
            },
            "any_label": {
                "type": "array",
                "description": "Restrict frontier to tasks carrying any of these labels",
                "items": {"type": "string"},
            },
        },
    },
    # -- gate operator surface (work-graph WG-3) ---------------------------
    "gate_create": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project id that owns the gate"},
            "gate_type": {"type": "string", "description": "Gate kind, e.g. 'review'"},
            "title": {"type": "string", "description": "Human-readable gate title"},
            "question": {
                "type": "string",
                "description": "Optional prompt shown to the resolver",
            },
            "await_id": {
                "type": "string",
                "description": "Optional external id the gate is waiting on",
            },
            "timeout_at": {
                "type": "number",
                "description": "Optional epoch after which the gate is considered expired",
            },
            "waiter_task_ids": {
                "type": "array",
                "description": "Task ids that should be blocked by this gate",
                "items": {"type": "string"},
            },
        },
        "required": ["project_id", "gate_type", "title"],
    },
    "gate_list": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Filter by project"},
            "status": {
                "type": "string",
                "description": "Filter by status (open|resolved|expired)",
            },
            "gate_type": {"type": "string", "description": "Filter by gate kind"},
        },
    },
    "gate_show": {
        "type": "object",
        "properties": {
            "gate_id": {"type": "string", "description": "Gate id to fetch"},
        },
        "required": ["gate_id"],
    },
    "gate_resolve": {
        "type": "object",
        "properties": {
            "gate_id": {"type": "string", "description": "Gate id to resolve"},
            "resolved_by": {
                "type": "string",
                "description": "Identity of the resolver (user id or session)",
            },
            "resolution": {
                "type": "string",
                "description": "Optional free-text explanation stored with the resolve event",
            },
        },
        "required": ["gate_id", "resolved_by"],
    },
    # -- session operator surface (session-runtime spec §3, §5) ------------
    "session_list": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "description": "Filter by session state (starting|running|draining|...)",
            },
            "lifecycle": {
                "type": "string",
                "description": "Filter by lifecycle (task|named)",
            },
            "project_id": {
                "type": "string",
                "description": "Filter by project (falls back to the active project)",
            },
            "live_only": {
                "type": "boolean",
                "description": "Only include sessions that are not stopped/quarantined",
                "default": False,
            },
        },
    },
    "session_show": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session id (uuid4 hex)"},
            "id": {"type": "string", "description": "Alias for session_id"},
            "name": {"type": "string", "description": "Session name (provider name)"},
            "task_id": {"type": "string", "description": "Resolve session from this task id"},
        },
    },
    "session_peek": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session id (uuid4 hex)"},
            "id": {"type": "string", "description": "Alias for session_id"},
            "name": {"type": "string", "description": "Session name"},
            "task_id": {"type": "string", "description": "Resolve session from this task id"},
            "lines": {
                "type": "integer",
                "description": "Number of tail lines to return (default 60)",
            },
            "n": {"type": "integer", "description": "Alias for lines"},
        },
    },
    "session_attach": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session id (uuid4 hex)"},
            "id": {"type": "string", "description": "Alias for session_id"},
            "name": {"type": "string", "description": "Session name"},
            "task_id": {"type": "string", "description": "Resolve session from this task id"},
        },
    },
    "session_nudge": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session id (uuid4 hex)"},
            "id": {"type": "string", "description": "Alias for session_id"},
            "name": {"type": "string", "description": "Session name"},
            "task_id": {"type": "string", "description": "Resolve session from this task id"},
            "text": {"type": "string", "description": "Text to inject and submit"},
            "message": {"type": "string", "description": "Alias for text"},
        },
    },
    "session_logs": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session id (uuid4 hex)"},
            "id": {"type": "string", "description": "Alias for session_id"},
            "name": {"type": "string", "description": "Session name"},
            "task_id": {"type": "string", "description": "Resolve session from this task id"},
            "limit": {
                "type": "integer",
                "description": "Max transcript entries to return (default 100)",
            },
            "lines": {"type": "integer", "description": "Alias for limit"},
            "n": {"type": "integer", "description": "Alias for limit"},
        },
    },
    "session_sleep": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session id (uuid4 hex)"},
            "id": {"type": "string", "description": "Alias for session_id"},
            "name": {"type": "string", "description": "Session name"},
            "task_id": {"type": "string", "description": "Resolve session from this task id"},
        },
    },
    "session_wake": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session id (uuid4 hex)"},
            "id": {"type": "string", "description": "Alias for session_id"},
            "name": {"type": "string", "description": "Session name"},
            "task_id": {"type": "string", "description": "Resolve session from this task id"},
        },
    },
    "session_kill": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session id (uuid4 hex)"},
            "id": {"type": "string", "description": "Alias for session_id"},
            "name": {"type": "string", "description": "Session name"},
            "task_id": {"type": "string", "description": "Resolve session from this task id"},
            "grace": {
                "type": "number",
                "description": "Seconds to wait between signal and force-kill (default 2.0)",
            },
        },
    },
}


def _category_to_api_path(cat_name: str) -> str:
    """Derive API path segment from category name.

    Uses the category name directly — no hardcoded mapping needed.
    """
    return cat_name


# ---------------------------------------------------------------------------
# Input model generation from JSON Schema
# ---------------------------------------------------------------------------


def _json_schema_type_to_python(prop_schema: dict) -> type:
    """Map a JSON Schema property to a Python type."""
    if "enum" in prop_schema:
        return str

    schema_type = prop_schema.get("type", "string")

    # JSON Schema union types: {"type": ["string", "integer"]} → str
    if isinstance(schema_type, list):
        schema_type = schema_type[0] if schema_type else "string"

    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return type_map.get(schema_type, str)


def _make_input_model(cmd_name: str, input_schema: dict) -> type[BaseModel]:
    """Build a Pydantic model from a tool's input_schema JSON Schema."""
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        py_type = _json_schema_type_to_python(prop_schema)
        description = prop_schema.get("description", "")
        default = prop_schema.get("default")

        if prop_name in required:
            fields[prop_name] = (py_type, Field(..., description=description))
        elif default is not None:
            fields[prop_name] = (py_type, Field(default=default, description=description))
        else:
            fields[prop_name] = (py_type | None, Field(default=None, description=description))

    # Generate a clean model name: list_tasks → ListTasksRequest
    parts = cmd_name.split("_")
    model_name = "".join(p.capitalize() for p in parts) + "Request"

    return create_model(model_name, **fields)


# ---------------------------------------------------------------------------
# Route generation
# ---------------------------------------------------------------------------


def _make_route_handler(cmd_name: str, input_model: type[BaseModel]):
    """Create an async route handler that delegates to CommandHandler.execute()."""
    from typing import Annotated

    # Capture the model in a default arg so the closure resolves correctly.
    # Use Annotated to set the concrete type for FastAPI's schema generation.
    BodyType = Annotated[input_model, ...]  # noqa: N806

    async def handler(
        body: BodyType,
        ch=Depends(get_command_handler),
        request: Request = None,  # type: ignore[assignment]
    ):
        # aq-surface Phase S2: mirror /api/execute's scope enforcement so
        # session-token holders cannot reach out-of-scope commands via the
        # typed routes.  Strip any client-supplied ``_scope`` before we
        # inject the middleware-derived one — clients cannot spoof identity.
        args = body.model_dump(exclude_none=True)
        args.pop("_scope", None)

        scope: RequestScope = (
            getattr(request.state, "scope", LOCAL_SCOPE) if request is not None else LOCAL_SCOPE
        )
        scope_err = check_command_scope(cmd_name, args, scope)
        if scope_err is not None:
            return JSONResponse({"error": scope_err}, status_code=403)

        # Forward the server-derived scope so surface commands can resolve
        # ``task_id``/``project_id``/``session_id`` without an explicit arg.
        args["_scope"] = {
            "kind": scope.kind,
            "session_id": scope.session_id,
            "task_id": scope.task_id,
            "project_id": scope.project_id,
        }

        result = await ch.execute(cmd_name, args)
        if "error" in result:
            return JSONResponse(
                {"error": result["error"]},
                status_code=422,
            )
        return result

    # Fix annotations so FastAPI resolves the concrete model, not a forward ref
    handler.__annotations__["body"] = input_model
    handler.__name__ = cmd_name
    handler.__qualname__ = cmd_name
    return handler


def build_category_routers() -> list[APIRouter]:
    """Build one APIRouter per category with auto-generated routes.

    Returns a list of routers ready to be included in the FastAPI app.
    """
    response_models = get_all_response_models()

    # Build complete tool map
    tool_map: dict[str, dict] = {t["name"]: t for t in _ALL_TOOL_DEFINITIONS}
    try:
        from src.mcp_registration import _discover_all_commands

        discovered = _discover_all_commands()
        for name, defn in discovered.items():
            if name not in tool_map:
                tool_map[name] = defn
    except Exception:
        pass

    # Collect internal plugin tool definitions
    plugin_categories: dict[str, str] = {}
    try:
        from src.plugins.internal import collect_internal_tool_definitions

        for category, tool_defs in collect_internal_tool_definitions():
            for defn in tool_defs:
                name = defn["name"]
                if name not in tool_map:
                    tool_map[name] = defn
                if name not in _TOOL_CATEGORIES and name not in _CLI_CATEGORY_OVERRIDES:
                    plugin_categories[name] = category
    except Exception:
        pass

    # Apply codegen-only input-schema overrides for commands whose entries
    # in tool_map come from the ``_discover_all_commands`` fallback (empty
    # schema).  See ``_CODEGEN_INPUT_SCHEMAS`` above.
    for cmd_name, schema in _CODEGEN_INPUT_SCHEMAS.items():
        defn = tool_map.get(cmd_name)
        if defn is None:
            continue
        existing = defn.get("input_schema") or {}
        if not existing.get("properties"):
            # Shallow copy so we don't mutate the shared discovered dict.
            tool_map[cmd_name] = {**defn, "input_schema": schema}

    # Group tools by category
    category_tools: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for cmd_name, defn in tool_map.items():
        if cmd_name in API_EXCLUDED:
            continue
        cat = (
            _TOOL_CATEGORIES.get(cmd_name)
            or _CLI_CATEGORY_OVERRIDES.get(cmd_name)
            or plugin_categories.get(cmd_name)
        )
        if cat:
            category_tools[cat].append((cmd_name, defn))

    routers: list[APIRouter] = []

    for cat_name in sorted(CATEGORIES.keys()):
        tools = category_tools.get(cat_name, [])
        if not tools:
            continue

        api_name = _category_to_api_path(cat_name)
        CATEGORIES[cat_name].description
        router = APIRouter(prefix=f"/api/{api_name}", tags=[cat_name])

        for cmd_name, defn in sorted(tools):
            try:
                input_schema = defn.get("input_schema", {})
                input_model = _make_input_model(cmd_name, input_schema)
                response_model = response_models.get(cmd_name)

                stripped = _strip_category_prefix(cmd_name, cat_name)
                path_name = stripped.replace("_", "-")

                handler = _make_route_handler(cmd_name, input_model)

                router.add_api_route(
                    f"/{path_name}",
                    handler,
                    methods=["POST"],
                    response_model=response_model,
                    summary=defn.get("description", cmd_name),
                    description=defn.get("description", ""),
                    operation_id=cmd_name,
                    responses={
                        422: {
                            "description": "Command error",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"error": {"type": "string"}},
                                    }
                                }
                            },
                        },
                    },
                )
            except Exception:
                logger.exception("Failed to generate API route for %s", cmd_name)

        if router.routes:
            routers.append(router)

    return routers
