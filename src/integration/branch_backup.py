"""Verified bundles and durable deletion logs shared by branch cleanup paths."""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from src.git.manager import GitError

logger = logging.getLogger(__name__)
BACKUP_REF_PREFIX = "refs/aq-backup/heads/"


def _month(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m")


async def _bundle(
    run_git, store, heads: dict[str, str], *, main_head, backup_dir, repository_id, now
) -> Path:
    """Write *heads* to a new verified bundle; raise rather than return an unproved one."""
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%S%fZ")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", repository_id) or "repository"
    directory = Path(backup_dir) / _month(now)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}-{slug}.bundle"
    refs = {f"{BACKUP_REF_PREFIX}{branch}": sha for branch, sha in heads.items()}
    try:
        for ref, sha in refs.items():
            await run_git(store, "update-ref", ref, sha)
        exclusions = ("--not", main_head) if main_head else ()
        await run_git(store, "bundle", "create", str(path), *sorted(refs), *exclusions)
        await run_git(store, "bundle", "verify", str(path))
        listed = await run_git(store, "bundle", "list-heads", str(path))
    finally:
        for ref in refs:
            try:
                await run_git(store, "update-ref", "-d", ref)
            except GitError:
                logger.warning("could not remove temporary backup ref %s", ref)
    present = {tuple(line.split(" ", 1)) for line in listed.splitlines() if " " in line}
    missing = sorted(ref for ref, sha in refs.items() if (sha, ref) not in present)
    if missing:
        raise GitError(f"branch backup {path} is missing {missing}")
    return path


def _record_deletions(
    backup_dir, targets, *, bundled, bundle, repository_id, now
) -> Path:
    """Append one line per branch to the month's deletion log, durably."""
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_month(now)}.tsv"
    recorded_at = datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")

    def clean(value) -> str:
        return re.sub(r"[\t\r\n]+", " ", str(value))

    lines = [
        "\t".join((
            branch,
            targets[branch]["head"],
            clean(targets[branch].get("reason") or "-"),
            str(bundle) if branch in bundled else "-",
            recorded_at,
            clean(repository_id),
        ))
        + "\n"
        for branch in sorted(targets)
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    return path
