"""Drift detection between shipped system profiles and their vault copies.

``vault.ensure_default_profiles()`` is deliberately write-if-absent: once
``vault/agent-types/<id>/profile.md`` exists it is never overwritten, so
operator edits survive upgrades.  The cost is that a *system* profile keeps
its original schema and semantics forever — a vault ``reviewer`` seeded
before ``read_only`` became load-bearing still says ``read_only: false``,
and ``GitOpsMixin._task_produces_no_code()``
(``src/orchestrator/git_ops.py``) then re-arms the require-a-PR gate for a
session that is told never to push.

This module is the read-only half of the answer: it compares each vault copy
of a shipped profile against the in-tree default and reports what diverged.
Nothing here writes; :func:`reseed_profile` is the explicit, opt-in write
path and it always leaves a backup behind.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

#: ``## Config`` keys whose value changes behaviour rather than presentation.
#: A divergence in any of these is what makes a stale vault profile dangerous:
#:
#: * ``read_only`` — gates the require-a-PR close check (``git_ops.py``).
#: * ``harness`` — selects which CLI actually runs the agent.
#: * ``lifecycle`` — push (``task``) vs pull (``pool``) vs ``named``.
#: * ``needs_workspace`` — whether the orchestrator acquires a worktree.
#:
#: Everything else (``description``, ``model``, ``default_class``, prompt
#: text) is presentation or tuning an operator is expected to own, and is
#: deliberately not compared.
SEMANTIC_CONFIG_FIELDS: tuple[str, ...] = (
    "read_only",
    "harness",
    "lifecycle",
    "needs_workspace",
)

#: Status values a :class:`ProfileDrift` can carry, worst last.
STATUS_OK = "ok"
STATUS_NOT_SEEDED = "not_seeded"
STATUS_DRIFTED = "drifted"
STATUS_UNREADABLE = "unreadable"


def defaults_root() -> str:
    """Absolute path of the in-tree ``src/profiles/defaults`` directory."""
    return os.path.join(os.path.dirname(__file__), "defaults")


def shipped_profile_path(profile_id: str, root: str | None = None) -> str:
    """Path of the shipped ``profile.md`` for ``profile_id``."""
    return os.path.join(root or defaults_root(), profile_id, "profile.md")


def vault_profile_path(data_dir: str, profile_id: str) -> str:
    """Path of the vault copy of ``profile_id`` under ``data_dir``."""
    return os.path.join(data_dir, "vault", "agent-types", profile_id, "profile.md")


def system_profile_ids(root: str | None = None) -> list[str]:
    """Ids of every shipped system profile, sorted.

    A directory only counts when it actually holds a ``profile.md``, which
    mirrors what :func:`src.vault.ensure_default_profiles` seeds.
    """
    base = root or defaults_root()
    if not os.path.isdir(base):
        return []
    return sorted(
        entry
        for entry in os.listdir(base)
        if os.path.isfile(shipped_profile_path(entry, base))
    )


@dataclass
class ConfigDivergence:
    """One ``## Config`` field whose vault value differs from the shipped one.

    ``vault`` / ``shipped`` are ``None`` when the field is absent from that
    side.  None of :data:`SEMANTIC_CONFIG_FIELDS` legitimately takes a JSON
    ``null``, so ``None`` unambiguously means "not declared".
    """

    field: str
    shipped: Any
    vault: Any

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "shipped": self.shipped, "vault": self.vault}


@dataclass
class ProfileDrift:
    """The comparison of one system profile's vault copy against the default."""

    profile_id: str
    status: str = STATUS_OK
    #: Semantic ``## Config`` fields that differ.
    config: list[ConfigDivergence] = field(default_factory=list)
    #: Section headings the shipped default has that the vault copy lacks
    #: (lowercased).  A rename shows up here plus in :attr:`extra_sections`.
    missing_sections: list[str] = field(default_factory=list)
    #: Section headings only the vault copy has.  Reported for context —
    #: an operator adding a section is legitimate and is not drift on its own.
    extra_sections: list[str] = field(default_factory=list)
    #: Parse errors from either file; a non-empty list means ``unreadable``.
    errors: list[str] = field(default_factory=list)

    @property
    def is_drifted(self) -> bool:
        return self.status in (STATUS_DRIFTED, STATUS_UNREADABLE)

    def summary(self) -> str:
        """One human line describing this profile's drift."""
        if self.status == STATUS_OK:
            return f"{self.profile_id}: matches shipped default"
        if self.status == STATUS_NOT_SEEDED:
            return f"{self.profile_id}: no vault copy (seeded on next daemon start)"
        if self.status == STATUS_UNREADABLE:
            return f"{self.profile_id}: {'; '.join(self.errors)}"
        parts = [
            f"{d.field}={d.vault!r} (shipped {d.shipped!r})" for d in self.config
        ]
        if self.missing_sections:
            renamed = ", ".join(sorted(self.missing_sections))
            parts.append(f"missing section(s): {renamed}")
        return f"{self.profile_id}: " + ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "config": [d.to_dict() for d in self.config],
            "missing_sections": list(self.missing_sections),
            "extra_sections": list(self.extra_sections),
            "errors": list(self.errors),
            "summary": self.summary(),
        }


def _parse(path: str) -> tuple[dict, set[str], list[str]]:
    """Parse ``path`` into (config, section-name set, errors)."""
    from src.profiles.parser import parse_profile

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return {}, set(), [f"cannot read {path}: {exc}"]
    parsed = parse_profile(text)
    return dict(parsed.config or {}), set(parsed.sections), list(parsed.errors)


def diff_profile(
    profile_id: str,
    data_dir: str,
    root: str | None = None,
) -> ProfileDrift:
    """Compare one system profile's vault copy against the shipped default."""
    drift = ProfileDrift(profile_id=profile_id)

    shipped_path = shipped_profile_path(profile_id, root)
    if not os.path.isfile(shipped_path):
        drift.status = STATUS_UNREADABLE
        drift.errors.append(f"no shipped default at {shipped_path}")
        return drift

    vault_path = vault_profile_path(data_dir, profile_id)
    if not os.path.isfile(vault_path):
        drift.status = STATUS_NOT_SEEDED
        return drift

    shipped_config, shipped_sections, shipped_errors = _parse(shipped_path)
    vault_config, vault_sections, vault_errors = _parse(vault_path)

    # A shipped default that does not parse is a packaging bug, not operator
    # drift, but the operator still needs to see it.
    drift.errors = [f"shipped: {e}" for e in shipped_errors]
    drift.errors += [f"vault: {e}" for e in vault_errors]
    if drift.errors:
        drift.status = STATUS_UNREADABLE
        return drift

    for name in SEMANTIC_CONFIG_FIELDS:
        shipped_value = shipped_config.get(name)
        vault_value = vault_config.get(name)
        if shipped_value != vault_value:
            drift.config.append(ConfigDivergence(name, shipped_value, vault_value))

    drift.missing_sections = sorted(shipped_sections - vault_sections)
    drift.extra_sections = sorted(vault_sections - shipped_sections)

    if drift.config or drift.missing_sections:
        drift.status = STATUS_DRIFTED
    return drift


def scan_profile_drift(
    data_dir: str,
    root: str | None = None,
) -> list[ProfileDrift]:
    """Compare every shipped system profile against its vault copy."""
    return [diff_profile(pid, data_dir, root) for pid in system_profile_ids(root)]


def reseed_profile(
    data_dir: str,
    profile_id: str,
    root: str | None = None,
    backup: bool = True,
) -> dict[str, Any]:
    """Overwrite one vault profile with the shipped default.

    The explicit counterpart to :func:`src.vault.ensure_default_profiles`'s
    write-if-absent rule: startup never clobbers an operator's file, but an
    operator who has read the drift report can ask for the shipped version
    back one profile at a time.  The previous file is copied to
    ``profile.md.bak-<epoch>`` first unless ``backup=False``.

    Returns a dict with ``profile_id``, ``path``, ``backup_path`` (``None``
    when nothing was there to back up) and ``created`` (True when no vault
    copy existed).
    """
    shipped_path = shipped_profile_path(profile_id, root)
    if not os.path.isfile(shipped_path):
        raise FileNotFoundError(f"'{profile_id}' is not a shipped system profile")

    dst = vault_profile_path(data_dir, profile_id)
    existed = os.path.isfile(dst)
    backup_path: str | None = None
    if existed and backup:
        backup_path = f"{dst}.bak-{int(time.time())}"
        shutil.copy2(dst, backup_path)

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(shipped_path, dst)
    return {
        "profile_id": profile_id,
        "path": dst,
        "backup_path": backup_path,
        "created": not existed,
    }
