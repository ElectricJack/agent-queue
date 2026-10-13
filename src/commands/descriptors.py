"""Immutable command inventory shared by contracts and transport projections.

This layer knows neither CommandHandler nor MCP. Composition supplies inventories;
schemas are stored as JSON so neither callers nor their input dictionaries can
mutate a published snapshot. Transport and playbook views are deliberately distinct.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from src.commands.contracts.models import CommandContract

SchemaView = Literal["playbook", "transport"]
PLAYBOOK_DENIALS = MappingProxyType({
    "run_command": "Arbitrary host shell execution is forbidden in playbooks.",
    "shutdown": "Daemon lifecycle commands are forbidden in playbooks.",
    "restart_daemon": "Daemon lifecycle commands are forbidden in playbooks.",
    "update_and_restart": "Daemon lifecycle commands are forbidden in playbooks.",
})
_EMPTY = {"type": "object", "properties": {}}
_HELPERS = frozenset({"read_project_memory_file", "count_project_memory_files"})
_LEGACY_PLUGIN_ADAPTERS = {"git_diff": "aq-git", "memory_save": "aq-memory",
                           "memory_search": "aq-memory"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def argument_usage(method: Any) -> bool | None:
    """Unknown source is opaque, never evidence that a command is argumentless."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        parameters = set(inspect.signature(method).parameters) - {"self", "cls"}
    except (OSError, TypeError, ValueError, SyntaxError, IndentationError):
        return None
    return any(
        isinstance(node, ast.Name) and node.id in parameters and isinstance(node.ctx, ast.Load)
        for node in ast.walk(tree)
    )


@dataclass(frozen=True, slots=True)
class HandlerCommand:
    name: str
    description: str
    needs_arguments: bool | None


def discover_handler_inventory(handler: Any) -> tuple[HandlerCommand, ...]:
    """Inspect a supplied class/instance without constructing a handler."""
    commands = []
    for attr in dir(handler):
        if not attr.startswith("_cmd_"):
            continue
        method = getattr(handler, attr, None)
        if callable(method):
            name = attr[5:]
            doc = inspect.getdoc(method) or f"Execute the {name} command."
            commands.append(HandlerCommand(name, doc.split("\n")[0].strip(), argument_usage(method)))
    return tuple(commands)


@dataclass(frozen=True, slots=True)
class PluginCommand:
    name: str
    plugin: str
    available: bool = True
    category: str | None = None
    definition_json: str | None = None

    @classmethod
    def from_definition(cls, definition: dict, *, plugin: str, **kwargs: Any) -> PluginCommand:
        return cls(definition["name"], plugin, definition_json=_json(definition), **kwargs)


@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    name: str
    description: str
    origin: Literal["handler", "plugin", "helper", "declared"]
    available: bool
    plugin: str | None
    schema_source: str
    schema_quality: str
    transport_schema_source: str
    result_mode: Literal["typed", "json_object"]
    capability: str
    caller_requirements: tuple[str, ...] = ("original_principal", "resource_authorization_required")
    effect_class: str = "destructive"
    idempotency_mechanism: str = "none"
    idempotency_key_field: str | None = None
    retry_safe: bool = False
    timeout_seconds: int | None = None
    preview_available: bool = False
    sensitivity: str = "protected"
    receipt_projection: tuple[str, ...] = ()
    denial_reason: str | None = None
    audit: tuple[str, ...] = ()
    _playbook_schema: str = field(default="{}", repr=False)
    _transport_schema: str = field(default="{}", repr=False)
    _transport_definition: str = field(default="{}", repr=False)

    def input_schema(self, view: SchemaView = "playbook") -> dict[str, Any]:
        if view not in {"playbook", "transport"}:
            raise ValueError(f"Unknown schema view: {view}")
        return json.loads(self._playbook_schema if view == "playbook" else self._transport_schema)

    def tool_definition(self) -> dict[str, Any]:
        """Existing MCP/CLI/API shape, with their explicit-definition precedence."""
        return json.loads(self._transport_definition)

    def execution_metadata(self) -> dict[str, Any]:
        """Generation-2 security/execution identity, excluding inventory churn."""
        return {
            "schema_source": self.schema_source, "schema_quality": self.schema_quality,
            "result_mode": self.result_mode, "capability": self.capability,
            "caller_requirements": list(self.caller_requirements),
            "effect_class": self.effect_class, "idempotency_mechanism": self.idempotency_mechanism,
            "idempotency_key_field": self.idempotency_key_field, "retry_safe": self.retry_safe,
            "timeout_seconds": self.timeout_seconds, "preview_available": self.preview_available,
            "sensitivity": self.sensitivity, "receipt_projection": list(self.receipt_projection),
            "denial_reason": self.denial_reason,
        }


@dataclass(frozen=True, slots=True)
class DescriptorSnapshot:
    commands: Mapping[str, CommandDescriptor]
    handler_names: frozenset[str]
    explicit_names: tuple[str, ...]
    plugins: tuple[PluginCommand, ...]
    plugin_generation: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))

    def schema_quality_report(self) -> tuple[dict[str, Any], ...]:
        return tuple({
            "name": d.name, "source": d.schema_source, "quality": d.schema_quality,
            "transport_source": d.transport_schema_source, "audit": list(d.audit),
        } for d in self.commands.values())


def build_descriptor_snapshot(
    *,
    handlers: Iterable[HandlerCommand],
    explicit_definitions: Iterable[dict],
    fallback_schemas: Mapping[str, dict],
    typed_contracts: Mapping[str, CommandContract[Any, Any]],
    plugins: Iterable[PluginCommand] = (),
    plugin_generation: int = 0,
) -> DescriptorSnapshot:
    """Playbooks: typed > explicit > fallback > closed empty > opaque object.

    Existing transports keep explicit > typed > fallback > legacy empty. A
    plugin's declared definition is explicit for that plugin. No schema keyword
    is reconstructed or dropped during either projection.
    """
    from src.commands.resource_authorization import AUDITED_READS, resource_requirements

    inventory = {h.name: h for h in handlers}
    explicit = {d["name"]: d for d in explicit_definitions}
    plugins = tuple(plugins)
    plugin_map = {p.name: p for p in plugins}
    descriptors = {}
    for name in sorted(inventory.keys() | explicit.keys() | typed_contracts.keys() | plugin_map.keys()):
        handler, contract, plugin = inventory.get(name), typed_contracts.get(name), plugin_map.get(name)
        definition = explicit.get(name)
        if definition is None and plugin and plugin.definition_json:
            definition = json.loads(plugin.definition_json)
        description = handler.description if handler else f"Execute the {name} command."
        if contract:
            description = contract.presentation.summary or description
        typed_schema = contract.execution.args_model.model_json_schema() if contract else None
        explicit_schema = definition.get("input_schema", _EMPTY) if definition else None
        fallback = fallback_schemas.get(name)
        if typed_schema is not None:
            schema, source, quality = typed_schema, "typed_contract", "typed"
        elif explicit_schema is not None:
            schema, source, quality = explicit_schema, "explicit_tool", "declared"
        elif fallback is not None:
            schema, source, quality = fallback, "fallback", "declared"
        elif handler and handler.needs_arguments is False:
            schema, source, quality = dict(_EMPTY, additionalProperties=False), "argumentless", "closed"
        else:
            schema, source, quality = dict(_EMPTY, additionalProperties=True), "opaque", "opaque"
        transport = dict(definition) if definition is not None else {
            "name": name, "description": description,
            "input_schema": typed_schema if typed_schema is not None else (
                fallback if fallback is not None else _EMPTY
            ),
        }
        transport.setdefault("input_schema", _EMPTY)
        transport_source = "explicit_tool" if definition else (
            "typed_contract" if contract else "fallback" if fallback is not None else "legacy_empty"
        )
        audit = []
        if explicit_schema is not None and typed_schema is not None and explicit_schema != typed_schema:
            audit.append("explicit_transport_schema_differs_from_typed_playbook_schema")
        if quality == "opaque":
            audit.append("opaque_arguments_require_runtime_validation")
        ec = contract.execution if contract else None
        descriptors[name] = CommandDescriptor(
            name=name, description=description,
            origin="handler" if handler else "helper" if name in _HELPERS else (
                "plugin" if plugin or (contract and name in _LEGACY_PLUGIN_ADAPTERS) else "declared"
            ),
            available=bool(handler or name in _HELPERS or (plugin and plugin.available)),
            plugin=plugin.plugin if plugin else _LEGACY_PLUGIN_ADAPTERS.get(name),
            schema_source=source, schema_quality=quality, transport_schema_source=transport_source,
            result_mode="typed" if contract else "json_object",
            capability=ec.capability if ec else name,
            caller_requirements=("original_principal", *resource_requirements(name)),
            # Existing typed annotations are trusted evidence, never a name heuristic.
            effect_class=("read" if (ec and ec.side_effect.value == "read")
                          or (not ec and name in AUDITED_READS) else "destructive"),
            idempotency_mechanism=ec.idempotency.mode if ec else "none",
            idempotency_key_field=ec.idempotency.key_field if ec else None,
            retry_safe=ec.retry_safe if ec else False,
            timeout_seconds=ec.timeout_seconds if ec else None,
            preview_available=ec.supports_preview if ec else False,
            receipt_projection=ec.receipt_projection if ec else (),
            denial_reason=PLAYBOOK_DENIALS.get(name), audit=tuple(audit),
            _playbook_schema=_json(schema), _transport_schema=_json(transport["input_schema"]),
            _transport_definition=_json(transport),
        )
    return DescriptorSnapshot(
        descriptors, frozenset(inventory), tuple(explicit), plugins, plugin_generation,
    )
