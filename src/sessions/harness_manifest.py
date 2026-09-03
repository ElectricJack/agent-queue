"""Shipped-harness manifest — lets harness fixes reach existing vault copies.

``ensure_default_harnesses`` copies ``src/sessions/default_harnesses/*.md``
into ``vault/harnesses/`` and, once a copy exists, the vault copy is the
source of truth.  That protects operator edits, but it also meant a fix to
a shipped file (PR #212's ``is_regex`` flags on the dialog rules, for
example) never reached an install seeded before the fix: the vault copy
kept the broken rule until someone deleted it by hand.

This module records the sha256 of **every version each shipped harness has
ever had**.  A vault copy whose hash is in that set is a pristine copy of
some earlier shipped version — nobody edited it — so it is safe to refresh
in place.  Anything else is treated as an operator edit: left alone and
reported.

The manifest is a ratchet: ``tests/test_harness_manifest.py`` fails when
the current shipped file's hash is missing here, so a change to a shipped
harness cannot land without also recording the version it replaces.  When
you change a shipped file, append the *new* file's hash (``sha256sum
src/sessions/default_harnesses/<name>.md``) — the old one is already
listed.

Statuses returned by :func:`classify_vault_harness`:

* ``missing`` — no vault copy; seeding creates it.
* ``current`` — byte-identical to the shipped file.
* ``stale``   — byte-identical to a *previously* shipped version; safe to
  refresh.
* ``edited``  — anything else; never touched automatically.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Mapping
from typing import Literal

logger = logging.getLogger(__name__)

HarnessStatus = Literal["missing", "current", "stale", "edited"]

#: Every sha256 each shipped harness file has ever had, oldest first.  The
#: commit and date next to each hash say which version it was; they are
#: documentation only.  Never remove an entry — an install seeded from
#: that version may still be out there.
SHIPPED_HARNESS_HASHES: Mapping[str, frozenset[str]] = {
    "claude.md": frozenset(
        {
            "99870ea8ee162cf7f43c4f8d2705afc645ce463f91843bbc80e9535a3d8d3b20",  # bcad7104 2026-08-19
            "df620660ea22ec2eff6096a08c2e723fabfdb403a6332b74853c75e08be043c2",  # 9244b370 2026-08-19
            "336b4e2cb4ac3bdadfde5e5d88782dd4776af4af21585023a589ba830ceb6cc4",  # 48bfab98 2026-08-20
            "858cef94897e6643a3e50c146690f1ddd86d59fafdbfa37903c8c38f4f7ce5e1",  # d7b28ccc 2026-08-20
            "0c4c16a131331bf22c9dfa995bce43ae24263c72368f93d88d654e30812e1ea9",  # 9f82cd90 2026-08-20
            "fa925868786c3f5e55eb8f5afe362c81709137fbbd8f8c94d7bfa688e9dd516d",  # b85a48d0 2026-08-24
            "0a4fbef8ce0ec2e1748cd358e671e724cd85d39d9985e673879c3367cdb19f24",  # eb22f55a 2026-08-27
            "f871cb35c6d9af29b138e5608bcc03b2d122da50b2bbed1cf1dd585f28c740b0",  # ead0a037 2026-08-30
            "399f26ff96e1c64bf34cce72a8291e73912e7f271cf084f7536e6da50b710173",  # 58c44f1a 2026-09-01
            "9816b0b8abdc39cafe4e7886820a3e284d7565c4fd497f60f481574d11b2e66e",  # 21688ff2 2026-09-01
            "314a36b9a946f95737230ba41401e7624b84325b9c8f4616c3d80ecfc9daf854",  # 0bcaefe9 2026-09-01
            "69c946e751315e37f8af36d7c43ae364d01ffe166c248f314c74db46e499e991",  # 28f8481e 2026-09-02
            "76ad7c1261dd56beeafa750b53394fe28040cd86b2301a42fafee66b51db2d78",  # 5e65efe9 2026-09-02 (PR #212)
            "c002fa5b88f0a942a38f81649eac0cb694bb48ec8b80ef190b992ac91b15e135",  # ca7ffda9 2026-09-03
        }
    ),
    "codex.md": frozenset(
        {
            "b3d21d32fba38accc6320c7db07fad15d4037197f172db14b7358529ab4cc92e",  # 5e7ea1f5 2026-08-21
            "6ce63f882c66a0c0be4ef683b82f473afb78177e16e63612249a8b499ed00526",  # 334dc034 2026-08-27
            "c9c5b100a8274ac2f3a6a958b46cf0679034352fcbd3e465478421d290f97348",  # f2fc32df 2026-08-30
            "b5bf632c9d32f6a48604d1c7085a8adc3f3c987aede9ecd9abf1e46661ddf1c7",  # 9ae73d8d 2026-08-30
            "b0f51d8ca480d4a027de71154d0eb7b36424696a06a1ff1155d3120d0efd08de",  # 58c44f1a 2026-09-01
            "7eb57eb1e25979ba733cc795fb4830c4cdd863d6b86f5cfd88fdbcca41801368",  # 21688ff2 2026-09-01
            "e8d6273fd9c27416b9b72287f477e91d3655bebd0ff1b4e0bc177bb1d4a71bf5",  # 0bcaefe9 2026-09-01
            "2d322783e676dfa4fede595a93b6d3b2706a4628c306ce00a10d7233ca10b07e",  # 5e65efe9 2026-09-02 (PR #212)
            "8cc90bc5c0e99cf7e737bef15368de5e34ff0293b78044fc76aaaceda837f9a3",  # ca7ffda9 2026-09-03
        }
    ),
    "gemini.md": frozenset(
        {
            "9a68080765918a81b14b93f645c7ab8404c5b502aef8fde1a3fecf030f5ad7f4",  # 7024d380 2026-08-22
            "3af7b3252e2dd0dc1f47aea2301ce0e4223c5561c07cffc9dd99cd484f6cbc78",  # 5e65efe9 2026-09-02 (PR #212)
        }
    ),
}


def shipped_harness_dir() -> str:
    """Absolute path of the in-tree ``default_harnesses`` directory."""
    return os.path.join(os.path.dirname(__file__), "default_harnesses")


def vault_harness_dir(data_dir: str) -> str:
    """Absolute path of the system-scope ``vault/harnesses`` directory."""
    return os.path.join(data_dir, "vault", "harnesses")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: str) -> str:
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


def list_shipped_harnesses(defaults_dir: str | None = None) -> list[str]:
    """Filenames (``claude.md`` …) of the shipped harnesses, sorted."""
    root = defaults_dir or shipped_harness_dir()
    if not os.path.isdir(root):
        return []
    return sorted(f for f in os.listdir(root) if f.endswith(".md"))


def classify_vault_harness(
    vault_path: str,
    shipped_path: str,
    *,
    known_hashes: frozenset[str] | set[str] = frozenset(),
) -> HarnessStatus:
    """Say how the vault copy at *vault_path* relates to *shipped_path*.

    *known_hashes* is the set of hashes the shipped file has ever had; the
    current shipped file's hash is always treated as known even when the
    manifest lags behind it.
    """
    if not os.path.exists(vault_path):
        return "missing"
    shipped_hash = sha256_path(shipped_path)
    vault_hash = sha256_path(vault_path)
    if vault_hash == shipped_hash:
        return "current"
    if vault_hash in known_hashes:
        return "stale"
    return "edited"


def audit_vault_harnesses(
    data_dir: str,
    *,
    defaults_dir: str | None = None,
    known_hashes: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, dict]:
    """Classify every shipped harness against its vault copy.

    Returns ``{filename: {"status", "vault_path", "shipped_path"}}`` for
    each shipped file.  Read-only.
    """
    defaults = defaults_dir or shipped_harness_dir()
    manifest = SHIPPED_HARNESS_HASHES if known_hashes is None else known_hashes
    target_dir = vault_harness_dir(data_dir)

    report: dict[str, dict] = {}
    for filename in list_shipped_harnesses(defaults):
        shipped_path = os.path.join(defaults, filename)
        vault_path = os.path.join(target_dir, filename)
        status = classify_vault_harness(
            vault_path, shipped_path, known_hashes=manifest.get(filename, frozenset())
        )
        report[filename] = {
            "status": status,
            "vault_path": vault_path,
            "shipped_path": shipped_path,
        }
    return report


def sync_vault_harnesses(
    data_dir: str,
    *,
    defaults_dir: str | None = None,
    known_hashes: Mapping[str, frozenset[str]] | None = None,
) -> dict:
    """Create missing vault copies and refresh stale ones; leave edits alone.

    Returns ``{"created", "refreshed", "skipped", "edited"}`` filename
    lists.  ``skipped`` is every copy left untouched (current *or* edited)
    so callers that only care about "did it write" keep working;
    ``edited`` narrows that to the copies an operator changed, which are
    reported at WARNING because a shipped fix cannot reach them.
    """
    target_dir = vault_harness_dir(data_dir)
    os.makedirs(target_dir, exist_ok=True)

    result: dict = {"created": [], "refreshed": [], "skipped": [], "edited": []}
    for filename, info in audit_vault_harnesses(
        data_dir, defaults_dir=defaults_dir, known_hashes=known_hashes
    ).items():
        status = info["status"]
        if status == "missing":
            shutil.copy2(info["shipped_path"], info["vault_path"])
            result["created"].append(filename)
        elif status == "stale":
            shutil.copy2(info["shipped_path"], info["vault_path"])
            result["refreshed"].append(filename)
        elif status == "edited":
            result["skipped"].append(filename)
            result["edited"].append(filename)
        else:
            result["skipped"].append(filename)

    if result["created"]:
        logger.info(
            "Installed %d default harness(es) to %s: %s",
            len(result["created"]),
            target_dir,
            ", ".join(result["created"]),
        )
    if result["refreshed"]:
        logger.info(
            "Refreshed %d shipped harness copy(ies) in %s that matched an older "
            "shipped version: %s",
            len(result["refreshed"]),
            target_dir,
            ", ".join(result["refreshed"]),
        )
    for filename in result["edited"]:
        logger.warning(
            "Harness %s in %s differs from every shipped version and was left "
            "alone; shipped fixes will not reach it. Run `aq doctor --check "
            "harness.drift` to compare, or `aq vault reset-harness %s` to "
            "restore the shipped file.",
            filename,
            target_dir,
            filename.removesuffix(".md"),
        )
    return result


def restore_shipped_harness(
    data_dir: str,
    name: str,
    *,
    defaults_dir: str | None = None,
) -> dict:
    """Overwrite ``vault/harnesses/<name>.md`` with the shipped file.

    *name* may be given with or without the ``.md`` suffix.  Unlike
    :func:`sync_vault_harnesses` this **does** clobber operator edits — it
    is the explicit "give me the shipped version back" path behind
    ``aq vault reset-harness``.  Raises ``FileNotFoundError`` when no such
    harness is shipped.

    Returns ``{"name", "previous_status", "vault_path"}``.
    """
    filename = name if name.endswith(".md") else f"{name}.md"
    defaults = defaults_dir or shipped_harness_dir()
    shipped_path = os.path.join(defaults, filename)
    if filename not in list_shipped_harnesses(defaults):
        raise FileNotFoundError(f"no shipped harness named {filename!r}")

    target_dir = vault_harness_dir(data_dir)
    os.makedirs(target_dir, exist_ok=True)
    vault_path = os.path.join(target_dir, filename)
    previous = classify_vault_harness(
        vault_path, shipped_path, known_hashes=SHIPPED_HARNESS_HASHES.get(filename, frozenset())
    )
    if previous != "current":
        shutil.copy2(shipped_path, vault_path)
    return {"name": filename.removesuffix(".md"), "previous_status": previous, "vault_path": vault_path}
