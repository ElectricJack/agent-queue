"""One-shot retirement of the intelligence classes this release stopped shipping.

The shipped ladder used to be ``{fast,standard,deep}`` x ``{off,low,medium,high}``
— twelve classes, most of which differed from a sibling only in how much
reasoning they bought.  It is now seven: ``fast-{low,high}``,
``standard-high``, ``deep-{low,high}`` and ``astra-{low,high}``.

Seeding is write-if-absent, so an install that already ran keeps every retired
file and every profile still pointing at one.  Two things therefore have to
happen on an existing vault, and both are done here:

* **Profiles are repointed.**  A profile whose ``default_class`` names a
  retired class would resolve no model at launch, so it is rewritten to the
  surviving class in the same tier (see :data:`RETIRED_CLASS_REPLACEMENTS`).
* **The retired files are moved aside**, not deleted: ``standard-medium.md``
  becomes ``standard-medium.md.retired``, keeping the operator's bytes (and
  any edits) recoverable with a rename.  A file marked ``customized: true`` is
  an explicit operator asset and is left exactly where it is.

Both halves are idempotent.  The retirement half also records the ids it has
processed in ``vault/intelligence-classes/.retired-classes`` so that a class an
operator deliberately re-authors under a retired id is moved aside once, not on
every daemon start.

Tasks, agents and routes that are *pinned* to a retired class live in the
database, not the vault; alembic revision ``a0000000000f`` repoints those.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_BLOCK = re.compile(r"(## Config\s*\n```json\s*\n)(.*?)(\n```)", re.DOTALL)

#: Retired class id -> the surviving class a reference to it becomes.  The
#: replacement stays inside the original tier so a repointed profile keeps its
#: cost bracket; only the reasoning level moves, because a level is all these
#: classes ever differed by.
RETIRED_CLASS_REPLACEMENTS: dict[str, str] = {
    "fast-off": "fast-low",
    "fast-medium": "fast-high",
    "standard-off": "standard-high",
    "standard-low": "standard-high",
    "standard-medium": "standard-high",
    "deep-off": "deep-low",
    "deep-medium": "deep-high",
}

#: Basename of the record of ids already moved aside.  Dot-prefixed so the
#: class loader's ``*.md`` scan and Obsidian both pass over it.
RETIRED_CLASSES_FILENAME = ".retired-classes"

RETIRED_SUFFIX = ".retired"


@dataclass(frozen=True)
class ClassRetirementResult:
    """What one vault pass changed, for logging and tests."""

    repointed_profiles: tuple[tuple[str, str, str], ...] = ()  # (path, from, to)
    retired_files: tuple[str, ...] = ()
    kept_customized: tuple[str, ...] = ()


def _classes_root(data_dir: str | Path) -> Path:
    return Path(data_dir) / "vault" / "intelligence-classes"


def _load_processed(root: Path) -> set[str]:
    try:
        payload = json.loads((root / RETIRED_CLASSES_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    retired = payload.get("retired") if isinstance(payload, dict) else None
    return {str(value) for value in retired} if isinstance(retired, list) else set()


def _save_processed(root: Path, ids: set[str]) -> None:
    try:
        (root / RETIRED_CLASSES_FILENAME).write_text(
            json.dumps({"version": 1, "retired": sorted(ids)}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Could not record retired intelligence classes under %s", root)


def _is_customized(path: Path) -> bool:
    """True when the file's frontmatter opts out of default management.

    Read with the same shallow scan the other vault migrations use: a full
    parse would pull the class loader into a path that runs before it.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            head = handle.read(4096)
    except OSError:
        return True  # Unreadable: never touch it.
    if not head.startswith("---"):
        return False
    for line in head.split("\n")[1:]:
        if line.strip() == "---":
            break
        key, _sep, value = line.partition(":")
        if key.strip() == "customized":
            return value.strip().strip("\"'").lower() == "true"
    return False


def repoint_vault_profile_classes(vault_root: str | Path) -> list[tuple[str, str, str]]:
    """Rewrite every profile ``default_class`` that names a retired class.

    Returns one ``(path, from, to)`` per rewritten file.  A profile whose class
    still exists is untouched, so re-running this is a no-op.
    """
    changed: list[tuple[str, str, str]] = []
    for path in Path(vault_root).glob("**/agent-types/**/profile.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            logger.warning("Could not read profile %s for class retirement", path)
            continue
        match = _CONFIG_BLOCK.search(text)
        if not match:
            continue
        try:
            config = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue
        if not isinstance(config, dict):
            continue
        current = str(config.get("default_class") or "").strip()
        replacement = RETIRED_CLASS_REPLACEMENTS.get(current)
        if not replacement:
            continue
        # Rewrite the literal rather than re-serializing the block: a profile
        # is operator-owned markdown, and reformatting its config would show
        # up as a spurious edit in every diff and drift check.
        block = re.sub(
            rf'("default_class"\s*:\s*)"{re.escape(current)}"',
            rf'\g<1>"{replacement}"',
            match.group(2),
        )
        try:
            path.write_text(text[: match.start(2)] + block + text[match.end(2) :], encoding="utf-8")
        except OSError:
            logger.warning("Could not rewrite profile %s for class retirement", path)
            continue
        changed.append((str(path), current, replacement))
        logger.info(
            "Profile %s: intelligence class '%s' is retired; repointed to '%s'",
            path, current, replacement,
        )
    return changed


def retire_vault_intelligence_classes(data_dir: str | Path) -> ClassRetirementResult:
    """Move retired class files aside and repoint the profiles that used them.

    The profile half runs first: a profile must never be left naming a class
    whose file has just moved, even if the process dies between the two.
    """
    repointed = repoint_vault_profile_classes(Path(data_dir) / "vault")
    root = _classes_root(data_dir)
    if not root.is_dir():
        return ClassRetirementResult(repointed_profiles=tuple(repointed))

    processed = _load_processed(root)
    retired: list[str] = []
    customized: list[str] = []
    for class_id in sorted(RETIRED_CLASS_REPLACEMENTS):
        if class_id in processed:
            continue
        source = root / f"{class_id}.md"
        if not source.is_file():
            continue
        if _is_customized(source):
            customized.append(class_id)
            processed.add(class_id)
            logger.info(
                "Intelligence class '%s' is retired but marked customized; leaving it in place",
                class_id,
            )
            continue
        destination = root / f"{class_id}.md{RETIRED_SUFFIX}"
        try:
            os.replace(source, destination)
        except OSError:
            logger.warning("Could not retire intelligence class file %s", source)
            continue
        processed.add(class_id)
        retired.append(class_id)
        logger.info(
            "Retired intelligence class '%s'; its file is kept at %s (use '%s' instead)",
            class_id, destination, RETIRED_CLASS_REPLACEMENTS[class_id],
        )
    if retired or customized:
        _save_processed(root, processed)
    return ClassRetirementResult(
        repointed_profiles=tuple(repointed),
        retired_files=tuple(retired),
        kept_customized=tuple(customized),
    )


__all__ = [
    "RETIRED_CLASSES_FILENAME",
    "RETIRED_CLASS_REPLACEMENTS",
    "ClassRetirementResult",
    "repoint_vault_profile_classes",
    "retire_vault_intelligence_classes",
]
