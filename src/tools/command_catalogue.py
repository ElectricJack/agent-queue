"""Packaged CLI definitions, generated from live server discovery."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CATALOGUE_PATH = Path(__file__).with_name("command_catalogue.json")


@lru_cache(maxsize=1)
def load_command_catalogue() -> dict:
    """Read schemas without importing the daemon's handler or contract registry."""
    return json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))


def render_command_catalogue() -> str:
    """Build the artifact; used only by generation and drift checks."""
    from src.mcp_registration import _discover_all_commands
    from src.plugins.internal import collect_internal_tool_definitions
    from src.tools.definitions import (
        _ALL_TOOL_DEFINITIONS,
        _CLI_CATEGORY_OVERRIDES,
        _TOOL_CATEGORIES,
    )
    from src.tools.registry import CATEGORIES

    explicit = {definition["name"] for definition in _ALL_TOOL_DEFINITIONS}
    fallback = {
        name: definition
        for name, definition in _discover_all_commands().items()
        if name not in explicit
    }
    # Preserve each model's field order: Click renders options in schema order.
    catalogue = {
        "explicit": _ALL_TOOL_DEFINITIONS,
        "fallback": fallback,
        "categories": {name: meta.description for name, meta in CATEGORIES.items()},
        "tool_categories": _TOOL_CATEGORIES,
        "cli_category_overrides": _CLI_CATEGORY_OVERRIDES,
        "internal": collect_internal_tool_definitions(),
    }
    return json.dumps(catalogue, indent=2) + "\n"
