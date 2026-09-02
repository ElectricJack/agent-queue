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
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from src.playbooks.artifact_ref import SHA256_RE

logger = logging.getLogger(__name__)

#: How long a ``*.json.tmp-*`` file left by an interrupted
#: :meth:`ArtifactStore.put` is allowed to survive (§12.1).  An hour is far
#: longer than any write takes and short enough that a crash loop cannot fill
#: the directory.
TEMP_FILE_TTL_SECONDS = 3600.0

_DAY_SECONDS = 86400.0

#: The categories every sweep reports, even when it collected nothing — a
#: caller (and the ``one logger.info per sweep`` of §14) should never have to
#: distinguish "zero" from "this build does not collect that".
CATEGORIES = (
    "pending_events",
    "receipts",
    "runs",
    "artifact_rows",
    "artifact_files",
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

    async def sweep(self, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        counts = dict.fromkeys(CATEGORIES, 0)
        counts["pending_events"] = await self._db.purge_pending_events(now)
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
        counts["artifact_files"] = self._unlink_artifacts(collected)
        counts["temp_files"] = self._sweep_temp_files(now)
        counts["health_downgraded"] = await self._downgrade_missing_artifacts()
        logger.info("Playbook V2 retention sweep: %s", counts)
        return counts

    # -- files ---------------------------------------------------------------

    def _unlink_artifacts(self, collected: list[tuple[str, str]]) -> int:
        """Unlink the files whose rows this sweep already deleted.

        The stored ``path`` is trusted only as far as its filename: the row is
        written by this daemon, but a hash-named file is the one thing whose
        identity can be re-checked for free, so an entry whose stem is not the
        digest it claims is left on disk and reported rather than unlinked.
        """
        removed = 0
        for sha, path in collected:
            if not SHA256_RE.fullmatch(sha):
                logger.warning("Refusing to unlink artifact with malformed hash %r", sha)
                continue
            target = Path(path)
            if target.stem != sha[7:]:
                logger.warning("Artifact row %s names unrelated file %s; leaving it", sha, path)
                continue
            try:
                target.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:  # pragma: no cover - permissions/filesystem
                logger.warning("Could not unlink artifact %s at %s: %s", sha, path, exc)
                continue
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
