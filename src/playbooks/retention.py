"""Retention sweep for Playbook V2 durable state (child plan §12).

One object, one entry point: :meth:`ArtifactRetentionSweeper.sweep` returns a
count per collected category, mirroring ``MetricsSampler.prune``
(``src/metrics/sampler.py``).  The orchestrator calls it at most once an hour
and logs the returned counts; nothing else in this package has a schedule.

Ordering inside a sweep is not arbitrary.  Pending events and receipts go
first because neither is referenced by anything; runs next, because a run row
is what pins an artifact; artifacts last, so an artifact freed by this sweep's
own run collection is collectable in the same pass rather than an hour later.
Within the artifact step the row is deleted before its file — a crash between
them leaves an unreferenced file that the next sweep removes, whereas the
reverse order would leave a row pointing at a missing file and read as
``unavailable`` health on a playbook that was fine (§12.1).

"the next sweep removes it" is :meth:`~ArtifactRetentionSweeper._sweep_orphan_files`,
and it is a separate step from :meth:`~ArtifactRetentionSweeper._unlink_artifacts`
on purpose: that one only knows the rows *this* sweep deleted, so on its own it
can never see a file left behind by a previous process.  Orphan discovery is
the other direction — start from the directory, ask the database which of those
hashes anything still names, and remove only the rest.

Row-before-file has a second hazard the ordering alone does not answer.  The
store is content-addressed, so a compile that produces bytes already on disk
*adopts* the existing file rather than rewriting it; if that adoption happens
after this sweep has selected and deleted the old row, the caller writes a
fresh row for the same hash and an unlink between the two leaves that new row
pointing at nothing.  Deleting the file is therefore two-phase (§12.1): the
sweep renames it to a tombstone, re-asks under
:meth:`~PlaybookArtifactQueryMixin.artifact_hash_lock` whether anything
references the hash, and restores the file if the answer changed under it.
Tombstones nothing has claimed are finalized a grace period later by
:meth:`~ArtifactRetentionSweeper._sweep_tombstones`, which repeats the check —
and while a tombstone survives, ``upsert_playbook_artifact`` can restore it,
so a row that lands just after the sweep's re-check still gets its file back.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from src.playbooks.artifact_ref import SHA256_RE
from src.playbooks.artifact_tombstone import (
    DIGEST_RE,
    TOMBSTONE_GLOB,
    digest_for,
    discard,
    entomb,
    restore,
)

logger = logging.getLogger(__name__)

#: How long a ``*.json.tmp-*`` file left by an interrupted
#: :meth:`ArtifactStore.put` is allowed to survive (§12.1).  An hour is far
#: longer than any write takes and short enough that a crash loop cannot fill
#: the directory.
TEMP_FILE_TTL_SECONDS = 3600.0

#: How long a hash-named ``*.json`` artifact file with no row anywhere in V2
#: storage is allowed to survive (§12.1).  The same hour the temp files get,
#: and for the same reason: ``ArtifactStore.put`` writes the file before its
#: caller records the row, so a file younger than this may simply be a write
#: whose row has not landed yet.  ``put`` also refreshes the mtime of a file it
#: adopts, which is what keeps the window meaningful for content already on
#: disk.
ORPHAN_FILE_TTL_SECONDS = 3600.0

#: How long a tombstoned artifact file — one this sweep or an earlier one
#: renamed out of the live namespace — is kept before the deletion is
#: finalized (§12.1).  The same hour the orphan and temp files get, and for a
#: related reason: it is the window in which a ``put`` that adopted the file
#: just before it was entombed can still have its row land and restore it.
TOMBSTONE_TTL_SECONDS = 3600.0

#: How many tombstones one pass finalizes.  The reference re-check and the
#: unlinks happen inside a write transaction — on SQLite that is the whole
#: database's writer lock — so an accumulated backlog is worked off over
#: several sweeps rather than held in one.  The same order of magnitude as
#: ``collect_playbook_artifacts``'s own ``limit``.
_TOMBSTONE_FINALIZE_LIMIT = 1000

_DAY_SECONDS = 86400.0

#: The categories every sweep reports, even when it collected nothing — a
#: caller (and the ``one logger.info per sweep`` of §14) should never have to
#: distinguish "zero" from "this build does not collect that".
CATEGORIES = (
    "pending_events",
    "pending_events_expired",
    "receipts",
    "runs",
    "artifact_rows",
    "artifact_files",
    "tombstone_files",
    "orphan_files",
    "temp_files",
    "health_downgraded",
)


class ArtifactRetentionSweeper:
    """Collect aged-out V2 state and the files behind it.

    ``db`` is the database adapter (it supplies ``PlaybookRunQueryMixin`` and
    ``PlaybookArtifactQueryMixin``); ``config`` is ``AppConfig.playbooks``;
    ``compiled_root`` is the directory ``ArtifactStore`` writes beneath.
    """

    def __init__(self, db: Any, config: Any, compiled_root: str) -> None:
        self._db = db
        self._config = config
        self._artifacts_dir = Path(compiled_root) / "artifacts"
        self._layouts_dir = Path(compiled_root) / "layouts"

    async def sweep(self, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        counts = dict.fromkeys(CATEGORIES, 0)
        pending_events = await self._db.purge_pending_events(
            now,
            resolved_before=now - self._config.v2_pending_event_retention_days * _DAY_SECONDS,
        )
        counts["pending_events"] = pending_events.purged
        counts["pending_events_expired"] = pending_events.expired
        # Runs and their receipts share ``v2_receipt_retention_days``: the
        # locked ten-field config block (§12.3) has no separate run horizon,
        # and giving receipts a longer life than the run they belong to would
        # be meaningless because collecting the run deletes them anyway.
        run_horizon = now - self._config.v2_receipt_retention_days * _DAY_SECONDS
        counts["receipts"] = await self._db.purge_receipts(run_horizon)
        counts["runs"] = await self._db.purge_runs(run_horizon)
        collected = await self._db.collect_playbook_artifacts(
            now - self._config.v2_artifact_retention_days * _DAY_SECONDS,
            min_versions=self._config.v2_artifact_min_versions,
        )
        counts["artifact_rows"] = len(collected)
        counts["artifact_files"] = await self._unlink_artifacts(collected)
        counts["tombstone_files"] = await self._sweep_tombstones(now)
        counts["orphan_files"] = await self._sweep_orphan_files(now)
        counts["temp_files"] = self._sweep_temp_files(now)
        counts["health_downgraded"] = await self._downgrade_missing_artifacts()
        logger.info("Playbook V2 retention sweep: %s", counts)
        return counts

    # -- files ---------------------------------------------------------------

    async def _unlink_artifacts(self, collected: list[tuple[str, str]]) -> int:
        """Take the files whose rows this sweep deleted out of the live namespace.

        The stored ``path`` is trusted only as far as its filename: the row is
        written by this daemon, but a hash-named file is the one thing whose
        identity can be re-checked for free, so an entry whose stem is not the
        digest it claims is left on disk and reported rather than unlinked.

        Removal is a rename, not an unlink, and the reference question is then
        asked a second time (§12.1).  ``collect_playbook_artifacts`` proved the
        hash unreferenced in *its* transaction, which is a fact about a moment
        that has already passed by the time this runs: content-addressed
        storage lets a concurrent ``ArtifactStore.put`` adopt the very file
        being collected and its caller then insert a new row for the same hash.
        Renaming first is what makes that adoption safe — a ``put`` racing this
        step finds no file and writes the bytes again — and re-asking under
        :meth:`artifact_hash_lock`, which ``upsert_playbook_artifact`` also
        takes, is what catches a row that landed in the window.  A hash that
        came back restores its file immediately; the rest keep their tombstones
        until :meth:`_sweep_tombstones` finalizes them.

        The count is files taken out of the live namespace, which is what the
        sweep's caller logs, not tombstones erased from disk.
        """
        entombed: list[tuple[str, Path, Path]] = []
        for sha, path in collected:
            if not SHA256_RE.fullmatch(sha):
                logger.warning("Refusing to unlink artifact with malformed hash %r", sha)
                continue
            target = Path(path)
            if target.stem != sha[7:]:
                logger.warning("Artifact row %s names unrelated file %s; leaving it", sha, path)
                continue
            grave = entomb(target)
            if grave is not None:
                entombed.append((sha, target, grave))
        if not entombed:
            return 0
        shas = [sha for sha, _, _ in entombed]
        removed = 0
        async with self._db.artifact_hash_lock(shas) as conn:
            referenced = await self._db.filter_referenced_artifact_shas(shas, conn=conn)
            for sha, target, grave in entombed:
                if sha not in referenced:
                    removed += 1
                    continue
                restore(grave, target)
                logger.warning(
                    "Artifact %s was re-adopted while this sweep collected it; kept %s",
                    sha,
                    target,
                )
        return removed

    async def _sweep_tombstones(self, now: float) -> int:
        """Finalize deletions an earlier pass started, once the grace period is up.

        The second and last chance to notice that a collected hash came back.
        A tombstone older than :data:`TOMBSTONE_TTL_SECONDS` has outlived any
        ``put``/row handoff that could still be in flight, so the reference
        answer is asked once more — under the same lock, for the same reason
        :meth:`_unlink_artifacts` takes it — and only a hash nothing names is
        actually erased.  One that does get named has its file moved back:
        finalizing it would recreate exactly the dangling reference the
        two-phase deletion exists to prevent.

        This also collects tombstones a crashed sweep left behind, the way
        :meth:`_sweep_orphan_files` collects files an earlier crash orphaned:
        both start from the directory rather than from anything in memory.
        """
        entries: list[tuple[str, Path, Path]] = []
        try:
            graves = list(self._artifacts_dir.glob(TOMBSTONE_GLOB))
        except OSError:  # pragma: no cover - unreadable directory
            return 0
        for grave in graves:
            digest = digest_for(grave)
            if digest is None:
                continue
            try:
                if now - grave.stat().st_mtime < TOMBSTONE_TTL_SECONDS:
                    continue
            except OSError:
                continue
            entries.append((f"sha256:{digest}", self._artifacts_dir / f"{digest}.json", grave))
            if len(entries) >= _TOMBSTONE_FINALIZE_LIMIT:
                break
        if not entries:
            return 0
        shas = [sha for sha, _, _ in entries]
        removed = 0
        async with self._db.artifact_hash_lock(shas) as conn:
            referenced = await self._db.filter_referenced_artifact_shas(shas, conn=conn)
            for sha, target, grave in entries:
                if sha in referenced:
                    if restore(grave, target):
                        logger.warning(
                            "Artifact %s is referenced again; restored %s from its tombstone",
                            sha,
                            target,
                        )
                    continue
                if discard(grave):
                    (self._layouts_dir / f"{sha[7:]}.json").unlink(missing_ok=True)
                    removed += 1
        return removed

    async def _sweep_orphan_files(self, now: float) -> int:
        """Remove aged hash-named files that nothing in the database names.

        This is the recovery half of the row-then-file ordering (§12.1): a
        crash between :meth:`collect_playbook_artifacts` deleting a row and
        :meth:`_unlink_artifacts` unlinking its file leaves a file no row
        points at, and nothing else would ever collect it — ``_unlink_artifacts``
        is given only the rows the *current* sweep deleted, and
        ``_sweep_temp_files`` only matches ``*.json.tmp-*``.

        Three guards make it safe to delete a file the database did not name:

        * the stem must be a bare 64-hex digest, so a file this store did not
          write is never a candidate;
        * the file must be older than :data:`ORPHAN_FILE_TTL_SECONDS`, because
          ``ArtifactStore.put`` writes bytes before its caller writes the row;
        * the directory is listed **before** the database is asked, so a row
          inserted during the sweep is seen and protects its file.  The
          opposite order could read "no row", then have the row appear, then
          unlink the file underneath it.

        The reference query spans artifacts, activations and runs rather than
        the artifact table alone, so a hash still named by an activation or a
        retained run survives even if its artifact row is missing.
        """
        candidates: list[Path] = []
        try:
            entries = list(self._artifacts_dir.glob("*.json"))
        except OSError:  # pragma: no cover - unreadable directory
            return 0
        for entry in entries:
            if not DIGEST_RE.fullmatch(entry.stem):
                continue
            try:
                if now - entry.stat().st_mtime < ORPHAN_FILE_TTL_SECONDS:
                    continue
            except OSError:
                continue
            candidates.append(entry)
        if not candidates:
            return 0
        referenced = await self._db.filter_referenced_artifact_shas(
            [f"sha256:{entry.stem}" for entry in candidates]
        )
        removed = 0
        for entry in candidates:
            if f"sha256:{entry.stem}" in referenced:
                continue
            try:
                entry.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:  # pragma: no cover - permissions/filesystem
                logger.warning("Could not remove orphaned artifact file %s: %s", entry, exc)
                continue
            logger.info("Removed orphaned artifact file %s (no row references it)", entry)
            removed += 1
        return removed

    def _sweep_temp_files(self, now: float) -> int:
        """Remove ``*.json.tmp-*`` leftovers older than :data:`TEMP_FILE_TTL_SECONDS`.

        A temp file younger than the TTL may belong to a concurrent
        ``ArtifactStore.put`` on another process, so age is the whole guard.
        """
        removed = 0
        try:
            entries = list(self._artifacts_dir.glob("*.json.tmp-*"))
        except OSError:  # pragma: no cover - unreadable directory
            return 0
        for entry in entries:
            try:
                if now - entry.stat().st_mtime < TEMP_FILE_TTL_SECONDS:
                    continue
                os.unlink(entry)
            except FileNotFoundError:
                continue
            except OSError as exc:  # pragma: no cover - permissions/filesystem
                logger.warning("Could not remove stale temp artifact %s: %s", entry, exc)
                continue
            removed += 1
        return removed

    # -- health (§11, clause (b)) --------------------------------------------

    async def _downgrade_missing_artifacts(self) -> int:
        """Persist ``unavailable`` for activations whose artifact file is gone.

        Deliberately one-directional.  Deciding that an activation is *ready*
        again needs the validation record and the live contract fingerprints,
        which belong to Packages 2 and 5; deciding it is unavailable needs
        only a stat, and it is the state the ``playbooks.artifact_integrity``
        doctor check reports, so leaving it stale would make the check and the
        stored health disagree.
        """
        downgraded = 0
        for row in await self._db.list_playbook_activations(enabled_only=True):
            sha = row.get("active_artifact_sha256")
            if not sha or row.get("health") == "unavailable":
                continue
            path = await self._db.get_playbook_artifact_path(sha)
            if path and Path(path).is_file():
                continue
            await self._db.mark_activation_unavailable(row["activation_id"])
            logger.warning(
                "Playbook %s activation %s: artifact %s is missing; health -> unavailable",
                row.get("playbook_id"),
                row["activation_id"],
                sha,
            )
            downgraded += 1
        return downgraded
