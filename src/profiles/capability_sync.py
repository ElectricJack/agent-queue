"""Additive auto-sync of shipped ``## Capabilities`` grants into vault profiles.

``vault.ensure_default_profiles()`` is write-if-absent, so a grant a release
adds to a shipped profile never reaches a vault copy that already exists
(:mod:`src.profiles.drift`).  For most profiles that is the right default: the
grant list is the operator's to curate, and ``profiles.system_drift`` names
the gap.  The supervisor is the exception.  It is the operator's own control
plane and the operator decided it should be able to do basically anything,
yet every control a release added for it (``integration_eject``,
``review_dispatch``, ``integration_release_stale_owners`` …) was denied as
``capability denied`` until someone hand-edited the vault copy.

So on daemon start, and whenever the vault watcher reloads the file, the
vault copy of each *synced* profile receives every grant its shipped default
has and it lacks, through the same additive merge ``aq agent profile-reseed
--grants-only`` uses (:func:`src.profiles.drift.merge_profile_grants`).
Nothing is removed, no other section or prose changes, the write is atomic
and a ``profile.md.bak-<epoch>`` copy is kept.  Only explicit names are
copied: wildcard grants stay prohibited (``src/profiles/capabilities.py``).

Which profiles are synced:

* ``supervisor`` by default (:data:`DEFAULT_SYNCED_PROFILE_IDS`);
* any profile whose vault frontmatter says ``capability_sync: false`` is
  left alone — the opt-out for an operator who curates its grants by hand;
* any other shipped profile whose vault frontmatter says
  ``capability_sync: true`` opts in to the same treatment.

The doctor check ``profiles.supervisor_capability_drift``
(:mod:`src.doctor.profile_checks`) reports what the supervisor's vault copy
is missing, and its ``--fix`` runs :func:`sync_profile_capabilities`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from src.profiles.capabilities import NAMESPACES
from src.profiles.drift import (
    merge_profile_grants,
    shipped_profile_path,
    system_profile_ids,
    vault_profile_path,
)

logger = logging.getLogger(__name__)

#: Frontmatter key that opts a vault profile out of (``false``) or in to
#: (``true``) the additive capability sync.
CAPABILITY_SYNC_KEY = "capability_sync"

#: Shipped profiles synced unless their vault copy opts out.
DEFAULT_SYNCED_PROFILE_IDS: frozenset[str] = frozenset({"supervisor"})

#: Event emitted once per profile whose vault copy gained grants.
CAPABILITIES_SYNCED_EVENT = "profile.capabilities_synced"

#: Result statuses of :func:`sync_profile_capabilities`.
STATUS_SYNCED = "synced"  # grants were added
STATUS_CURRENT = "current"  # nothing was missing
STATUS_DISABLED = "disabled"  # not synced: opted out, or never opted in
STATUS_NOT_SEEDED = "not_seeded"  # no vault copy to merge into
STATUS_FAILED = "failed"  # the merge refused; see ``error``

_TRUE_WORDS = frozenset({"true", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "no", "off"})


@dataclass
class CapabilitySyncResult:
    """What one sync pass did to one profile's vault copy."""

    profile_id: str
    status: str
    #: Per capability namespace, the grant names appended to the vault copy.
    added: dict[str, list[str]] = field(default_factory=dict)
    backup_path: str | None = None
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.status == STATUS_SYNCED

    def added_names(self) -> list[str]:
        """Every added grant name, namespace order then shipped order."""
        return [name for ns in NAMESPACES for name in self.added.get(ns, [])]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "added": {ns: list(names) for ns, names in self.added.items()},
            "backup_path": self.backup_path,
            "error": self.error,
        }


def capability_sync_setting(text: str) -> bool | None:
    """The ``capability_sync`` frontmatter value of profile ``text``.

    ``None`` when the key is absent or holds something that is not a
    boolean (YAML ``true``/``false``, or the words ``true``/``yes``/``on`` and
    ``false``/``no``/``off`` in any case).
    """
    from src.profiles.parser import parse_frontmatter

    frontmatter, _ = parse_frontmatter(text)
    value = frontmatter.extra.get(CAPABILITY_SYNC_KEY)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def capability_sync_enabled(profile_id: str, vault_text: str) -> bool:
    """Whether ``profile_id``'s vault copy (``vault_text``) receives shipped grants."""
    setting = capability_sync_setting(vault_text)
    if setting is not None:
        return setting
    return profile_id in DEFAULT_SYNCED_PROFILE_IDS


def sync_profile_capabilities(
    data_dir: str,
    profile_id: str,
    *,
    root: str | None = None,
) -> CapabilitySyncResult:
    """Merge the shipped grants ``profile_id``'s vault copy lacks into it.

    A no-op (``disabled``) unless :func:`capability_sync_enabled` says the
    profile is synced.  Never raises for an ordinary refusal: a vault copy
    with no ``## Capabilities`` block, one that does not parse, or an I/O
    error comes back as ``failed`` with the reason in ``error`` and nothing
    written.
    """
    if not os.path.isfile(shipped_profile_path(profile_id, root)):
        return CapabilitySyncResult(
            profile_id, STATUS_FAILED, error=f"'{profile_id}' is not a shipped system profile"
        )
    vault_path = vault_profile_path(data_dir, profile_id)
    try:
        with open(vault_path, encoding="utf-8") as handle:
            vault_text = handle.read()
    except FileNotFoundError:
        return CapabilitySyncResult(profile_id, STATUS_NOT_SEEDED)
    except OSError as exc:
        return CapabilitySyncResult(profile_id, STATUS_FAILED, error=f"cannot read {vault_path}: {exc}")

    if not capability_sync_enabled(profile_id, vault_text):
        return CapabilitySyncResult(profile_id, STATUS_DISABLED)

    try:
        merged = merge_profile_grants(data_dir, profile_id, root=root)
    except FileNotFoundError:
        # Deleted between the read above and the merge.
        return CapabilitySyncResult(profile_id, STATUS_NOT_SEEDED)
    except (OSError, ValueError) as exc:
        return CapabilitySyncResult(profile_id, STATUS_FAILED, error=str(exc))

    if not merged["changed"]:
        return CapabilitySyncResult(profile_id, STATUS_CURRENT)
    return CapabilitySyncResult(
        profile_id,
        STATUS_SYNCED,
        added=merged["added"],
        backup_path=merged["backup_path"],
    )


def sync_shipped_capabilities(
    data_dir: str,
    *,
    root: str | None = None,
) -> list[CapabilitySyncResult]:
    """Run :func:`sync_profile_capabilities` for every shipped system profile."""
    return [
        sync_profile_capabilities(data_dir, profile_id, root=root)
        for profile_id in system_profile_ids(root)
    ]


async def publish_sync_result(
    result: CapabilitySyncResult,
    *,
    event_bus: Any | None = None,
    trigger: str,
) -> None:
    """Log one sync result and emit :data:`CAPABILITIES_SYNCED_EVENT` if it changed.

    ``trigger`` names what ran the sync: ``startup``, ``reload`` or
    ``doctor``.  ``current``/``disabled``/``not_seeded`` results are logged
    at debug only; a refusal is a warning, because it leaves the profile
    short of grants its shipped default carries.
    """
    if result.status == STATUS_FAILED:
        logger.warning(
            "capability sync (%s) left %s's vault profile unchanged: %s",
            trigger,
            result.profile_id,
            result.error,
        )
        return
    if not result.changed:
        logger.debug("capability sync (%s): %s %s", trigger, result.profile_id, result.status)
        return

    names = result.added_names()
    logger.info(
        "capability sync (%s): added %d shipped capability grant(s) to %s's vault "
        "profile: %s (backup %s)",
        trigger,
        len(names),
        result.profile_id,
        ", ".join(names),
        result.backup_path,
    )
    if event_bus is None:
        return
    try:
        await event_bus.emit(
            "profile.capabilities_synced",
            {
                "profile_id": result.profile_id,
                "added": {ns: list(v) for ns, v in result.added.items()},
                "backup_path": result.backup_path,
                "trigger": trigger,
            },
        )
    except Exception:
        logger.debug("failed to emit %s", CAPABILITIES_SYNCED_EVENT, exc_info=True)
