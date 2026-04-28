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
)

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _round_trip_yaml():
    """Lazy import of ruamel.yaml configured for round-trip editing.

    Imported lazily so the read path doesn't pull in ruamel.yaml at
    module-load time (the runtime daemon never edits config).
    """
    from ruamel.yaml import YAML

    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    return yaml_rt


# ---------------------------------------------------------------------------
# Raw YAML reader (preserves env placeholders)
# ---------------------------------------------------------------------------


def read_raw_config(path: str) -> dict[str, Any]:
    """Read the YAML config file *without* env-var substitution.

    Returns the parsed dict exactly as written on disk so that
    ``${ENV_VAR}`` references survive round-trip into the editor UI.
    Returns an empty dict if the file is empty.
    """
    with open(path) as f:
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
    return [f.name for f in dataclasses.fields(AppConfig) if not f.name.startswith("_")]


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
        default = _field_default(f)
        if default is not dataclasses.MISSING:
            prop["default"] = default
        properties[f.name] = prop
    return {"type": "object", "properties": properties}


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

    The caller is responsible for validation; ``write_section`` is the dumb
    persistence layer.
    """
    yaml_rt = _round_trip_yaml()
    with open(path) as f:
        doc = yaml_rt.load(f) or {}

    if new_data is None:
        if section in doc:
            del doc[section]
    else:
        doc[section] = new_data

    with open(path, "w") as f:
        yaml_rt.dump(doc, f)


def write_full_config(path: str, new_data: dict[str, Any]) -> None:
    """Replace the entire config document at ``path``.

    Used by ``aq system config edit`` after the user closes their editor.
    Comments inside replaced sections are NOT preserved (the user
    presumably saw them while editing).
    """
    yaml_rt = _round_trip_yaml()
    with open(path, "w") as f:
        yaml_rt.dump(new_data, f)
