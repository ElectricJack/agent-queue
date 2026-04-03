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

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, create_model

from src.api.dependencies import get_command_handler
from src.api.models import get_all_response_models
from src.cli.auto_commands import _strip_category_prefix
from src.tool_registry import (
    CATEGORIES,
    _ALL_TOOL_DEFINITIONS,
    _CLI_CATEGORY_OVERRIDES,
    _TOOL_CATEGORIES,
)

logger = logging.getLogger(__name__)

# Commands to exclude from the API entirely (internal/MCP-only).
API_EXCLUDED = {
    "browse_tools",
    "load_tools",
    "send_message",
    "reply_to_user",
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

    async def handler(body: BodyType, ch=Depends(get_command_handler)):
        result = await ch.execute(cmd_name, body.model_dump(exclude_none=True))
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

    # Group tools by category
    category_tools: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for cmd_name, defn in tool_map.items():
        if cmd_name in API_EXCLUDED:
            continue
        cat = _TOOL_CATEGORIES.get(cmd_name) or _CLI_CATEGORY_OVERRIDES.get(cmd_name)
        if cat:
            category_tools[cat].append((cmd_name, defn))

    routers: list[APIRouter] = []

    for cat_name in sorted(CATEGORIES.keys()):
        tools = category_tools.get(cat_name, [])
        if not tools:
            continue

        api_name = _category_to_api_path(cat_name)
        cat_desc = CATEGORIES[cat_name].description
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
