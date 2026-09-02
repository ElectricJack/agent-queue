"""Operator opt-out record for shipped system profiles.

``vault.ensure_default_profiles()`` is write-if-absent and runs on every
daemon start, so "missing from the vault" is the only signal it has — and
it reads that signal as *fresh install*, never as *deliberately removed*.
An operator who deletes ``worker-standard`` because their fleet has moved
to the provider-explicit ladder gets it back at the next restart, along
with a pool that starts sizing itself again.

This module is the missing bit of state: a tombstone list at
``vault/agent-types/.retired-defaults`` naming the shipped ids the
operator has deleted.  Seeding skips those ids; ``aq agent
profile-reseed <id>`` clears the tombstone and is the explicit way back.

Kept as a leaf module (stdlib only, no ``src.`` imports) because both
:mod:`src.vault` and :mod:`src.commands.profile_commands` need it and
must not import each other.

File format — one JSON object, so the record can carry *why* without a
second file::

    {
      "version": 1,
      "retired": {
        "worker-standard": {"retired_at": 1756800000.0, "reason": "..."}
      }
    }

A malformed or unreadable file is treated as "nothing retired": a corrupt
opt-out record must not be able to suppress seeding silently, and the
worst case of ignoring it is one profile the operator deletes again.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

#: Basename of the tombstone file inside ``vault/agent-types/``.  Dot-prefixed
#: so the profile-directory scan in ``ensure_default_profiles`` and the vault
#: watcher both pass over it, and so Obsidian keeps it out of the graph.
RETIRED_DEFAULTS_FILENAME = ".retired-defaults"

#: Bumped only for a breaking change to the on-disk shape.
RETIRED_DEFAULTS_VERSION = 1


def retired_defaults_path(data_dir: str) -> str:
    """Absolute path of the tombstone file under ``data_dir``."""
    return os.path.join(data_dir, "vault", "agent-types", RETIRED_DEFAULTS_FILENAME)


def _load(data_dir: str) -> dict:
    path = retired_defaults_path(data_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning(
            "Ignoring unreadable retired-defaults record at %s: %s", path, exc
        )
        return {}
    if not isinstance(payload, dict):
        logger.warning("Ignoring retired-defaults record at %s: not an object", path)
        return {}
    retired = payload.get("retired")
    if not isinstance(retired, dict):
        return {}
    # Normalise: entries may be any shape, ids must be non-empty strings.
    return {
        pid: (entry if isinstance(entry, dict) else {})
        for pid, entry in retired.items()
        if isinstance(pid, str) and pid.strip()
    }


def _store(data_dir: str, retired: dict) -> str:
    path = retired_defaults_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"version": RETIRED_DEFAULTS_VERSION, "retired": retired}
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def retired_default_ids(data_dir: str) -> set[str]:
    """Shipped profile ids the operator has deleted, as a set."""
    return set(_load(data_dir))


def is_retired(data_dir: str, profile_id: str) -> bool:
    """True when ``profile_id`` carries a tombstone."""
    return profile_id in _load(data_dir)


def retire_default(data_dir: str, profile_id: str, reason: str = "") -> bool:
    """Record that the operator deleted shipped profile ``profile_id``.

    Idempotent: re-retiring an already-retired id refreshes nothing and
    returns ``False``.  Returns ``True`` when a new tombstone was written.
    """
    profile_id = (profile_id or "").strip()
    if not profile_id:
        return False
    retired = _load(data_dir)
    if profile_id in retired:
        return False
    entry: dict = {"retired_at": time.time()}
    if reason:
        entry["reason"] = reason
    retired[profile_id] = entry
    path = _store(data_dir, retired)
    logger.info(
        "Recorded shipped profile '%s' as operator-retired in %s "
        "(startup seeding will skip it; 'aq agent profile-reseed %s' restores it)",
        profile_id,
        path,
        profile_id,
    )
    return True


def unretire_default(data_dir: str, profile_id: str) -> bool:
    """Drop the tombstone for ``profile_id``; ``True`` if one was removed."""
    profile_id = (profile_id or "").strip()
    if not profile_id:
        return False
    retired = _load(data_dir)
    if profile_id not in retired:
        return False
    del retired[profile_id]
    _store(data_dir, retired)
    logger.info(
        "Cleared the operator-retired tombstone for shipped profile '%s'", profile_id
    )
    return True
