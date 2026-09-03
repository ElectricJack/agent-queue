"""One-shot migration that retires project-scoped agent profiles.

Agents are shared between projects, so a profile that only exists for one
project is a contradiction: the same durable worker can serve several
projects, and the pool it belongs to is configuration, not per-project state.
Project-scoped profiles (rows keyed ``project:<project_id>:<agent_type>``,
sourced from ``vault/projects/<pid>/agent-types/<type>/profile.md``) were
therefore removed from the product.

This module promotes any override a vault still carries into its **system**
profile at ``vault/agent-types/<agent_type>/profile.md`` and deletes the
override, so an operator upgrading from an older release keeps the behaviour
their pools had:

* No system profile yet — the override is moved into place verbatim, with its
  frontmatter ``id`` rewritten to the bare agent type.
* A system profile exists — the override's ``## Config`` keys are merged into
  it (**last writer wins**: the override's value replaces the system one) and
  the per-key diff is logged.  Prose sections (Role, Rules, …) that differ are
  reported but never merged: silently concatenating two roles produces a
  profile nobody wrote.  The operator sees them in the report and in the
  ``profiles.project_overrides`` doctor check.

The migration is idempotent — once no override files or ``project:`` rows
remain it is a no-op — and safe to run from startup, ``aq doctor --fix`` and
tests alike.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.profiles.parser import parse_profile, set_frontmatter_id, update_config_keys

logger = logging.getLogger(__name__)

#: Prose sections compared for a "you must hand-merge this" warning.  Config
#: is merged automatically; everything here is the operator's own writing.
_PROSE_SECTIONS = ("role", "rules", "reflection")

#: Sizing/lifecycle keys are the whole reason overrides existed in practice,
#: so the report names them first when it lists a promoted config diff.
_POOL_KEYS = ("lifecycle", "min_active", "max_active", "max_claims_per_session")


def project_override_profile_id(profile_id: str | None) -> bool:
    """True for a legacy ``project:<project_id>:<agent_type>`` profile id."""
    return bool(profile_id) and str(profile_id).startswith("project:")


def split_project_override_id(profile_id: str) -> tuple[str, str] | None:
    """Split ``project:<project_id>:<agent_type>`` into its two parts."""
    parts = profile_id.split(":", 2)
    if len(parts) == 3 and parts[0] == "project" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


@dataclass
class OverridePromotion:
    """What happened to a single project override."""

    project_id: str
    agent_type: str
    path: str
    action: str  # "moved" | "merged" | "removed" | "error"
    config_changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    prose_conflicts: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        head = f"{self.project_id}/{self.agent_type}: {self.action}"
        if self.error:
            return f"{head} ({self.error})"
        bits: list[str] = []
        if self.config_changes:
            ordered = sorted(
                self.config_changes,
                key=lambda k: (_POOL_KEYS.index(k) if k in _POOL_KEYS else len(_POOL_KEYS), k),
            )
            bits.append(
                ", ".join(
                    f"{k}: {json.dumps(self.config_changes[k][0])}"
                    f" -> {json.dumps(self.config_changes[k][1])}"
                    for k in ordered
                )
            )
        if self.prose_conflicts:
            bits.append(f"unmerged sections: {', '.join(sorted(self.prose_conflicts))}")
        return f"{head} ({'; '.join(bits)})" if bits else head

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "agent_type": self.agent_type,
            "path": self.path,
            "action": self.action,
            "config_changes": {k: list(v) for k, v in self.config_changes.items()},
            "prose_conflicts": list(self.prose_conflicts),
            "error": self.error,
        }


def find_project_override_paths(data_dir: str) -> list[tuple[str, str, Path]]:
    """Return ``(project_id, agent_type, path)`` for every override markdown.

    Both layouts are scanned: the canonical
    ``vault/projects/<pid>/agent-types/<type>/profile.md`` and the legacy
    colon-encoded ``vault/agent-types/project:<pid>:<type>/profile.md`` that
    an older writer produced.  The startup scanner refuses to read the second
    one, but it is still on disk and still has to be cleaned up.
    """
    vault = Path(data_dir) / "vault"
    found: list[tuple[str, str, Path]] = []

    projects_root = vault / "projects"
    if projects_root.is_dir():
        for path in sorted(projects_root.glob("*/agent-types/*/profile.md")):
            found.append((path.parents[2].name, path.parent.name, path))

    agent_types_root = vault / "agent-types"
    if agent_types_root.is_dir():
        for entry in sorted(agent_types_root.iterdir()):
            if not entry.is_dir() or ":" not in entry.name:
                continue
            scoped = split_project_override_id(entry.name)
            if not scoped or not (entry / "profile.md").is_file():
                continue
            found.append((scoped[0], scoped[1], entry / "profile.md"))

    return found


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove *start* and its now-empty parents, never passing *stop*."""
    current = start
    while current != stop and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def _config_diff(system_config: dict, override_config: dict) -> dict[str, tuple[Any, Any]]:
    """Keys the override changes, as ``{key: (system_value, override_value)}``."""
    return {
        key: (system_config.get(key), value)
        for key, value in override_config.items()
        if system_config.get(key) != value
    }


def _prose_conflicts(system, override) -> list[str]:
    conflicts = []
    for name in _PROSE_SECTIONS:
        over = (getattr(override, name, "") or "").strip()
        sys_text = (getattr(system, name, "") or "").strip()
        if over and over != sys_text:
            conflicts.append(name)
    return conflicts


def _promote_one(data_dir: str, project_id: str, agent_type: str, path: Path) -> OverridePromotion:
    system_path = Path(data_dir) / "vault" / "agent-types" / agent_type / "profile.md"
    result = OverridePromotion(
        project_id=project_id, agent_type=agent_type, path=str(path), action="error"
    )
    try:
        override_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.error = f"could not read override: {exc}"
        return result

    override = parse_profile(override_text)

    if not system_path.is_file():
        # Nothing to merge into: the override becomes the system profile.
        try:
            system_path.parent.mkdir(parents=True, exist_ok=True)
            system_path.write_text(
                set_frontmatter_id(override_text, agent_type), encoding="utf-8"
            )
            result.action = "moved"
            result.config_changes = _config_diff({}, override.config)
        except OSError as exc:
            result.error = f"could not write {system_path}: {exc}"
            return result
    else:
        try:
            system_text = system_path.read_text(encoding="utf-8")
        except OSError as exc:
            result.error = f"could not read {system_path}: {exc}"
            return result
        system = parse_profile(system_text)
        changes = _config_diff(system.config, override.config)
        result.config_changes = changes
        result.prose_conflicts = _prose_conflicts(system, override)
        if changes:
            try:
                system_path.write_text(
                    update_config_keys(system_text, {k: v[1] for k, v in changes.items()}),
                    encoding="utf-8",
                )
            except OSError as exc:
                result.error = f"could not write {system_path}: {exc}"
                return result
        result.action = "merged" if changes else "removed"

    try:
        path.unlink()
        _prune_empty_dirs(path.parent, Path(data_dir) / "vault")
    except OSError as exc:
        result.error = f"promoted but could not delete {path}: {exc}"
        result.action = "error"
    return result


def promote_project_profile_overrides(data_dir: str) -> dict[str, Any]:
    """Promote every project override into its system profile and delete it.

    Returns ``{"success", "promoted", "failed", "details", "promotions"}``.
    ``success`` is strict: false when any override could not be promoted, so
    the caller can leave the doctor check red rather than reporting a clean
    upgrade over a half-finished one.
    """
    if not data_dir:
        return {"success": True, "promoted": 0, "failed": 0, "details": [], "promotions": []}

    promotions = [
        _promote_one(data_dir, project_id, agent_type, path)
        for project_id, agent_type, path in find_project_override_paths(data_dir)
    ]
    failed = [p for p in promotions if p.action == "error"]
    for promotion in promotions:
        if promotion.action == "error":
            logger.warning("Project profile override migration failed: %s", promotion.summary())
        else:
            logger.info("Promoted project profile override: %s", promotion.summary())

    return {
        "success": not failed,
        "promoted": len(promotions) - len(failed),
        "failed": len(failed),
        "details": [p.summary() for p in promotions],
        "promotions": [p.to_dict() for p in promotions],
    }


async def delete_project_override_rows(db) -> list[str]:
    """Delete every ``project:<pid>:<type>`` row from ``agent_profiles``.

    The vault is the source of truth, so once the markdown is promoted the
    override row is dead weight that would keep resolving ahead of nothing.
    Returns the ids that were deleted.
    """
    deleted: list[str] = []
    for profile in await db.list_profiles():
        if not project_override_profile_id(profile.id):
            continue
        await db.delete_profile(profile.id)
        deleted.append(profile.id)
    if deleted:
        logger.info(
            "Removed %d legacy project-scoped profile row(s): %s", len(deleted), ", ".join(deleted)
        )
    return deleted


async def retire_project_scoped_profiles(data_dir: str, db) -> dict[str, Any]:
    """Run the full retirement: promote vault overrides, then drop DB rows."""
    report = promote_project_profile_overrides(data_dir)
    report["deleted_rows"] = await delete_project_override_rows(db)
    return report


def project_override_dirs(data_dir: str) -> list[str]:
    """Leftover ``vault/projects/<pid>/agent-types/`` dirs, for reporting."""
    projects_root = Path(data_dir) / "vault" / "projects"
    if not projects_root.is_dir():
        return []
    return [
        str(path)
        for path in sorted(projects_root.glob("*/agent-types"))
        if path.is_dir() and any(path.iterdir())
    ]


__all__ = [
    "OverridePromotion",
    "delete_project_override_rows",
    "find_project_override_paths",
    "project_override_dirs",
    "project_override_profile_id",
    "promote_project_profile_overrides",
    "retire_project_scoped_profiles",
    "split_project_override_id",
]
