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

from src.playbooks.compiler import PlaybookCompiler
from src.playbooks.definition import SourceRef, truncate_excerpt

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
        frontmatter, body = PlaybookCompiler._parse_frontmatter(raw)
        errors = PlaybookCompiler._validate_frontmatter(frontmatter)
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
