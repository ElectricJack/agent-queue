"""Bounded empty-object recovery for isolated retained integration clones."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import uuid
from pathlib import Path

from src.git.manager import GitError

logger = logging.getLogger(__name__)
_HEX = re.compile(r"[0-9a-f]+\Z")


async def harden_retained_repository(git, store):
    store = Path(store).resolve()
    git_dir = store / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise GitError("retained repository must be an isolated clone")
    identity = (str(store), git_dir.stat().st_ino)
    if identity in git._retained_fsync_stores:
        return
    async with git.arepository_transaction(str(store)):
        if identity in git._retained_fsync_stores:
            return
        for key, value in (("core.fsync", "committed"), ("core.fsyncMethod", "fsync")):
            await git._arun_unlocked(["config", key, value], cwd=str(store))
        git._retained_fsync_stores.add(identity)


def _quarantine_empty_objects(git_dir: Path) -> int:
    objects = git_dir / "objects"
    if objects.is_symlink() or not objects.is_dir():
        raise GitError("retained object directory is not isolated")
    empty = []
    for fanout in objects.iterdir():
        if len(fanout.name) != 2 or not _HEX.fullmatch(fanout.name) or fanout.is_symlink():
            continue
        if not fanout.is_dir():
            continue
        for obj in fanout.iterdir():
            if len(obj.name) not in {38, 62} or not _HEX.fullmatch(obj.name):
                continue
            info = obj.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_size == 0:
                empty.append(obj)
    if not empty:
        return 0
    backup = git_dir / "aq-empty-object-backups"
    backup.mkdir(exist_ok=True)
    # Set this before moving anything: a cancelled/crashed recovery resumes
    # with a full refetch even when no empty object remains to trigger Git.
    with (backup / "pending").open("a") as marker:
        os.fsync(marker.fileno())
    for directory in (backup, git_dir):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    destination = backup / uuid.uuid4().hex
    destination.mkdir()
    moved = 0
    for obj in empty:
        info = obj.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_size == 0:
            obj.rename(destination / (obj.parent.name + obj.name))
            moved += 1
    return moved


async def fetch_retained_origin(git, store, *, repository_url):
    """Recover once, preserving all local candidates and failing closed on fsck.

    This seam is used only by integration's isolated clones. It deliberately
    refuses linked worktrees and never re-clones away unpublished candidates.
    """
    store = Path(store)
    git_dir = store / ".git"
    pending = git_dir / "aq-empty-object-backups" / "pending"
    try:
        if pending.exists():
            raise GitError("retained repository recovery pending")
        await git.afetch_origin(str(store), repository_url=repository_url, all_heads=True)
        return
    except GitError:
        # The credential-isolated importer deliberately suppresses raw Git
        # diagnostics. Inspect actual objects on any fetch failure, rather
        # than relying on an error string to identify corruption.
        if not git_dir.is_dir() or git_dir.is_symlink():
            raise
        async with git.arepository_transaction(str(store)):
            count = await asyncio.to_thread(_quarantine_empty_objects, git_dir)
            if not count and not pending.exists():
                raise
            logger.warning("retained repository %s: quarantined %d empty objects; refetching",
                           store, count)
            await git.afetch_origin(str(store), repository_url=repository_url, all_heads=True,
                                   lock_held=True, refetch=True)
            await git._arun_unlocked(["fsck", "--full", "--no-dangling"], cwd=str(store))
            pending.unlink(missing_ok=True)
            logger.warning("retained repository %s: empty-object recovery validated by fsck", store)
