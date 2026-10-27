"""Bounded, nonpruning pack consolidation outside retained-store acquisition."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from src.git.manager import GitError, safe_git_detail
from src.git.repository_lock import RepositoryLockTimeout, repository_lock_wait_limit
from src.integration.repository_recovery import _git_dir, _write
from src.projects.storage_binding import repository_path

PACK_THRESHOLD = 2
LOOSE_THRESHOLD = 1024
LOOSE_BYTES_THRESHOLD = 256 * 1024 * 1024
INTERVAL_SECONDS = 300
COOLDOWN_SECONDS = 3600
REPACK_TIMEOUT_SECONDS = 240
_REPACK = ["-c", "pack.threads=1", "-c", "pack.windowMemory=64m",
           "repack", "-a", "-d", "-k", "--quiet"]
logger = logging.getLogger(__name__)


def _temporary_packs(directory, now, *, remove=False):
    stale = [path for path in (directory / "objects" / "pack").glob("tmp_pack_*")
             if not path.is_symlink() and path.is_file()
             and now - path.stat().st_mtime >= COOLDOWN_SECONDS]
    if remove:
        for path in stale:
            path.unlink()
    return len(stale)


def _inspect(store, now):
    directory = _git_dir(store)
    pack_dir = directory / "objects" / "pack"
    if pack_dir.is_symlink() or pack_dir.parent.is_symlink():
        raise GitError("retained object directory is not isolated")
    packs = list(pack_dir.glob("*.pack"))
    if any(pack.is_symlink() or not pack.is_file() for pack in packs):
        raise GitError("retained pack is not a regular file")
    loose = list(pack_dir.parent.glob("[0-9a-f][0-9a-f]/" + "[0-9a-f]" * 38))
    if any(path.is_symlink() or path.parent.is_symlink() or not path.is_file() for path in loose):
        raise GitError("loose object is not a regular file")
    loose_bytes = sum(path.stat().st_size for path in loose)
    repack = (len(packs) >= PACK_THRESHOLD or len(loose) >= LOOSE_THRESHOLD
              or loose_bytes >= LOOSE_BYTES_THRESHOLD)
    temporary = _temporary_packs(directory, now)
    marker = directory / "aq-store-maintenance.json"
    journal = directory / "aq-empty-object-backups" / "incident.json"
    if marker.is_symlink() or journal.is_symlink() or journal.parent.is_symlink():
        raise GitError("retained maintenance metadata is not isolated")
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    recovery = json.loads(journal.read_text()) if journal.exists() else {}
    if not isinstance(previous, dict) or not isinstance(recovery, dict):
        raise GitError("retained maintenance metadata is not an object")
    attempted_at = previous.get("attempted_at", 0)
    if not isinstance(attempted_at, (int, float)):
        raise GitError("retained maintenance timestamp is invalid")
    pending = (journal.parent / "pending").exists() or (
        bool(recovery) and recovery.get("status") != "recovered")
    retry_at = attempted_at + COOLDOWN_SECONDS
    outcome = ("recovery_pending" if pending else "below_threshold" if not repack and not temporary
               else "cooldown" if previous and now < retry_at else "eligible")
    return {"cache": str(store), "pack_count": len(packs),
            "loose_count": len(loose), "loose_bytes": loose_bytes,
            "repack_needed": repack, "stale_temp_count": temporary,
            "pack_bytes": sum(pack.stat().st_size for pack in packs), "outcome": outcome,
            "retry_at": retry_at if previous else None}, directory, marker


class RetainedStoreMaintenance:
    def __init__(self, db, git, data_dir, *, clock=time.time):
        self.db, self.git = db, git
        self.data_dir, self.clock = Path(data_dir), clock
        self._next_tick, self._cursor = 0, 0
        self._run_lock = asyncio.Lock()

    async def stores(self, project_id=None):
        """Derive known stores from configuration/bindings; never scan arbitrary clones."""
        result = []
        for repo in await self.db.list_repos(project_id):
            digest = hashlib.sha256(repo.id.encode()).hexdigest()
            paths = [self.data_dir / "development-integration" / digest[:20] / name
                     for name in ("repository", "observer")]
            paths.append(self.data_dir / "integration-repositories" / f"{digest}.git")
            paths.extend(Path(path).expanduser() for path in
                         {repo.checkout_base_path, repo.source_path} if path)
            for path in paths:
                bound = await repository_path(self.db, repo.id, path)
                if (repo, bound) not in result and await asyncio.to_thread(bound.exists):
                    result.append((repo, bound))
        return result

    async def tick(self, now):
        if now < self._next_tick or self._run_lock.locked():
            return
        self._next_tick = now + INTERVAL_SECONDS
        async with self._run_lock:
            stores = await self.stores()
            if stores:
                repo, path = stores[self._cursor % len(stores)]
                self._cursor += 1
                result = await self._store(repo, path, apply=True)
                if result["outcome"] == "unavailable":
                    logger.warning("retained store maintenance: %s", result)
                return result

    async def run(self, project_id, *, apply=False):
        if self._run_lock.locked():
            return {"outcome": "busy", "evidence": {"stores": []}}
        async with self._run_lock:
            stores = [await self._store(repo, path, apply=apply)
                      for repo, path in await self.stores(project_id)]
        outcome = ("unavailable" if any(item["outcome"] == "unavailable" for item in stores)
                   else "maintained" if apply else "preview")
        return {"outcome": outcome, "evidence": {"stores": stores}}

    async def _store(self, repo, path, *, apply):
        try:
            # Maintenance never queues behind a visit. Ordinary writers keep priority.
            with repository_lock_wait_limit(0.05):
                async with self.git.arepository_transaction(str(path)):
                    item, directory, marker = await asyncio.to_thread(
                        _inspect, path, self.clock())
                    origin = await self.git.arun_git_result(
                        ["config", "--get", "remote.origin.url"], cwd=str(path), lock_held=True,
                    )
                    if not repo.url or origin.returncode or origin.stdout.strip() != repo.url:
                        raise GitError("retained repository origin differs from configuration")
                    if not apply or item["outcome"] != "eligible":
                        return item
                    # Record even failed/interrupted attempts, to bound churn across restarts.
                    await asyncio.to_thread(_write, marker, {"attempted_at": self.clock()})
                    removed = await asyncio.to_thread(
                        _temporary_packs, directory, self.clock(), remove=True)
                    if item["repack_needed"]:
                        result = await self.git.arun_git_result(
                            _REPACK, cwd=str(path), lock_held=True, env={"LC_ALL": "C"},
                            timeout=REPACK_TIMEOUT_SECONDS, low_priority=True,
                        )
                        if result.returncode or result.stderr.strip():
                            raise GitError(safe_git_detail(result.stderr or "retained repack failed"))
                        report = directory / "gc.log"
                        if report.is_file() and not report.is_symlink():
                            report.unlink()
                    after, _, _ = await asyncio.to_thread(_inspect, path, self.clock())
                    return {**item, "outcome": "repacked" if item["repack_needed"] else "cleaned",
                            "removed_temp_count": removed, "after_loose_count": after["loose_count"],
                            "after_loose_bytes": after["loose_bytes"],
                            "after_pack_count": after["pack_count"],
                            "after_pack_bytes": after["pack_bytes"]}
        except RepositoryLockTimeout:
            return {"cache": str(path), "outcome": "busy"}
        except (GitError, OSError, ValueError) as exc:
            return {"cache": str(path), "outcome": "unavailable",
                    "error": safe_git_detail(str(exc), limit=1000)}


def maintenance_for(orchestrator):
    service = vars(orchestrator).get("_retained_store_maintenance")
    if service is None:
        service = RetainedStoreMaintenance(
            orchestrator.db, orchestrator.git, orchestrator.config.data_dir)
        orchestrator._retained_store_maintenance = service
    return service
