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
    from src.commands.command_inventory import command_snapshot
    from src.tools.definitions import (
        _CLI_CATEGORY_OVERRIDES,
        _TOOL_CATEGORIES,
    )
    from src.tools.registry import CATEGORIES

    snapshot = command_snapshot()
    explicit = set(snapshot.explicit_names)
    fallback = {
        name: snapshot.commands[name].tool_definition()
        for name in sorted(snapshot.handler_names) if name not in explicit
    }
    internal: dict[str, list[dict]] = {}
    for plugin in snapshot.plugins:
        if plugin.category and plugin.definition_json:
            internal.setdefault(plugin.category, []).append(json.loads(plugin.definition_json))
    # Preserve each model's field order: Click renders options in schema order.
    catalogue = {
        "explicit": [snapshot.commands[name].tool_definition() for name in snapshot.explicit_names],
        "fallback": fallback,
        "categories": {name: meta.description for name, meta in CATEGORIES.items()},
        "tool_categories": _TOOL_CATEGORIES,
        "cli_category_overrides": _CLI_CATEGORY_OVERRIDES,
        "internal": list(internal.items()),
    }
    return json.dumps(catalogue, indent=2) + "\n"
