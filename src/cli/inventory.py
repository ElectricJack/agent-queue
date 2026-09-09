"""Deterministic inventory and conformance metadata for the ``aq`` CLI.

The live Click tree is the source of truth.  This module deliberately does
not carry a frozen command count: it walks the registered tree, records how
each leaf was registered and validates that every advertised generated
operation has either a core handler, a named plugin provider, or an explicit
unsupported classification.
"""

from __future__ import annotations

import inspect
import re
from collections import Counter
from typing import Any

import click

INVENTORY_SCHEMA_VERSION = 1

# Compatibility spellings are explicit API surface, not coincidences inferred
# from callback identity.  ``inbox`` intentionally has hook-safe error
# semantics while sharing the message-inbox operation.
SUPPORTED_ALIASES: dict[str, str] = {
    "aq inbox": "aq message inbox",
    "aq reply": "aq message reply",
    "aq task details": "aq task show",
}

DEPRECATED_COMMANDS: dict[str, str] = {
    "aq plugin logs": "Plugin hook logs were removed; use `aq playbook list`.",
}

# These definitions are intentionally advertised even though their provider is
# not a CommandHandler._cmd_* method.  Keeping the provider explicit prevents
# the old inventory's false "missing handler" findings.
EXTERNAL_PLUGIN_COMMANDS: dict[str, str] = {
    "memory_save": "aq-memory",
    "memory_search": "aq-memory",
}

# A newly advertised generated command without an implementation fails
# inventory validation unless it is classified here.  Entries must remain
# advertised and handler-less; validation rejects stale exceptions.
EXPLICITLY_UNSUPPORTED: dict[str, str] = {}

# These paths have focused payload/output/exit tests, rather than registration
# or schema-dispatch evidence alone.  This is intentionally a small,
# representative ledger; adding a behavioral test can promote a path here.
BEHAVIORAL_EVIDENCE: frozenset[str] = frozenset(
    {
        "aq inbox",
        "aq message reply",
        "aq plugin logs",
        "aq reply",
        "aq task details",
        "aq task list",
        "aq test",
    }
)


def _walk_leaves(group: click.Group, prefix: tuple[str, ...] = ("aq",)):
    """Yield ``(path, command)`` for every leaf in stable lexical order."""
    ctx = click.Context(group, info_name=" ".join(prefix))
    for name in sorted(group.list_commands(ctx)):
        command = group.get_command(ctx, name)
        if command is None or command.hidden:
            continue
        path = (*prefix, name)
        if isinstance(command, click.Group):
            yield from _walk_leaves(command, path)
        else:
            yield " ".join(path), command


def _callback_identity(command: click.Command) -> tuple[str, str]:
    callback = command.callback
    return (
        getattr(callback, "__module__", "") if callback else "",
        getattr(callback, "__name__", "") if callback else "",
    )


def _infer_handwritten_backend(command: click.Command) -> str | None:
    """Infer a literal ``client.execute('name', ...)`` from a callback.

    This is evidence, not dispatch machinery.  Helpers and composite commands
    legitimately return ``None`` rather than being assigned a guessed backend.
    """
    callback = command.callback
    if callback is None:
        return None
    try:
        source = inspect.getsource(callback)
    except (OSError, TypeError):
        return None
    names = set(re.findall(r"\.execute\(\s*[\"']([a-z0-9_]+)[\"']", source))
    return next(iter(names)) if len(names) == 1 else None


def _parameter_label(param: click.Parameter) -> str:
    if isinstance(param, click.Option) and param.opts:
        return max(param.opts, key=len)
    return param.name or ""


def _parameter_contract(command: click.Command) -> dict[str, list[str]]:
    """Compact option-shape summary; dispatch tests exercise the full types."""
    from .auto_commands import NullableParam, StructuredParam

    return {
        "required": [_parameter_label(param) for param in command.params if param.required],
        "optional": [_parameter_label(param) for param in command.params if not param.required],
        "structured": [
            _parameter_label(param)
            for param in command.params
            if isinstance(param.type, StructuredParam)
        ],
        "nullable": [
            _parameter_label(param)
            for param in command.params
            if isinstance(param.type, NullableParam)
        ],
    }


def _counter(values) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def build_cli_inventory(
    cli_group: click.Group,
    *,
    include_external_extensions: bool = False,
    validate_surface_ledgers: bool = True,
) -> dict[str, Any]:
    """Build and validate a deterministic inventory of the live Click tree."""
    from src.commands.handler import CommandHandler

    records: list[dict[str, Any]] = []
    for path, command in _walk_leaves(cli_group):
        module, callback_name = _callback_identity(command)
        registration = getattr(command, "_aq_registration", "handwritten")
        owner_kind = getattr(command, "_aq_owner_kind", "core")
        owner = getattr(command, "_aq_owner", "agent-queue")
        if owner_kind == "external-plugin" and not include_external_extensions:
            continue

        backend = getattr(command, "_aq_backend_command", None)
        if backend is None and registration == "handwritten":
            backend = _infer_handwritten_backend(command)

        # memory_* is runtime-provided by aq-memory.  Its schema lives in core
        # so the command exists even when the optional plugin is absent.
        if backend in EXTERNAL_PLUGIN_COMMANDS:
            owner_kind = "external-plugin"
            owner = EXTERNAL_PLUGIN_COMMANDS[backend]

        alias_for = SUPPORTED_ALIASES.get(path)
        deprecation = DEPRECATED_COMMANDS.get(path)
        unsupported = EXPLICITLY_UNSUPPORTED.get(backend or "")
        support = "unsupported" if unsupported else "deprecated" if deprecation else "supported"

        evidence = ["click-registration"]
        if registration == "generated":
            evidence.extend(["json-schema", "mock-dispatch"])
        if backend and hasattr(CommandHandler, f"_cmd_{backend}"):
            evidence.append("core-handler")
        elif owner_kind in {"internal-plugin", "external-plugin"}:
            evidence.append("plugin-provider")
        elif unsupported:
            evidence.append("explicit-unsupported")
        if alias_for:
            evidence.append("documented-alias")
        if deprecation:
            evidence.append("documented-deprecation")

        evidence_level = (
            "behavioral"
            if path in BEHAVIORAL_EVIDENCE
            else "dispatch"
            if registration == "generated"
            else "registration"
        )
        records.append(
            {
                "path": path,
                "registration": registration,
                "owner_kind": owner_kind,
                "owner": owner,
                "backend_command": backend,
                "callback": f"{module}.{callback_name}" if module else callback_name,
                "support": support,
                "alias_for": alias_for,
                "deprecation": deprecation,
                "unsupported_reason": unsupported,
                "evidence_level": evidence_level,
                "evidence": evidence,
                "parameters": _parameter_contract(command),
            }
        )

    _validate_inventory(
        records,
        CommandHandler,
        validate_surface_ledgers=validate_surface_ledgers,
    )
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "generated_by": "scripts/generate-cli-command-inventory.py",
        "counts": {
            "leaf_commands": len(records),
            "registration": _counter(row["registration"] for row in records),
            "ownership": _counter(row["owner_kind"] for row in records),
            "support": _counter(row["support"] for row in records),
            "evidence_level": _counter(row["evidence_level"] for row in records),
        },
        "commands": records,
    }


def _validate_inventory(
    records: list[dict[str, Any]],
    handler_type: type,
    *,
    validate_surface_ledgers: bool,
) -> None:
    paths = {row["path"] for row in records}
    if len(paths) != len(records):
        raise ValueError("CLI inventory contains duplicate leaf paths")

    if validate_surface_ledgers:
        bad_aliases = {
            path: target
            for path, target in SUPPORTED_ALIASES.items()
            if path not in paths or target not in paths
        }
        if bad_aliases:
            raise ValueError(f"stale CLI alias classifications: {bad_aliases}")

    advertised_backends = {row["backend_command"] for row in records}
    errors: list[str] = []
    for row in records:
        backend = row["backend_command"]
        if row["registration"] != "generated" or not backend:
            continue
        has_handler = hasattr(handler_type, f"_cmd_{backend}")
        plugin_owned = row["owner_kind"] in {"internal-plugin", "external-plugin"}
        classified = backend in EXPLICITLY_UNSUPPORTED
        if not has_handler and not plugin_owned and not classified:
            errors.append(f"{row['path']} -> {backend}: no handler or plugin provider")

    if validate_surface_ledgers:
        stale_unsupported = [
            name
            for name in EXPLICITLY_UNSUPPORTED
            if name not in advertised_backends or hasattr(handler_type, f"_cmd_{name}")
        ]
        if stale_unsupported:
            errors.append(f"stale explicitly unsupported commands: {', '.join(stale_unsupported)}")
    if errors:
        raise ValueError("invalid advertised CLI operations:\n  " + "\n  ".join(errors))
