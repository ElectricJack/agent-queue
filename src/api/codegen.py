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
from src.api.models.system import EditIntelligenceClassConflictResponse
from src.api.scope import check_request_scope
from src.commands.principal import SERVER_OWNED_ARG_KEYS
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
# Response serialization overrides
# ---------------------------------------------------------------------------
#
# Commands whose responses model "absent" as an omitted key rather than an
# explicit null.  ``playbook_graph_view`` returns compiled node details
# straight from ``PlaybookNode.to_dict()``, where a key is present only when
# the compiler set it; serializing the unset fields as ``null`` would fill the
# dashboard's node inspector with empty rows (design spec §3.4).
RESPONSE_EXCLUDE_NONE: frozenset[str] = frozenset({"playbook_graph_view"})


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
        if cmd_name == "task_set":
            # Explicit null is invalid, not omission. Preserve it so the
            # command rejects before writing any accompanying legacy field.
            for field in {"description", "expected_description"} & body.model_fields_set:
                args[field] = getattr(body, field)
        if cmd_name == "pool_scale":
            # ``max: null`` means an unbounded pool.  Normal generated-route
            # serialization drops nulls, which would turn that explicit
            # operator request into a no-op.
            for field in {"min", "max"} & body.model_fields_set:
                args[field] = getattr(body, field)
        if cmd_name == "edit_task":
            # Explicit null clears routing; omitted option defaults must not.
            for field in {"profile_id", "intelligence_class"} & body.model_fields_set:
                if getattr(body, field) is None:
                    args[field] = None
        for _key in SERVER_OWNED_ARG_KEYS:
            args.pop(_key, None)

        scope: RequestScope = (
            getattr(request.state, "scope", LOCAL_SCOPE) if request is not None else LOCAL_SCOPE
        )
        scope_err = await check_request_scope(
            cmd_name, args, scope, db=getattr(ch, "db", None),
        )
        if scope_err is not None:
            return JSONResponse({"error": scope_err}, status_code=403)

        # Forward the server-derived scope so surface commands can resolve
        # ``task_id``/``project_id``/``session_id`` without an explicit arg.
        args["_scope"] = {
            "kind": scope.kind,
            "session_id": scope.session_id,
            "task_id": scope.task_id,
            "project_id": scope.project_id,
            "elevated": scope.elevated,
        }

        result = await ch.execute(cmd_name, args)
        if cmd_name == "edit_intelligence_class" and result.get("error_code") == "revision_conflict":
            return JSONResponse(
                {"error": result["error"], "error_code": "revision_conflict",
                 "current_revision": result["current_revision"]},
                status_code=409,
            )
        if "error" in result:
            if result.get("error_code") == "capability_denied":
                return JSONResponse(
                    {"error": result["error"], "error_code": "capability_denied"},
                    status_code=403,
                )
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

    # ``_discover_all_commands`` already stamps ``_FALLBACK_INPUT_SCHEMAS``
    # onto the commands that have no ``_ALL_TOOL_DEFINITIONS`` entry, so every
    # definition in ``tool_map`` carries the real properties by the time it
    # gets here (src/tools/definitions.py).  Plugin definitions ship their own.

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
                    response_model_exclude_none=cmd_name in RESPONSE_EXCLUDE_NONE,
                    summary=defn.get("description", cmd_name),
                    description=defn.get("description", ""),
                    operation_id=cmd_name,
                    responses={
                        **({409: {
                            "description": "Intelligence class changed since it was loaded",
                            "model": EditIntelligenceClassConflictResponse,
                        }} if cmd_name == "edit_intelligence_class" else {}),
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
