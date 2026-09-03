"""Reversible deletion for content-addressed artifact files (child plan §12.1).

The retention sweep deletes an artifact row and *then* unlinks the file behind
it.  Between those two steps a concurrent ``ArtifactStore.put`` can adopt the
very file being collected — content-addressed storage adopts identical bytes
rather than rewriting them — and its caller then writes a fresh row for the
same hash.  An unlink at that point leaves a live row pointing at nothing,
which is the one outcome the row-then-file ordering was chosen to avoid.

So the sweep never unlinks a collected artifact directly: it *renames*.  A
tombstone is the same bytes under a name nothing looks up, which buys two
things at once.  It is invisible to a racing ``put``, which then finds no file
at the canonical path and writes the content again instead of adopting a file
that is about to disappear; and it is reversible, so anything that later
discovers a live row whose file is missing can put the bytes back.  Deletion
is finalized on a later pass, once the reference check has been repeated.

Names are ``<digest>.json.tombstone-<pid>-<uuid4>`` beside the artifact they
came from.  Same-directory renames are atomic, and the suffix matches neither
the ``*.json`` glob the orphan step scans nor the ``*.json.tmp-*`` glob the
temp step scans, so the two existing sweeps ignore tombstones entirely.

A fresh tombstone has its mtime refreshed on creation: ``os.replace`` preserves
the original file's timestamps, and an aged artifact's file is aged by
definition, so without the touch every tombstone would be born already past
its grace period and the deferred half of the deletion would collapse back
into an immediate unlink.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

#: An artifact file's stem is exactly the bare digest ``ArtifactStore.path_for``
#: writes.  Anything else under ``artifacts/`` was put there by something that
#: is not this store, and no sweep guesses about it.
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: What separates the artifact's own filename from the unique suffix that
#: makes each tombstone distinct.  Two sweeps (or a sweep and a crashed
#: predecessor) can hold tombstones for the same hash without colliding.
TOMBSTONE_INFIX = ".tombstone-"

#: Glob matching every tombstone under an artifacts directory.
TOMBSTONE_GLOB = f"*.json{TOMBSTONE_INFIX}*"


def tombstone_pattern(path: Path) -> str:
    """Glob matching every tombstone that was made from ``path``."""
    return f"{path.name}{TOMBSTONE_INFIX}*"


def digest_for(grave: Path) -> str | None:
    """The bare digest a tombstone's name carries, or ``None``.

    The same identity check the orphan step makes: only a name this module
    could have produced from a hash-named artifact is claimed, so a file some
    other tool dropped in the directory is never finalized as a deletion.
    """
    head, separator, _ = grave.name.partition(TOMBSTONE_INFIX)
    if not separator or not head.endswith(".json"):
        return None
    digest = head[: -len(".json")]
    return digest if DIGEST_RE.fullmatch(digest) else None


def entomb(path: Path) -> Path | None:
    """Move ``path`` out of the live namespace and return its tombstone.

    ``None`` when there was nothing to move — a file already gone is already
    in the state the caller wanted — or when the rename failed, which is
    reported rather than retried so the sweep can leave the file alone.
    """
    grave = path.with_name(f"{path.name}{TOMBSTONE_INFIX}{os.getpid()}-{uuid4().hex}")
    try:
        os.replace(path, grave)
    except FileNotFoundError:
        return None
    except OSError as exc:  # pragma: no cover - permissions/filesystem
        logger.warning("Could not tombstone artifact file %s: %s", path, exc)
        return None
    try:
        os.utime(grave)
    except OSError:  # pragma: no cover - permissions/filesystem
        pass
    return grave


def restore(grave: Path, path: Path) -> bool:
    """Move a tombstone back to ``path``; report whether the file is live again.

    An existing file at ``path`` wins: a racing ``put`` that wrote the content
    afresh has already satisfied the row, and its bytes are the same bytes by
    construction, so the tombstone is simply discarded.
    """
    try:
        if path.exists():
            grave.unlink(missing_ok=True)
            return False
        os.replace(grave, path)
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover - permissions/filesystem
        logger.warning("Could not restore artifact file %s from %s: %s", path, grave, exc)
        return False
    return True


def restore_any(path: Path) -> bool:
    """Put ``path`` back from the newest tombstone left for it, if there is one.

    The repair half of the two-phase deletion, for the caller that discovers a
    live row whose file is missing.  Newest first because a hash can have more
    than one tombstone and only the most recent is certain to predate nothing.
    """
    if path.exists():
        return False
    try:
        graves = list(path.parent.glob(tombstone_pattern(path)))
    except OSError:  # pragma: no cover - unreadable directory
        return False

    def _mtime(candidate: Path) -> float:
        try:
            return candidate.stat().st_mtime
        except OSError:  # pragma: no cover - vanished between glob and stat
            return 0.0

    for grave in sorted(graves, key=_mtime, reverse=True):
        if restore(grave, path):
            return True
    return False


def discard(grave: Path) -> bool:
    """Delete a tombstone for good; report whether this call removed it."""
    try:
        grave.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover - permissions/filesystem
        logger.warning("Could not remove artifact tombstone %s: %s", grave, exc)
        return False
    return True
