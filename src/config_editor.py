"""Read/edit support for the YAML config used by the dashboard, API, and CLI.

The runtime ``load_config`` in :mod:`src.config` resolves ``${ENV_VAR}``
references eagerly and produces a typed :class:`AppConfig` for the daemon.
Editing tooling needs the *opposite*: the raw YAML as written on disk, with
placeholders preserved, plus a JSON schema describing what fields exist and
how they're typed.

This module owns those two read-only concerns.  The companion writer (step 2)
will live alongside.
"""

from __future__ import annotations

import dataclasses
import os
import re
import types
import typing
from typing import Any, get_args, get_origin

import yaml

from src.config import (
    HOT_RELOADABLE_SECTIONS,
    RESTART_REQUIRED_SECTIONS,
    AppConfig,
    config_section_names,
)

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")

# Free-text notes surfaced on top-level schema sections as ``x-note``.
# Used by the dashboard/CLI to explain why a section exists or is paused.
# Keep entries short — one sentence, present tense.
SECTION_NOTES: dict[str, str] = {
    "memory": "Temporary — overhaul pause; see docs/specs/design/feature-pauses.md.",
    "playbooks": "Temporary — overhaul pause; see docs/specs/design/feature-pauses.md.",
    "sessions": "Session runtime: the only path a task runs on. Off dispatches nothing.",
    "worktrees": "Framework overhaul: per-slot git worktrees. Disabled until the lane lands.",
    "security": "Framework overhaul: env scrubbing + doctor thresholds.",
    "pricing": "Framework overhaul: model price table for token-ledger cost rollups.",
    "messages": "Framework overhaul: inter-agent message queue. Disabled until the lane lands.",
    "supervisor_agent": "Framework overhaul: supervisor-as-a-session. Disabled until the lane lands.",
    "api_auth": "Framework overhaul: session-token auth for the local HTTP API.",
    "surface": "Framework overhaul: agent-surface ergonomics knobs.",
    "state_machine": "Framework overhaul: task state-machine enforcement (warn-only while off).",
    "work_graph": "Framework overhaul: blocked-state projection, gates, typed edges.",
    "swarm": "Framework overhaul: pull-based worker pools (claims, pool reconciliation).",
}

# Individual flags that gate a whole subsystem at construction time.  Keys are
# dotted paths into the schema; each entry annotates the field with
# ``restart_required`` and a human-readable ``description`` so the
# dashboard/CLI can render the pause banner without hard-coding the list.
# See docs/specs/implementation/feature-pauses.md §2.5.
FLAG_NOTES: dict[str, str] = {
    "memory.enabled": (
        "Temporary — overhaul pause. When false the aq-memory plugin is not "
        "loaded, L1/L2 prompt tiers stay empty and reflection is forced off. "
        "Data is preserved. See docs/specs/design/feature-pauses.md."
    ),
    "playbooks.enabled": (
        "When false the V2 playbook runtime, TimerService, resume handlers, "
        "and workflow recovery are not started. V2 artifacts and run data are preserved. "
        "See docs/specs/design/feature-pauses.md."
    ),
}


def _round_trip_yaml():
    """Lazy import of ruamel.yaml configured for round-trip editing.

    Imported lazily so the read path doesn't pull in ruamel.yaml at
    module-load time (the runtime daemon never edits config).
    """
    from ruamel.yaml import YAML

    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    # ruamel wraps at 80 columns by default, which folds a long value — a
    # database DSN, a URL — onto a continuation line.  That is valid YAML and
    # round-trips, but it rewrites lines the writer never touched and is easy
    # for a human editing the file afterwards to break.
    yaml_rt.width = 4096
    return yaml_rt


# ruamel's round-trip emitter follows YAML 1.2, in which ``off`` is a plain
# string, so it writes the Python string "off" bare.  Every reader here is
# PyYAML, which follows YAML 1.1 and resolves bare ``off``/``on``/``yes``/
# ``no`` (and sexagesimals like ``1:30``, octals like ``012``) as non-strings.
# Values in that gap have to be quoted on write or they change type on the
# next read.  The check asks PyYAML's own resolver rather than carrying a word
# list, so it stays exactly as wide as the reader it is protecting.
_PYYAML_RESOLVER = yaml.resolver.Resolver()


def _reads_back_as_non_string(value: str) -> bool:
    """True when PyYAML would resolve *value*, written bare, as a non-string."""
    try:
        tag = _PYYAML_RESOLVER.resolve(yaml.ScalarNode, value, (True, False))
    except Exception:
        # Unresolvable is the conservative case: quote it.
        return True
    return tag != "tag:yaml.org,2002:str"


def _quote_yaml11_scalars(value: Any) -> Any:
    """Force quoting on every string a PyYAML reader would not read back as one.

    Applied to data on its way into the round-trip writer.  Quoting is always
    safe for a string, so the check only has to be conservative in one
    direction.
    """
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.scalarstring import ScalarString, SingleQuotedScalarString

    if isinstance(value, ScalarString):
        # An explicit style is already attached; leave the caller's choice alone.
        return value
    if isinstance(value, str):
        return SingleQuotedScalarString(value) if _reads_back_as_non_string(value) else value
    if isinstance(value, CommentedMap | CommentedSeq):
        # These carry comment attachments keyed by position, so fix the scalars
        # in place rather than rebuilding the container.
        items = value.items() if isinstance(value, CommentedMap) else enumerate(value)
        for key, item in list(items):
            value[key] = _quote_yaml11_scalars(item)
        return value
    if isinstance(value, dict):
        return {_quote_yaml11_scalars(k): _quote_yaml11_scalars(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_quote_yaml11_scalars(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Raw YAML reader (preserves env placeholders)
# ---------------------------------------------------------------------------


def read_raw_config(path: str) -> dict[str, Any]:
    """Read the YAML config file *without* env-var substitution.

    Returns the parsed dict exactly as written on disk so that
    ``${ENV_VAR}`` references survive round-trip into the editor UI.
    Returns an empty dict if the file is empty.
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def find_env_var_refs(raw: Any, _path: str = "") -> list[dict[str, Any]]:
    """Walk a raw config dict and report every ``${ENV_VAR}`` reference.

    Each entry is ``{"path": "discord.bot_token", "var": "DISCORD_BOT_TOKEN",
    "resolved": True}``.  ``resolved`` is True when the env var is set at the
    time of the call — useful for the UI to flag broken references.
    """
    refs: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            sub = f"{_path}.{key}" if _path else str(key)
            refs.extend(find_env_var_refs(value, sub))
    elif isinstance(raw, list):
        for idx, value in enumerate(raw):
            sub = f"{_path}[{idx}]"
            refs.extend(find_env_var_refs(value, sub))
    elif isinstance(raw, str):
        for match in _ENV_VAR_RE.finditer(raw):
            var = match.group(1)
            refs.append(
                {
                    "path": _path,
                    "var": var,
                    "resolved": var in os.environ,
                }
            )
    return refs


# ---------------------------------------------------------------------------
# Section classification
# ---------------------------------------------------------------------------


def _top_level_sections() -> list[str]:
    """Names of every top-level config key (matching AppConfig fields)."""
    return list(config_section_names())


def classify_sections() -> dict[str, list[str]]:
    """Return ``{"hot_reloadable": [...], "restart_required": [...], "other": [...]}``.

    A section is "other" when it isn't classified explicitly — the safe
    default is to treat it as restart-required, which the UI does, but we
    surface the unclassified set so it's auditable.
    """
    sections = _top_level_sections()
    hot, restart, other = [], [], []
    for s in sections:
        if s in HOT_RELOADABLE_SECTIONS:
            hot.append(s)
        elif s in RESTART_REQUIRED_SECTIONS:
            restart.append(s)
        else:
            other.append(s)
    return {
        "hot_reloadable": sorted(hot),
        "restart_required": sorted(restart),
        "other": sorted(other),
    }


# ---------------------------------------------------------------------------
# JSON schema generation from the AppConfig dataclass tree
# ---------------------------------------------------------------------------


def _is_optional(tp: Any) -> tuple[bool, Any]:
    """If ``tp`` is ``X | None`` / ``Optional[X]``, return ``(True, X)``."""
    origin = get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return True, args[0]
    return False, tp


def _type_to_schema(tp: Any) -> dict[str, Any]:
    """Map a Python type annotation to a JSON Schema fragment."""
    optional, inner = _is_optional(tp)
    schema = _type_to_schema_inner(inner)
    if optional:
        # Express optionality as nullable rather than oneOf for simpler form rendering.
        existing = schema.get("type")
        if isinstance(existing, str):
            schema["type"] = [existing, "null"]
        else:
            schema["nullable"] = True
    return schema


def _type_to_schema_inner(tp: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(tp):
        return _dataclass_to_schema(tp)
    if tp is str:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    origin = get_origin(tp)
    if origin in (list, typing.List):  # noqa: UP006
        (item_tp,) = get_args(tp) or (Any,)
        return {"type": "array", "items": _type_to_schema(item_tp)}
    if origin in (dict, typing.Dict):  # noqa: UP006
        args = get_args(tp)
        value_tp = args[1] if len(args) == 2 else Any
        return {
            "type": "object",
            "additionalProperties": _type_to_schema(value_tp),
        }
    # Fallback for Any / unknown.
    return {}


def _dataclass_to_schema(cls: type) -> dict[str, Any]:
    # ``from __future__ import annotations`` is in effect across this codebase,
    # so dataclass field types are strings — resolve them once via get_type_hints.
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        hints = {}
    properties: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name.startswith("_"):
            continue
        tp = hints.get(f.name, f.type)
        prop = _type_to_schema(tp)
        prop.update(f.metadata.get("json_schema", {}))
        default = _field_default(f)
        if default is not dataclasses.MISSING:
            prop["default"] = default
        properties[f.name] = prop
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }


def _field_default(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        try:
            value = f.default_factory()  # type: ignore[misc]
        except Exception:
            return dataclasses.MISSING
        if dataclasses.is_dataclass(value):
            return dataclasses.asdict(value)
        return value
    return dataclasses.MISSING


def build_config_schema() -> dict[str, Any]:
    """Return a JSON Schema document describing the full AppConfig tree.

    Top-level properties are annotated with ``x-reload`` set to
    ``"hot"`` / ``"restart"`` / ``"unclassified"`` so the dashboard can
    render the correct badge without a second lookup.
    """
    schema = _dataclass_to_schema(AppConfig)
    classification = classify_sections()
    reload_by_section: dict[str, str] = {}
    for s in classification["hot_reloadable"]:
        reload_by_section[s] = "hot"
    for s in classification["restart_required"]:
        reload_by_section[s] = "restart"
    for s in classification["other"]:
        reload_by_section[s] = "unclassified"
    for name, prop in schema["properties"].items():
        prop["x-reload"] = reload_by_section.get(name, "unclassified")
        note = SECTION_NOTES.get(name)
        if note:
            prop["x-note"] = note

    # Annotate the individual subsystem-pause flags (feature-pauses §2.5).
    for dotted, description in FLAG_NOTES.items():
        node: Any = schema
        for part in dotted.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get("properties", {}).get(part)
        if isinstance(node, dict):
            node["restart_required"] = True
            node["description"] = description

    schema["x-classification"] = classification
    return schema


# ---------------------------------------------------------------------------
# Round-trip writer
# ---------------------------------------------------------------------------


def write_section(path: str, section: str, new_data: Any) -> None:
    """Replace ``section`` in the YAML at ``path`` with ``new_data`` in place.

    Uses ruamel.yaml round-trip mode, so:
      - Comments, key order, and quoting style outside the touched section
        are preserved byte-for-byte.
      - ``${ENV_VAR}`` placeholders are written verbatim if the caller passed
        them back unchanged (the read layer never resolved them, so the
        dashboard round-trips them naturally).

    If ``new_data`` is ``None`` the section is deleted.

    Strings that a YAML 1.1 reader would resolve as something else (``off``,
    ``yes``, ``n``, ``1:30``, ...) are quoted, so the section reads back with
    the types the caller wrote.  Untouched sections keep whatever the file
    already said.

    The caller is responsible for validation; ``write_section`` is the dumb
    persistence layer.
    """
    yaml_rt = _round_trip_yaml()
    with open(path, encoding="utf-8") as f:
        doc = yaml_rt.load(f) or {}

    if new_data is None:
        if section in doc:
            del doc[section]
    else:
        doc[section] = _quote_yaml11_scalars(new_data)

    with open(path, "w", encoding="utf-8") as f:
        yaml_rt.dump(doc, f)


def write_full_config(path: str, new_data: dict[str, Any]) -> None:
    """Replace the entire config document at ``path``.

    Used by ``aq system config edit`` after the user closes their editor.
    Comments inside replaced sections are NOT preserved (the user
    presumably saw them while editing).  Strings are quoted on the same terms
    as :func:`write_section`.
    """
    yaml_rt = _round_trip_yaml()
    with open(path, "w", encoding="utf-8") as f:
        yaml_rt.dump(_quote_yaml11_scalars(new_data), f)
