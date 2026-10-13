"""Composition root for offline and live command descriptor snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.commands.descriptors import (
    DescriptorSnapshot,
    HandlerCommand,
    PluginCommand,
    build_descriptor_snapshot,
    discover_handler_inventory,
)


def compose_snapshot(
    handler: Any, typed_contracts: Mapping, *, plugin_registry: Any = None,
) -> DescriptorSnapshot:
    from src.plugins.internal import collect_internal_tool_definitions
    from src.plugins.registry import PluginRegistry
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _FALLBACK_INPUT_SCHEMAS

    # Declared inventory is useful offline; it is not evidence of a loaded plugin.
    plugins = {
        definition["name"]: PluginCommand.from_definition(
            definition, plugin=f"internal:{category}", category=category, available=False,
        )
        for category, definitions in collect_internal_tool_definitions()
        for definition in definitions
    }
    generation = 0
    if isinstance(plugin_registry, PluginRegistry):
        generation = plugin_registry.command_generation
        plugins.update({p.name: p for p in plugin_registry.command_inventory()})
    return build_descriptor_snapshot(
        handlers=discover_handler_inventory(handler),
        explicit_definitions=_ALL_TOOL_DEFINITIONS,
        fallback_schemas=_FALLBACK_INPUT_SCHEMAS,
        typed_contracts=typed_contracts,
        plugins=plugins.values(), plugin_generation=generation,
    )


def offline_snapshot(typed_contracts: Mapping) -> DescriptorSnapshot:
    # Only composition imports the fully defined handler; the registry never
    # discovers through MCP, and descriptors never import their consumer.
    from src.commands.handler import CommandHandler

    return compose_snapshot(CommandHandler, typed_contracts)


def command_snapshot(*, plugin_tools: list[dict] | None = None) -> DescriptorSnapshot:
    """The current registry-owned snapshot, shared by every projection."""
    from src.commands.contracts.registry import CONTRACTS

    snapshot = CONTRACTS.descriptors
    if not plugin_tools:
        return snapshot
    # An embedding transport can supply declared tools before a live handler is
    # installed. Merge those inputs through the same descriptor builder.
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _FALLBACK_INPUT_SCHEMAS

    plugins = {p.name: p for p in snapshot.plugins}
    for definition in plugin_tools:
        if definition.get("name"):
            plugins[definition["name"]] = PluginCommand.from_definition(
                definition, plugin=definition.get("_plugin", "declared"), available=False,
            )
    return build_descriptor_snapshot(
        handlers=[
            HandlerCommand(name, snapshot.commands[name].description,
                           snapshot.commands[name].schema_source != "argumentless")
            for name in sorted(snapshot.handler_names)
        ],
        explicit_definitions=_ALL_TOOL_DEFINITIONS, fallback_schemas=_FALLBACK_INPUT_SCHEMAS,
        typed_contracts=CONTRACTS.typed_contracts(), plugins=plugins.values(),
        plugin_generation=snapshot.plugin_generation,
    )
