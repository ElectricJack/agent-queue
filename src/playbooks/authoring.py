"""Trusted Markdown input for the Playbook V2 proposal compiler.

Only frontmatter and literal backtick spans may grant an identifier to a
compiler-produced semantic body.  The inventory deliberately retains the
author's spelling: normalising an identifier here would create authority the
source did not grant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from src.playbooks.definition import SourceRef, truncate_excerpt


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        return yaml.safe_load(parts[1]) or {}, parts[2]
    except yaml.YAMLError:
        return {}, content


def _validate_frontmatter(frontmatter: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(frontmatter, dict):
        return ["Frontmatter must be a mapping"]
    if not frontmatter:
        return ["Missing YAML frontmatter (file must start with '---')"]
    if not frontmatter.get("id"):
        errors.append("Frontmatter missing required field: 'id'")
    triggers = frontmatter.get("triggers")
    if not isinstance(triggers, list) or not triggers:
        errors.append("Frontmatter 'triggers' must be a non-empty list")
    if not frontmatter.get("scope"):
        errors.append("Frontmatter missing required field: 'scope'")
    return errors + execution_source_errors(frontmatter)


_BACKTICK = re.compile(r"`([^`\n]{1,128})`")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,}).*$")


@dataclass(frozen=True)
class SourceError:
    path: Path
    errors: tuple[str, ...]


@dataclass(frozen=True)
class IdentifierInventory:
    names: Mapping[str, tuple[SourceRef, ...]]

    def contains(self, name: str) -> bool:
        if name in self.names:
            return True
        # A prose declaration of event.task permits its nested paths, and a
        # declaration of a nested path also establishes its prefixes.
        return any(
            name.startswith(candidate + ".") or candidate.startswith(name + ".")
            for candidate in self.names
        )

    def refs(self, name: str) -> tuple[SourceRef, ...]:
        return self.names.get(name, ())


@dataclass(frozen=True)
class PlaybookSource:
    vault_path: str
    raw: str
    frontmatter: dict[str, Any]
    body: str
    body_start_line: int
    inventory: IdentifierInventory

    @classmethod
    def load(cls, path: Path, *, vault_root: Path) -> "PlaybookSource | SourceError":
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return SourceError(path, (str(exc),))
        frontmatter, body = _parse_frontmatter(raw)
        errors = _validate_frontmatter(frontmatter)
        if errors:
            return SourceError(path, tuple(errors))
        try:
            vault_path = path.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            # Bundled sources are not beneath an installed vault.  Their
            # caller supplies the bundle root, so this remains relative.
            vault_path = path.name
        closing = raw.find("---", 3)
        body_start_line = raw[: closing + 3].count("\n") + 1 if closing >= 0 else 1
        return cls(
            vault_path=vault_path,
            raw=raw,
            frontmatter=frontmatter,
            body=body,
            body_start_line=body_start_line,
            inventory=_inventory(vault_path, frontmatter, body, body_start_line),
        )


def _source(path: str, line: int, text: str) -> SourceRef:
    excerpt, _ = truncate_excerpt(text.strip())
    return SourceRef(path=path, start_line=line, end_line=line, excerpt=excerpt or None)


def _add(found: dict[str, list[SourceRef]], name: Any, ref: SourceRef) -> None:
    if isinstance(name, str) and name.strip():
        found.setdefault(name.strip(), []).append(ref)


def _inventory(
    path: str, frontmatter: Mapping[str, Any], body: str, start_line: int
) -> IdentifierInventory:
    found: dict[str, list[SourceRef]] = {}
    first = _source(path, 1, "frontmatter")
    for key in ("id", "scope", "profile_id", "role"):
        _add(found, frontmatter.get(key), first)
    for trigger in (
        frontmatter.get("triggers", []) if isinstance(frontmatter.get("triggers"), list) else []
    ):
        if isinstance(trigger, str):
            _add(found, trigger, first)
        elif isinstance(trigger, Mapping):
            _add(found, trigger.get("type") or trigger.get("event_type"), first)
            for key in trigger.get("filter") or {}:
                _add(found, key, first)

    fenced = False
    for offset, text in enumerate(body.splitlines(), start=start_line):
        if _FENCE.match(text):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in _BACKTICK.finditer(text):
            name = match.group(1).strip()
            if name:
                _add(found, name, _source(path, offset, text))
    return IdentifierInventory(
        MappingProxyType({name: tuple(refs) for name, refs in found.items()})
    )


# Source declares policy, never an execution principal or resolved identity.
_SOURCE_AUTHORITY_FIELDS = frozenset({
    "principal", "execution_principal", "namespace", "namespace_id", "namespace_owner",
    "scope_owner", "owner", "_scope", "_principal", "execution_context", "trusted_execution", "required_features", "compiled_against",
    "resolved_digest", "program_sha256", "runtime_sha256", "executable_sha256",
})


def execution_source_errors(frontmatter: Mapping[str, Any]) -> list[str]:
    """Validate the same boundary for loaded sources and programmatic proposals."""
    from pydantic import ValidationError
    from src.playbooks.script_policy import ScriptProgram

    errors = [f"Source field {key!r} is server-owned" for key in
              sorted(_SOURCE_AUTHORITY_FIELDS.intersection(frontmatter))]
    if "state_schema_version" in frontmatter:
        version = frontmatter["state_schema_version"]
        if type(version) is not int or version < 1:
            errors.append("state_schema_version must be a positive integer")
    capabilities = frontmatter.get("required_capabilities", [])
    if (not isinstance(capabilities, list) or len(capabilities) > 500
            or any(not isinstance(name, str) or re.fullmatch(
                r"[a-z0-9][a-z0-9._-]{0,127}", name
            ) is None for name in capabilities)
            or len(set(capabilities)) != len(capabilities)):
        errors.append("required_capabilities must be a unique list of command names")
    programs = frontmatter.get("scripts", {})
    if not isinstance(programs, dict) or len(programs) > 500:
        errors.append("scripts must be a mapping of at most 500 step programs")
    else:
        for step_id, program in programs.items():
            if not isinstance(step_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", step_id) is None:
                errors.append("scripts keys must be step identifiers")
                continue
            try:
                ScriptProgram.model_validate(program)
            except ValidationError:
                errors.append(f"scripts.{step_id} is not a valid source program/dependency/policy manifest")
    return errors
